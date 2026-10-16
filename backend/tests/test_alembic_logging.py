"""回归测试:alembic 的 fileConfig 不得禁用应用日志器。

背景:`backend/alembic/env.py` 在模块级调用 `fileConfig(config.config_file_name)`,
该函数默认 `disable_existing_loggers=True`,会把 `alembic.ini` 里未声明的日志器
(本项目的 `smon` / `smon.runner` / `smon.publisher`)全部置为 disabled。

生产走 Postgres 时,`backend/app/main.py` 的 lifespan 每次启动都先执行迁移,
于是应用日志整体静默 —— 采集器是否启动、是否掉线、发布器是否在跑,全都看不到。
SQLite 路径不在启动时跑 alembic,所以本地开发与测试都发现不了这个问题。
"""
from pathlib import Path

ENV_PY = Path(__file__).resolve().parents[1] / "alembic" / "env.py"


def test_alembic_fileconfig_keeps_existing_loggers():
    source = ENV_PY.read_text(encoding="utf-8")
    assert "fileConfig(" in source, "env.py 应仍调用 fileConfig 读取 alembic.ini"
    compact = source.replace(" ", "")
    assert "disable_existing_loggers=False" in compact, (
        "env.py 必须显式传 disable_existing_loggers=False,"
        "否则 smon.* 应用日志会被 alembic 的 fileConfig 禁用"
    )
