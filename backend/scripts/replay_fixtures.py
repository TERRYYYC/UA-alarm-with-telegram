"""把 fixtures/sample_messages.jsonl 重放到运行中的 API。

默认把样本基准时间(2026-09-07T12:00:00Z)平移到当前时刻,便于本地演示;
样本本身仍是合成数据,UI 上必须保留"非真实事件"标识。

用法:
  .venv/bin/python scripts/replay_fixtures.py [--api http://127.0.0.1:8000] [--keep-base]
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import request

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures" / "sample_messages.jsonl"
BASE_TIME = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--keep-base", action="store_true", help="保持样本原始时间(2026-09-07),不平移到当前时刻")
    args = ap.parse_args()

    shift = timedelta(0) if args.keep_base else (datetime.now(timezone.utc) - BASE_TIME)

    ok = dup = fail = 0
    for line in FIXTURES.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        published = datetime.fromisoformat(item["published_at"].replace("Z", "+00:00"))
        item["published_at"] = (published + shift).isoformat().replace("+00:00", "Z")
        req = request.Request(
            f"{args.api}/api/v1/ingest",
            data=json.dumps(item).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            print(f"FAIL  {item['external_id']} rev{item['revision']}: {exc}")
            fail += 1
            continue
        status = result.get("status", "?")
        if status == "duplicate":
            dup += 1
        elif status in ("ok", "retracted"):
            ok += 1
        else:
            fail += 1
        print(f"{status:<10} {item['external_id']} rev{item['revision']}")

    print(f"\n共 {ok + dup + fail} 条:ok={ok}, duplicate={dup}, fail={fail}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
