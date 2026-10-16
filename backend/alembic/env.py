import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# 使 app 包可导入(alembic 从 backend/ 运行)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config as app_config
from app.db import Base
from app import models  # noqa: F401  导入以注册全部元数据

config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers 默认为 True:alembic.ini 只声明了 root/sqlalchemy/alembic,
    # 会把 app 自己的 smon.* 日志器一并禁用。Postgres 路径下每次启动 lifespan 都先跑迁移,
    # 结果应用日志(采集器启动/掉线、发布器 tick)全部静默。必须显式关掉。
    fileConfig(config.config_file_name, disable_existing_loggers=False)

config.set_main_option("sqlalchemy.url", app_config.DATABASE_URL)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
