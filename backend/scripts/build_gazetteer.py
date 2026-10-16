"""构建真实乌克兰地名表 data/gazetteer_uk.json。

分层:
- GeoNames UA dump(CC-BY-4.0):feature class P 全部聚落,多语别名(拉/乌/俄/英等)
- VIINA/KATOTTH tessellation(ODbL):官方乌克兰语聚落名 + 质心,按距离 ≤1.5km 关联,
  未关联的 KATOTTH 聚落独立入库
- 种子(gazetteer_seed.json,人工精选)优先:冲突条目跳过,保证精选别名唯一

用法: .venv/bin/python scripts/build_gazetteer.py
"""

from __future__ import annotations

import io
import json
import sys
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.gazetteer import normalize

BACKEND = Path(__file__).resolve().parent.parent
SEED = BACKEND / "data" / "gazetteer_seed.json"
DEST = BACKEND / "data" / "gazetteer_uk.json"
URL = "https://download.geonames.org/export/dump/UA.zip"
KATOTTH_URL = "https://raw.githubusercontent.com/zhukovyuri/VIINA/main/Data/katotth_UA_tess.geojson"
JOIN_KM = 1.5
NEXT_ID_BASE = 100_000_000

# GeoNames UA 的 admin1 编码 → 乌克兰语州名(从 dump 的 ADM1 行与官方区划核对)
ADMIN1_UK = {
    "01": "Черкаська область", "02": "Чернігівська область", "03": "Чернівецька область",
    "04": "Дніпропетровська область", "05": "Донецька область", "06": "Івано-Франківська область",
    "07": "Харківська область", "08": "Херсонська область", "09": "Хмельницька область",
    "10": "Кіровоградська область", "11": "Автономна Республіка Крим", "12": "Київ",
    "13": "Київська область", "14": "Луганська область", "15": "Львівська область",
    "16": "Миколаївська область", "17": "Одеська область", "18": "Полтавська область",
    "19": "Рівненська область", "20": "Севастополь", "21": "Сумська область",
    "22": "Тернопільська область", "23": "Вінницька область", "24": "Волинська область",
    "25": "Закарпатська область", "26": "Запорізька область", "27": "Житомирська область",
}
SETTLEMENT_TYPES = {"village", "hamlet", "town", "city", "locality", "isolated_dwelling"}


def _is_cyrillic(s: str) -> bool:
    return any(ord(c) > 0x400 for c in s)


def load_katotth() -> tuple[list[dict], dict]:
    print("下载 KATOTTH...")
    data = json.loads(urllib.request.urlopen(KATOTTH_URL, timeout=300).read().decode("utf-8"))
    points = []
    for f in data["features"]:
        p = f["properties"]
        if p.get("admin_level") not in SETTLEMENT_TYPES:
            continue
        try:
            lat, lng = float(p["latitude"]), float(p["longitude"])
        except (TypeError, ValueError):
            continue
        name = (p.get("name") or "").strip()
        if not name:
            continue
        points.append({"name": name, "lat": lat, "lng": lng, "admin1": p.get("ADM1_NAME_ALT")})

    grid = defaultdict(list)
    for i, pt in enumerate(points):
        grid[(int(pt["lat"] * 50), int(pt["lng"] * 50))].append(i)

    def nearest(lat: float, lng: float) -> tuple[float, int] | None:
        best = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in grid.get((int(lat * 50) + dx, int(lng * 50) + dy), ()):
                    pt = points[i]
                    d = ((pt["lat"] - lat) * 111) ** 2 + ((pt["lng"] - lng) * 111 * 0.6) ** 2
                    if best is None or d < best[0]:
                        best = (d, i)
        if best is not None and best[0] <= JOIN_KM**2:
            return best
        return None

    return points, nearest


def main() -> int:
    print("下载", URL)
    data = urllib.request.urlopen(URL, timeout=180).read()
    zf = zipfile.ZipFile(io.BytesIO(data))
    txt_name = next(n for n in zf.namelist() if n.endswith(".txt") and "readme" not in n.lower())

    seed_keys = set()
    for entry in json.loads(SEED.read_text(encoding="utf-8")):
        for n in entry["names"]:
            seed_keys.add(normalize(n))

    kat_points, nearest = load_katotth()
    kat_used: set[int] = set()

    out = []
    for line in zf.read(txt_name).decode("utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        cols = line.split("\t")
        if len(cols) < 19 or cols[6] != "P":
            continue
        names = []
        for x in [cols[1], cols[2]] + (cols[3].split(",") if cols[3] else []):
            x = x.strip()
            if x and x not in names:
                names.append(x)
        if not names:
            continue

        lat, lng = float(cols[4]), float(cols[5])
        canonical = names[0]
        hit = nearest(lat, lng)
        if hit is not None:
            _, ki = hit
            uk_name = kat_points[ki]["name"]
            kat_used.add(ki)
            if uk_name and uk_name not in names:
                names.append(uk_name)
            canonical = uk_name

        # 碰撞检查放在乌语名加入之后,避免拉丁条目漏网
        if any(normalize(n) in seed_keys for n in names):
            continue  # 与精选种子冲突,种子优先

        out.append(
            {
                "id": int(cols[0]),
                "canonical_name": canonical,
                "names": names,
                "lat": lat,
                "lng": lng,
                "admin1": ADMIN1_UK.get(cols[10]),
                "precision": "settlement",
                "source": "geonames",
            }
        )

    # 未关联到 GeoNames 的 KATOTTH 聚落独立入库
    next_id = NEXT_ID_BASE
    added_kat = 0
    for i, pt in enumerate(kat_points):
        if i in kat_used:
            continue
        if normalize(pt["name"]) in seed_keys:
            continue
        out.append(
            {
                "id": next_id,
                "canonical_name": pt["name"],
                "names": [pt["name"]],
                "lat": pt["lat"],
                "lng": pt["lng"],
                "admin1": pt["admin1"],
                "precision": "settlement",
                "source": "katotth",
            }
        )
        next_id += 1
        added_kat += 1

    DEST.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"GeoNames 聚落: {len(out) - added_kat},KATOTTH 补充: {added_kat} → 共 {len(out)} 条")
    print(f"写入 {DEST}({DEST.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
