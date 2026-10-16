import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models, state
from app.db import Base, get_session
from app.gazetteer import Gazetteer
from app.main import app
from app.service import EventBus


def _make_client(extra_gazetteer_files=(), zh=False, zh_gn=False):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    gaz = Gazetteer()
    gaz.load_json(str(BACKEND / "data" / "gazetteer_seed.json"))
    for f in extra_gazetteer_files:
        gaz.load_json(str(BACKEND / "data" / f))
    if zh:
        gaz.apply_zh_names(str(BACKEND / "data" / "place_names_zh.json"))
    if zh_gn:
        gaz.apply_zh_geonames(str(BACKEND / "data" / "place_names_zh_gn.json"))
    state.gazetteer = gaz
    state.bus = EventBus()
    state.geocoder = None

    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = override
    with Session() as db:
        db.add(models.Source(id=1, adapter="fixture", name="fixture"))
        db.commit()

    c = TestClient(app)
    c.Session = Session
    yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def client():
    yield from _make_client()


@pytest.fixture()
def client_uk():
    """种子 + 真实乌克兰地名库(GeoNames/KATOTTH)。"""
    yield from _make_client(extra_gazetteer_files=("gazetteer_uk.json",))


@pytest.fixture()
def client_zh():
    """种子 + 中文地名映射(显示中文,识别仍用乌克兰语原文)。"""
    yield from _make_client(zh=True)


@pytest.fixture()
def client_uk_zh():
    """种子 + 真实地名库 + 两层中文映射(含 Wikidata 大众地名标签)。"""
    yield from _make_client(extra_gazetteer_files=("gazetteer_uk.json",), zh=True, zh_gn=True)
