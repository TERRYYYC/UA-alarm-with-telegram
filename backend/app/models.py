from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    adapter: Mapped[str] = mapped_column(String(32), default="telegram")
    name: Mapped[str] = mapped_column(String(120))
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    checkpoint_json: Mapped[str] = mapped_column(Text, default="{}")
    gap_notes: Mapped[str] = mapped_column(Text, default="")


class RawMessage(Base):
    __tablename__ = "raw_messages"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", "revision", name="uq_source_ext_rev"),
        Index("ix_raw_family", "source_id", "external_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    external_id: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    body_text: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    forward_json: Mapped[str] = mapped_column(Text, default="{}")
    edit_json: Mapped[str] = mapped_column(Text, default="{}")
    original_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)


class GazetteerPlace(Base):
    __tablename__ = "gazetteer_places"

    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_name: Mapped[str] = mapped_column(String(120), index=True)
    names_json: Mapped[str] = mapped_column(Text)  # JSON 别名列表
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    admin1: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    admin2: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    precision: Mapped[str] = mapped_column(String(20), default="settlement")
    source: Mapped[str] = mapped_column(String(40), default="seed")
    name_zh: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)  # 管理端可编辑的中文名
    version: Mapped[int] = mapped_column(Integer, default=1)


class Candidate(Base):
    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("raw_messages.id"), index=True)
    extractor_version: Mapped[str] = mapped_column(String(32), default="rules-v1")
    event_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # 有意不加 FK:地名目录主存是文件(启动载入内存),gazetteer_places 只是运行时叠加层
    place_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    place_raw: Mapped[str] = mapped_column(String(200), default="")
    occurred_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    uncertainty_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    time_basis: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    # accepted | ignored | needs_review | failed
    status: Mapped[str] = mapped_column(String(20), default="needs_review", index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, index=True)  # 单调序号,SSE 断线补读
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    # 有意不加 FK:同上,place_id 多来自文件目录,DB 表里没有对应行
    place_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    geometry_json: Mapped[str] = mapped_column(Text, default="{}")
    occurred_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True, nullable=True)
    occurred_uncertainty_s: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 事件结束时刻(空袭警报解除等);为 NULL 表示仍在持续
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # unverified | confirmed | rejected
    verification: Mapped[str] = mapped_column(String(20), default="unverified", index=True)
    report_count: Mapped[int] = mapped_column(Integer, default=1)
    version: Mapped[int] = mapped_column(Integer, default=1)
    merged_into_id: Mapped[Optional[int]] = mapped_column(ForeignKey("events.id"), nullable=True)
    reason_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)


class Evidence(Base):
    __tablename__ = "evidence"
    __table_args__ = (
        UniqueConstraint("message_id", "event_id", "role", name="uq_evidence"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("raw_messages.id"), index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    # report | merge_basis | manual
    role: Mapped[str] = mapped_column(String(20), default="report")


class UnknownPlace(Base):
    """报文中出现但地名库未收录的专名(按原样聚合),驱动地名库维护。"""

    __tablename__ = "unknown_places"

    id: Mapped[int] = mapped_column(primary_key=True)
    raw: Mapped[str] = mapped_column(String(200), index=True)  # 规范化键(去重)
    display_name: Mapped[str] = mapped_column(String(200))  # 原始书写形式
    context: Mapped[str] = mapped_column(Text, default="")  # 首条出现时的消息片段
    report_count: Mapped[int] = mapped_column(Integer, default=1)
    source_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sources.id"), nullable=True)
    sample_message_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("raw_messages.id"), nullable=True
    )
    # open | resolved | ignored
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    # 有意不加 FK:解析到的地名可能来自文件目录而非 DB
    resolved_place_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    message_ids_json: Mapped[str] = mapped_column(Text, default="[]")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    lease_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
