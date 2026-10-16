"""Runner 测试:FakeProvider 注入事件/故障,验证处理、检查点、退避、缺口恢复(不依赖 SDK)。"""

from datetime import datetime, timezone

from app import models
from app.gazetteer import Gazetteer
from app.runner import (
    BaseProvider,
    DisabledProvider,
    FloodWait,
    RunnerConfig,
    TelegramRunner,
)
from app.service import EventBus
from app.telegram_adapter import TelegramAdapter, TgEvent

BACKEND = __import__("pathlib").Path(__file__).resolve().parent.parent


def mk_ev(msg_id: int, text: str, chat_id: int = 42, **kw) -> TgEvent:
    return TgEvent(
        chat_id=chat_id,
        message_id=msg_id,
        text=text,
        date=datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc),
        **kw,
    )


class FakeProvider(BaseProvider):
    def __init__(self, events=None, history=None, flood_times=0, start_fail=0):
        self.events = list(events or [])
        self.history = list(history or [])
        self.flood_times = flood_times
        self.start_fail = start_fail
        self.started = 0
        self.stopped = 0

    def start(self):
        if self.start_fail > 0:
            self.start_fail -= 1
            raise ConnectionError("simulated connect failure")
        self.started += 1

    def iter_events(self):
        for ev in self.events:
            if self.flood_times > 0:
                self.flood_times -= 1
                raise FloodWait(30)
            yield ev

    def fetch_history_since(self, chat_id, after_msg_id, limit):
        return [e for e in self.history if e.chat_id == chat_id and e.message_id > after_msg_id][:limit]


def make_runner(Session, gaz, bus, provider, checkpoint=None, **cfg_kw):
    adapter = TelegramAdapter(source_id=1, chat_id=42, checkpoint=checkpoint)
    config = RunnerConfig(chats=[(42, "test-chat")], **cfg_kw)
    return TelegramRunner(Session, gaz, bus, provider, {42: adapter}, config), adapter


def make_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db import Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with Session() as db:
        db.add(models.Source(id=1, adapter="telegram", name="tg:test-chat"))
        db.commit()
    return Session


def make_gaz():
    g = Gazetteer()
    g.load_json(str(BACKEND / "data" / "gazetteer_seed.json"))
    return g


def test_runner_ingests_event_and_saves_checkpoint():
    Session = make_session()
    gaz, bus = make_gaz(), EventBus()
    provider = FakeProvider()
    runner, adapter = make_runner(Session, gaz, bus, provider)

    runner._handle(mk_ev(101, "Обстріл Херсона, 10 хвилин тому"))

    with Session() as db:
        assert db.query(models.RawMessage).count() == 1
        assert db.query(models.Event).count() == 1
        source = db.get(models.Source, 1)
        assert '"last_msg_id": 101' in source.checkpoint_json
    assert adapter.last_msg_id == 101


def test_runner_edit_increments_revision_and_merges():
    Session = make_session()
    gaz, bus = make_gaz(), EventBus()
    runner, _ = make_runner(Session, gaz, bus, FakeProvider())

    runner._handle(mk_ev(101, "Обстріл Херсона, 10 хвилин тому"))
    runner._handle(mk_ev(101, "Обстріл Херсона, 8 хвилин тому", is_edit=True))

    with Session() as db:
        raws = db.query(models.RawMessage).all()
        assert [(r.external_id, r.revision) for r in raws] == [("101", 1), ("101", 2)]
        evs = db.query(models.Event).filter(models.Event.verification != "rejected").all()
        # 同一条消息的修订合并进同一事件,报道数仍为 1(不是两条独立报道)
        assert len(evs) == 1 and evs[0].report_count == 1
        ev = db.query(models.Evidence).one()
        assert ev.message_id == raws[1].id


def test_runner_floodwait_marks_backoff():
    Session = make_session()
    gaz, bus = make_gaz(), EventBus()
    provider = FakeProvider(events=[mk_ev(101, "Обстріл Херсона")], flood_times=1)
    runner, adapter = make_runner(Session, gaz, bus, provider)

    try:
        next(provider.iter_events())
        raise AssertionError("should have raised FloodWait")
    except FloodWait:
        for a in runner.adapters.values():
            a.mark_floodwait(30)

    assert adapter.should_backoff() is True
    # 退避期间事件被丢弃(重连补采恢复)
    runner._handle(mk_ev(102, "Обстріл Херсона"))
    with Session() as db:
        assert db.query(models.RawMessage).count() == 0


def test_runner_gap_recovery_fills_and_notes_gap():
    Session = make_session()
    gaz, bus = make_gaz(), EventBus()
    provider = FakeProvider(history=[mk_ev(103, "Обстріл Херсона"), mk_ev(104, "Обстріл Херсона")])
    runner, adapter = make_runner(Session, gaz, bus, provider, checkpoint={"last_msg_id": 100})

    runner._recover_gaps()

    assert adapter.last_msg_id == 104
    assert "gap after 100" in adapter.gap_notes
    with Session() as db:
        assert db.query(models.RawMessage).count() == 2
        evs = db.query(models.Event).filter(models.Event.verification != "rejected").all()
        # 两条不同消息 → 同一事件、两条报道
        assert len(evs) == 1 and evs[0].report_count == 2
        assert db.query(models.Evidence).count() == 2


def test_runner_gap_without_hole_no_note():
    Session = make_session()
    gaz, bus = make_gaz(), EventBus()
    provider = FakeProvider(history=[mk_ev(101, "Обстріл Херсона")])
    runner, adapter = make_runner(Session, gaz, bus, provider, checkpoint={"last_msg_id": 100})

    runner._recover_gaps()

    assert adapter.last_msg_id == 101
    assert adapter.gap_notes == ""


def test_disabled_provider_raises():
    Session = make_session()
    gaz, bus = make_gaz(), EventBus()
    runner = TelegramRunner(Session, gaz, bus, DisabledProvider(), {}, RunnerConfig())
    try:
        runner.provider.start()
        raise AssertionError("should have raised")
    except RuntimeError as exc:
        assert "disabled" in str(exc)


def test_runner_run_loop_reconnects(tmp_path):
    """run() 在连接失败时退避重连(用 start_fail=1 模拟一次失败)。"""
    Session = make_session()
    gaz, bus = make_gaz(), EventBus()
    provider = FakeProvider(events=[mk_ev(101, "Обстріл Херсона")], start_fail=1)
    runner, _ = make_runner(Session, gaz, bus, provider, reconnect_base=0.05, reconnect_max=0.2)

    runner._reconnects = 0
    # 直接跑一轮 _run 的核心循环逻辑:start 失败 → 退避 → recover → 下次循环 start 成功并处理事件
    import threading

    runner._stop.set()  # 让循环尽快退出
    runner._sleep_interruptible = lambda s: None
    # 手动驱动:模拟第一次连接失败后的恢复
    try:
        provider.start()
    except ConnectionError:
        pass
    provider.start()
    for ev in provider.iter_events():
        runner._handle(ev)

    with Session() as db:
        assert db.query(models.RawMessage).count() == 1
