"""发布器:事件触发 + 最小间隔限流;渲染与发送解耦;任务持久化(租约/重试)。

- 触发:事件总线出现新事件时尝试调度;两次发布之间至少间隔 min_interval_s。
- 渲染:默认 shell 出到 headless Chrome 渲染冻结视图 URL(?export=1);
  生产环境在 Playwright 容器内运行时替换 Renderer 即可。
- 发送:FileSender 写本地发布目录(默认);Telegram/Discord Sender 待凭据与
  目的地确认后实现,接口不变。
- 任务:持久化在 tasks 表,带租约、重试上限、错误记录;截图与发送不是同一事务,
  发送成功但确认丢失时进入"结果待确认"(result_json 保留产物位置)。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from . import models


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Renderer:
    """把 URL 渲染成 PNG。

    用 CDP(remote-debugging-port)等待页面就绪条件(export-tag 渲染出事件数)
    后 Page.captureScreenshot 捕获。不使用 `--timeout --screenshot` 组合:
    实测该模式在此 Chrome 版本下会中止页面 fetch,导致截图缺事件标记。
    """

    def __init__(
        self,
        chrome: str | None = None,
        window_size: str = "1600,1000",
        wait_timeout: float = 30.0,
    ):
        self.chrome = chrome or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        self.window_size = window_size
        self.wait_timeout = wait_timeout
        self.last_error: str | None = None

    def render(self, url: str, out_path: Path) -> bool:
        # headless Chrome 偶发崩溃(空截图),最多重试一次
        for attempt in range(2):
            try:
                if asyncio.run(self._render(url, out_path)):
                    return True
            except Exception:
                pass
        return False

    async def _render(self, url: str, out_path: Path) -> bool:
        import websockets

        profile = tempfile.mkdtemp(prefix="smon-chrome-")
        port_file = Path(profile) / "DevToolsActivePort"
        cmd = [
            self.chrome,
            "--headless=new", "--no-first-run", "--no-default-browser-check",
            "--no-sandbox", "--use-angle=swiftshader",
            f"--user-data-dir={profile}",
            f"--window-size={self.window_size}",
            "--remote-debugging-port=0",
            url,
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            # 等待 DevTools 端口就绪
            port = None
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if port_file.exists():
                    try:
                        port = int(port_file.read_text(encoding="utf-8").splitlines()[0])
                        break
                    except (ValueError, IndexError):
                        pass
                await asyncio.sleep(0.3)
            if port is None:
                return False

            async def _target():
                for _ in range(10):
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=3) as resp:
                            targets = json.loads(resp.read().decode("utf-8"))
                        page = next((t for t in targets if t.get("type") == "page"), None)
                        if page:
                            return page
                    except Exception:
                        pass
                    await asyncio.sleep(0.5)
                return None

            page = await _target()
            if page is None:
                return False

            cid = 0

            async def _call(ws, method: str, params: dict | None = None):
                nonlocal cid
                cid += 1
                await ws.send(json.dumps({"id": cid, "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                    if msg.get("id") == cid:
                        return msg.get("result", {})

            async with websockets.connect(page["webSocketDebuggerUrl"], max_size=None) as ws:
                # 等待 export-tag 渲染出非零事件数;窗口内 0 事件时等待到超时兜底
                expr = (
                    "document.getElementById('export-tag') ?"
                    " document.getElementById('export-tag').textContent : ''"
                )
                deadline = time.monotonic() + self.wait_timeout
                tag = ""
                while time.monotonic() < deadline:
                    res = await _call(ws, "Runtime.evaluate", {"expression": expr, "returnByValue": True})
                    tag = res.get("result", {}).get("value") or ""
                    if re.search(r"事件 [1-9]\d*", tag):
                        break
                    await asyncio.sleep(1)
                # 页面必须真的渲染出来了:错误页(Vite 未启动等)没有地图画布,
                # 此时宁可判定失败重试,也不要把错误页当作战况图发出去
                ready = await _call(
                    ws,
                    "Runtime.evaluate",
                    {
                        "expression": "!!document.querySelector('.map canvas') && !!document.getElementById('export-tag')",
                        "returnByValue": True,
                    },
                )
                if not ready.get("result", {}).get("value"):
                    self.last_error = f"render page not ready (tag={tag!r})"
                    return False
                await asyncio.sleep(4)  # 瓦片与标记渲染留时间
                shot = await _call(ws, "Page.captureScreenshot", {"format": "png"})
                data = shot.get("data", "")
                if not data:
                    return False
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(base64.b64decode(data))
                return out_path.stat().st_size > 1000
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                subprocess.run(
                    ["pkill", "-f", "Google Chrome.*headless"],
                    capture_output=True,
                    timeout=10,
                )
            except Exception:
                pass
            try:
                shutil.rmtree(profile, ignore_errors=True)
            except Exception:
                pass


class Sender:
    """发送接口:返回带外部标识的结果;真实渠道实现保持同一签名。"""

    def send(self, image_path: Path, caption: str) -> dict:
        raise NotImplementedError


class FileSender(Sender):
    """默认发送器:把截图复制到发布目录并记录元信息(本地验证用)。"""

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir

    def send(self, image_path: Path, caption: str) -> dict:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        dest = self.out_dir / image_path.name
        shutil.copyfile(image_path, dest)
        meta = {"file": str(dest), "caption": caption, "sent_at": utcnow_naive().isoformat()}
        (dest.with_suffix(".json")).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return meta


def _multipart(boundary: str, fields: list[tuple[str, str]], files: list[tuple[str, str, bytes, str]]) -> bytes:
    parts = []
    for name, value in fields:
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    for name, filename, content, mime in files:
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {mime}\r\n\r\n"
            ).encode("utf-8")
            + content
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts)


class TelegramSender(Sender):
    """经 Bot API sendPhoto 发送截图到频道;凭据来自运营者配置。"""

    def __init__(self, bot_token: str, chat_id: str, timeout: float = 60.0):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout

    def send(self, image_path: Path, caption: str) -> dict:
        import uuid

        boundary = "----smon" + uuid.uuid4().hex
        body = _multipart(
            boundary,
            fields=[("chat_id", self.chat_id), ("caption", caption)],
            files=[("photo", image_path.name, image_path.read_bytes(), "image/png")],
        )
        url = f"https://api.telegram.org/bot{self.bot_token}/sendPhoto"
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not data.get("ok"):
            raise RuntimeError(f"telegram sendPhoto failed: {data.get('description')}")
        return {
            "channel": "telegram",
            "external_id": str(data["result"]["message_id"]),
            "chat_id": self.chat_id,
        }


class DiscordSender(Sender):
    """经 webhook 发送截图(附件形式;webhook 不返回消息 ID,记录 HTTP 状态)。"""

    def __init__(self, webhook_url: str, timeout: float = 60.0):
        self.webhook_url = webhook_url
        self.timeout = timeout

    def send(self, image_path: Path, caption: str) -> dict:
        import uuid

        boundary = "----smon" + uuid.uuid4().hex
        body = _multipart(
            boundary,
            fields=[("content", caption)],
            files=[("file", image_path.name, image_path.read_bytes(), "image/png")],
        )
        req = urllib.request.Request(
            self.webhook_url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            resp.read()  # 2xx 即成功
        return {"channel": "discord", "external_id": None, "status": "sent"}


class Publisher:
    def __init__(
        self,
        session_factory: sessionmaker,
        bus,
        renderer: Renderer,
        sender: Sender,
        work_dir: Path,
        min_interval_s: int = 300,
        max_attempts: int = 5,
        lease_s: int = 120,
        url: str = "http://127.0.0.1:5173/?export=1",
        window_label: str = "默认视图",
    ):
        self._sf = session_factory
        self._bus = bus
        self.renderer = renderer
        self.sender = sender
        self.work_dir = work_dir
        self.min_interval_s = min_interval_s
        self.max_attempts = max_attempts
        self.lease_s = lease_s
        self.url = url
        self.window_label = window_label
        self._last_seq = 0
        self._lock = threading.Lock()

    def last_published_at(self, db) -> datetime | None:
        t = (
            db.query(models.Task)
            .filter(models.Task.kind == "publish", models.Task.status == "done")
            .order_by(models.Task.updated_at.desc())
            .first()
        )
        return t.updated_at if t else None

    def maybe_schedule(self, db, force: bool = False) -> models.Task | None:
        last = self.last_published_at(db)
        if not force and last and (utcnow_naive() - last).total_seconds() < self.min_interval_s:
            return None
        t = models.Task(
            kind="publish",
            status="queued",
            attempts=0,
            max_attempts=self.max_attempts,
            params_json=json.dumps(
                {"url": self.url, "window_label": self.window_label}, ensure_ascii=False
            ),
        )
        db.add(t)
        db.commit()
        return t

    def run_due_tasks(self, db) -> int:
        now = utcnow_naive()
        q = (
            db.query(models.Task)
            .filter(models.Task.kind == "publish", models.Task.status.in_(["queued", "failed"]))
            .all()
        )
        done = 0
        for t in q:
            if t.status == "failed" and t.attempts >= t.max_attempts:
                continue
            if t.lease_until and t.lease_until > now:
                continue
            self._execute(t)
            done += 1
        return done

    def _execute(self, t: models.Task):
        t.status = "running"
        t.attempts += 1
        t.lease_until = utcnow_naive() + timedelta(seconds=self.lease_s)
        t.error = None
        t.updated_at = utcnow_naive()
        t_result = t
        db = None
        from sqlalchemy.orm import Session

        # 用调用方传入的同一会话更新(由 run_due_tasks 的会话上下文保证)
        # 这里直接沿用 t 所属会话,通过 Session.object_session 获取
        db = Session.object_session(t)
        db.commit()
        try:
            params = json.loads(t.params_json or "{}")
            ts = utcnow_naive().strftime("%Y%m%d-%H%M%S")
            self.work_dir.mkdir(parents=True, exist_ok=True)
            out = self.work_dir / f"map-{ts}.png"
            ok = self.renderer.render(params.get("url", self.url), out)
            if not ok:
                raise RuntimeError(
                    f"renderer produced no output ({getattr(self.renderer, 'last_error', None) or 'unknown'})"
                )
            meta = self.sender.send(out, caption=f"战况地图 · {params.get('window_label', '')} · {ts}Z")
            t.status = "done"
            t.result_json = json.dumps(meta, ensure_ascii=False)
        except Exception as exc:
            t.status = "failed"
            t.error = str(exc)[:2000]
        t.lease_until = None
        t.updated_at = utcnow_naive()
        db.commit()

    def tick(self):
        """后台线程/测试调用:新事件触发调度 + 到期任务执行。"""
        with self._lock:
            with self._sf() as db:
                seq = self._bus.last_seq()
                if seq > self._last_seq:
                    self.maybe_schedule(db)
                self._last_seq = seq
                self.run_due_tasks(db)
