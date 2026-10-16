"""真实地名库(种子 + GeoNames/KATOTTH)与乌克兰语词形还原测试。"""

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.gazetteer import Gazetteer, normalize


@pytest.fixture(scope="module")
def gaz():
    g = Gazetteer()
    g.load_json(str(BACKEND / "data" / "gazetteer_seed.json"))
    g.load_json(str(BACKEND / "data" / "gazetteer_uk.json"))
    return g


def test_size(gaz):
    assert len(gaz) >= 35000


def test_exact_curated_city(gaz):
    r = gaz.resolve("Київ")
    assert r and r["place_id"] == 15  # 精选种子


def test_genitive_curated_via_lemma(gaz):
    r = gaz.resolve("Києва")  # 种子无 "києва" 别名 → 词形还原命中
    assert r and r["place_id"] == 15


def test_instrumental_curated_via_lemma(gaz):
    r = gaz.resolve("Херсоном")  # 工具格,种子没有 → lemma → херсон
    assert r and r["place_id"] == 1


def test_katotth_village_lemma_with_context(gaz):
    # 无上下文:同名村庄分属两州 → 保守歧义
    r = gaz.resolve("Новорозівка")
    assert r and r["place_id"] is None and len(r["ambiguous_ids"]) == 2
    # 属格变体 + 州上下文 → 词形还原后消歧到 Одеська 的村庄
    r2 = gaz.resolve("Новорозівки", context="прильоти в Одеській області")
    assert r2 and r2["place_id"] == 617711
    assert gaz.get(617711)["admin1"] == "Одеська область"


def test_no_seed_collision(gaz):
    ids = set(gaz._alias_to_ids.get("херсон", []))
    assert ids == {1}


def test_context_disambiguation_inflected(gaz):
    # "Одеській області"(变格上下文)消歧到 Одеська;换 Запорізькій 则消歧到另一村
    r = gaz.resolve("Новорозівка", context="обстріл в Одеській області")
    assert r and r["place_id"] == 617711
    r2 = gaz.resolve("Новорозівка", context="обстріл в Запорізькій області")
    assert r2 and r2["place_id"] == 699299


def test_mass_entry_has_cyrillic_alias(gaz):
    # 抽查:某个 KATOTTH 关联条目应含西里尔规范名
    found = False
    for pid, p in list(gaz._places.items())[:5000]:
        for alias, ids in gaz._alias_to_ids.items():
            if pid in ids and any(ord(c) > 0x400 for c in alias):
                found = True
                break
        if found:
            break
    assert found


# 州名的形容词变格形式("Одеській"、"Черкаської")应挂到对应州,而不是当成未收录地名
def test_region_adjective_forms_resolve(client_uk):
    from app import state

    cases = {
        "Одеській": "Одеська область",
        "Черкаської": "Черкаська область",
        "Запорізька": "Запорізька область",
        "Донецькій": "Донецька область",
        "Сумської": "Сумська область",
        "Херсонській": "Херсонська область",
    }
    for raw, expected in cases.items():
        hit = state.gazetteer.resolve(raw, context=f"Загроза балістики {raw}")
        assert hit and hit.get("place_id"), f"{raw} 未解析"
        assert state.gazetteer.get(hit["place_id"])["canonical_name"] == expected, raw

    # 端到端:形容词形式的州名照样能落到空袭警报事件上
    r = client_uk.post(
        "/api/v1/ingest",
        json={
            "source_id": 1,
            "external_id": "adj-odesa",
            "revision": 1,
            "body_text": "Повітряна тривога в Одеській області",
            "published_at": "2026-09-07T12:00:00Z",
        },
    )
    assert r.status_code == 200, r.text
    evs = client_uk.get("/api/v1/events", params={"window_minutes": 100000, "types": "air_alert"}).json()
    assert any(e["place"]["name_original"] == "Одеська область" for e in evs), evs


# 非乌克兰州名的形容词不应被误挂("Курська"/"Брянська" 是俄罗斯州)
def test_foreign_adjective_not_matched(client_uk):
    from app import state

    assert state.gazetteer.resolve("Курська") is None
    assert state.gazetteer.resolve("Брянська") is None
