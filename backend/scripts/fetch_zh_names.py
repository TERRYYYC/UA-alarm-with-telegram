"""从 Wikidata 抓取乌克兰地点的中文标签(按 GeoNames ID 关联)。

用于把大众地名库(GeoNames/KATOTTH)的显示名本地化为中文;
识别仍以乌克兰语原文为准,中文只作用于显示。

用法: .venv/bin/python scripts/fetch_zh_names.py
输出: data/place_names_zh_gn.json  {"<geonameid>": "中文名", ...}
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
DEST = BACKEND / "data" / "place_names_zh_gn.json"
ENDPOINT = "https://query.wikidata.org/sparql"
QUERY = """
SELECT ?gn ?label WHERE {
  ?item wdt:P1566 ?gn .
  ?item wdt:P17 wd:Q212 .
  ?item rdfs:label ?label .
  FILTER(LANG(?label) IN ("zh","zh-hans","zh-cn","zh-hk","zh-tw","zh-sg"))
}
"""

# 简体优先
LANG_PRIORITY = ["zh-hans", "zh-cn", "zh-sg", "zh", "zh-hk", "zh-tw"]


def _to_simplified(text: str) -> str:
    """繁体一律转简体(opencc 可选依赖;缺失时原样返回)。"""
    try:
        from opencc import OpenCC

        return OpenCC("t2s").convert(text)
    except Exception:
        return text


def main() -> int:
    url = f"{ENDPOINT}?{urllib.parse.urlencode({'query': QUERY, 'format': 'json'})}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/sparql-results+json",
            "User-Agent": "situation-monitor/0.1 (local war-monitoring research)",
        },
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    rows = data["results"]["bindings"]

    best: dict[str, tuple[int, str]] = {}
    for r in rows:
        gn = r["gn"]["value"]
        label = r["label"]["value"]
        lang = r["label"].get("xml:lang", "zh")
        rank = LANG_PRIORITY.index(lang) if lang in LANG_PRIORITY else len(LANG_PRIORITY)
        cur = best.get(gn)
        if cur is None or rank < cur[0]:
            best[gn] = (rank, label)

    out = {gn: _to_simplified(v[1]) for gn, v in best.items()}
    DEST.write_text(json.dumps(out, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"写入 {len(out)} 条中文标签 → {DEST}（{DEST.stat().st_size / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
