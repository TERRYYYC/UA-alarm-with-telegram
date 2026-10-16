"""发布器测试:最小间隔限流、任务租约/重试、渲染与发送解耦、FileSender/Telegram/Discord 产物。"""

import json
from pathlib import Path

from app import models
from app.publisher import DiscordSender, FileSender, Publisher, Renderer, TelegramSender
from app.service import EventBus


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def read(self):
        return json.dumps(self._data).encode("utf-8") if not isinstance(self._data, bytes) else self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_telegram_sender_payload_and_result(tmp_path, monkeypatch):
    img = tmp_path / "map-x.png"
    img.write_bytes(b"png-bytes")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = req.headers
        captured["body"] = req.data
        return _FakeResp({"ok": True, "result": {"message_id": 777}})

    monkeypatch.setattr("app.publisher.urllib.request.urlopen", fake_urlopen)
    s = TelegramSender("TOKEN123", "-100999")
    r = s.send(img, caption="тест")
    assert r == {"channel": "telegram", "external_id": "777", "chat_id": "-100999"}
    assert "botTOKEN123/sendPhoto" in captured["url"]
    assert b'name="chat_id"' in captured["body"]
    assert b"-100999" in captured["body"]
    assert b"map-x.png" in captured["body"]
    assert b"image/png" in captured["body"]


def test_telegram_sender_raises_on_not_ok(tmp_path, monkeypatch):
    img = tmp_path / "map-x.png"
    img.write_bytes(b"png-bytes")

    def fake_urlopen(req, timeout=None):
        return _FakeResp({"ok": False, "description": "Forbidden: bot was blocked"})

    monkeypatch.setattr("app.publisher.urllib.request.urlopen", fake_urlopen)
    import pytest

    with pytest.raises(RuntimeError, match="bot was blocked"):
        TelegramSender("TOKEN123", "-100999").send(img, caption="x")


def test_discord_sender_payload(tmp_path, monkeypatch):
    img = tmp_path / "map-x.png"
    img.write_bytes(b"png-bytes")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = req.data
        return _FakeResp(b"")

    monkeypatch.setattr("app.publisher.urllib.request.urlopen", fake_urlopen)
    r = DiscordSender("https://discord.com/api/webhooks/1/2").send(img, caption="战况地图")
    assert r == {"channel": "discord", "external_id": None, "status": "sent"}
    assert captured["url"] == "https://discord.com/api/webhooks/1/2"
    assert b'name="content"' in captured["body"]
    assert "战况地图".encode("utf-8") in captured["body"]
    assert b'name="file"' in captured["body"]


class FakeRenderer(Renderer):
    def __init__(self, fail_times: int = 0):
        super().__init__(chrome="/nonexistent")
        self.calls = 0
        self.fail_times = fail_times
        self.failed = 0

    def render(self, url, out_path):
        self.calls += 1
        if self.failed < self.fail_times:
            self.failed += 1
            return False
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"x" * 5000)
        return True


class RecordingSender:
    def __init__(self):
        self.sent = []

    def send(self, image_path, caption):
        self.sent.append({"path": str(image_path), "caption": caption})
        return {"ok": True, "n": len(self.sent)}


def make_publisher(Session, bus, tmp_path: Path, **kw):
    pub = Publisher(
        session_factory=Session,
        bus=bus,
        renderer=FakeRenderer(),
        sender=RecordingSender(),
        work_dir=tmp_path / "work",
        max_attempts=5,
        **kw,
    )
    return pub


def bump(bus):
    bus.publish(bus.next_seq(), {"seq": bus.last_seq(), "data": {}})


def test_event_trigger_publishes_once_per_interval(tmp_path):
    from app.db import Base

    # 复用一个 SQLite 内存库
    import pytest
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    bus = EventBus()
    pub = make_publisher(Session, bus, tmp_path, min_interval_s=3600)

    bump(bus)
    pub.tick()  # 第一次:新事件 → 调度并执行
    with Session() as db:
        assert db.query(models.Task).count() == 1
        t = db.query(models.Task).one()
        assert t.status == "done" and t.attempts == 1

    bump(bus)
    pub.tick()  # 间隔内 → 不调度
    with Session() as db:
        assert db.query(models.Task).count() == 1

    pub.min_interval_s = 0
    bump(bus)
    pub.tick()  # 间隔过后 → 第二次发布
    with Session() as db:
        assert db.query(models.Task).count() == 2


def test_failure_retry_and_max_attempts(tmp_path):
    import pytest
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db import Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    bus = EventBus()

    renderer = FakeRenderer(fail_times=2)
    pub = Publisher(
        session_factory=Session,
        bus=bus,
        renderer=renderer,
        sender=RecordingSender(),
        work_dir=tmp_path / "work",
        min_interval_s=0,
        max_attempts=2,
    )
    bump(bus)
    pub.tick()  # 失败 1
    with Session() as db:
        t = db.query(models.Task).one()
        assert t.status == "failed" and t.attempts == 1 and t.error

    # 第一次失败后,seq 未变,直接再次 tick 只重试失败任务
    pub.tick()  # 重试 → 失败 2 → 达到上限
    with Session() as db:
        t = db.query(models.Task).one()
        assert t.status == "failed" and t.attempts == 2

    pub.tick()  # 达到上限后不再执行
    with Session() as db:
        t = db.query(models.Task).one()
        assert t.attempts == 2


def test_file_sender_writes_artifact(tmp_path):
    img = tmp_path / "map-x.png"
    img.write_bytes(b"png-bytes" * 1000)
    sender = FileSender(out_dir=tmp_path / "published")
    meta = sender.send(img, caption="test")
    dest = tmp_path / "published" / "map-x.png"
    assert dest.exists()
    assert (dest.with_suffix(".json")).exists()
    assert meta["file"] == str(dest)


def test_force_schedule_bypasses_interval(tmp_path):
    import pytest
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db import Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    bus = EventBus()
    pub = make_publisher(Session, bus, tmp_path, min_interval_s=3600)

    bump(bus)
    pub.tick()  # 第一次发布完成
    with Session() as db:
        assert db.query(models.Task).count() == 1

    with Session() as db:
        t = pub.maybe_schedule(db, force=True)  # 间隔内强制调度
    assert t is not None
    pub.tick()
    with Session() as db:
        tasks = db.query(models.Task).order_by(models.Task.id.desc()).all()
        assert len(tasks) == 2
        assert tasks[0].status == "done"


def test_tasks_record_caption_and_url(tmp_path):
    import pytest
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db import Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    bus = EventBus()
    pub = make_publisher(Session, bus, tmp_path, min_interval_s=0, url="http://127.0.0.1:5173/?export=1", window_label="60 分钟")
    bump(bus)
    pub.tick()
    with Session() as db:
        t = db.query(models.Task).one()
        assert t.status == "done"
        assert "http://127.0.0.1:5173/?export=1" in t.params_json
        assert "60 分钟" in t.params_json
        assert '"ok": true' in t.result_json


# 渲染兜底:错误页(没有地图画布)不能当成战况图发出去
def test_renderer_refuses_error_page(tmp_path):
    import http.server
    import os
    import socketserver
    import threading

    chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if not os.path.exists(chrome):
        import pytest

        pytest.skip("headless Chrome not installed")

    root = tmp_path / "www"
    root.mkdir()
    (root / "broken.html").write_text("<html><body>ERR_CONNECTION_REFUSED</body></html>", encoding="utf-8")
    (root / "ok.html").write_text(
        "<html><body><div class='map'><canvas></canvas></div>"
        "<div id='export-tag'>战况地图 · 测试 · 事件 3</div></body></html>",
        encoding="utf-8",
    )

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(root), **kw)

        def log_message(self, *a):
            pass

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as httpd:
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        renderer = Renderer(wait_timeout=3)
        broken = renderer.render(f"http://127.0.0.1:{port}/broken.html", tmp_path / "broken.png")
        assert broken is False
        assert "not ready" in (renderer.last_error or "")
        ok = renderer.render(f"http://127.0.0.1:{port}/ok.html", tmp_path / "ok.png")
        assert ok is True and (tmp_path / "ok.png").stat().st_size > 1000
        httpd.shutdown()
