"""drop place_id foreign keys to gazetteer_places

Revision ID: d4e5f6a7b8c9
Revises: b07f49e510c4
Create Date: 2026-09-20 23:45:00.000000

地名目录的主存是文件(gazetteer_seed.json + gazetteer_uk.json,约 3.5 万条),
启动时载入内存;DB 表 gazetteer_places 只是"运行时自动地理编码 + 管理端编辑"
条目的叠加层(见 docs/HANDOFF.md)。因此 events.place_id / candidates.place_id /
unknown_places.resolved_place_id 会引用大量并不存在于该表里的目录地名。

SQLite 默认不强制外键(app 也未打开 PRAGMA foreign_keys),所以该问题一直潜伏;
Postgres 强制执行,任何来自文件目录的地名都会让 ingest 报
ForeignKeyViolation: events_place_id_fkey 而返回 500。

这里按 SQLite 的实际语义移除这三个外键,列本身保留(查询始终走内存目录)。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'b07f49e510c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("events", "candidates", "unknown_places")


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    for table in _TABLES:
        for fk in insp.get_foreign_keys(table):
            if fk.get("referred_table") == "gazetteer_places":
                op.drop_constraint(fk["name"], table, type_="foreignkey")


def downgrade() -> None:
    # 恢复约束要求表中已存在被引用的地名;若已有目录地名被事件引用(常态),
    # 该 downgrade 会失败——这正说明约束与数据模型不符。
    op.create_foreign_key(
        "events_place_id_fkey", "events", "gazetteer_places", ["place_id"], ["id"]
    )
    op.create_foreign_key(
        "candidates_place_id_fkey", "candidates", "gazetteer_places", ["place_id"], ["id"]
    )
    op.create_foreign_key(
        "unknown_places_resolved_place_id_fkey",
        "unknown_places",
        "gazetteer_places",
        ["resolved_place_id"],
        ["id"],
    )
