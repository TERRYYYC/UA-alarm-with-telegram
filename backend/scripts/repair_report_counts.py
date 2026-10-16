"""修复历史数据里"同一条消息的多个修订被当成多条报道"的问题。

现象:Telegram 消息被编辑后会产生 revision 2/3 的独立原始记录,旧逻辑把它们
各算一条报道 —— 报道数虚高,单频道一条消息编辑两次就能凑到 ≥3 报而"自动确认"。

本脚本(默认 dry-run):
  1. 每个事件的同族证据(来源 + external_id)只保留指向最新修订的一行;
  2. 按"消息族"重算 report_count;
  3. 对不再满足印证阈值、且没有人工判定记录的 auto_confirmed 事件,
     降回 unverified 并写入修订记录(人工 confirmed/rejected 不动)。

用法(backend 目录):
    .venv/bin/python scripts/repair_report_counts.py            # 只报告
    .venv/bin/python scripts/repair_report_counts.py --apply    # 落库
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, models  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.service import iso_utc, utcnow_naive  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库(默认只报告)")
    args = ap.parse_args()

    with SessionLocal() as db:
        rows = (
            db.query(
                models.Evidence.id,
                models.Evidence.event_id,
                models.RawMessage.source_id,
                models.RawMessage.external_id,
                models.RawMessage.revision,
                models.RawMessage.id,
            )
            .join(models.RawMessage, models.RawMessage.id == models.Evidence.message_id)
            .all()
        )
        families: dict[tuple[int, int, str], list[tuple[int, int]]] = collections.defaultdict(list)
        for ev_id, event_id, source_id, external_id, revision, message_id in rows:
            families[(event_id, source_id, external_id)].append((revision, message_id))

        drop_evidence: list[int] = []
        repoint: list[tuple[int, int]] = []
        for (event_id, _sid, _ext), items in families.items():
            if len(items) < 2:
                continue
            items.sort()
            keep_rev, keep_mid = items[-1]
            for _rev, mid in items[:-1]:
                ev_row = next(r for r in rows if r[0] and r[5] == mid and r[1] == event_id)
                drop_evidence.append(ev_row[0])
            if keep_mid is not None:
                del keep_rev
                repoint.append((event_id, keep_mid))

        report_counts: dict[int, int] = collections.Counter()
        for (event_id, _sid, _ext) in families:
            report_counts[event_id] += 1

        changed_reports = 0
        demote: list[int] = []
        for e in db.query(models.Event).all():
            real = report_counts.get(e.id, 0)
            if e.report_count != real:
                changed_reports += 1
                if not args.apply:
                    print(f"  #{e.id} report_count {e.report_count} → {real}")
                e.report_count = real
            if e.verification != "auto_confirmed":
                continue
            reasons = json.loads(e.reason_json or "[]")
            manual = any(r.get("operator") and r.get("operator") != "auto" for r in reasons)
            fam = [k for k in families if k[0] == e.id]
            sources = len({k[1] for k in fam})
            if manual:
                continue
            if sources >= config.AUTO_CONFIRM_MIN_SOURCES or real >= config.AUTO_CONFIRM_MIN_REPORTS:
                continue
            demote.append(e.id)
            if args.apply:
                e.verification = "unverified"
                reasons.append(
                    {
                        "at": iso_utc(utcnow_naive()),
                        "action": "auto_confirm_revoked",
                        "reason": f"同一条消息的修订曾被重复计数:真实报道 {real} 条 / 独立来源 {sources} 个",
                    }
                )
                e.reason_json = json.dumps(reasons, ensure_ascii=False)

        dup_evidence = len(drop_evidence)
        if args.apply:
            db.query(models.Evidence).filter(models.Evidence.id.in_(drop_evidence)).delete(
                synchronize_session=False
            )
            db.commit()

        print(f"重复计数的证据行:{dup_evidence} 条(将删除,保留最新修订那一行)")
        print(f"report_count 被修正的事件:{changed_reports} 个")
        print(f"因修订虚高而错误 auto_confirmed、需降回未核实的事件:{len(demote)} 个 {demote[:10]}")
        if not args.apply:
            print("(dry-run;加 --apply 才会写库)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
