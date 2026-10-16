"""Telegram 适配器测试:事件语义映射、游标幂等、FloodWait 退避(无需真实 SDK)。"""

from datetime import datetime, timezone

from app.telegram_adapter import TelegramAdapter, TgEvent


def ev(msg_id: int, text: str, **kw) -> TgEvent:
    return TgEvent(
        chat_id=42,
        message_id=msg_id,
        text=text,
        date=datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc),
        **kw,
    )


def test_new_message_maps_to_payload():
    a = TelegramAdapter(source_id=1, chat_id=42)
    p = a.handle_event(ev(101, "Обстріл Херсона"))
    assert p is not None
    assert p["source_id"] == 1
    assert p["external_id"] == "101"
    assert p["revision"] == 1
    assert p["body_text"] == "Обстріл Херсона"
    assert p["published_at"] == datetime(2026, 9, 7, 12, 0, 0)  # naive UTC
    assert p["forward_json"] == "{}"
    assert p["edit_json"] == "{}"


def test_duplicate_delivery_skipped():
    a = TelegramAdapter(source_id=1, chat_id=42)
    assert a.handle_event(ev(101, "x")) is not None
    assert a.handle_event(ev(101, "x")) is None  # 重复投递幂等跳过


def test_edit_increments_revision():
    a = TelegramAdapter(source_id=1, chat_id=42)
    a.handle_event(ev(101, "Обстріл Херсона"))
    p = a.handle_event(ev(101, "Обстріл Херсона, 10 хвилин тому", is_edit=True))
    assert p is not None
    assert p["revision"] == 2
    assert '"edited": true' in p["edit_json"]


def test_delete_marks_deleted():
    a = TelegramAdapter(source_id=1, chat_id=42)
    a.handle_event(ev(101, "Обстріл Херсона"))
    p = a.handle_event(ev(101, "", is_deleted=True))
    assert p is not None
    assert p["revision"] == 2
    assert '"deleted": true' in p["edit_json"]


def test_checkpoint_skips_old_messages():
    a = TelegramAdapter(source_id=1, chat_id=42)
    a.handle_event(ev(100, "old"))
    b = TelegramAdapter(source_id=1, chat_id=42, checkpoint=a.checkpoint())
    assert b.last_msg_id == 100
    assert b.handle_event(ev(99, "older")) is None
    assert b.handle_event(ev(101, "new")) is not None
    # 编辑事件不受游标限制(先前的消息编辑仍要处理)
    assert b.handle_event(ev(100, "edited", is_edit=True)) is not None


def test_forward_records_source():
    a = TelegramAdapter(source_id=1, chat_id=42)
    p = a.handle_event(ev(101, "x", is_forward=True, forward_from="ch-B"))
    assert '"from_channel": "ch-B"' in p["forward_json"]


def test_floodwait_backoff():
    a = TelegramAdapter(source_id=1, chat_id=42)
    wait = a.mark_floodwait(5.0)
    assert wait >= 5.0
    assert a.should_backoff() is True
    assert a.backoff_remaining() > 0
    a.clear_floodwait()
    assert a.should_backoff() is False


def test_floodwait_capped():
    a = TelegramAdapter(source_id=1, chat_id=42, flood_backoff_max=10.0)
    wait = a.mark_floodwait(1000.0)
    assert wait <= 10.0


def test_checkpoint_roundtrip_fields():
    a = TelegramAdapter(source_id=1, chat_id=42, checkpoint={"last_msg_id": 77, "gap_notes": "gap"})
    cp = a.checkpoint()
    assert cp["last_msg_id"] == 77
    assert cp["gap_notes"] == "gap"
