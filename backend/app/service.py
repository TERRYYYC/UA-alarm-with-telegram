from __future__ import annotations

import json
import queue as queue_mod
import re
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from . import config, models
from .extractor import ADMIN_WORD, analyze_message, is_event_keyword, is_noise_token
from .gazetteer import PLACE_TOKEN_RE, Gazetteer, normalize

_UPPER_START = re.compile(r"^[А-ЯІЇЄҐ]")

# 与真实地名同形的常见词,不得当作地点(如 GeoNames 里有叫 "Село" 的村庄)
# 注意:口语州名(Київщина 等)已是区域实体别名,不在此列
_PLACE_STOPWORDS = {
    "село", "села", "селі", "селом", "місто", "міста", "місті", "город", "города", "городе",
    "селище", "селища", "смт", "район", "района", "району", "районі", "область", "області",
    "областей", "напрямок", "напрямку", "направление", "направлении", "хутір", "хутор",
    # 国家名与超区域概念(非点状/州级事件地点;含俄语与拉丁转写变体)
    "україна", "україни", "україні", "україною", "украина", "украины", "ukrayina", "ukraina",
    "росія", "росії", "росією", "рф", "донбас", "донбасу", "поділля",
    # 高频话语词:常出现在句首且首字母大写,但绝不是地名(避免刷屏待补列表)
    "загалом", "знову", "попередньо", "поки", "якщо", "внаслідок", "також", "наразі",
    "столиця", "центр", "приват", "карта", "дорозвідка", "повітряний", "простір",
    "почуйте", "зігрій", "бійця", "приблизно", "орієнтовно", "увага", "важливо",
    # 频道模板文字(订阅按钮等)
    "підписатись", "підписатися", "підпишись", "подписаться", "підписка", "подписка",
    # 常见套话与截断形容词(非地名)
    "загроза", "загрози", "загроз", "наразі", "працює", "ппо", "околиці", "околиць",
    "нова", "нове", "новий", "стара", "старе", "старий",
}

# 归一化后的停用词集合(兼容拉丁混写 "Ukrayina" 等变体)
_PLACE_STOPWORDS_NORM = {normalize(w) for w in _PLACE_STOPWORDS}

# 地点介词:其后的大写专名大概率是地名("по Новомихайлівці"、"у Краматорську")
_PLACE_PREPOSITIONS = {
    "в", "у", "до", "з", "із", "зі", "від", "на", "по", "під", "над", "біля", "поблизу",
    "за", "між", "понад", "околицях", "околиці", "районі", "напрямку", "бік", "сторону",
}


def to_naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso_utc(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


class EventBus:
    """进程内发布/订阅:seq 单调,供 SSE 断线补读。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._seq = 0
        self._subs: list[queue_mod.Queue] = []

    def next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def subscribe(self) -> queue_mod.Queue:
        q: queue_mod.Queue = queue_mod.Queue(maxsize=2000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue_mod.Queue):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, seq: int, data: dict):
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait({"seq": seq, "data": data})
            except queue_mod.Full:
                pass


def serialize_event(gaz: Gazetteer, e: models.Event) -> dict:
    p = gaz.get(e.place_id) if e.place_id else None
    admin1 = p["admin1"] if p else None
    return {
        "id": e.id,
        "seq": e.seq,
        "event_type": e.event_type,
        "place": (
            {
                "id": p["id"] if p else None,
                "name": p.get("name_zh") or p["canonical_name"],
                "name_original": p["canonical_name"],
                "lat": p["lat"],
                "lng": p["lng"],
                "admin1": admin1,
                "admin1_name": gaz.zh_for(admin1) or admin1,
                "admin2": p["admin2"],
                "precision": p["precision"],
            }
            if p
            else None
        ),
        "geometry": json.loads(e.geometry_json or "{}"),
        "occurred_at": iso_utc(e.occurred_at),
        "ended_at": iso_utc(e.ended_at),
        "occurred_uncertainty_s": e.occurred_uncertainty_s,
        "verification": e.verification,
        "report_count": e.report_count,
        "version": e.version,
    }


def _touch(db: Session, bus: EventBus, e: models.Event, reason: dict | None = None):
    e.version += 1
    e.updated_at = utcnow_naive()
    e.seq = bus.next_seq()
    if reason:
        reasons = json.loads(e.reason_json or "[]")
        reasons.append(reason)
        e.reason_json = json.dumps(reasons, ensure_ascii=False)


def _find_places(gaz: Gazetteer, text: str, admin_bias: str | None = None) -> list[dict]:
    tokens = PLACE_TOKEN_RE.findall(text)
    found: list[dict] = []
    seen: set[str] = set()
    pairs = [f"{tokens[i]} {tokens[i + 1]}" for i in range(len(tokens) - 1)]
    pair_constituents: set[str] = set()
    resolved_pair_parts: set[str] = set()

    def add(raw: str, r: dict | None):
        key = raw.lower()
        if key in seen:
            return
        seen.add(key)
        found.append(
            {
                "raw": raw,
                "place_id": r.get("place_id") if r else None,
                "fuzzy": (r or {}).get("fuzzy", False),
                "ambiguous_ids": (r or {}).get("ambiguous_ids", []),
            }
        )

    # 1) 已解析的双 token 词组(先于单 token,避免 "Кривий Ріг" 被拆出同名村庄 "Кривий")
    for p in pairs:
        r = gaz.resolve(p, context=text, admin_bias=admin_bias)
        if r:
            add(p, r)
            resolved_pair_parts.update(x.lower() for x in p.split())

    # 2) 未解析的专名对(两部分均大写开头且非噪音)——多词地名如 "Малі Копані"
    resolved_tokens = {t.lower() for t in tokens if gaz.resolve(t, context=text, admin_bias=admin_bias)}
    for p in pairs:
        parts = p.split()
        if all(_UPPER_START.match(x) for x in parts) and not any(is_noise_token(x) for x in parts):
            if any(x.lower() in resolved_tokens for x in parts):
                continue  # 组成部分已单独解析(如 "Іванівки Херсонська"),词组本身不再入队
            add(p, None)
            pair_constituents.update(x.lower() for x in parts)

    # 3) 单个 token:已解析的直接收录;未解析的大写专名入待处理
    for i, t in enumerate(tokens):
        if len(t) < 3 or re.fullmatch(r"\d+", t):
            continue
        if t.lower() in _PLACE_STOPWORDS or normalize(t) in _PLACE_STOPWORDS_NORM:
            continue
        if t.lower() in resolved_pair_parts or t.lower() in pair_constituents:
            continue  # 是已处理词组的组成部分,不再单列(避免 "Кривий Ріг" 拆出 "Ріг")
        r = gaz.resolve(t, context=text, admin_bias=admin_bias)
        if r:
            add(t, r)
        elif _UPPER_START.match(t) and not is_noise_token(t):
            neighbors = [tokens[j] for j in (i - 1, i + 1) if 0 <= j < len(tokens)]
            if any(ADMIN_WORD.search(n) for n in neighbors):
                continue
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            if nxt and nxt[0].islower():
                continue  # "Масована атака" 式的大写形容词+小写名词,首词不是地名
            add(t, None)
    return found


def find_merge_target(db: Session, event_type: str, place_id: int, occurred_at: datetime | None) -> models.Event | None:
    q = db.query(models.Event).filter(
        models.Event.event_type == event_type,
        models.Event.place_id == place_id,
        models.Event.merged_into_id.is_(None),
        models.Event.verification != "rejected",
    )
    for e in q:
        if e.occurred_at is None or occurred_at is None:
            continue
        if abs((e.occurred_at - occurred_at).total_seconds()) <= config.MERGE_WINDOW_MINUTES * 60:
            return e
    return None


def _family_evidence(
    db: Session, event_id: int, raw: models.RawMessage
) -> models.Evidence | None:
    """同一"消息族"(来源 + external_id)在该事件下已有的证据行。"""
    return (
        db.query(models.Evidence)
        .join(models.RawMessage, models.RawMessage.id == models.Evidence.message_id)
        .filter(
            models.Evidence.event_id == event_id,
            models.Evidence.role == "report",
            models.RawMessage.source_id == raw.source_id,
            models.RawMessage.external_id == raw.external_id,
        )
        .first()
    )


def corroboration_stats(db: Session, event_id: int) -> tuple[int, int]:
    """返回 (报告数, 独立来源数)。

    按"消息族"(来源 + external_id)去重:同一条 Telegram 消息被编辑后会产生
    revision 2/3 的独立原始记录,它们仍是同一条报道,不能算成多条,也不能
    因此触发多源印证(否则单频道一条消息编辑两次就会被"自动确认")。
    """
    rows = (
        db.query(models.RawMessage.source_id, models.RawMessage.external_id)
        .join(models.Evidence, models.Evidence.message_id == models.RawMessage.id)
        .filter(models.Evidence.event_id == event_id)
        .distinct()
        .all()
    )
    sources = {r[0] for r in rows}
    return len(rows), len(sources)


def _event_sources(db: Session, event_id: int) -> set[int]:
    """事件背后的独立来源 id 集合(按消息族去重)。"""
    rows = (
        db.query(models.RawMessage.source_id)
        .join(models.Evidence, models.Evidence.message_id == models.RawMessage.id)
        .filter(models.Evidence.event_id == event_id)
        .distinct()
        .all()
    )
    return {r[0] for r in rows}


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    import math

    lat1, lng1 = a
    lat2, lng2 = b
    p = math.pi / 180
    h = 0.5 - math.cos((lat2 - lat1) * p) / 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * (
        1 - math.cos((lng2 - lng1) * p)
    ) / 2
    return 12742 * math.asin(math.sqrt(max(0.0, h)))


def wave_counterpart(
    db: Session, gaz: Gazetteer, e: models.Event
) -> tuple[models.Event, float] | None:
    """找到"同一波攻击"的证据:另一来源、同类型、邻近且时间接近的另一处事件。

    只用于核验(说明这波攻击确实有两个独立来源),不合并事件本身。
    """
    if not config.WAVE_CORROBORATION or e.place_id is None or e.occurred_at is None:
        return None
    here = gaz.get(e.place_id)
    if here is None:
        return None
    mine = _event_sources(db, e.id)
    window = timedelta(minutes=config.WAVE_WINDOW_MIN)
    candidates = (
        db.query(models.Event)
        .filter(
            models.Event.id != e.id,
            models.Event.event_type == e.event_type,
            models.Event.merged_into_id.is_(None),
            models.Event.verification != "rejected",
            models.Event.place_id.isnot(None),
            models.Event.occurred_at >= e.occurred_at - window,
            models.Event.occurred_at <= e.occurred_at + window,
        )
        .all()
    )
    best: tuple[models.Event, float] | None = None
    for other in candidates:
        if _event_sources(db, other.id) & mine:
            continue  # 同一来源不算独立印证
        p = gaz.get(other.place_id)
        if p is None:
            continue
        d = _haversine_km((here["lat"], here["lng"]), (p["lat"], p["lng"]))
        if d <= config.WAVE_RADIUS_KM and (best is None or d < best[1]):
            best = (other, d)
    return best


def promote_if_corroborated(db: Session, gaz: Gazetteer, bus: EventBus, e: models.Event) -> bool:
    """多源交叉印证 → 自动确认(仅推进 unverified;人工判定不覆盖)。"""
    if e.verification != "unverified":
        return False
    reports, sources = corroboration_stats(db, e.id)
    if sources >= config.AUTO_CONFIRM_MIN_SOURCES or reports >= config.AUTO_CONFIRM_MIN_REPORTS:
        e.verification = "auto_confirmed"
        _touch(
            db,
            bus,
            e,
            {
                "at": iso_utc(utcnow_naive()),
                "action": "auto_confirmed",
                "reports": reports,
                "sources": sources,
            },
        )
        return True
    # 波次级印证:另有一来源在邻近地点、相近时间报了同一类型的事件
    wave = wave_counterpart(db, gaz, e)
    if wave is None:
        return False
    other, distance = wave
    e.verification = "auto_confirmed"
    _touch(
        db,
        bus,
        e,
        {
            "at": iso_utc(utcnow_naive()),
            "action": "auto_confirmed",
            "wave": True,
            "sources": sources,
            "reports": reports,
            "peer_event_id": other.id,
            "distance_km": round(distance, 1),
            "reason": f"邻近同类事件 #{other.id} 来自另一来源(相距 {distance:.1f} км),判定为同一波攻击",
        },
    )
    # 对端同样获得印证(它先到,当时还没有这一来源的报道)
    if other.verification == "unverified":
        other.verification = "auto_confirmed"
        _touch(
            db,
            bus,
            other,
            {
                "at": iso_utc(utcnow_naive()),
                "action": "auto_confirmed",
                "wave": True,
                "peer_event_id": e.id,
                "distance_km": round(distance, 1),
                "reason": f"邻近同类事件 #{e.id} 来自另一来源(相距 {distance:.1f} км),判定为同一波攻击",
            },
        )
    return True


def verify_existing_events(db: Session, gaz: Gazetteer, bus: EventBus) -> int:
    """对既有未核验事件执行一次交叉印证(重放/维护时调用)。"""
    promoted = 0
    for e in db.query(models.Event).filter(models.Event.verification == "unverified").all():
        if promote_if_corroborated(db, gaz, bus, e):
            promoted += 1
    return promoted


def _accept_candidate(db: Session, gaz: Gazetteer, bus: EventBus, c: models.Candidate) -> list[models.Event]:
    """把 accepted 候选并入事件(合并或新建);证据幂等。"""
    touched: list[models.Event] = []
    target = find_merge_target(db, c.event_type, c.place_id, c.occurred_at)
    if target is not None and target.verification in ("confirmed", "rejected"):
        c.status = "ignored"
        c.payload_json = json.dumps({"reason": "event_manually_confirmed"}, ensure_ascii=False)
        return touched
    if target is not None:
        raw = db.get(models.RawMessage, c.message_id)
        family_ev = _family_evidence(db, target.id, raw) if raw is not None else None
        if family_ev is not None:
            if family_ev.message_id != c.message_id:
                # 同一消息的新修订取代旧修订:证据指向最新版本,报道数不变
                family_ev.message_id = c.message_id
                _touch(db, bus, target)
                touched.append(target)
        else:
            target.report_count += 1
            _touch(db, bus, target)
            db.add(models.Evidence(message_id=c.message_id, event_id=target.id, role="report"))
            touched.append(target)
    else:
        p = gaz.get(c.place_id)
        e = models.Event(
            seq=bus.next_seq(),
            event_type=c.event_type,
            place_id=c.place_id,
            geometry_json=json.dumps({"type": "Point", "coordinates": [p["lng"], p["lat"]]}),
            occurred_at=c.occurred_at,
            occurred_uncertainty_s=c.uncertainty_seconds,
            verification="unverified",
            report_count=1,
            version=1,
        )
        db.add(e)
        db.flush()
        db.add(models.Evidence(message_id=c.message_id, event_id=e.id, role="report"))
        touched.append(e)
    # session 关闭了 autoflush,交叉印证前必须先把证据落库,否则统计不到本次报道
    db.flush()
    for ev in touched:
        promote_if_corroborated(db, gaz, bus, ev)
    return touched


def find_admin1_in_text(gaz: Gazetteer, text: str) -> str | None:
    """在消息文本中寻找州级上下文(词干匹配),供自动地理编码使用。"""
    from .gazetteer import _admin_stems, _ctx_stems

    stems = _ctx_stems(text)
    if not stems:
        return None
    for p in gaz._places.values():
        adm = p.get("admin1")
        if adm and _admin_stems(adm).issubset(stems):
            return adm
    return None


def auto_resolve_place(
    db: Session,
    gaz: Gazetteer,
    geocoder,
    message_text: str,
    candidate: models.Candidate,
    ambiguous_ids: list[int] | None = None,
) -> int | None:
    """未收录地名 → Nominatim 自动解析;命中则持久化入地名库并返回 place_id。

    同名歧义时:仅当解析结果与歧义候选之一同名(且同州)才采纳,不引入新地点。
    """
    from sqlalchemy import func

    admin1 = find_admin1_in_text(gaz, message_text)
    hit = geocoder.search(candidate.place_raw, admin1)
    if hit is None or hit["importance"] < config.GEOCODER_MIN_IMPORTANCE:
        return None
    if ambiguous_ids:
        for pid in ambiguous_ids:
            p = gaz.get(pid)
            if p and normalize(p["canonical_name"]).split(" (")[0] == normalize(hit["name"]):
                return pid
        return None
    existing = gaz.resolve(hit["name"], context=hit.get("admin1") or "")
    if existing and existing.get("place_id"):
        return existing["place_id"]
    max_id = db.query(func.max(models.GazetteerPlace.id)).scalar() or 0
    next_id = max(max_id + 1, 1_000_000_000)
    row = models.GazetteerPlace(
        id=next_id,
        canonical_name=hit["name"],
        names_json=json.dumps([hit["name"], candidate.place_raw], ensure_ascii=False),
        lat=hit["lat"],
        lng=hit["lng"],
        admin1=hit.get("admin1"),
        precision=hit.get("type") or "settlement",
        source="nominatim",
    )
    db.add(row)
    db.flush()
    gaz.add_place(
        next_id,
        hit["name"],
        [hit["name"], candidate.place_raw],
        hit["lat"],
        hit["lng"],
        admin1=hit.get("admin1"),
        precision=hit.get("type") or "settlement",
    )
    return next_id


def _apply_retract(db: Session, bus: EventBus, gaz: Gazetteer, source_id: int, external_id: str):
    raws = db.query(models.RawMessage).filter_by(source_id=source_id, external_id=external_id).all()
    msg_ids = [r.id for r in raws]
    if not msg_ids:
        return
    db.query(models.Candidate).filter(models.Candidate.message_id.in_(msg_ids)).update(
        {"status": "ignored", "payload_json": json.dumps({"reason": "retracted"}, ensure_ascii=False)},
        synchronize_session=False,
    )
    evs = db.query(models.Evidence).filter(models.Evidence.message_id.in_(msg_ids)).all()
    affected: dict[int, int] = {}
    for ev in evs:
        affected[ev.event_id] = affected.get(ev.event_id, 0) + 1
        db.delete(ev)
    touched: list[models.Event] = []
    for eid, n in affected.items():
        e = db.get(models.Event, eid)
        if e is None:
            continue
        e.report_count = max(0, e.report_count - n)
        if e.report_count == 0:
            e.verification = "rejected"
            _touch(db, bus, e, {"at": iso_utc(utcnow_naive()), "action": "retracted_all_evidence"})
        else:
            _touch(db, bus, e)
        touched.append(e)
    db.commit()
    for e in touched:
        bus.publish(e.seq, serialize_event(gaz, e))


def _end_active_alerts(db: Session, gaz: Gazetteer, bus: EventBus, raw: models.RawMessage) -> list[models.Event]:
    """警报解除消息:结束同地点仍在持续的 air_alert 事件。

    无地点但来源有州先验时,结束该州(含州内城市,如 bias=Київська область
    覆盖 admin1=Київ)所有进行中的警报。
    """
    from .gazetteer import _admin_stems

    bias = None
    src = db.get(models.Source, raw.source_id)
    if src is not None:
        cfg = json.loads(src.config_json or "{}")
        bias = cfg.get("admin1_bias")
    analysis = analyze_message(raw.body_text, raw.published_at)
    places = _find_places(gaz, raw.body_text, admin_bias=bias)
    ended: list[models.Event] = []
    seen: set[int] = set()

    def end_one(e: models.Event):
        if e.id in seen:
            return
        seen.add(e.id)
        e.ended_at = analysis["occurred_at"]
        _touch(db, bus, e, {"at": iso_utc(utcnow_naive()), "action": "alert_ended"})
        ended.append(e)

    resolved = [pr for pr in places if pr["place_id"] is not None]
    if resolved:
        for pr in resolved:
            q = db.query(models.Event).filter(
                models.Event.event_type == "air_alert",
                models.Event.place_id == pr["place_id"],
                models.Event.ended_at.is_(None),
                models.Event.merged_into_id.is_(None),
            )
            for e in q:
                end_one(e)
    elif bias:
        bias_stems = _admin_stems(bias)
        for e in (
            db.query(models.Event)
            .filter(
                models.Event.event_type == "air_alert",
                models.Event.ended_at.is_(None),
                models.Event.merged_into_id.is_(None),
            )
            .all()
        ):
            p = gaz.get(e.place_id) if e.place_id else None
            if p and p.get("admin1") and _admin_stems(p["admin1"]).issubset(bias_stems):
                end_one(e)
    return ended


def ingest_message(
    db: Session,
    gaz: Gazetteer,
    bus: EventBus,
    *,
    source_id: int,
    external_id: str,
    revision: int = 1,
    body_text: str,
    published_at: datetime,
    received_at: datetime | None = None,
    forward_json: str = "{}",
    edit_json: str = "{}",
    original_url: str | None = None,
    geocoder=None,
) -> dict:
    existing = (
        db.query(models.RawMessage)
        .filter_by(source_id=source_id, external_id=external_id, revision=revision)
        .first()
    )
    if existing:
        if original_url and not existing.original_url:
            existing.original_url = original_url
            db.commit()
            return {"status": "duplicate", "raw_id": existing.id, "url_backfilled": True}
        return {"status": "duplicate", "raw_id": existing.id}

    pub = to_naive_utc(published_at)
    recv = to_naive_utc(received_at) or utcnow_naive()
    raw = models.RawMessage(
        source_id=source_id,
        external_id=external_id,
        revision=revision,
        body_text=body_text,
        published_at=pub,
        received_at=recv,
        forward_json=forward_json,
        edit_json=edit_json,
        original_url=original_url,
    )
    db.add(raw)
    db.flush()

    analysis = analyze_message(body_text, pub)

    edit_meta = json.loads(edit_json or "{}")
    if analysis["retracted"] or edit_meta.get("deleted"):
        _apply_retract(db, bus, gaz, source_id, external_id)
        return {"status": "retracted", "raw_id": raw.id}

    if analysis["all_clear"]:
        ended = _end_active_alerts(db, gaz, bus, raw)
        db.commit()
        for e in ended:
            bus.publish(e.seq, serialize_event(gaz, e))
        return {
            "status": "all_clear",
            "raw_id": raw.id,
            "ended_events": [e.id for e in ended],
        }

    result = _process_message(db, gaz, bus, raw, geocoder=geocoder)
    db.commit()
    for e in result["touched"]:
        bus.publish(e.seq, serialize_event(gaz, e))
    return {
        "status": "ok",
        "raw_id": raw.id,
        "candidates": result["candidate_ids"],
        "events_touched": [e.id for e in result["touched"]],
    }

def _line_head_keys(text: str) -> set[str]:
    """行首专名的规范化键(后接 — - : 等分隔符的"标题式"地名,如 "Краматорськ: вибухи")。"""
    out: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip().lstrip("*⚠️❗️🔥💥🚀→>•-–— ").strip()
        m = re.match(r"([А-ЯЁҐІЇЄA-Z][\w'’\-]{2,})\s*[—–:\-]", stripped)
        if m:
            out.add(normalize(m.group(1)))
    return out


def _suspect_tokens(gaz: Gazetteer, text: str, admin_bias: str | None = None) -> list[str]:
    """报文中疑似地名、但目录未收录的专名(供地名库维护列表)。

    比 _find_places 宽(不因后接小写词丢弃),但要求"像地名"的上下文:
    介词短语("по Новомихайлівці")、行政词搭配("Запорізька обл")或标题式行首,
    否则普通大写词(Знову / Загалом / Попередньо…)会把维护列表淹没。
    """
    matches = list(PLACE_TOKEN_RE.finditer(text))
    tokens = [m.group(0) for m in matches]
    heads = _line_head_keys(text)
    out: list[str] = []
    seen: set[str] = set()
    handled: set[int] = set()  # 已被成对处理的下标

    def resolved(raw: str) -> bool:
        return gaz.resolve(raw, context=text, admin_bias=admin_bias) is not None

    def usable(t: str) -> bool:
        return (
            len(t) >= 3
            and bool(_UPPER_START.match(t))
            and not is_noise_token(t)
            and t.lower() not in _PLACE_STOPWORDS
            and normalize(t) not in _PLACE_STOPWORDS_NORM
        )

    def adjacent(i: int) -> bool:
        """两个 token 在原文中只隔空格(同行的多词地名,如 "Жовті Води")。"""
        gap = text[matches[i].end() : matches[i + 1].start()]
        return gap.strip() == "" and "\n" not in gap

    def add(raw: str) -> None:
        key = normalize(raw)
        if key and key not in seen:
            seen.add(key)
            out.append(raw)

    def headline(t: str) -> bool:
        """全大写词多为新闻标题/口号("УЛЬТИМАТУМ ТРАМПА"),不是地名。"""
        return len(t) > 3 and t.isupper()

    # 1) 相邻成对专名:"Жовті Води"、"Біла Церква" 作为整体,避免拆成两个半截词
    for i in range(len(tokens) - 1):
        a, b = tokens[i], tokens[i + 1]
        if i in handled or not usable(a) or not usable(b) or not adjacent(i):
            continue
        if headline(a) or headline(b):
            handled.update({i, i + 1})
            continue
        if resolved(f"{a} {b}"):
            handled.update({i, i + 1})
            continue
        if ADMIN_WORD.search(b) or b[0].isupper() or normalize(a) in heads:
            add(f"{a} {b}")
            handled.update({i, i + 1})

    # 2) 单个专名:必须落在"像地名"的上下文里
    for i, t in enumerate(tokens):
        if i in handled or not usable(t) or len(t) < 4:
            continue
        if resolved(t):
            continue
        prev = tokens[i - 1] if i > 0 else ""
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        after_prep = bool(prev) and normalize(prev) in _PLACE_PREPOSITIONS and adjacent(i - 1)
        if prev and ADMIN_WORD.search(prev):
            continue
        if headline(t) and not after_prep:
            continue
        if normalize(t) in heads:
            add(t)
            continue
        if after_prep:
            add(t)
            continue
        if prev and is_event_keyword(prev) and adjacent(i - 1):
            add(t)  # "Обстріл Новотестівки" 式无介词结构
            continue
        if nxt and ADMIN_WORD.search(nxt) and adjacent(i):
            pair = f"{t} {nxt}"
            if resolved(pair):
                handled.update({i, i + 1})  # "Запорізька область" 已收录,不是待补地名
                continue
            add(pair)  # 补的是带行政词的写法("Сумський р-н")
    return out


def _record_unknown_places(
    db: Session, raw: models.RawMessage, suspects: list[str], source_id: int | None
) -> None:
    """记录未收录专名(按规范化键聚合),用于管理端"待补地名"列表。"""
    for name in suspects:
        key = normalize(name)
        if not key:
            continue
        row = db.query(models.UnknownPlace).filter_by(raw=key).first()
        if row is None:
            db.add(
                models.UnknownPlace(
                    raw=key,
                    display_name=name,
                    context=raw.body_text[:200],
                    report_count=1,
                    source_id=source_id,
                    sample_message_id=raw.id,
                    message_ids_json=json.dumps([raw.id]),
                )
            )
            continue
        ids = json.loads(row.message_ids_json or "[]")
        if raw.id in ids:
            continue  # 重放/编辑不重复计数
        ids.append(raw.id)
        row.message_ids_json = json.dumps(ids[-200:])
        row.report_count = len(ids)
        row.last_seen_at = utcnow_naive()
        if row.status == "resolved" and row.resolved_place_id is None:
            row.status = "open"
    db.flush()


def list_unknown_places(db: Session, status: str = "open", limit: int = 50) -> list[dict]:
    q = db.query(models.UnknownPlace)
    if status:
        q = q.filter(models.UnknownPlace.status == status)
    rows = q.order_by(models.UnknownPlace.report_count.desc(), models.UnknownPlace.id.desc()).limit(limit).all()
    return [
        {
            "id": r.id,
            "name": r.display_name,
            "raw": r.raw,
            "report_count": r.report_count,
            "status": r.status,
            "context": r.context,
            "sample_message_id": r.sample_message_id,
            "source_id": r.source_id,
            "first_seen_at": iso_utc(r.first_seen_at),
            "last_seen_at": iso_utc(r.last_seen_at),
            "resolved_place_id": r.resolved_place_id,
        }
        for r in rows
    ]


def ignore_unknown_place(db: Session, unknown_id: int, operator: str = "operator") -> bool:
    row = db.get(models.UnknownPlace, unknown_id)
    if row is None:
        return False
    row.status = "ignored"
    db.commit()
    return True


def _process_message(
    db: Session, gaz: Gazetteer, bus: EventBus, raw: models.RawMessage, geocoder=None
) -> dict:
    """对一条原始消息执行提取/合并/证据关联(不落 raw、不提交)。

    重提取幂等:证据已存在时不重复计数、不重复插入。
    """
    analysis = analyze_message(raw.body_text, raw.published_at)
    bias = None
    src = db.get(models.Source, raw.source_id)
    if src is not None:
        cfg = json.loads(src.config_json or "{}")
        bias = cfg.get("admin1_bias")
    places = _find_places(gaz, raw.body_text, admin_bias=bias)
    candidate_ids: list[int] = []
    touched: list[models.Event] = []

    if analysis["types"] and not analysis["negated"] and not analysis["retracted"]:
        # 有事件类型关键词的报文才值得摊开未收录专名(噪音消息不进维护列表)
        _record_unknown_places(db, raw, _suspect_tokens(gaz, raw.body_text, admin_bias=bias), raw.source_id)

    if analysis["negated"]:
        for pr in places:
            c = models.Candidate(
                message_id=raw.id,
                extractor_version="rules-v1",
                event_type=analysis["types"][0] if analysis["types"] else None,
                place_id=pr["place_id"],
                place_raw=pr["raw"],
                occurred_at=analysis["occurred_at"],
                uncertainty_seconds=analysis["uncertainty"],
                time_basis=analysis["time_basis"],
                confidence=0.0,
                status="ignored",
                payload_json=json.dumps({"reason": "negated"}, ensure_ascii=False),
            )
            db.add(c)
            db.flush()
            candidate_ids.append(c.id)
    else:
        if not analysis["types"]:
            # 无类型关键词时,只为已解析地点建待核验候选;未解析大写词多为噪音,不刷屏待处理
            for pr in places:
                if pr["place_id"] is None:
                    continue
                c = models.Candidate(
                    message_id=raw.id,
                    extractor_version="rules-v1",
                    event_type=None,
                    place_id=pr["place_id"],
                    place_raw=pr["raw"],
                    occurred_at=analysis["occurred_at"],
                    uncertainty_seconds=analysis["uncertainty"],
                    time_basis=analysis["time_basis"],
                    confidence=0.3,
                    status="needs_review",
                    payload_json=json.dumps({"reason": "unknown_type"}, ensure_ascii=False),
                )
                db.add(c)
                db.flush()
                candidate_ids.append(c.id)
        else:
            for pr in places:
                for t in analysis["types"]:
                    # 区域实体(州级)只承载 air_alert;其他类型中出现的州名视为语境
                    if pr["place_id"] is not None:
                        p_info = gaz.get(pr["place_id"])
                        if p_info and p_info.get("precision") == "region" and t != "air_alert":
                            continue
                    c = models.Candidate(
                        message_id=raw.id,
                        extractor_version="rules-v1",
                        event_type=t,
                        place_id=pr["place_id"],
                        place_raw=pr["raw"],
                        occurred_at=analysis["occurred_at"],
                        uncertainty_seconds=analysis["uncertainty"],
                        time_basis=analysis["time_basis"],
                        confidence=1.0 if pr["place_id"] and not pr["fuzzy"] else 0.5,
                        status="needs_review",
                    )
                    if pr["place_id"] is None:
                        # 自动地理编码:未收录地名先自动解析(无需人工);同名歧义仅在
                        # 解析结果与候选之一同名时采纳
                        if geocoder is not None:
                            c.status = "accepted"
                            resolved = auto_resolve_place(
                                db, gaz, geocoder, raw.body_text, c,
                                ambiguous_ids=pr["ambiguous_ids"] or None,
                            )
                            if resolved is not None:
                                c.place_id = resolved
                        if c.place_id is None:
                            c.status = "needs_review"
                            c.payload_json = json.dumps(
                                {
                                    "reason": "ambiguous_place" if pr["ambiguous_ids"] else "unresolved_place",
                                    "ambiguous_ids": pr["ambiguous_ids"],
                                },
                                ensure_ascii=False,
                            )
                        else:
                            touched.extend(_accept_candidate(db, gaz, bus, c))
                    else:
                        c.status = "accepted"
                        touched.extend(_accept_candidate(db, gaz, bus, c))
                    db.add(c)
                    db.flush()
                    candidate_ids.append(c.id)

    return {"candidate_ids": candidate_ids, "touched": touched}


def reject_candidate(
    db: Session,
    candidate_id: int,
    reason: str,
    operator: str = "operator",
) -> models.Candidate | None:
    """驳回待处理候选(噪音/非事件),标记 ignored 并记录原因。"""
    c = db.get(models.Candidate, candidate_id)
    if c is None:
        return None
    c.status = "ignored"
    payload = json.loads(c.payload_json or "{}")
    payload["rejected_by"] = operator
    payload["reject_reason"] = reason
    c.payload_json = json.dumps(payload, ensure_ascii=False)
    db.commit()
    return c


def reprocess_all(db: Session, gaz: Gazetteer, bus: EventBus, geocoder=None) -> dict:
    """规则升级后的全量重放:清空候选、按原始消息重跑提取。

    事件与证据幂等:已有证据不重复计数、不重复插入;撤回/删除消息跳过。
    """
    deleted = db.query(models.Candidate).delete(synchronize_session=False)
    touched: list[models.Event] = []
    processed = 0
    for raw in db.query(models.RawMessage).order_by(models.RawMessage.id).all():
        analysis = analyze_message(raw.body_text, raw.published_at)
        edit_meta = json.loads(raw.edit_json or "{}")
        if analysis["retracted"] or edit_meta.get("deleted"):
            continue
        if analysis["all_clear"]:
            # 解除消息按时间顺序结束进行中的警报(与 ingest 同语义)
            ended = _end_active_alerts(db, gaz, bus, raw)
            touched.extend(ended)
        else:
            result = _process_message(db, gaz, bus, raw, geocoder=geocoder)
            touched.extend(result["touched"])
        processed += 1
        # 逐条提交:避免长事务独占写锁(sqlite 单写者),让实时采集可以交错写入
        db.commit()
    promoted = verify_existing_events(db, gaz, bus)
    db.commit()
    for e in touched:
        bus.publish(e.seq, serialize_event(gaz, e))
    return {
        "deleted_candidates": deleted,
        "processed_messages": processed,
        "events_touched": len(touched),
        "auto_confirmed": promoted,
    }


def _source_bias(db: Session, source_id: int | None) -> str | None:
    if source_id is None:
        return None
    src = db.get(models.Source, source_id)
    if src is None:
        return None
    return json.loads(src.config_json or "{}").get("admin1_bias")


def rematch_pending(db: Session, gaz: Gazetteer, bus: EventBus) -> dict:
    """管理端新增/修正地名后,只重试仍未解析的待处理候选(不重放全部历史)。

    比全量 reprocess 快得多:只在目录发生变化后使用。
    """
    cs = (
        db.query(models.Candidate)
        .filter(models.Candidate.status == "needs_review")
        .order_by(models.Candidate.id)
        .all()
    )
    retried = 0
    resolved = 0
    touched_ids: set[int] = set()
    for c in cs:
        if c.place_id is not None:
            continue
        raw = db.get(models.RawMessage, c.message_id)
        if raw is None or not c.place_raw:
            continue
        retried += 1
        hit = gaz.resolve(c.place_raw, context=raw.body_text, admin_bias=_source_bias(db, raw.source_id))
        if hit is None or hit.get("place_id") is None:
            db.commit()
            continue
        c.place_id = hit["place_id"]
        c.confidence = 1.0 if not hit.get("fuzzy") else 0.5
        c.status = "accepted"
        payload = json.loads(c.payload_json or "{}")
        payload["rematched"] = True
        payload["reason"] = None
        c.payload_json = json.dumps(payload, ensure_ascii=False)
        db.add(c)
        events = _accept_candidate(db, gaz, bus, c)
        for e in events:
            touched_ids.add(e.id)
        resolved += 1
        db.commit()
    promoted = verify_existing_events(db, gaz, bus)
    db.commit()
    for eid in touched_ids:
        e = db.get(models.Event, eid)
        if e is not None:
            bus.publish(e.seq, serialize_event(gaz, e))
    return {
        "retried": retried,
        "resolved": resolved,
        "events_touched": len(touched_ids),
        "auto_confirmed": promoted,
    }


def _reprocess_messages(db: Session, gaz: Gazetteer, bus: EventBus, message_ids: list[int]) -> list[int]:
    """重跑指定原始消息(先清掉这条消息的候选,再重提取)。

    与 reprocess_all 同语义,只用于"待补地名"被补齐后的定点回填:
    这些消息此前没有解析出地点,因此没有事件证据,重跑不会重复计数。
    """
    touched_ids: list[int] = []
    for mid in message_ids:
        raw = db.get(models.RawMessage, mid)
        if raw is None:
            continue
        db.query(models.Candidate).filter(models.Candidate.message_id == mid).delete(
            synchronize_session=False
        )
        analysis = analyze_message(raw.body_text, raw.published_at)
        edit_meta = json.loads(raw.edit_json or "{}")
        if analysis["retracted"] or edit_meta.get("deleted"):
            db.commit()
            continue
        if analysis["all_clear"]:
            events = _end_active_alerts(db, gaz, bus, raw)
        else:
            events = _process_message(db, gaz, bus, raw)["touched"]
        touched_ids.extend(e.id for e in events)
        db.commit()
    return touched_ids


def rematch_unknown_places(db: Session, gaz: Gazetteer, bus: EventBus) -> dict:
    """待补地名里已经能被目录解析的条目 → 标记 resolved,并回填相关报文。"""
    rows = db.query(models.UnknownPlace).filter_by(status="open").all()
    message_ids: list[int] = []
    resolved = 0
    for row in rows:
        hit = gaz.resolve(row.display_name, context=row.context, admin_bias=_source_bias(db, row.source_id))
        if hit is None or hit.get("place_id") is None:
            continue
        row.status = "resolved"
        row.resolved_place_id = hit["place_id"]
        resolved += 1
        for mid in json.loads(row.message_ids_json or "[]"):
            if mid not in message_ids:
                message_ids.append(mid)
        db.commit()
    touched = _reprocess_messages(db, gaz, bus, message_ids) if message_ids else []
    for eid in set(touched):
        e = db.get(models.Event, eid)
        if e is not None:
            bus.publish(e.seq, serialize_event(gaz, e))
    return {"unknown_resolved": resolved, "messages_reprocessed": len(message_ids), "events": len(set(touched))}


def rematch_all(db: Session, gaz: Gazetteer, bus: EventBus) -> dict:
    """管理端"重新匹配":补齐待补地名 → 回填报文 → 重试待处理候选 → 交叉印证。"""
    unknown = rematch_unknown_places(db, gaz, bus)
    pending = rematch_pending(db, gaz, bus)
    return {"unknown": unknown, "pending": pending}


def publish_place_events(db: Session, gaz: Gazetteer, bus: EventBus, place_id: int) -> int:
    """地名被编辑后,把引用该地点的事件重新推给前端(只推最近 2 小时内的)。"""
    since = utcnow_naive() - timedelta(hours=2)
    rows = (
        db.query(models.Event)
        .filter(
            models.Event.place_id == place_id,
            models.Event.merged_into_id.is_(None),
            models.Event.occurred_at >= since,
        )
        .all()
    )
    for e in rows:
        bus.publish(e.seq, serialize_event(gaz, e))
    return len(rows)


def upsert_place(
    db: Session,
    gaz: Gazetteer,
    *,
    place_id: int | None = None,
    canonical_name: str,
    names: list[str] | None = None,
    lat: float,
    lng: float,
    admin1: str | None = None,
    admin2: str | None = None,
    precision: str = "settlement",
    name_zh: str | None = None,
    source: str = "admin",
) -> int:
    """新增或编辑地名(管理端),同时写入 DB(重启后仍生效)与内存目录。"""
    from sqlalchemy import func

    if place_id is None:
        max_id = db.query(func.max(models.GazetteerPlace.id)).scalar() or 0
        place_id = max(max_id + 1, 1_000_000_000)
    alias_list = [a for a in (names or []) if a and a.strip()] or [canonical_name]
    if canonical_name not in alias_list:
        alias_list.insert(0, canonical_name)
    row = db.get(models.GazetteerPlace, place_id)
    if row is None:
        row = models.GazetteerPlace(id=place_id)
        db.add(row)
    row.canonical_name = canonical_name
    row.names_json = json.dumps(alias_list, ensure_ascii=False)
    row.lat = float(lat)
    row.lng = float(lng)
    row.admin1 = admin1
    row.admin2 = admin2
    row.precision = precision
    row.source = source
    row.name_zh = name_zh
    db.commit()
    gaz.upsert_place(
        place_id,
        canonical_name,
        alias_list,
        float(lat),
        float(lng),
        admin1,
        admin2,
        precision,
        source=source,
        name_zh=name_zh,
    )
    return place_id


def correct_event(
    db: Session,
    bus: EventBus,
    gaz: Gazetteer,
    event_id: int,
    *,
    verification: str | None = None,
    occurred_at: datetime | None = None,
    place_id: int | None = None,
    reason: str,
    operator: str = "operator",
) -> models.Event | None:
    e = db.get(models.Event, event_id)
    if e is None:
        return None
    if verification:
        e.verification = verification
    if occurred_at is not None:
        e.occurred_at = to_naive_utc(occurred_at)
    if place_id is not None:
        p = gaz.get(place_id)
        if p is None:
            raise ValueError("unknown place_id")
        e.place_id = place_id
        e.geometry_json = json.dumps({"type": "Point", "coordinates": [p["lng"], p["lat"]]})
    _touch(
        db,
        bus,
        e,
        {
            "at": iso_utc(utcnow_naive()),
            "operator": operator,
            "action": "correct",
            "reason": reason,
            "verification": e.verification,
            "occurred_at": iso_utc(e.occurred_at),
            "place_id": e.place_id,
        },
    )
    db.commit()
    bus.publish(e.seq, serialize_event(gaz, e))
    return e


def resolve_candidate(
    db: Session,
    bus: EventBus,
    gaz: Gazetteer,
    candidate_id: int,
    *,
    place_id: int,
    event_type: str | None = None,
    occurred_at: datetime | None = None,
    operator: str = "operator",
) -> models.Event | None:
    """把待定位/待核验候选解析为事件(运营确认)。"""
    c = db.get(models.Candidate, candidate_id)
    if c is None:
        return None
    p = gaz.get(place_id)
    if p is None:
        raise ValueError("unknown place_id")
    occurred = to_naive_utc(occurred_at) if occurred_at is not None else c.occurred_at
    etype = event_type or c.event_type
    if not etype:
        raise ValueError("event_type is required when candidate has none")
    target = find_merge_target(db, etype, place_id, occurred)
    if target is not None and target.verification == "confirmed":
        raise ValueError("matching event is manually confirmed; split or correct it instead")
    if target is not None:
        target.report_count += 1
        _touch(db, bus, target)
        db.add(models.Evidence(message_id=c.message_id, event_id=target.id, role="report"))
        ev = target
    else:
        ev = models.Event(
            seq=bus.next_seq(),
            event_type=etype,
            place_id=place_id,
            geometry_json=json.dumps({"type": "Point", "coordinates": [p["lng"], p["lat"]]}),
            occurred_at=occurred,
            occurred_uncertainty_s=c.uncertainty_seconds,
            verification="unverified",
            report_count=1,
            version=1,
        )
        db.add(ev)
        db.flush()
        db.add(models.Evidence(message_id=c.message_id, event_id=ev.id, role="report"))
    c.status = "accepted"
    c.place_id = place_id
    c.event_type = etype
    if occurred is not None:
        c.occurred_at = occurred
    payload = json.loads(c.payload_json or "{}")
    payload["resolved_by"] = operator
    c.payload_json = json.dumps(payload, ensure_ascii=False)
    db.commit()
    bus.publish(ev.seq, serialize_event(gaz, ev))
    return ev


def split_event(
    db: Session, bus: EventBus, gaz: Gazetteer, event_id: int, operator: str = "operator"
) -> list[int]:
    e = db.get(models.Event, event_id)
    if e is None:
        return []
    evs = db.query(models.Evidence).filter_by(event_id=event_id).all()
    new_ids: list[int] = []
    for ev in evs:
        cand = (
            db.query(models.Candidate)
            .filter(
                models.Candidate.message_id == ev.message_id,
                models.Candidate.event_type == e.event_type,
                models.Candidate.status == "accepted",
            )
            .first()
        )
        place_id = cand.place_id if cand and cand.place_id else e.place_id
        geometry = e.geometry_json
        if place_id and gaz.get(place_id):
            p = gaz.get(place_id)
            geometry = json.dumps({"type": "Point", "coordinates": [p["lng"], p["lat"]]})
        ne = models.Event(
            seq=bus.next_seq(),
            event_type=e.event_type,
            place_id=place_id,
            geometry_json=geometry,
            occurred_at=cand.occurred_at if cand else e.occurred_at,
            occurred_uncertainty_s=cand.uncertainty_seconds if cand else e.occurred_uncertainty_s,
            verification="unverified",
            report_count=1,
            version=1,
        )
        db.add(ne)
        db.flush()
        ev.event_id = ne.id
        new_ids.append(ne.id)
    e.merged_into_id = new_ids[0] if new_ids else None
    e.verification = "rejected"
    _touch(db, bus, e, {"at": iso_utc(utcnow_naive()), "operator": operator, "action": "split", "into": new_ids})
    db.commit()
    for i in new_ids:
        ne = db.get(models.Event, i)
        bus.publish(ne.seq, serialize_event(gaz, ne))
    bus.publish(e.seq, serialize_event(gaz, e))
    return new_ids
