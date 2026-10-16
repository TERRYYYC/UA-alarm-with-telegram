"""Runner 环境装配测试:凭据/群清单环境变量 → Runner 构建(不连接网络)。"""

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.db import Base
from app.gazetteer import Gazetteer
from app.runner import RunnerConfig, TelegramRunner, build_runner_from_env
from app.service import EventBus


def make_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def make_gaz():
    g = Gazetteer()
    g.load_json(str(BACKEND / "data" / "gazetteer_seed.json"))
    return g


def test_build_runner_from_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abc")
    monkeypatch.setenv("TELEGRAM_PHONE", "+380991234567")
    monkeypatch.setenv("TELEGRAM_SESSION_PATH", "data/test.session")
    monkeypatch.setenv("TELEGRAM_CHATS", "-100111:Front News;-100222:Rear News")

    Session = make_session()
    runner = build_runner_from_env(Session, make_gaz(), EventBus())

    assert isinstance(runner, TelegramRunner)
    assert isinstance(runner.config, RunnerConfig)
    assert runner.config.api_id == 12345
    assert runner.config.phone == "+380991234567"
    assert set(runner.adapters.keys()) == {-100111, -100222}
    assert runner.adapters[-100111].source_id != runner.adapters[-100222].source_id
    with Session() as db:
        sources = db.query(models.Source).filter(models.Source.adapter == "telegram").all()
        assert len(sources) == 2


def test_build_runner_disabled_without_creds(monkeypatch):
    for k in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH"):
        monkeypatch.delenv(k, raising=False)
    Session = make_session()
    assert build_runner_from_env(Session, make_gaz(), EventBus()) is None


def test_build_runner_creates_session_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abc")
    monkeypatch.setenv("TELEGRAM_CHATS", "-100111:Front")
    session = tmp_path / "nested" / "dir" / "telegram.session"
    monkeypatch.setenv("TELEGRAM_SESSION_PATH", str(session))
    Session = make_session()
    runner = build_runner_from_env(Session, make_gaz(), EventBus())
    assert runner is not None
    assert session.parent.exists()


def test_build_runner_skips_invalid_chat_entries(monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abc")
    monkeypatch.setenv("TELEGRAM_CHATS", "-100111:Ok;not-a-number:Bad; ;-100222:Also Ok")
    Session = make_session()
    runner = build_runner_from_env(Session, make_gaz(), EventBus())
    assert set(runner.adapters.keys()) == {-100111, -100222}
