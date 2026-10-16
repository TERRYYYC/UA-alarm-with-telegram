from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache

from rapidfuzz import fuzz

from . import config

PLACE_TOKEN_RE = re.compile(r"[А-Яа-яЁёҐґІіЇїЄєA-Za-z'’\-]+")
_CYR_ONLY = re.compile(r"^[а-яіїєґ'’-]+$")

# pymorphy3 乌克兰语词形还原(可选依赖,缺失时降级为 None)
_morph = None
_morph_failed = False


@lru_cache(maxsize=200000)
def _lemmatize(n: str) -> str | None:
    global _morph, _morph_failed
    if _morph_failed:
        return None
    if _morph is None:
        try:
            from pymorphy3 import MorphAnalyzer

            _morph = MorphAnalyzer(lang="uk")
        except Exception:
            _morph_failed = True
            return None
    # 重组组合字符(нормализованный 输入可能是 NFKD 分解态),pymorphy 需要 NFC
    n = unicodedata.normalize("NFC", n)
    if not _CYR_ONLY.fullmatch(n):
        return None
    try:
        return _morph.parse(n)[0].normal_form
    except Exception:
        return None


# 行政名形容词/名词词干(用于上下文消歧:"Одеська" 与 "Одеській" 同干)
_ADJ_SUFFIXES = (
    "ської", "ській", "ського", "ська", "ське", "ський", "ськ",
    "зької", "зькій", "зького", "зька", "зьке", "зький", "зьк",
    "цької", "цькій", "цького", "цька", "цьке", "цький", "цьк",
    "ької", "ькій", "ького", "ька", "ьке", "ький", "ьк",
    "ої", "ій", "ого", "а", "о", "і", "й", "е", "ї",
)


def _stem(s: str) -> str:
    for suf in _ADJ_SUFFIXES:
        if s.endswith(suf) and len(s) - len(suf) >= 4:
            return s[: len(s) - len(suf)]
    return s


def _admin_stems(admin1: str) -> set[str]:
    out = set()
    for t in (admin1 or "").split():
        n = normalize(t)
        if not n:
            continue
        lemma = _lemmatize(n) or n
        out.add(_stem(lemma))
    return out


def _ctx_stems(context: str) -> set[str]:
    out = set()
    # 含组合符范围:normalize 后 й/ї 可能处于 NFKD 分解态
    for t in re.findall(r"[а-яіїєґ'\u0300-\u036f-]+", normalize(context)):
        lemma = _lemmatize(t) or t
        out.add(_stem(lemma))
    return out


# 拉丁/西里尔同形字符折叠(消息中常见混写,如 "БПЛA" 的 A 是拉丁 A)
_LATIN_FOLD = str.maketrans(
    {
        "a": "а", "b": "в", "c": "с", "e": "е", "i": "і", "k": "к",
        "m": "м", "o": "о", "p": "р", "t": "т", "x": "х", "y": "у",
    }
)


def normalize(s: str) -> str:
    s = s.lower().strip()
    s = s.translate(_LATIN_FOLD)
    s = s.replace("ё", "е").replace("ґ", "г")
    s = s.replace("’", "'").replace("`", "'")
    out = []
    for ch in unicodedata.normalize("NFKD", s):
        if unicodedata.combining(ch):
            # 西里尔组合符必须保留(й = и + 短音符,ї = і + 分音符),
            # 否则 й/ї 会被错误降级成 и/і
            if out and ("\u0430" <= out[-1] <= "\u044f" or out[-1] in "\u0456\u0457\u0454\u0491"):
                out.append(ch)
            continue
        out.append(ch)
    # 重组为 NFC:别名与查询两侧一致,且正则(відбій 等)可匹配
    return unicodedata.normalize("NFC", " ".join("".join(out).split()))


# 自然/纪念物名称特征(KATOTTH 中的 locality 类,如 "Два вікові дуби")
_NATURAL_PREFIXES = (
    "дуб", "могил", "курган", "урочищ", "заповід", "парк", "гай", "балк",
    "криниц", "джерел", "скел", "печер", "яр", "гор", "ліс", "лук", "острів", "сад", "ставок",
    "колон", "пам'ятник", "монумент",
)

# 描述性短语特征(如 "... - міжнародний пункт пропуску та контролю через кордон")
_DESCRIPTIVE_WORDS = ("пункт", "пропуск", "контрол", "кордон", "митниц", "перехід", "дільниц")

# 通用词/截断名/国名变体:大众地名库中不得作为可上报地点
_EXCLUDED_NAMES = {
    "нова", "нове", "новий", "стара", "старе", "старий",
    "україна", "ukrayina", "ukraina", "украина",
    "росія", "russia", "рф",
}
_EXCLUDED_NORM = {normalize(n) for n in _EXCLUDED_NAMES}


def _is_natural_feature(name: str) -> bool:
    words = normalize(name).split()
    if len(words) < 3:
        return False
    return any(w.startswith(_NATURAL_PREFIXES) for w in words)


def _is_descriptive_phrase(name: str) -> bool:
    words = normalize(name).split()
    if len(words) >= 4:
        return True
    return any(w.startswith(_DESCRIPTIVE_WORDS) for w in words)


# 形容词的格词尾(长词尾优先);词干必须保留 -ськ/-цьк/-зьк,避免把普通名词当形容词
_ADJ_ENDINGS = (
    "ого", "ому", "ими", "ий", "ій", "ої", "ою", "им", "ім", "а", "я", "е", "є", "у", "ю", "і",
)
_ADJ_STEMS = ("ськ", "цьк", "зьк")


def _adjective_stem(token: str) -> str | None:
    """去掉形容词格词尾得到词干;不是"州名式形容词"返回 None。"""
    for end in _ADJ_ENDINGS:
        if token.endswith(end):
            stem = token[: -len(end)]
            if len(stem) >= 5 and stem.endswith(_ADJ_STEMS):
                return stem
            return None
    return None


def is_reportable(name: str | None) -> bool:
    """该名称能否作为可上报地点(自然地貌/纪念物/描述性短语/通用词都不行)。

    大众地名库与自动地理编码结果必须过这一关;管理端手工维护的地点不受限。
    """
    if not name or not name.strip():
        return False
    return not (
        _is_natural_feature(name)
        or _is_descriptive_phrase(name)
        or name.strip().lower() in _EXCLUDED_NAMES
        or normalize(name) in _EXCLUDED_NORM
    )


class Gazetteer:
    """内存地名目录:别名 → 地点 ID,支持模糊匹配与上下文消歧。"""
    def __init__(self):
        self._alias_to_ids: dict[str, list[int]] = {}
        self._places: dict[int, dict] = {}
        self._buckets: dict[tuple[str, int], list[tuple[str, list[int]]]] = {}
        self._zh: dict[str, str] = {}
        # 别名的原始书写形式(管理端编辑时显示,规范化键不可读)
        self._names: dict[int, list[str]] = {}

    def apply_zh_names(self, path: str):
        """加载中文地名映射(键为乌克兰语规范名),写入各地点 name_zh。"""
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self._zh = {normalize(k): v for k, v in data.items()}
        for p in self._places.values():
            zh = self._zh.get(normalize(p["canonical_name"]))
            if zh:
                p["name_zh"] = zh

    def zh_for(self, name: str | None) -> str | None:
        """按乌克兰语名称查中文(含行政州名)。"""
        if not name:
            return None
        return self._zh.get(normalize(name))

    def apply_zh_geonames(self, path: str):
        """按 GeoNames ID 应用 Wikidata 中文标签(大众地名库显示本地化)。

        无 GeoNames ID 的 KATOTTH 补充条目按"同名 + 同州"继承标签。
        """
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        by_id = {int(k): v for k, v in data.items()}
        applied = 0
        name_map: dict[str, list[tuple[str, str | None]]] = {}
        for pid, p in self._places.items():
            zh = by_id.get(pid)
            if zh:
                if "name_zh" not in p:
                    p["name_zh"] = zh
                    applied += 1
                name_map.setdefault(normalize(p["canonical_name"]), []).append((zh, p.get("admin1")))
        for p in self._places.values():
            if "name_zh" in p:
                continue
            candidates = name_map.get(normalize(p["canonical_name"]))
            if not candidates:
                continue
            zh_values = {c[0] for c in candidates}
            if len(zh_values) != 1:
                continue  # 同名多译名,无法安全继承
            if p.get("admin1") and not any(c[1] == p.get("admin1") for c in candidates):
                continue  # 同名但不同州,不继承
            p["name_zh"] = candidates[0][0]
            applied += 1
        return applied

    def load_json(self, path: str):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for item in data:
            pid = int(item["id"])
            if item.get("source") in ("katotth", "geonames") and not is_reportable(
                item["canonical_name"]
            ):
                continue  # 自然/纪念物/描述性/通用名称不是可上报地点
            self._places[pid] = {
                "id": pid,
                "canonical_name": item["canonical_name"],
                "lat": float(item["lat"]),
                "lng": float(item["lng"]),
                "admin1": item.get("admin1"),
                "admin2": item.get("admin2"),
                "precision": item.get("precision", "settlement"),
                "source": item.get("source", ""),
            }
            for a in item["names"]:
                n = normalize(a)
                if n:
                    ids = self._alias_to_ids.setdefault(n, [])
                    ids.append(pid)
                    # 模糊匹配分桶(首字符 + 长度),避免每次遍历全量别名
                    self._buckets.setdefault((n[0], len(n)), []).append((n, ids))
                    self._names.setdefault(pid, []).append(a)

    def get(self, place_id: int) -> dict | None:
        return self._places.get(place_id)

    def add_place(
        self,
        place_id: int,
        canonical_name: str,
        names: list[str],
        lat: float,
        lng: float,
        admin1: str | None = None,
        admin2: str | None = None,
        precision: str = "settlement",
    ):
        """运行时新增地点(自动地理编码结果),更新内存索引。"""
        if not is_reportable(canonical_name):
            return  # 自然/纪念物/描述性/通用名称不作为可上报地点
        self._places[place_id] = {
            "id": place_id,
            "canonical_name": canonical_name,
            "lat": lat,
            "lng": lng,
            "admin1": admin1,
            "admin2": admin2,
            "precision": precision,
            "source": "nominatim",
        }
        for a in names:
            n = normalize(a)
            if n:
                ids = self._alias_to_ids.setdefault(n, [])
                if place_id not in ids:
                    ids.append(place_id)
                self._buckets.setdefault((n[0], len(n)), []).append((n, ids))
        self._names[place_id] = [a for a in names if a and a.strip()]

    def __len__(self) -> int:
        return len(self._places)

    def all_places(self) -> list[dict]:
        return [
            {
                "id": p["id"],
                "name": p["canonical_name"],
                "name_zh": p.get("name_zh"),
                "lat": p["lat"],
                "lng": p["lng"],
                "admin1": p["admin1"],
                "admin2": p["admin2"],
                "precision": p["precision"],
                "source": p.get("source", ""),
            }
            for p in sorted(self._places.values(), key=lambda x: x["canonical_name"].lower())
        ]

    def remove_place(self, place_id: int):
        """从目录移除地点(如自动地理编码留下的噪音条目)。不存在则忽略。"""
        if place_id not in self._places:
            return
        del self._places[place_id]
        self._names.pop(place_id, None)
        for ids in self._alias_to_ids.values():
            if place_id in ids:
                ids.remove(place_id)

    def aliases_for(self, place_id: int) -> list[str]:
        """该地点的全部别名原始书写形式(管理端编辑用)。"""
        return list(self._names.get(place_id, []))

    def place_detail(self, place_id: int) -> dict | None:
        p = self._places.get(place_id)
        if p is None:
            return None
        return {
            "id": p["id"],
            "name": p["canonical_name"],
            "name_zh": p.get("name_zh"),
            "names": self.aliases_for(place_id),
            "lat": p["lat"],
            "lng": p["lng"],
            "admin1": p["admin1"],
            "admin2": p["admin2"],
            "precision": p["precision"],
            "source": p.get("source", ""),
        }

    def upsert_place(
        self,
        place_id: int,
        canonical_name: str,
        names: list[str],
        lat: float,
        lng: float,
        admin1: str | None = None,
        admin2: str | None = None,
        precision: str = "settlement",
        source: str = "admin",
        name_zh: str | None = None,
    ):
        """新增或整体替换地点(管理端编辑);旧别名一并清理。"""
        # 清理旧别名引用(列表对象被 buckets 共享,原地移除即可)
        for ids in self._alias_to_ids.values():
            if place_id in ids:
                ids.remove(place_id)
        entry = {
            "id": place_id,
            "canonical_name": canonical_name,
            "lat": lat,
            "lng": lng,
            "admin1": admin1,
            "admin2": admin2,
            "precision": precision,
            "source": source,
        }
        if name_zh:
            entry["name_zh"] = name_zh
        self._places[place_id] = entry
        for a in names:
            n = normalize(a)
            if n:
                ids = self._alias_to_ids.setdefault(n, [])
                if place_id not in ids:
                    ids.append(place_id)
                self._buckets.setdefault((n[0], len(n)), []).append((n, ids))
        self._names[place_id] = [a for a in names if a and a.strip()]

    def _region_by_adjective(self, token: str) -> int | None:
        """州名的形容词变格形式 → 州("Одеській"/"Черкаської" → Одеська/Черкаська область)。

        战况报文里州名极常以形容词出现("з Черкаської"、"на Одещині" 之外的形式),
        逐个补别名不可持续。这里按词干前缀匹配州名,只在唯一命中时采纳;
        俄罗斯州名("Курська")在目录里没有对应州,自然不会被误挂。
        """
        if " " in token or len(token) < 7:
            return None
        stem = _adjective_stem(token)
        if stem is None:
            return None
        hits = [
            p["id"]
            for p in self._places.values()
            if p.get("precision") == "region" and normalize(p["canonical_name"]).startswith(stem)
        ]
        return hits[0] if len(hits) == 1 else None

    def resolve(self, raw: str, context: str = "", admin_bias: str | None = None) -> dict | None:
        """返回 {place_id, fuzzy, ambiguous_ids?};无法解析返回 None。

        admin_bias: 来源级州级先验(如 Київська область),同名歧义且消息上下文
        不足以消歧时,优先该州候选。
        """
        n = normalize(raw)
        if not n:
            return None
        ids = self._alias_to_ids.get(n)
        fuzzy = False
        if not ids:
            # 乌克兰语词形还原:"Херсона" → "херсон"(确定性匹配,不算模糊)
            lemma = _lemmatize(n)
            if lemma and lemma != n:
                ids = self._alias_to_ids.get(lemma)
        if not ids and " " not in n:
            # 模糊匹配仅限单 token(拼写变体);多词短语必须精确命中别名,
            # 否则会把 "у Краматорську" 这类词组误匹配成单地名
            best = None
            for length in range(max(1, len(n) - 2), len(n) + 3):
                for alias, alias_ids in self._buckets.get((n[0], length), ()):
                    # ratio 而非 token_set_ratio:后者对"包含地名词的短语"恒为 100,导致词组误匹配
                    s = fuzz.ratio(n, alias)
                    if s >= config.FUZZY_THRESHOLD and (best is None or s > best[0]):
                        best = (s, alias_ids)
            if best:
                ids, fuzzy = best[1], True
        if not ids:
            region = self._region_by_adjective(n)
            if region is not None:
                return {"place_id": region, "fuzzy": False}
            return None
        uniq = sorted(set(ids))
        if len(uniq) > 1:
            # 区域实体优先(州名与同名村庄歧义时,如 "Харківщина")
            regions = [pid for pid in uniq if self._places[pid].get("precision") == "region"]
            if len(regions) == 1:
                return {"place_id": regions[0], "fuzzy": fuzzy}
            ctx_stems = _ctx_stems(context)

            def _ctx_match(pid: int) -> bool:
                for key in ("admin1", "admin2"):
                    v = self._places[pid].get(key)
                    if v and _admin_stems(v).issubset(ctx_stems):
                        return True
                return False

            filtered = [pid for pid in uniq if _ctx_match(pid)]
            if len(filtered) == 1:
                return {"place_id": filtered[0], "fuzzy": fuzzy}
            if not filtered and admin_bias:
                bias_stems = _admin_stems(admin_bias)

                def _bias_match(pid: int) -> bool:
                    for key in ("admin1", "admin2"):
                        v = self._places[pid].get(key)
                        if v and _admin_stems(v).issubset(bias_stems):
                            return True
                    return False

                biased = [pid for pid in uniq if _bias_match(pid)]
                if len(biased) == 1:
                    return {"place_id": biased[0], "fuzzy": fuzzy}
                if len(biased) > 1:
                    # 同州多个同名:优先精选条目,其次城市/镇级,再按 id
                    for pref_src in ("curated",):
                        picks = [pid for pid in biased if self._places[pid].get("source") == pref_src]
                        if picks:
                            return {"place_id": picks[0], "fuzzy": fuzzy}
                    for pref_prec in ("city", "town"):
                        picks = [pid for pid in biased if self._places[pid].get("precision") == pref_prec]
                        if picks:
                            return {"place_id": picks[0], "fuzzy": fuzzy}
                    return {"place_id": biased[0], "fuzzy": fuzzy}
            # 上下文仍无法消歧时,形容词形式的州名按州理解("Донецькій" 优先于同名小村庄)
            region = self._region_by_adjective(n)
            if region is not None:
                return {"place_id": region, "fuzzy": fuzzy}
            return {"place_id": None, "fuzzy": fuzzy, "ambiguous_ids": uniq}
        return {"place_id": uniq[0], "fuzzy": fuzzy}
