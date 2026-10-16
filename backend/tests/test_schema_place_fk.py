"""回归测试:place_id 不得对 gazetteer_places 建外键。

背景:地名目录主存是 JSON 文件(启动载入内存),gazetteer_places 表只是
"运行时/管理端新增"条目的叠加层。若这三列带 FK,SQLite(默认不强制)看不出来,
Postgres 会在 ingest 时报 ForeignKeyViolation 直接 500。
详见 backend/alembic/versions/d4e5f6a7b8c9_drop_place_fk.py。
"""
from app import models

FK_COLUMNS = {
    "events": "place_id",
    "candidates": "place_id",
    "unknown_places": "resolved_place_id",
}


def test_place_columns_have_no_fk():
    for table_name, column_name in FK_COLUMNS.items():
        table = models.Base.metadata.tables[table_name]
        column = table.columns[column_name]
        targets = {fk.target_fullname for fk in column.foreign_keys}
        assert not targets, f"{table_name}.{column_name} 不应有外键,实际指向 {targets}"
