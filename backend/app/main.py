import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from . import config, models, state
from .api import add_cors, router
from .db import Base, SessionLocal, engine
from .gazetteer import Gazetteer, is_reportable
from .service import EventBus

log = logging.getLogger("smon")


def _setup_logging():
    """给本项目日志器挂独立 handler(uvicorn 默认配置不显示 INFO 级非 uvicorn 日志)。"""
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    for name in ("smon", "smon.runner", "smon.publisher"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        lg.propagate = False
        lg.addHandler(handler)


_setup_logging()


def _start_publisher():
    """配置了 PUBLISH_OUT_DIR 时,启动发布器后台线程(事件触发 + 最小间隔)。"""
    if not config.PUBLISH_OUT_DIR:
        return
    from .publisher import DiscordSender, FileSender, Publisher, Renderer, Sender, TelegramSender

    work_dir = Path(config.PUBLISH_OUT_DIR) / "work"
    out_dir = Path(config.PUBLISH_OUT_DIR) / "published"
    sender: Sender
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_PUBLISH_CHAT:
        sender = TelegramSender(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_PUBLISH_CHAT)
        log.info("publisher sender: telegram %s", config.TELEGRAM_PUBLISH_CHAT)
    elif config.DISCORD_WEBHOOK_URL:
        sender = DiscordSender(config.DISCORD_WEBHOOK_URL)
        log.info("publisher sender: discord webhook")
    else:
        sender = FileSender(out_dir)
        log.info("publisher sender: file (%s)", out_dir)
    state.publisher = Publisher(
        session_factory=SessionLocal,
        bus=state.bus,
        renderer=Renderer(),
        sender=sender,
        work_dir=work_dir,
        min_interval_s=config.PUBLISH_MIN_INTERVAL_S,
        url=config.PUBLISH_RENDER_URL,
    )
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                state.publisher.tick()
            except Exception:
                log.exception("publisher tick failed")
            stop.wait(15)

    th = threading.Thread(target=loop, daemon=True, name="publisher")
    th.start()
    state.publisher_stop = stop


def _start_runner():
    """配置了 TELEGRAM_API_ID/HASH 时,构建并启动采集 Runner。"""
    from .runner import build_runner_from_env

    runner = build_runner_from_env(SessionLocal, state.gazetteer, state.bus)
    if runner is not None:
        runner.start()
        state.runner = runner


@asynccontextmanager
async def lifespan(app: FastAPI):
    if config.DATABASE_URL.startswith("sqlite") and "///" in config.DATABASE_URL:
        path = config.DATABASE_URL.split("///", 1)[1]
        if path and path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    if config.DATABASE_URL.startswith("sqlite"):
        Base.metadata.create_all(engine)  # 本地/测试便捷路径
    else:
        # 生产(Postgres):启动前自动执行迁移
        from alembic import command as alembic_command
        from alembic.config import Config as AlembicConfig

        alembic_command.upgrade(AlembicConfig("alembic.ini"), "head")
    state.gazetteer = Gazetteer()
    state.gazetteer.load_json(config.GAZETTEER_SEED)
    # 真实乌克兰地名库(GeoNames + KATOTTH,由 scripts/build_gazetteer.py 生成;可选)
    uk_file = os.path.join(os.path.dirname(config.GAZETTEER_SEED), "gazetteer_uk.json")
    if os.path.exists(uk_file):
        state.gazetteer.load_json(uk_file)
    # 中文地名映射(显示用;识别仍以乌克兰语原文为准)
    zh_file = os.path.join(os.path.dirname(config.GAZETTEER_SEED), "place_names_zh.json")
    if os.path.exists(zh_file):
        state.gazetteer.apply_zh_names(zh_file)
    # Wikidata 中文标签(按 GeoNames ID,覆盖大众地名库)
    zh_gn_file = os.path.join(os.path.dirname(config.GAZETTEER_SEED), "place_names_zh_gn.json")
    if os.path.exists(zh_gn_file):
        state.gazetteer.apply_zh_geonames(zh_gn_file)
    # 运行时新增/管理端编辑的地点从 DB 加载,跨重启保留(同 ID 的编辑覆盖文件条目)
    with SessionLocal() as db:
        for row in db.query(models.GazetteerPlace).order_by(models.GazetteerPlace.id).all():
            existing = state.gazetteer.resolve(row.canonical_name)
            if existing and existing.get("place_id") and existing["place_id"] != row.id:
                continue  # 与既有目录重复(如重复州条目),以既有条目为准
            if (row.source or "") != "admin" and not is_reportable(row.canonical_name):
                # 自动地理编码写入的库中残留噪音(自然地貌/描述性名称),启动时不再载入
                state.gazetteer.remove_place(row.id)
                continue
            prev = state.gazetteer.get(row.id) or {}
            if row.name_zh is None:
                zh = prev.get("name_zh")  # 保留文件中已有的中文名(Wikidata/人工维护)
            else:
                zh = row.name_zh or None
            state.gazetteer.upsert_place(
                row.id,
                row.canonical_name,
                json.loads(row.names_json or "[]"),
                row.lat,
                row.lng,
                row.admin1,
                row.admin2,
                row.precision,
                source=row.source or "admin",
                name_zh=zh,
            )
    state.bus = EventBus()
    if config.GEOCODER_ENABLED:
        from .geocoder import Geocoder

        state.geocoder = Geocoder()
    with SessionLocal() as db:
        if not db.query(models.Source).first():
            db.add(models.Source(id=1, adapter="fixture", name="fixture"))
            db.commit()
    _start_publisher()
    _start_runner()
    yield
    stop = getattr(state, "publisher_stop", None)
    if stop:
        stop.set()
    runner = getattr(state, "runner", None)
    if runner:
        runner.stop()


app = FastAPI(title="situation-monitor API", version="0.1.0", lifespan=lifespan)
add_cors(app)
app.include_router(router)
