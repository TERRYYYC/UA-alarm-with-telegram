"""扫描历史报文,回填"待补地名"列表(不重放事件、不调地理编码)。

用法(backend 目录):
    .venv/bin/python scripts/scan_unknown_places.py [--limit N]

规则与实时采集一致:只扫描含事件类型关键词的报文,聚合未收录专名。
管理端"地名库 → 待补地名"页签看到的就是这份结果。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, models  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.extractor import analyze_message  # noqa: E402
from app.gazetteer import Gazetteer  # noqa: E402
from app.service import _record_unknown_places, _suspect_tokens  # noqa: E402


def build_gazetteer() -> Gazetteer:
    gaz = Gazetteer()
    gaz.load_json(config.GAZETTEER_SEED)
    uk = os.path.join(os.path.dirname(config.GAZETTEER_SEED), "gazetteer_uk.json")
    if os.path.exists(uk):
        gaz.load_json(uk)
    zh = os.path.join(os.path.dirname(config.GAZETTEER_SEED), "place_names_zh.json")
    if os.path.exists(zh):
        gaz.apply_zh_names(zh)
    zgn = os.path.join(os.path.dirname(config.GAZETTEER_SEED), "place_names_zh_gn.json")
    if os.path.exists(zgn):
        gaz.apply_zh_geonames(zgn)
    return gaz


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只扫描最近 N 条(0 = 全部)")
    args = ap.parse_args()

    gaz = build_gazetteer()
    biases: dict[int, str | None] = {}
    scanned = 0
    suspects_total = 0
    with SessionLocal() as db:
        for s in db.query(models.Source).all():
            biases[s.id] = json.loads(s.config_json or "{}").get("admin1_bias")
        q = db.query(models.RawMessage).order_by(models.RawMessage.id)
        rows = q.all()
        if args.limit:
            rows = rows[-args.limit :]
        for raw in rows:
            analysis = analyze_message(raw.body_text, raw.published_at)
            if not analysis["types"] or analysis["negated"] or analysis["retracted"]:
                continue
            suspects = _suspect_tokens(gaz, raw.body_text, admin_bias=biases.get(raw.source_id))
            scanned += 1
            if not suspects:
                continue
            suspects_total += len(suspects)
            _record_unknown_places(db, raw, suspects, raw.source_id)
            db.commit()
    with SessionLocal() as db:
        open_rows = db.query(models.UnknownPlace).filter_by(status="open").count()
        top = (
            db.query(models.UnknownPlace)
            .filter_by(status="open")
            .order_by(models.UnknownPlace.report_count.desc())
            .limit(15)
            .all()
        )
    print(f"扫描报文 {scanned} 条,记录未收录专名 {suspects_total} 次;当前待补 {open_rows} 个")
    for r in top:
        print(f"  {r.report_count:>3} 次  {r.display_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
