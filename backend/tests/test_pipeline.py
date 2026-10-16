"""第 1 步端到端链路测试:消息 → 候选 → 校验 → 合并 → 事件 → 查询。

覆盖:正常报告、幂等重放、否定、转发合并、多事件、未知地点、
同名歧义与上下文消歧、时钟/昨夜时间锚定、编辑链、撤回、
人工修正不被重提取覆盖、事件拆分。
"""

from app import models

BASE = "2026-09-07T12:00:00Z"
BIG_WINDOW = {"window_minutes": 100000}


def ingest(client, external_id, body, published_at=BASE, revision=1, **extra):
    payload = {
        "source_id": 1,
        "external_id": external_id,
        "revision": revision,
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


def pending(client):
    r = client.get("/api/v1/pending")
    assert r.status_code == 200, r.text
    return r.json()


# 1. 正常报告 + 相对时间锚定
def test_normal_message_relative_time(client):
    r = ingest(client, "m1", "Обстріл Херсона, 10 хвилин тому")
    assert all(isinstance(cid, int) for cid in r["candidates"])
    evs = events(client)
    assert len(evs) == 1
    e = evs[0]
    assert e["event_type"] == "shelling"
    assert e["place"]["name"] == "Херсон"
    assert e["occurred_at"] == "2026-09-07T11:50:00Z"
    assert e["report_count"] == 1
    assert e["verification"] == "unverified"


# 2. 幂等重放
def test_idempotent_replay(client):
    a = ingest(client, "m1", "Обстріл Херсона, 10 хвилин тому")
    b = ingest(client, "m1", "Обстріл Херсона, 10 хвилин тому")
    assert b["status"] == "duplicate"
    assert b["raw_id"] == a["raw_id"]
    evs = events(client)
    assert len(evs) == 1 and evs[0]["report_count"] == 1
    with client.Session() as db:
        assert db.query(models.RawMessage).count() == 1


# 3. 否定消息
def test_negation_ignored(client):
    ingest(client, "m2", "У Херсоні тихо, обстрілів не було")
    assert events(client) == []
    assert pending(client) == []
    with client.Session() as db:
        cands = db.query(models.Candidate).all()
        assert cands and all(c.status == "ignored" for c in cands)
        assert "negated" in cands[0].payload_json


# 4. 转发副本合并(同地同类时间窗)
def test_forward_merge(client):
    ingest(client, "m1", "Обстріл Херсона, 10 хвилин тому")
    ingest(
        client,
        "m3",
        "Обстріл Херсона, 12 хвилин тому",
        published_at="2026-09-07T12:02:00Z",
        forward_json='{"from_channel":"ch-B"}',
    )
    evs = events(client)
    assert len(evs) == 1
    assert evs[0]["report_count"] == 2


# 5. 一条消息多个事件
def test_multi_event_one_message(client):
    ingest(client, "m4", "Вибухи в Одесі та Миколаєві", published_at="2026-09-07T12:05:00Z")
    evs = events(client)
    assert len(evs) == 2
    assert all(e["event_type"] == "explosion" for e in evs)
    names = {e["place"]["name"] for e in evs}
    assert names == {"Одеса", "Миколаїв"}


# 6. 未知地点 → 待定位,不上图
def test_unknown_place_pending(client):
    ingest(client, "m5", "Приліт у селі Малі Копані", published_at="2026-09-07T12:06:00Z")
    assert events(client) == []
    p = pending(client)
    assert len(p) == 1
    assert p[0]["place_raw"] == "Малі Копані"
    assert p[0]["reason"] == "unresolved_place"


# 7. 同名地点无上下文 → 歧义待审
def test_ambiguous_place_pending(client):
    ingest(client, "m6", "Обстріл Іванівки", published_at="2026-09-07T12:07:00Z")
    assert events(client) == []
    p = pending(client)
    assert len(p) == 1
    assert p[0]["reason"] == "ambiguous_place"


# 8. 同名地点有上下文 → 消歧成功
def test_context_disambiguation(client):
    ingest(client, "m7", "Обстріл Іванівки, Херсонська область", published_at="2026-09-07T12:08:00Z")
    evs = events(client)
    assert len(evs) == 1
    assert evs[0]["place"]["admin1"] == "Херсонська область"
    assert pending(client) == []


# 9. 时钟时间锚定发布日期
def test_clock_time(client):
    ingest(client, "m8", "Обстріл Бахмута о 14:32", published_at="2026-09-07T15:00:00Z")
    evs = events(client)
    assert len(evs) == 1
    assert evs[0]["occurred_at"] == "2026-09-07T14:32:00Z"


# 10. 昨夜时间语义
def test_yesterday_night(client):
    ingest(client, "m9", "Вчора вночі вибухи в Запоріжжі", published_at="2026-09-07T09:00:00Z")
    evs = events(client)
    assert len(evs) == 1
    assert evs[0]["occurred_at"] == "2026-09-06T23:30:00Z"


# 11. 编辑链与撤回
def test_edit_and_retract(client):
    ingest(client, "m10", "Обстріл Харкова", published_at="2026-09-07T12:10:00Z")
    ingest(
        client,
        "m10",
        "Обстріл Харкова, 20 минут назад",
        published_at="2026-09-07T12:11:00Z",
        revision=2,
        edit_json='{"edited":true}',
    )
    evs = events(client)
    # 编辑是同一条消息的修订,不是第二条报道:报道数不变,证据指向最新修订
    assert len(evs) == 1 and evs[0]["report_count"] == 1
    detail = client.get(f"/api/v1/events/{evs[0]['id']}").json()
    assert len(detail["evidence"]) == 1
    assert "20 минут назад" in detail["evidence"][0]["text"]
    r = ingest(
        client,
        "m10",
        "Помилкове повідомлення: обстрілу Харкова не було",
        published_at="2026-09-07T12:12:00Z",
        revision=3,
        edit_json='{"edited":true}',
    )
    assert r["status"] == "retracted"
    assert events(client) == []
    with client.Session() as db:
        e = db.query(models.Event).one()
        assert e.verification == "rejected"
        assert e.report_count == 0
        assert db.query(models.Evidence).count() == 0


# 12. 人工确认后重提取不覆盖
def test_manual_correction_survives_reextract(client):
    ingest(client, "m11", "Вибух у Одесі", published_at="2026-09-07T12:20:00Z")
    evs = events(client)
    assert len(evs) == 1 and evs[0]["verification"] == "unverified"
    eid = evs[0]["id"]
    r = client.post(
        f"/api/v1/events/{eid}/correct",
        json={
            "verification": "confirmed",
            "occurred_at": "2026-09-07T12:00:00Z",
            "reason": "оператор підтвердив",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["verification"] == "confirmed"
    ingest(client, "m11", "Вибух у Одесі", published_at="2026-09-07T12:22:00Z", revision=2)
    evs = events(client)
    assert len(evs) == 1
    e = evs[0]
    assert e["verification"] == "confirmed"
    assert e["occurred_at"] == "2026-09-07T12:00:00Z"
    assert e["report_count"] == 1
    with client.Session() as db:
        c = db.query(models.Candidate).filter_by(status="ignored").one()
        assert "event_manually_confirmed" in c.payload_json


# 13. 合并事件可拆分(保留各自时间与证据)
def test_split_event(client):
    ingest(client, "m20", "Обстріл Нікополя, 10 хвилин тому", published_at="2026-09-07T12:00:00Z")
    ingest(client, "m21", "Обстріл Нікополя, 30 хвилин тому", published_at="2026-09-07T12:40:00Z")
    evs = events(client)
    assert len(evs) == 1 and evs[0]["report_count"] == 2
    eid = evs[0]["id"]
    r = client.post(f"/api/v1/events/{eid}/split")
    assert r.status_code == 200, r.text
    ids = r.json()["split_into"]
    assert len(ids) == 2
    evs = events(client)
    assert len(evs) == 2
    assert all(e["report_count"] == 1 for e in evs)
    times = {e["occurred_at"] for e in evs}
    assert times == {"2026-09-07T11:50:00Z", "2026-09-07T12:10:00Z"}


# 14. 俄语别名
def test_russian_alias(client):
    ingest(client, "m30", "Обстрел Харькова", published_at="2026-09-07T12:15:00Z")
    evs = events(client)
    assert len(evs) == 1
    assert evs[0]["event_type"] == "shelling"
    assert evs[0]["place"]["name"] == "Харків"


# 15. 待定位候选经运营解析成事件
def test_resolve_pending_candidate(client):
    ingest(client, "m5", "Приліт у селі Малі Копані", published_at="2026-09-07T12:06:00Z")
    p = pending(client)
    assert len(p) == 1
    r = client.post(
        f"/api/v1/pending/{p[0]['candidate_id']}/resolve",
        json={
            "place_id": 6,
            "event_type": "shelling",
            "occurred_at": "2026-09-07T12:00:00Z",
        },
    )
    assert r.status_code == 200, r.text
    evs = events(client)
    assert len(evs) == 1
    assert evs[0]["place"]["name"] == "Очаків"
    assert evs[0]["event_type"] == "shelling"
    assert evs[0]["occurred_at"] == "2026-09-07T12:00:00Z"
    assert pending(client) == []
    with client.Session() as db:
        c = db.query(models.Candidate).one()
        assert c.status == "accepted"


# 16. 地点目录查询(名称与州级上下文都命中)
def test_places_search(client):
    r = client.get("/api/v1/places", params={"q": "херсон"})
    assert r.status_code == 200
    names = {p["name"] for p in r.json()}
    assert "Херсон" in names
    assert "Іванівка (Херсонська)" in names


# 17. 删除事件(edit_json.deleted)按撤回处理
def test_deleted_event_retracts(client):
    ingest(client, "m40", "Обстріл Очакова", published_at="2026-09-07T12:30:00Z")
    evs = events(client)
    assert len(evs) == 1 and evs[0]["report_count"] == 1
    r = ingest(
        client,
        "m40",
        "",
        published_at="2026-09-07T12:31:00Z",
        revision=2,
        edit_json='{"deleted": true}',
    )
    assert r["status"] == "retracted"
    assert events(client) == []
    with client.Session() as db:
        e = db.query(models.Event).one()
        assert e.verification == "rejected"


# 19. 来源列表端点
def test_sources_endpoint(client):
    r = client.get("/api/v1/sources")
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    assert items[0]["adapter"] == "fixture"
    assert items[0]["enabled"] is True


# 24. 中文显示(识别仍为乌克兰语原文)
def test_chinese_place_display(client_zh):
    ingest(client_zh, "zh1", "Обстріл Херсона, 10 хвилин тому")
    evs = events(client_zh)
    assert len(evs) == 1
    place = evs[0]["place"]
    assert place["name"] == "赫尔松"
    assert place["name_original"] == "Херсон"
    assert place["admin1_name"] == "赫尔松州"


# 25. Wikidata 中文标签覆盖大众地名库
def test_wikidata_chinese_for_mass_places(client_uk_zh):
    ingest(client_uk_zh, "zh2", "Вибухи в Татарбунарах")
    evs = events(client_uk_zh)
    assert len(evs) == 1
    assert evs[0]["place"]["name"] == "鞑靼布纳雷"
    assert evs[0]["place"]["name_original"] == "Татарбунари"
    assert evs[0]["place"]["admin1_name"] == "敖德萨州"


# 21. 驳回待处理候选(噪音/非事件)
def test_reject_pending_candidate(client):
    ingest(client, "r3", "Приліт у селі Малі Копані")
    p = pending(client)
    assert len(p) == 1
    r = client.post(f"/api/v1/pending/{p[0]['candidate_id']}/reject", json={"reason": "noise"})
    assert r.status_code == 200, r.text
    assert pending(client) == []
    with client.Session() as db:
        c = db.query(models.Candidate).one()
        assert c.status == "ignored"
        assert "noise" in c.payload_json


# 22. 原始消息链接回传(证据 → t.me 跳转)
def test_original_url_roundtrip(client):
    ingest(client, "m60", "Обстріл Херсона", original_url="https://t.me/testch/123")
    evs = events(client)
    d = client.get(f"/api/v1/events/{evs[0]['id']}").json()
    assert d["evidence"][0]["original_url"] == "https://t.me/testch/123"


# 23. 重复消息回填原始链接
def test_duplicate_backfills_original_url(client):
    ingest(client, "m61", "Обстріл Херсона")
    r = ingest(client, "m61", "Обстріл Херсона", original_url="https://t.me/testch/999")
    assert r["status"] == "duplicate" and r.get("url_backfilled") is True
    with client.Session() as db:
        raw = db.query(models.RawMessage).filter_by(external_id="m61").one()
        assert raw.original_url == "https://t.me/testch/999"


# 20. 全量重放幂等:候选重建、事件与证据不重复计数
def test_reprocess_is_idempotent(client):
    ingest(client, "r1", "Обстріл Херсона, 10 хвилин тому")
    ingest(client, "r2", "Приліт у селі Малі Копані")
    evs_before = events(client)
    pen_before = pending(client)
    assert len(evs_before) == 1 and len(pen_before) == 1

    r = client.post("/api/v1/admin/reprocess")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted_candidates"] >= 2
    assert body["processed_messages"] >= 2

    evs_after = events(client)
    pen_after = pending(client)
    assert len(evs_after) == len(evs_before) == 1
    assert evs_after[0]["report_count"] == evs_before[0]["report_count"] == 1
    assert len(pen_after) == len(pen_before) == 1
    with client.Session() as db:
        assert db.query(models.Evidence).count() == 1  # 无重复证据
        assert db.query(models.Candidate).count() == 2  # 1 accepted + 1 needs_review


# 18. 回归:词组不得模糊匹配单地名("у Краматорську" ≠ Краматорськ)
def test_multiword_phrase_no_fuzzy(client):
    ingest(client, "m50", "Вибух у Краматорську", published_at="2026-09-07T12:45:00Z")
    evs = events(client)
    assert len(evs) == 1
    assert evs[0]["place"]["name"] == "Краматорськ"
    assert evs[0]["report_count"] == 1
    with client.Session() as db:
        assert db.query(models.Evidence).count() == 1
