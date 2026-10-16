"""真实地名库下的端到端提取回归(复数词形、同形常见词、与格地名、自动地理编码、基辅区域)。"""

import pytest

from app import models, state


class FakeGeocoder:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def search(self, query, context_admin=None):
        self.calls.append((query, context_admin))
        return self.results.get(query)


def ingest(client, external_id, body, published_at="2026-09-07T12:00:00Z", **extra):
    payload = {
        "source_id": 1,
        "external_id": external_id,
        "revision": 1,
        "body_text": body,
        "published_at": published_at,
    }
    payload.update(extra)
    r = client.post("/api/v1/ingest", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def events(client):
    r = client.get("/api/v1/events", params={"window_minutes": 100000})
    assert r.status_code == 200, r.text
    return r.json()


def pending(client):
    return client.get("/api/v1/pending").json()


def test_plural_prylioty_and_stopword_selo(client_uk):
    """'прильоти'(复数)+ 'село'(同形村庄名)→ 事件正确,且无假阳性待处理。"""
    ingest(
        client_uk,
        "r1",
        "Прильоти в Одеській області, село Новорозівка",
        published_at="2026-09-07T12:00:00Z",
    )
    evs = events(client_uk)
    assert len(evs) == 1
    e = evs[0]
    assert e["event_type"] == "shelling"
    assert e["place"]["name"] == "Новорозівка"
    assert e["place"]["admin1"] == "Одеська область"
    raw_names = {p["place_raw"] for p in pending(client_uk)}
    assert "село" not in raw_names and "Прильоти" not in raw_names


def test_dative_kharkovu_resolves_via_lemma(client_uk):
    """'Харкову'(与格)→ 词形还原 → Харків(种子)。"""
    ingest(client_uk, "r2", "Приліт дронів по Харкову, 2 хвилини тому")
    evs = events(client_uk)
    assert len(evs) == 1
    assert evs[0]["event_type"] == "drone"
    assert evs[0]["place"]["name"] == "Харків"


def test_allcaps_spam_no_type_produces_nothing(client_uk):
    """全大写宣传文(无类型词、无已解析地名)→ 不产生事件也不刷屏待处理。"""
    ingest(
        client_uk,
        "r3",
        "‼️ВАС ІДЕНТИФІКУВАЛИ ЯК АКТИВНОГО ГРОМАДЯНИНА‼️ ТЕРМІНОВО ПРОЙДІТЬ РЕЄСТРАЦІЮ",
    )
    assert events(client_uk) == []
    assert pending(client_uk) == []


def test_banderol_slang_missile_type(client_uk):
    """'Бандероль'(群内导弹黑话)→ missile 类型;'Валки' 不在库 → 待定位;口语州名不入队。"""
    ingest(client_uk, "r4", "Бандероль🚀→Валки (Харківщина)")
    assert events(client_uk) == []
    p = pending(client_uk)
    assert len(p) == 1
    assert p[0]["event_type"] == "missile"
    assert p[0]["place_raw"] == "Валки"


def test_pair_place_not_split(client_uk):
    """'Кривий Ріг' 词组命中后,不再拆出同名村庄 'Кривий'/'Ріг'。"""
    ingest(client_uk, "r5", "⚠️БПЛА далі→Зеленодольськ/Кривий Ріг")
    evs = events(client_uk)
    kryvyi = [e for e in evs if e["place"]["name"] == "Кривий Ріг"]
    assert len(kryvyi) == 1 and kryvyi[0]["event_type"] == "drone"
    raw_names = {p["place_raw"] for p in pending(client_uk)}
    assert "Ріг" not in raw_names and "Кривий" not in raw_names


def test_country_word_not_a_place(client_uk):
    """'по Україні' 不得命中名为 Україна 的村庄。"""
    ingest(client_uk, "r6", "Масована атака по Україні, працює ППО")
    assert events(client_uk) == []
    assert pending(client_uk) == []


def test_auto_geocode_resolves_unknown_place(client_uk):
    """未收录地名 → Nominatim 自动解析 → 事件上地图,地名持久化(无需人工)。"""
    state.geocoder = FakeGeocoder(
        {
            "Загублене": {
                "name": "Загублене",
                "lat": 46.6,
                "lng": 32.9,
                "type": "village",
                "importance": 0.5,
                "admin1": "Херсонська область",
            }
        }
    )
    try:
        ingest(client_uk, "g1", "Приліт у селі Загублене")
        evs = events(client_uk)
        assert len(evs) == 1
        assert evs[0]["place"]["name"] == "Загублене"
        assert pending(client_uk) == []
        with client_uk.Session() as db:
            row = db.query(models.GazetteerPlace).filter_by(source="nominatim").first()
            assert row is not None
            assert row.id >= 1_000_000_000
    finally:
        state.geocoder = None


def test_auto_geocode_failure_stays_pending(client_uk):
    """地理编码无结果(低置信/网络失败)→ 罕见兜底:仍待人工。"""
    state.geocoder = FakeGeocoder({})
    try:
        ingest(client_uk, "g2", "Приліт у селі Невідоме")
        assert events(client_uk) == []
        assert len(pending(client_uk)) == 1
    finally:
        state.geocoder = None


def test_kyiv_districts_resolve(client_uk):
    """基辅行政区/社区词形变化 → 事件。"""
    ingest(client_uk, "g3", "Вибухи на Троєщині та Оболоні")
    evs = events(client_uk)
    names = {e["place"]["name"] for e in evs}
    assert {"Троєщина", "Оболонь"} <= names
    assert all(e["place"]["admin1"] == "Київ" for e in evs if e["place"]["name"] in names)


def test_warning_direction_format_auto_typed(client_uk):
    """频道方向告警格式('⚠️Полтавщина: →Нові Санжари',无动词)→ 自动判定 drone。"""
    state.geocoder = FakeGeocoder({})
    try:
        ingest(client_uk, "g4", "⚠️Полтавщина:\n→Неховороща/Нові Санжари.")
        evs = events(client_uk)
        sanzhary = [e for e in evs if e["place"]["name"] == "Нові Санжари"]
        assert len(sanzhary) == 1
        assert sanzhary[0]["event_type"] == "drone"
    finally:
        state.geocoder = None


def test_air_alert_type(client_uk):
    ingest(client_uk, "g5", "Повітряна тривога в Києві")
    evs = events(client_uk)
    assert len(evs) == 1
    assert evs[0]["event_type"] == "air_alert"
    assert evs[0]["place"]["name"] == "Київ"


def test_ambiguous_geocode_matches_candidate(client_uk):
    """同名歧义 + 地理编码命中候选之一 → 自动消歧,不新增地点。"""
    state.geocoder = FakeGeocoder(
        {"Новорозівки": {"name": "Новорозівка", "lat": 46.9, "lng": 30.1, "type": "village", "importance": 0.5}}
    )
    try:
        ingest(client_uk, "g6", "Обстріл Новорозівки")
        evs = events(client_uk)
        assert len(evs) == 1
        assert evs[0]["place"]["name"] == "Новорозівка"
        assert pending(client_uk) == []
    finally:
        state.geocoder = None


def test_mixed_alphabet_bpla(client_uk):
    """拉丁/西里尔混写 'БПЛA'(末尾拉丁 A)→ 折叠后识别为无人机。"""
    ingest(client_uk, "g7", "БПЛA на Дніпро")
    evs = events(client_uk)
    assert len(evs) == 1
    assert evs[0]["event_type"] == "drone"
    assert evs[0]["place"]["name"] == "Дніпро"
    assert all("БПЛ" not in p["place_raw"] for p in pending(client_uk))


def test_colloquial_oblast_not_a_place(client_uk):
    """'Дніпропетровщина' 等口语州名不入图、不入队。"""
    state.geocoder = FakeGeocoder({})
    try:
        ingest(client_uk, "g8", "⚠️Дніпропетровщина:\n→Павлоград")
        evs = events(client_uk)
        assert len(evs) == 1 and evs[0]["place"]["name"] == "Павлоград"
        raw_names = {p["place_raw"] for p in pending(client_uk)}
        assert "Дніпропетровщина" not in raw_names
    finally:
        state.geocoder = None


def test_threat_context_default_type(client_uk):
    """无类型词但有威胁语境('Кружляють біля Києва')→ 自动 drone。"""
    ingest(client_uk, "g9", "Кружляють біля Києва, ймовірно винюхують цілі")
    evs = events(client_uk)
    assert len(evs) == 1
    assert evs[0]["event_type"] == "drone"
    assert evs[0]["place"]["name"] == "Київ"


def test_emoji_explosion_and_huchno(client_uk):
    """💥 表情与 'гучно' 暗语 → explosion。"""
    ingest(client_uk, "g10", "💥 Харків")
    ingest(client_uk, "g11", "Падає у Вишневе, буде гучно")
    evs = events(client_uk)
    kh = [e for e in evs if e["place"]["name"] == "Харків"]
    assert len(kh) == 1 and kh[0]["event_type"] == "explosion"


def test_source_admin_bias_disambiguates(client_uk):
    """来源级州先验:Віраж Києва → 'Вишневе' 优先 Київська область。"""
    with client_uk.Session() as db:
        src = db.get(models.Source, 1)
        src.config_json = '{"admin1_bias": "Київська область"}'
        db.commit()
    ingest(client_uk, "g12", "Падає у Вишневе, буде гучно")
    evs = events(client_uk)
    vs = [e for e in evs if e["place"] and e["place"]["name"] == "Вишневе"]
    assert len(vs) == 1
    assert vs[0]["place"]["admin1"] == "Київська область"
    assert vs[0]["event_type"] == "explosion"


def test_alert_lifecycle_end_on_all_clear(client_uk):
    """警报生命周期:тривога 建立事件,відбій 结束事件(结束后不上图)。"""
    ingest(client_uk, "a1", "Київ - тривога")
    evs = events(client_uk)
    assert len(evs) == 1 and evs[0]["event_type"] == "air_alert"
    eid = evs[0]["id"]
    r = ingest(client_uk, "a2", "Київ - відбій")
    assert r["status"] == "all_clear"
    assert r["ended_events"] == [eid]
    assert events(client_uk) == []
    with client_uk.Session() as db:
        e = db.get(models.Event, eid)
        assert e.ended_at is not None


def test_alert_end_by_region_bias_without_place(client_uk):
    """无地名的解除('Чисто в області. Відбій')凭来源州先验结束警报。"""
    with client_uk.Session() as db:
        src = db.get(models.Source, 1)
        src.config_json = '{"admin1_bias": "Київська область"}'
        db.commit()
    ingest(client_uk, "b1", "Київ - тривога")
    evs = events(client_uk)
    assert len(evs) == 1
    r = ingest(client_uk, "b2", "Чисто в області. Відбій")
    assert r["status"] == "all_clear"
    assert len(r["ended_events"]) == 1
    assert events(client_uk) == []


def test_region_level_alert(client_uk):
    """州级警报:'Київська область - тривога' → 区域实体事件。"""
    ingest(client_uk, "a3", "Київська область - повітряна тривога")
    evs = events(client_uk)
    assert len(evs) == 1
    assert evs[0]["event_type"] == "air_alert"
    assert evs[0]["place"]["name"] == "Київська область"
    assert evs[0]["place"]["precision"] == "region"


def test_reaktyvni_missile_context(client_uk):
    """'Реактивні на ...'(战机)→ missile 而非 drone(同名村由来源州先验消歧)。"""
    with client_uk.Session() as db:
        src = db.get(models.Source, 1)
        src.config_json = '{"admin1_bias": "Київська область"}'
        db.commit()
    ingest(client_uk, "a4", "Реактивні на Троєщину")
    evs = events(client_uk)
    assert len(evs) == 1
    assert evs[0]["event_type"] == "missile"
    assert evs[0]["place"]["name"] == "Троєщина"
