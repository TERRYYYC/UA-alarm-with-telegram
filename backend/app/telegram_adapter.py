"""Telegram 来源适配器。

把底层 Telegram 事件(Telethon 或其替代实现,延迟导入)映射为 ingest 载荷,
并维护每来源的游标(checkpoint)与 FloodWait 退避状态。
连接/账号/session 与来源订阅分开建模:适配器只处理事件语义,不持有网络连接,
因此可以用假客户端完整测试;Runner(接入真实 SDK 的部分)待账号授权后实现。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone


def to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


@dataclass
class TgEvent:
    """底层事件统一形状(由 Runner 从 Telethon 或假客户端构造)。"""

    chat_id: int
    message_id: int
    text: str
    date: datetime
    is_edit: bool = False
    is_deleted: bool = False
    is_forward: bool = False
    forward_from: str | None = None  # 转发来源展示名
    original_url: str | None = None  # 原始消息链接(如 https://t.me/username/123)


class TelegramAdapter:
    """一个适配器实例对应一个来源实例(一个群/频道)。"""

    def __init__(
        self,
        source_id: int,
        chat_id: int,
        checkpoint: dict | None = None,
        flood_backoff_base: float = 2.0,
        flood_backoff_max: float = 300.0,
    ):
        self.source_id = source_id
        self.chat_id = chat_id
        self.flood_backoff_base = flood_backoff_base
        self.flood_backoff_max = flood_backoff_max
        cp = checkpoint or {}
        self.last_msg_id = int(cp.get("last_msg_id", 0))
        self.last_seen_at = str(cp.get("last_seen_at", ""))
        self.gap_notes = str(cp.get("gap_notes", ""))
        self.backoff_until = 0.0
        self.consecutive_floodwaits = 0
        self._revision_of: dict[int, int] = {}
        self._seen_new: set[int] = set()

    # ---- 游标 / 检查点 ----
    def checkpoint(self) -> dict:
        return {
            "last_msg_id": self.last_msg_id,
            "last_seen_at": self.last_seen_at,
            "gap_notes": self.gap_notes,
        }

    # ---- FloodWait ----
    def mark_floodwait(self, seconds: float) -> float:
        self.consecutive_floodwaits += 1
        wait = min(
            seconds * (self.flood_backoff_base ** (self.consecutive_floodwaits - 1)),
            self.flood_backoff_max,
        )
        self.backoff_until = time.monotonic() + wait
        return wait

    def clear_floodwait(self):
        self.backoff_until = 0.0
        self.consecutive_floodwaits = 0

    def should_backoff(self) -> bool:
        return time.monotonic() < self.backoff_until

    def backoff_remaining(self) -> float:
        return max(0.0, self.backoff_until - time.monotonic())

    # ---- 事件映射 ----
    def handle_event(self, ev: TgEvent) -> dict | None:
        """返回 ingest 载荷;应跳过的返回 None。

        语义:
        - 新消息 → revision=1;重复投递/游标之前的旧消息幂等跳过
        - 编辑 → 同一外部 ID,revision 递增
        - 删除 → 保留文本,edit_json 标记 deleted,由管线按撤回处理
        - 转发 → forward_json 记录来源展示名
        """
        if ev.is_edit or ev.is_deleted:
            rev = self._revision_of.get(ev.message_id, 1) + 1
        else:
            if ev.message_id in self._seen_new or ev.message_id <= self.last_msg_id:
                return None
            self._seen_new.add(ev.message_id)
            rev = 1

        edit_meta: dict = {}
        if ev.is_edit:
            edit_meta["edited"] = True
        if ev.is_deleted:
            edit_meta["deleted"] = True
        forward_meta: dict = {}
        if ev.is_forward:
            forward_meta["from_channel"] = ev.forward_from or ""

        payload = {
            "source_id": self.source_id,
            "external_id": str(ev.message_id),
            "revision": rev,
            "body_text": ev.text or "",
            "published_at": to_naive_utc(ev.date),
            "forward_json": json.dumps(forward_meta, ensure_ascii=False),
            "edit_json": json.dumps(edit_meta, ensure_ascii=False),
            "original_url": ev.original_url,
        }
        self._revision_of[ev.message_id] = rev
        if not (ev.is_edit or ev.is_deleted):
            self.last_msg_id = ev.message_id
            self.last_seen_at = to_naive_utc(ev.date).isoformat()
        return payload
