"""波次级交叉印证:同一次攻击被两个来源报成邻近的不同地点时,双方都算被印证。

背景(2026-09-11 数据):只有两个真实频道时,1286 条未核实事件的独立来源数全部
等于 1 —— 因为合并要求"同一个 place_id",而两个频道往往报同一波攻击的不同城镇
(实测 86 组:不同来源 + 同类型 + ≤60min + ≤60km)。本规则只影响核验状态,
不合并点位(两个城镇仍是地图上两个点)。
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


# 1. 邻近(≈9km)+ 同类型 + 20 分钟内,来自两个来源 → 双方都被印证
def test_wave_corroboration_between_two_sources(client):
    add_source(client, 2, "second")
    ingest(client, "w1", "Шахеди на Боярку", published_at="2026-09-07T12:00:00Z", source_id=1)
    ingest(client, "w2", "Шахеди на Вишневе", published_at="2026-09-07T12:20:00Z", source_id=2)
    evs = events(client)
    assert len(evs) == 2, "不同地点仍是地图上两个点,不合并"
    assert {e["verification"] for e in evs} == {"auto_confirmed"}


# 2. 距离超出阈值 → 各自保持未核实
def test_wave_corroboration_respects_radius(client):
    add_source(client, 2, "second")
    ingest(client, "far1", "Шахеди на Київ", published_at="2026-09-07T12:00:00Z", source_id=1)
    ingest(client, "far2", "Шахеди на Херсон", published_at="2026-09-07T12:20:00Z", source_id=2)
    evs = events(client)
    assert len(evs) == 2
    assert {e["verification"] for e in evs} == {"unverified"}


# 3. 时间超出窗口 → 各自保持未核实
def test_wave_corroboration_respects_time_window(client):
    add_source(client, 2, "second")
    ingest(client, "t1", "Шахеди на Боярку", published_at="2026-09-07T12:00:00Z", source_id=1)
    ingest(client, "t2", "Шахеди на Вишневе", published_at="2026-09-07T18:00:00Z", source_id=2)
    evs = events(client)
    assert {e["verification"] for e in evs} == {"unverified"}


# 4. 事件类型不同 → 不互相印证
def test_wave_corroboration_requires_same_type(client):
    add_source(client, 2, "second")
    ingest(client, "k1", "Шахеди на Боярку", published_at="2026-09-07T12:00:00Z", source_id=1)
    ingest(client, "k2", "Обстріл Вишневого", published_at="2026-09-07T12:20:00Z", source_id=2)
    evs = events(client)
    assert {e["verification"] for e in evs} == {"unverified"}


# 5. 同一来源报的两个邻近城镇 → 不是独立印证
def test_wave_corroboration_requires_other_source(client):
    ingest(client, "s1", "Шахеди на Боярку", published_at="2026-09-07T12:00:00Z", source_id=1)
    ingest(client, "s2", "Шахеди на Вишневе", published_at="2026-09-07T12:20:00Z", source_id=1)
    evs = events(client)
    assert len(evs) == 2
    assert {e["verification"] for e in evs} == {"unverified"}


# 6. 波次印证会写入理由,便于详情面板回看与复盘
def test_wave_corroboration_records_reason(client):
    add_source(client, 2, "second")
    ingest(client, "r1", "Шахеди на Боярку", published_at="2026-09-07T12:00:00Z", source_id=1)
    ingest(client, "r2", "Шахеди на Вишневе", published_at="2026-09-07T12:20:00Z", source_id=2)
    evs = events(client)
    detail = client.get(f"/api/v1/events/{evs[0]['id']}").json()
    actions = [r["action"] for r in detail["reason"]]
    assert "auto_confirmed" in actions
    wave = [r for r in detail["reason"] if r["action"] == "auto_confirmed"][-1]
    assert wave.get("wave") is True
    assert "км" in (wave.get("reason") or "")
