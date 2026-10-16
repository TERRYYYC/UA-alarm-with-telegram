"""Telegram Runner:账号生命周期、来源订阅、事件处理、重连/退避、检查点与缺口恢复。

- 真实 SDK(Telethon)延迟导入,仅当凭据配置时实例化;未配置 → runner 不构建。
- 连接/账号/session 与来源订阅分开:adapters(TelegramAdapter)负责事件语义与游标,
  Runner 负责连接生命周期与故障恢复。
- 测试使用 FakeProvider 注入事件与故障,不依赖网络与 SDK。
"""

from __future__ import annotations

import json
import logging
import queue as queue_mod
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from sqlalchemy.orm import sessionmaker

from . import models
from .gazetteer import Gazetteer
from .service import EventBus, ingest_message, utcnow_naive
from .telegram_adapter import TelegramAdapter, TgEvent

log = logging.getLogger("smon.runner")


class FloodWait(Exception):
    def __init__(self, seconds: float):
        super().__init__(f"flood wait {seconds}s")
        self.seconds = float(seconds)


@dataclass
class RunnerConfig:
    api_id: int | None = None
    api_hash: str | None = None
    phone: str | None = None
    session_path: str | None = None
    chats: list[tuple[int, str]] = field(default_factory=list)  # (chat_id, 显示名)
    reconnect_base: float = 5.0
    reconnect_max: float = 300.0
    gap_fetch_limit: int = 200


class BaseProvider:
    """客户端抽象:start/stop/iter_events/fetch_history_since。"""

    def start(self):
        raise NotImplementedError

    def stop(self):
        pass

    def iter_events(self):
        """产出 TgEvent;断线时抛 ConnectionError;限流抛 FloodWait。"""
        raise NotImplementedError

    def fetch_history_since(self, chat_id: int, after_msg_id: int, limit: int) -> list[TgEvent]:
        raise NotImplementedError


class DisabledProvider(BaseProvider):
    def start(self):
        raise RuntimeError("runner disabled: no telegram credentials configured")


class TelethonProvider(BaseProvider):
    """真实实现:专用线程内常驻 asyncio 事件循环,handler 事件经队列桥接给 Runner 线程。

    说明:telethon 顶层导出的是异步客户端,其 connect/get_messages/is_user_authorized
    均为裸协程;混合方法 start() 在无运行中 loop 时同步执行(本实现绕开它,
    显式管理 loop,行为确定)。
    """

    def __init__(self, config: RunnerConfig):
        self.config = config
        self._client = None
        self._loop = None
        self._thread = None
        self._ready = threading.Event()
        self._error = None
        self._link_base: dict[int, str] = {}
        self._q: queue_mod.Queue = queue_mod.Queue(maxsize=5000)

    def start(self):
        try:
            import telethon  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("telethon 未安装: pip install telethon") from exc
        import asyncio

        from telethon import TelegramClient, events

        self._loop = asyncio.new_event_loop()

        def run_loop():
            try:
                asyncio.set_event_loop(self._loop)
                client = TelegramClient(
                    self.config.session_path,
                    self.config.api_id,
                    self.config.api_hash,
                )
                chat_ids = [cid for cid, _ in self.config.chats]

                @client.on(events.NewMessage(chats=chat_ids))
                async def _on_new(event):
                    self._put(event, is_edit=False)

                @client.on(events.MessageEdited(chats=chat_ids))
                async def _on_edit(event):
                    self._put(event, is_edit=True)

                self._client = client
                self._loop.run_until_complete(client.connect())
                if not self._loop.run_until_complete(client.is_user_authorized()):
                    raise RuntimeError("session 未授权:请先运行 scripts/telegram_login.py 完成登录")
                # 解析频道 username → 原始消息跳转链接;私有频道用成员链接 t.me/c/<id>/
                for chat_id in chat_ids:
                    base = None
                    try:
                        entity = self._loop.run_until_complete(client.get_entity(chat_id))
                        username = getattr(entity, "username", None)
                        if username:
                            base = f"https://t.me/{username}/"
                    except Exception:
                        pass
                    if base is None:
                        base = f"https://t.me/c/{str(chat_id).replace('-100', '')}/"
                    self._link_base[chat_id] = base
                self._ready.set()
                log.info("telethon client started (chats=%d)", len(chat_ids))
                self._loop.run_forever()
            except Exception as exc:
                self._error = exc
                self._ready.set()

        self._thread = threading.Thread(target=run_loop, daemon=True, name="telethon-loop")
        self._thread.start()
        if not self._ready.wait(timeout=120):
            raise RuntimeError("telethon client start timed out")
        if self._error is not None:
            raise self._error

    def _put(self, event, is_edit: bool):
        msg = event.message
        forward_from = None
        if msg.forward is not None:
            forward_from = getattr(msg.forward, "chat_title", None) or getattr(
                msg.forward, "sender_name", None
            )
        base = self._link_base.get(event.chat_id)
        original_url = f"{base}{msg.id}" if base else None
        try:
            self._q.put_nowait(
                TgEvent(
                    chat_id=event.chat_id,
                    message_id=msg.id,
                    text=msg.text or "",
                    date=msg.date,
                    is_edit=is_edit,
                    is_forward=msg.forward is not None,
                    forward_from=forward_from,
                    original_url=original_url,
                )
            )
        except queue_mod.Full:
            log.warning("telegram event queue full, dropping message %s", msg.id)

    def stop(self):
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def iter_events(self):
        while True:
            try:
                yield self._q.get(timeout=1.0)
            except queue_mod.Empty:
                if self._client is None or self._loop is None or not self._loop.is_running():
                    raise ConnectionError("telegram disconnected")

    def fetch_history_since(self, chat_id: int, after_msg_id: int, limit: int) -> list[TgEvent]:
        import asyncio

        if self._client is None or self._loop is None:
            return []
        fut = asyncio.run_coroutine_threadsafe(
            self._client.get_messages(chat_id, min_id=after_msg_id, limit=limit),
            self._loop,
        )
        msgs = fut.result(timeout=120)
        out = []
        base = self._link_base.get(chat_id)
        for m in reversed(msgs or []):
            if m is None or not m.text:
                continue
            out.append(
                TgEvent(
                    chat_id=chat_id,
                    message_id=m.id,
                    text=m.text,
                    date=m.date,
                    is_forward=m.forward is not None,
                    forward_from=getattr(m.forward, "chat_title", None) if m.forward else None,
                    original_url=f"{base}{m.id}" if base else None,
                )
            )
        return out


class TelegramRunner:
    """把 provider 事件经 adapter 映射后送入 ingest 管线,并维护检查点。"""

    def __init__(
        self,
        session_factory: sessionmaker,
        gaz: Gazetteer,
        bus: EventBus,
        provider: BaseProvider,
        adapters: dict[int, TelegramAdapter],  # chat_id -> adapter
        config: RunnerConfig,
    ):
        self._sf = session_factory
        self._gaz = gaz
        self._bus = bus
        self.provider = provider
        self.adapters = adapters
        self.config = config
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._reconnects = 0

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="telegram-runner")
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            self.provider.stop()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def _sleep_interruptible(self, seconds: float):
        self._stop.wait(timeout=max(0.0, seconds))

    def _run(self):
        while not self._stop.is_set():
            try:
                self.provider.start()
                self._reconnects = 0
                self._recover_gaps()  # 启动/重连后按检查点补采(首次 = 回填最近历史)
                for ev in self.provider.iter_events():
                    if self._stop.is_set():
                        return
                    self._handle(ev)
            except FloodWait as fw:
                # 限流:给相关 adapter 记退避,短睡眠后重试(不重连)
                for adapter in self.adapters.values():
                    adapter.mark_floodwait(fw.seconds)
                log.warning("floodwait %ss, backing off", fw.seconds)
                self._sleep_interruptible(min(fw.seconds, 60.0))
            except Exception as exc:
                if self._stop.is_set():
                    return
                backoff = min(
                    self.config.reconnect_base * (2 ** self._reconnects),
                    self.config.reconnect_max,
                )
                self._reconnects += 1
                log.warning("runner disconnected (%s); reconnect in %.0fs", exc, backoff)
                self._sleep_interruptible(backoff)
                if self._stop.is_set():
                    return
                self._recover_gaps()

    def _handle(self, ev: TgEvent):
        adapter = self.adapters.get(ev.chat_id)
        if adapter is None:
            return
        if adapter.should_backoff():
            return  # 退避期间丢弃(消息会在重连补采中恢复)
        payload = adapter.handle_event(ev)
        if payload is None:
            return
        import time

        from sqlalchemy.exc import OperationalError

        # DB 写锁重试(如维护性重放期间),失败只影响本条,不触发 Telegram 重连
        for attempt in range(4):
            try:
                with self._sf() as db:
                    ingest_message(db, self._gaz, self._bus, **payload)
                    source = db.get(models.Source, adapter.source_id)
                    if source is not None:
                        source.checkpoint_json = json.dumps(adapter.checkpoint(), ensure_ascii=False)
                        source.last_success_at = utcnow_naive()
                        db.commit()
                return
            except OperationalError as exc:
                log.warning("db locked, retry %d/4: %s", attempt + 1, exc)
                time.sleep(1.5 * (attempt + 1))
        log.error("dropping message %s after db lock retries", payload["external_id"])

    def _recover_gaps(self):
        """重连后按检查点补采;发现无法确认的缺口时记录 gap_notes。"""
        for chat_id, adapter in self.adapters.items():
            try:
                msgs = self.provider.fetch_history_since(
                    chat_id, adapter.last_msg_id, self.config.gap_fetch_limit
                )
            except Exception:
                continue
            if not msgs:
                continue
            first_id = msgs[0].message_id
            if adapter.last_msg_id > 0 and first_id > adapter.last_msg_id + 1:
                adapter.gap_notes = (
                    f"gap after {adapter.last_msg_id}: next={first_id} at "
                    f"{utcnow_naive().isoformat()}"
                )
            for ev in msgs:
                if self._stop.is_set():
                    return
                self._handle(ev)


def build_runner_from_env(session_factory: sessionmaker, gaz: Gazetteer, bus: EventBus) -> TelegramRunner | None:
    """按环境变量构建 Runner;凭据缺失时返回 None(保持禁用状态,记录一次日志)。"""
    import os

    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        log.info("telegram runner disabled (TELEGRAM_API_ID/HASH not set)")
        return None
    chats = []
    for pair in (os.getenv("TELEGRAM_CHATS") or "").split(";"):
        pair = pair.strip()
        if not pair:
            continue
        cid, _, rest = pair.partition(":")
        name, _, bias = rest.partition(":")
        name, bias = name.strip(), bias.strip()
        try:
            chats.append((int(cid), name or str(cid), bias or None))
        except ValueError:
            log.warning("invalid TELEGRAM_CHATS entry: %r", pair)
    config = RunnerConfig(
        api_id=int(api_id),
        api_hash=api_hash,
        phone=os.getenv("TELEGRAM_PHONE"),
        session_path=os.getenv("TELEGRAM_SESSION_PATH", "data/telegram.session"),
        chats=[(cid, name) for cid, name, _ in chats],
    )
    if config.session_path:
        os.makedirs(os.path.dirname(os.path.abspath(config.session_path)) or ".", exist_ok=True)
    adapters: dict[int, TelegramAdapter] = {}
    with session_factory() as db:
        for chat_id, name, bias in chats:
            source = (
                db.query(models.Source)
                .filter(models.Source.adapter == "telegram", models.Source.name == f"tg:{name}")
                .first()
            )
            cfg = json.dumps({"admin1_bias": bias}, ensure_ascii=False) if bias else "{}"
            if source is None:
                source = models.Source(adapter="telegram", name=f"tg:{name}", config_json=cfg)
                db.add(source)
                db.commit()
            elif source.config_json != cfg:
                source.config_json = cfg
                db.commit()
            checkpoint = json.loads(source.checkpoint_json or "{}")
            adapters[chat_id] = TelegramAdapter(source.id, chat_id, checkpoint=checkpoint)
    provider = TelethonProvider(config) if chats else DisabledProvider()
    return TelegramRunner(session_factory, gaz, bus, provider, adapters, config)
