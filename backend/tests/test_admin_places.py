"""地名目录维护 + 自动核验测试。

覆盖:管理端新增/编辑地名(HTTP 与内存目录一致性)、中文/别名检索、
地名修正后事件坐标同步、未解析候选的重新匹配、多源交叉印证自动确认。
"""

from app import models

BASE = "2026-09-07T12:00:00Z"
BIG_WINDOW = {"window_minutes": 100000}


def ingest(client, external_id, body, published_at=BASE, source_id=1, **extra):
    payload = {
        "source_id": source_id,
        "external_id": external_id,
        "revision": 1,
        "body_text": body,
        "published_at": published_at,
    }
    payload.update(extra)
    r = client.post("/api/v1/ingest", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def events(client, **params):
    p = dict(BIG_WINDOW)
    p.update(params)
    r = client.get("/api/v1/events", params=p)
    assert r.status_code == 200, r.text
    return r.json()


def add_source(client, source_id, name="second"):
    with client.Session() as db:
        db.add(models.Source(id=source_id, adapter="fixture", name=name))
        db.commit()


# 1. 新增地名 → 立即可解析 → 待处理候选重新匹配
def test_create_place_and_rematch(client):
    ingest(client, "m1", "Приліт по Новомихайлівці, 5 хвилин тому")
    assert events(client) == []

    r = client.post(
        "/api/v1/places",
        json={
            "canonical_name": "Новомихайлівка",
            "names": ["Новомихайлівка", "Новомихайлівці"],
            "lat": 47.85,
            "lng": 37.48,
            "admin1": "Донецька область",
            "precision": "village",
            "name_zh": "新米哈伊利夫卡",
        },
    )
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["id"] >= 1_000_000_000
    assert created["name_zh"] == "新米哈伊利夫卡"

    # 目录已即时可用,DB 已落盘(重启后仍生效)
    with client.Session() as db:
        row = db.get(models.GazetteerPlace, created["id"])
        assert row is not None and row.name_zh == "新米哈伊利夫卡"

    # 待补地名列表已记录该专名
    unk = client.get("/api/v1/unknown-places").json()
    assert any("овомихайлів" in u["raw"] for u in unk), unk

    # 重新匹配:未收录专名被补齐,相关报文回填,事件上图
    r = client.post("/api/v1/admin/rematch")
    assert r.status_code == 200, r.text
    assert r.json()["unknown"]["unknown_resolved"] >= 1
    assert client.get("/api/v1/unknown-places").json() == []
    evs = events(client)
    assert len(evs) == 1
    assert evs[0]["place"]["name"] == "新米哈伊利夫卡"
    assert client.get("/api/v1/pending").json() == []


# 2. 中文名与别名都能检索到
def test_search_places_by_zh_and_alias(client_zh):
    client_zh.post(
        "/api/v1/places",
        json={
            "canonical_name": "Новомихайлівка",
            "names": ["Новомихайлівка", "Новомихайлівці"],
            "lat": 47.85,
            "lng": 37.48,
            "admin1": "Донецька область",
            "precision": "village",
            "name_zh": "新米哈伊利夫卡",
        },
    )
    r = client_zh.get("/api/v1/places", params={"q": "赫尔松"})
    assert r.status_code == 200
    assert any(p["name"] == "Херсон" for p in r.json()), "按中文名可检索"

    r = client_zh.get("/api/v1/places", params={"q": "Херсоні"})
    assert any(p["name"] == "Херсон" for p in r.json()), "按乌克兰语别名可检索"

    r = client_zh.get("/api/v1/places", params={"q": "新米哈伊利夫卡"})
    assert any(p["canonical_name" if "canonical_name" in p else "name"] == "Новомихайлівка" for p in r.json())


# 3. 编辑地名坐标 → 该地点事件坐标同步(事件实时读目录)
def test_update_place_moves_event(client):
    ingest(client, "m1", "Обстріл Херсона, 10 хвилин тому")
    ev = events(client)[0]
    place_id = ev["place"]["id"]
    assert (ev["place"]["lat"], ev["place"]["lng"]) != (46.6, 32.6)

    r = client.put(
        f"/api/v1/places/{place_id}",
        json={
            "canonical_name": "Херсон",
            "names": ["Херсон", "Херсона"],
            "lat": 46.6,
            "lng": 32.6,
            "admin1": "Херсонська область",
            "precision": "city",
            "name_zh": "赫尔松",
        },
    )
    assert r.status_code == 200, r.text
    moved = events(client)[0]
    assert (moved["place"]["lat"], moved["place"]["lng"]) == (46.6, 32.6)
    assert moved["place"]["name"] == "赫尔松"

    detail = client.get(f"/api/v1/places/{place_id}").json()
    assert "Херсона" in detail["names"]


# 4. 多源交叉印证 → 自动确认;单源单报保持未核验
def test_corroboration_auto_confirms(client):
    add_source(client, 2, "second")
    ingest(client, "a1", "Обстріл Херсона, 10 хвилин тому", source_id=1)
    ev = events(client)[0]
    assert ev["verification"] == "unverified"

    ingest(client, "b1", "Херсон під обстрілом, 8 хвилин тому", source_id=2)
    evs = events(client)
    assert len(evs) == 1, "同地同类同窗口应合并为一个事件"
    assert evs[0]["report_count"] == 2
    assert evs[0]["verification"] == "auto_confirmed"


# 5. 单源多条报道(≥3)也能自动确认
def test_multi_report_single_source(client):
    for i in range(3):
        ingest(client, f"m{i}", "Обстріл Херсона, 10 хвилин тому")
    evs = events(client)
    assert len(evs) == 1
    assert evs[0]["verification"] == "auto_confirmed"


# 6. 人工驳回不被自动确认覆盖
def test_manual_reject_not_overridden(client):
    add_source(client, 2, "second")
    ingest(client, "a1", "Обстріл Херсона, 10 хвилин тому", source_id=1)
    ev = events(client)[0]
    r = client.post(
        f"/api/v1/events/{ev['id']}/correct",
        json={"verification": "rejected", "reason": "误报", "operator": "tester"},
    )
    assert r.status_code == 200, r.text
    ingest(client, "b1", "Херсон під обстрілом, 8 хвилин тому", source_id=2)
    after = client.get(f"/api/v1/events/{ev['id']}").json()
    assert after["verification"] == "rejected"


# 7. 写接口仅限本机(测试客户端视为本机,伪造公网来源应被拒)
def test_write_endpoints_are_local_only(client):
    class FakeClient:
        host = "203.0.113.9"

    from app import state
    from app.api import _require_local
    from fastapi import HTTPException

    import pytest

    with pytest.raises(HTTPException) as exc:
        _require_local(type("R", (), {"client": FakeClient()})())
    assert exc.value.status_code == 403
    assert state.gazetteer is not None


# 8. 待补地名只收"像地名"的专名,不把普通大写词堆进维护列表
def test_suspect_tokens_filter_noise(client):
    from app import state
    from app.service import _suspect_tokens

    gaz = state.gazetteer
    noise = [
        "Знову тривога",
        "Загалом підготовлено: близько 50 крилатих ракет",
        "Реактивний на Бориспіль.",
        "**❗️ УЛЬТИМАТУМ ТРАМПА: ЗУПИНИТИ НАШІ «ФЛАМІНГО»!**",
        "🚀 Дніпропетровщина:\nБандеролі→Павлоград/р-н (2х)",
    ]
    for text in noise:
        assert _suspect_tokens(gaz, text) == [], text

    # 已收录的成对地名不被拆出半截词
    assert _suspect_tokens(gaz, "💥 Біла Церква - вибухи") == []
    # 完整写法与形容词形式都已能挂到州 → 不进待补列表
    assert _suspect_tokens(gaz, "⚠️Запорізька область:\n→Запоріжжя") == []
    assert _suspect_tokens(gaz, "⚠️Запорізька обл:\n→Запоріжжя") == []

    # 真正未收录的专名:行政词搭配、介词结构与"事件动词 + 属格"都要抓到
    assert _suspect_tokens(gaz, "💥 Тестівка р-н - вибухи") == ["Тестівка р-н"]
    assert _suspect_tokens(gaz, "Приліт по Новотестівці, 5 хвилин тому") == ["Новотестівці"]
    assert _suspect_tokens(gaz, "Обстріл Новотестівки, 5 хвилин тому") == ["Новотестівки"]


# 9. 检索按相关度排序:搜州名时州本身排在村镇之前
def test_place_search_ranking(client_uk):
    r = client_uk.get("/api/v1/places", params={"q": "Черкаська область", "limit": 5})
    assert r.status_code == 200
    items = r.json()
    assert items, "应能搜到该州"
    assert items[0]["name"] == "Черкаська область", items[0]
    assert items[0]["precision"] == "region"

    r = client_uk.get("/api/v1/places", params={"q": "Херсон", "limit": 5})
    names = [p["name"] for p in r.json()]
    assert "Херсон" in names[:2], names


# 10. 事件查询:核验状态过滤 + 内联证据(复核流程一次取全)
def test_events_filter_and_inline_evidence(client):
    add_source(client, 2, "second")
    ingest(client, "e1", "Обстріл Херсона, 10 хвилин тому", source_id=1)
    ingest(client, "e2", "Вибухи у Сумах, 20 хвилин тому", source_id=2)

    r = client.get(
        "/api/v1/events",
        params={"window_minutes": 100000, "verification": "unverified", "include_evidence": True, "limit": 5},
    )
    assert r.status_code == 200, r.text
    items = r.json()
    assert len(items) == 2
    for e in items:
        assert e["verification"] == "unverified"
        assert e["evidence"] and e["evidence"][0]["text"]

    r = client.get("/api/v1/events", params={"window_minutes": 100000, "verification": "any"})
    assert len(r.json()) == 2
    r = client.get("/api/v1/events", params={"window_minutes": 100000, "verification": "confirmed"})
    assert r.json() == []
    r = client.get(
        "/api/v1/events", params={"window_minutes": 100000, "verification": "unverified,auto_confirmed"}
    )
    assert len(r.json()) == 2
    r = client.get("/api/v1/events", params={"window_minutes": 100000, "limit": 1})
    assert len(r.json()) == 1


# 11. 同一条消息被编辑(revision 递增)不能算成多条报道
def test_edited_message_is_one_report(client):
    for rev in (1, 2, 3):
        ingest(client, "edit-1", f"Обстріл Херсона, 10 хвилин тому rev{rev}", revision=rev)
    evs = events(client)
    assert len(evs) == 1
    assert evs[0]["report_count"] == 1, "同频道同一条消息的修订只算一条报道"
    assert evs[0]["verification"] == "unverified", "单条消息的多次编辑不能触发多源印证"


# 12. 交叉印证按"消息族"计数:同一来源多次编辑 ≠ 多源
def test_revision_does_not_fake_corroboration(client):
    add_source(client, 2, "second")
    for rev in (1, 2, 3):
        ingest(client, "edit-2", f"Херсон під обстрілом rev{rev}", revision=rev, source_id=1)
    assert events(client)[0]["verification"] == "unverified"
    ingest(client, "edit-2b", "Херсон під обстрілом", source_id=2)
    evs = events(client)
    assert len(evs) == 1
    assert evs[0]["report_count"] == 2
    assert evs[0]["verification"] == "auto_confirmed"
