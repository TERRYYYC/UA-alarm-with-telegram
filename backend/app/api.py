import asyncio
import json
import queue as queue_mod
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from . import models, service, state
from .db import get_session
from .gazetteer import normalize
from .schemas import CorrectRequest, IngestRequest, PlaceUpsert, RejectRequest, ResolveRequest

router = APIRouter(prefix="/api/v1")


def _require_state():
    if state.gazetteer is None or state.bus is None:
        raise HTTPException(503, "backend not initialized")


_LOCAL_HOSTS = ("127.0.0.1", "::1", "localhost", "testclient")


def _require_local(request: Request):
    """写操作(改地名/重放)仅限本机;公网部署时由反向代理再加一层限制。"""
    if request.client is None or request.client.host not in _LOCAL_HOSTS:
        raise HTTPException(403, "admin endpoints are local-only")


@router.get("/healthz")
def healthz(db: Session = Depends(get_session)):
    return {
        "status": "ok",
        "places": len(state.gazetteer) if state.gazetteer else 0,
        "events": db.query(models.Event).count(),
        "pending": db.query(models.Candidate).filter(models.Candidate.status == "needs_review").count(),
    }


@router.post("/ingest")
def ingest(payload: IngestRequest, db: Session = Depends(get_session)):
    _require_state()
    try:
        return service.ingest_message(
            db, state.gazetteer, state.bus, **payload.model_dump(), geocoder=state.geocoder
        )
    except Exception as exc:  # 让调用方看到处理失败,不吞掉
        db.rollback()
        raise HTTPException(500, f"ingest failed: {exc}") from exc


@router.get("/events")
def list_events(
    window_minutes: int = 120,
    types: str = "",
    verification: str = "not_rejected",
    include_ended: bool = False,
    limit: int = 0,
    include_evidence: bool = False,
    db: Session = Depends(get_session),
):
    """事件查询。

    verification: not_rejected(默认)|any|confirmed|unverified|auto_confirmed|rejected,
    也可逗号分隔多个值(如 "unverified,auto_confirmed")。
    include_evidence: 内联证据原文(供 DSH/复核流程一次取全,避免逐条再查)。
    """
    _require_state()
    since = service.utcnow_naive() - timedelta(minutes=window_minutes)
    q = db.query(models.Event).filter(
        models.Event.merged_into_id.is_(None),
        models.Event.occurred_at >= since,
    )
    if not include_ended:
        q = q.filter(models.Event.ended_at.is_(None))
    if verification and verification not in ("any", "all", "not_rejected"):
        vset = [v.strip() for v in verification.split(",") if v.strip()]
        q = q.filter(models.Event.verification.in_(vset))
    elif verification == "not_rejected":
        q = q.filter(models.Event.verification != "rejected")
    if types:
        tset = [t.strip() for t in types.split(",") if t.strip()]
        q = q.filter(models.Event.event_type.in_(tset))
    q = q.order_by(models.Event.occurred_at.desc())
    if limit > 0:
        q = q.limit(limit * 2 if include_evidence else limit)
    out = []
    for e in q.all():
        # 地点已从目录移除(如自然/纪念物名被过滤)的事件不上图
        if e.place_id is not None and state.gazetteer.get(e.place_id) is None:
            continue
        data = service.serialize_event(state.gazetteer, e)
        if include_evidence:
            items = []
            for ev in db.query(models.Evidence).filter_by(event_id=e.id).all():
                m = db.get(models.RawMessage, ev.message_id)
                if m is None:
                    continue
                items.append(
                    {
                        "message_id": m.id,
                        "source_id": m.source_id,
                        "text": m.body_text,
                        "published_at": service.iso_utc(m.published_at),
                        "original_url": m.original_url,
                    }
                )
            data["evidence"] = items
        out.append(data)
        if limit > 0 and len(out) >= limit:
            break
    return out


@router.get("/events/{event_id}")
def event_detail(event_id: int, db: Session = Depends(get_session)):
    _require_state()
    e = db.get(models.Event, event_id)
    if e is None:
        raise HTTPException(404, "event not found")
    data = service.serialize_event(state.gazetteer, e)
    items = []
    for ev in db.query(models.Evidence).filter_by(event_id=event_id).all():
        m = db.get(models.RawMessage, ev.message_id)
        items.append(
            {
                "message_id": m.id,
                "role": ev.role,
                "text": m.body_text,
                "published_at": service.iso_utc(m.published_at),
                "forward_json": m.forward_json,
                "source_id": m.source_id,
                "original_url": m.original_url,
            }
        )
    data["evidence"] = items
    data["reason"] = json.loads(e.reason_json or "[]")
    return data


@router.get("/pending")
def pending(db: Session = Depends(get_session)):
    _require_state()
    cs = (
        db.query(models.Candidate)
        .filter(models.Candidate.status == "needs_review")
        .order_by(models.Candidate.id.desc())
        .all()
    )
    out = []
    for c in cs:
        m = db.get(models.RawMessage, c.message_id)
        out.append(
            {
                "candidate_id": c.id,
                "event_type": c.event_type,
                "place_raw": c.place_raw,
                "place_id": c.place_id,
                "reason": json.loads(c.payload_json or "{}").get("reason"),
                "message_text": m.body_text,
                "published_at": service.iso_utc(m.published_at),
            }
        )
    return out


@router.post("/events/{event_id}/correct")
def correct(event_id: int, payload: CorrectRequest, db: Session = Depends(get_session)):
    _require_state()
    try:
        e = service.correct_event(
            db,
            state.bus,
            state.gazetteer,
            event_id,
            verification=payload.verification,
            occurred_at=payload.occurred_at,
            place_id=payload.place_id,
            reason=payload.reason,
            operator=payload.operator,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if e is None:
        raise HTTPException(404, "event not found")
    return service.serialize_event(state.gazetteer, e)


@router.post("/events/{event_id}/split")
def split(event_id: int, db: Session = Depends(get_session)):
    _require_state()
    e = db.get(models.Event, event_id)
    if e is None:
        raise HTTPException(404, "event not found")
    new_ids = service.split_event(db, state.bus, state.gazetteer, event_id)
    return {"split_into": new_ids}


@router.get("/sources")
def sources(db: Session = Depends(get_session)):
    _require_state()
    out = []
    for s in db.query(models.Source).order_by(models.Source.id).all():
        out.append(
            {
                "id": s.id,
                "adapter": s.adapter,
                "name": s.name,
                "enabled": s.enabled,
                "last_success_at": service.iso_utc(s.last_success_at),
                "gap_notes": s.gap_notes or "",
            }
        )
    return out


@router.get("/stats")
def stats(window_minutes: int = 1440, db: Session = Depends(get_session)):
    """一屏概览:核验分布、最近窗口内类型分布、来源健康、待处理与待补地名计数。

    供运营面板与 DSH 插件一次取全,避免多次往返。
    """
    _require_state()
    from sqlalchemy import func

    since = service.utcnow_naive() - timedelta(minutes=window_minutes)
    by_verification = {
        v: n
        for v, n in db.query(models.Event.verification, func.count())
        .filter(models.Event.merged_into_id.is_(None))
        .group_by(models.Event.verification)
        .all()
    }
    by_type = {
        t: n
        for t, n in db.query(models.Event.event_type, func.count())
        .filter(models.Event.merged_into_id.is_(None), models.Event.occurred_at >= since)
        .group_by(models.Event.event_type)
        .all()
    }
    sources = []
    for s in db.query(models.Source).order_by(models.Source.id).all():
        cfg = json.loads(s.config_json or "{}")
        sources.append(
            {
                "id": s.id,
                "name": s.name,
                "adapter": s.adapter,
                "enabled": s.enabled,
                "admin1_bias": cfg.get("admin1_bias"),
                "last_success_at": service.iso_utc(s.last_success_at),
                "gap_notes": s.gap_notes or "",
            }
        )
    return {
        "window_minutes": window_minutes,
        "events_total": sum(by_verification.values()),
        "events_window": sum(by_type.values()),
        "by_verification": by_verification,
        "by_type": by_type,
        "pending": db.query(models.Candidate).filter(models.Candidate.status == "needs_review").count(),
        "unknown_places": db.query(models.UnknownPlace).filter(models.UnknownPlace.status == "open").count(),
        "places": len(state.gazetteer) if state.gazetteer else 0,
        "sources": sources,
    }


@router.get("/places")
def places(q: str = "", limit: int = 50, db: Session = Depends(get_session)):
    """地名检索(支持乌克兰语原名、中文名、行政州、别名),按相关度排序。

    名称完全一致 > 前缀一致 > 中文名命中 > 名称包含 > 别名/所属州命中,
    同级里州/区/城市优先于村镇——否则搜一个州名会被该州几千个村庄淹没。
    """
    _require_state()
    out = state.gazetteer.all_places()
    if not q:
        return out[:limit]

    needle = q.strip().lower()
    norm_needle = normalize(q)
    gaz = state.gazetteer
    precision_rank = {"region": 0, "district": 1, "city": 2, "town": 3}

    ranked = []
    for p in out:
        name = p["name"]
        norm_name = normalize(name)
        zh = p.get("name_zh") or ""
        fields = (name, zh, p.get("admin1"), p.get("admin2"))
        aliases = gaz.aliases_for(p["id"])
        if not any(needle in (f or "").lower() for f in fields) and not any(
            needle in (a or "").lower() for a in aliases
        ):
            continue
        if norm_name == norm_needle or zh == q.strip():
            tier = 0
        elif norm_name.startswith(norm_needle):
            tier = 1
        elif needle in zh.lower():
            tier = 2
        elif norm_needle in norm_name:
            tier = 3
        else:
            tier = 4
        ranked.append((tier, precision_rank.get(p["precision"], 4), len(name), name, p))
    ranked.sort(key=lambda r: r[:4])
    return [r[4] for r in ranked[:limit]]


@router.get("/places/{place_id}")
def place_detail(place_id: int):
    _require_state()
    detail = state.gazetteer.place_detail(place_id)
    if detail is None:
        raise HTTPException(404, "place not found")
    return detail


@router.post("/places")
def create_place(payload: PlaceUpsert, request: Request, db: Session = Depends(get_session)):
    _require_state()
    _require_local(request)
    if not payload.canonical_name.strip():
        raise HTTPException(400, "canonical_name is required")
    place_id = service.upsert_place(
        db,
        state.gazetteer,
        canonical_name=payload.canonical_name.strip(),
        names=payload.names,
        lat=payload.lat,
        lng=payload.lng,
        admin1=payload.admin1,
        admin2=payload.admin2,
        precision=payload.precision,
        name_zh=payload.name_zh,
    )
    return state.gazetteer.place_detail(place_id)


@router.put("/places/{place_id}")
def update_place(
    place_id: int, payload: PlaceUpsert, request: Request, db: Session = Depends(get_session)
):
    _require_state()
    _require_local(request)
    if state.gazetteer.get(place_id) is None:
        raise HTTPException(404, "place not found")
    if not payload.canonical_name.strip():
        raise HTTPException(400, "canonical_name is required")
    service.upsert_place(
        db,
        state.gazetteer,
        place_id=place_id,
        canonical_name=payload.canonical_name.strip(),
        names=payload.names,
        lat=payload.lat,
        lng=payload.lng,
        admin1=payload.admin1,
        admin2=payload.admin2,
        precision=payload.precision,
        name_zh=payload.name_zh,
    )
    # 地名变化后立即把受影响的事件重新推给前端(事件读取目录坐标是实时的)
    service.publish_place_events(db, state.gazetteer, state.bus, place_id)
    return state.gazetteer.place_detail(place_id)


@router.get("/unknown-places")
def unknown_places(status: str = "open", limit: int = 50, db: Session = Depends(get_session)):
    """待补地名:报文中出现但目录未收录的专名(按出现次数排序)。"""
    _require_state()
    return service.list_unknown_places(db, status=status, limit=limit)


@router.post("/unknown-places/{unknown_id}/ignore")
def ignore_unknown_place(
    unknown_id: int, request: Request, db: Session = Depends(get_session)
):
    _require_state()
    _require_local(request)
    if not service.ignore_unknown_place(db, unknown_id):
        raise HTTPException(404, "unknown place not found")
    return {"status": "ignored", "id": unknown_id}


@router.post("/admin/reverify")
def reverify(request: Request, db: Session = Depends(get_session)):
    """对既有事件重跑交叉印证(含波次级印证)。仅限本机调用。"""
    _require_state()
    _require_local(request)
    promoted = service.verify_existing_events(db, state.gazetteer, state.bus)
    db.commit()
    return {"auto_confirmed": promoted}


@router.post("/admin/rematch")
def rematch(request: Request, db: Session = Depends(get_session)):
    """新增/修正地名后重跑匹配:待补地名回填 + 待处理候选重试。仅限本机调用。"""
    _require_state()
    _require_local(request)
    return service.rematch_all(db, state.gazetteer, state.bus)


@router.post("/pending/{candidate_id}/resolve")
def resolve_pending(candidate_id: int, payload: ResolveRequest, db: Session = Depends(get_session)):
    _require_state()
    try:
        e = service.resolve_candidate(
            db,
            state.bus,
            state.gazetteer,
            candidate_id,
            place_id=payload.place_id,
            event_type=payload.event_type,
            occurred_at=payload.occurred_at,
            operator=payload.operator,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if e is None:
        raise HTTPException(404, "candidate not found")
    return service.serialize_event(state.gazetteer, e)


@router.post("/admin/reprocess")
def reprocess(request: Request, db: Session = Depends(get_session)):
    """规则升级后的全量重放(幂等)。仅限本机调用;公网部署时由反向代理再加一层限制。"""
    _require_state()
    _require_local(request)
    return service.reprocess_all(db, state.gazetteer, state.bus, geocoder=state.geocoder)


@router.post("/pending/{candidate_id}/reject")
def reject_pending(candidate_id: int, payload: RejectRequest, db: Session = Depends(get_session)):
    _require_state()
    c = service.reject_candidate(db, candidate_id, payload.reason, payload.operator)
    if c is None:
        raise HTTPException(404, "candidate not found")
    return {"status": "ignored", "candidate_id": candidate_id}


@router.post("/admin/publish-now")
def publish_now(request: Request, db: Session = Depends(get_session)):
    """运营手动触发一次发布(无视最小间隔)。仅限本机调用。"""
    _require_state()
    _require_local(request)
    if state.publisher is None:
        raise HTTPException(503, "publisher not enabled (set PUBLISH_OUT_DIR)")
    task = state.publisher.maybe_schedule(db, force=True)
    if task is None:
        raise HTTPException(500, "failed to schedule publish task")
    state.publisher.run_due_tasks(db)
    db.refresh(task)
    return {"task_id": task.id, "status": task.status, "error": task.error}


@router.get("/stream")
async def stream(request: Request, last_id: int = 0, db: Session = Depends(get_session)):
    _require_state()
    bus = state.bus
    q = bus.subscribe()
    replayed = (
        db.query(models.Event).filter(models.Event.seq > last_id).order_by(models.Event.seq).limit(1000).all()
    )

    async def gen():
        try:
            for e in replayed:
                yield {"event": "event", "data": json.dumps(service.serialize_event(state.gazetteer, e), ensure_ascii=False)}
            seen = replayed[-1].seq if replayed else last_id
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = q.get_nowait()
                except queue_mod.Empty:
                    await asyncio.sleep(0.2)
                    continue
                if item["seq"] <= seen:
                    continue
                seen = item["seq"]
                yield {"event": "event", "data": json.dumps(item["data"], ensure_ascii=False)}
        finally:
            bus.unsubscribe(q)

    return EventSourceResponse(gen())


def add_cors(app):
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
