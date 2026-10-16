import re
from datetime import datetime, timedelta

from .gazetteer import normalize as _fold

TYPE_PATTERNS = {
    "shelling": r"\b(обстріл|обстрел|артобстріл|артобстрел|міномет|миномет|приліт|прильот|прилёт|прилет|артилер)\w*\b",
    "explosion": r"\b(вибух|взрыв|детонац|підрив|подрыв|гучн\w*|бавовн\w*)\w*\b",
    "drone": r"\b(дрон|шахед|shahed|бпла|fpv|беспилотн|бєспілотн|безпілотн)\w*\b",
    "missile": r"\b(ракет|іскандер|искандер|с-300|крилат|баліст|баллист|бандерол)\w*\b",
    "air_alert": r"\b(тривог\w*|повітрян\w* тривог\w*)\b",
}

NEGATION = re.compile(
    r"(?:\b(тихо|відбій|отбой|нет)\b"
    r"|\bне (було|був|була|були|зафіксовано|зафиксировано|спостерігається|спостерігалось|наблюдалось|наблюдаются)\b"
    r"|\bбез обстріл\w*)",
    re.I,
)

RETRACT = re.compile(
    r"\b(помилков\w*|ошибочн\w*|спростов\w*|спростуванн\w*|опроверг\w*|відклик\w*|отзыв\w*)",
    re.I,
)

# 警报解除(отбой/відбій):不产生新事件,用于结束进行中的 air_alert
ALL_CLEAR = re.compile(
    r"\b(відбій|отбой|відбій тривоги|отбой тревоги|скасування тривоги|скасовано тривогу|чисто в області)\b",
    re.I,
)

REL_MIN = re.compile(r"(\d+)\s*(хв|хвилин|минут|мин|分钟)\w*\s*(тому|назад)?", re.I)
NOW_WORDS = re.compile(r"\b(щойно|тільки що|только что|зараз|сейчас|прямо зараз)\b", re.I)
CLOCK = re.compile(r"(?:о|в|у)\s+(\d{1,2})[:.](\d{2})", re.I)
NIGHT = re.compile(r"\b(вночі|ночью)\b", re.I)
YESTERDAY = re.compile(r"\b(вчора|вчера)\b", re.I)

_NOISE_PARTS = list(TYPE_PATTERNS.values()) + [
    NEGATION.pattern,
    RETRACT.pattern,
    r"\b(хвилин\w*|минут\w*|тому|назад|сьогодні|сегодня|вчора|вчера|вночі|ночью|зараз|щойно|сейчас|було|був|була|були|повідомлення|сообщение)\b",
]
NOISE = re.compile("|".join(_NOISE_PARTS), re.I)

# 行政层级词:出现在其邻接位置的地名 token 不作为待定位项(如 "Херсонська область")
ADMIN_WORD = re.compile(r"\b(област\w*|обл|район\w*|р-н|напрям\w*|направлен\w*|громада\w*)\b", re.I)


def is_noise_token(token: str) -> bool:
    return bool(NOISE.search(_fold(token)))


# 事件类型关键词(обстріл/приліт/вибух…),与 NOISE 里的时间词区分开
_EVENT_KEYWORD = re.compile("|".join(TYPE_PATTERNS.values()), re.I)


def is_event_keyword(token: str) -> bool:
    """"Обстріл Херсона" 这类无介词结构里,前一个词是不是事件动词。"""
    return bool(_EVENT_KEYWORD.search(_fold(token)))


def parse_time(text: str, published_at: datetime) -> tuple[datetime, int, str]:
    """返回 (occurred_at, uncertainty_seconds, time_basis)。所有时间为 naive UTC。"""
    if NOW_WORDS.search(text):
        return published_at, 120, "now"
    m = REL_MIN.search(text)
    if m:
        minutes = int(m.group(1))
        return published_at - timedelta(minutes=minutes), 300, "relative_minutes"
    m = CLOCK.search(text)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            dt = published_at.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if dt > published_at:
                dt -= timedelta(days=1)
            return dt, 1800, "clock"
    if NIGHT.search(text):
        prev = (published_at - timedelta(days=1)).replace(hour=23, minute=30, second=0, microsecond=0)
        return prev, 36000, "night"
    if YESTERDAY.search(text):
        return published_at - timedelta(days=1), 43200, "yesterday"
    return published_at, 3600, "published_at"


PRYLIT_ONLY = re.compile(r"\b(приліт|прильот|прилёт|прилет)\w*\b", re.I)


def analyze_message(text: str, published_at: datetime) -> dict:
    folded = _fold(text)  # 拉丁/西里尔混写折叠后做模式匹配
    types = [t for t, pat in TYPE_PATTERNS.items() if re.search(pat, folded, re.I)]
    if "shelling" in types and PRYLIT_ONLY.search(folded):
        # "приліт" 自身不携带具体手段;消息同时给出具体手段(drone/missile 等)时,以具体类型为准
        specific = [t for t in types if t != "shelling"]
        if specific:
            types = specific
    if not types and "💥" in folded:
        # 频道爆炸表情(无文字时)
        types = ["explosion"]
    if not types and "🚀" in folded:
        types = ["missile"]
    if not types and ("⚠" in folded or "→" in folded):
        # 本频道方向告警格式("⚠️Полтавщина: →Нові Санжари"),无动词;按无人机威胁处理
        types = ["drone"]
    if not types and re.search(
        r"\b(кружля\w*|заліта\w*|фіксу\w*|ціл\w*|рух\w*|ппо|тривожн\w*)\b",
        folded,
        re.I,
    ):
        # 无类型词但含威胁语境(盘旋/目标/防空/告警)→ 按无人机威胁处理
        types = ["drone"]
    if not types and re.search(r"\b(реактивн\w*)\b", folded, re.I):
        # 喷气机/战机语境 → 导弹威胁
        types = ["missile"]
    negated = bool(NEGATION.search(folded))
    retracted = bool(RETRACT.search(folded))
    all_clear = bool(ALL_CLEAR.search(folded))
    occurred_at, uncertainty, basis = parse_time(text, published_at)
    return {
        "types": types,
        "negated": negated,
        "retracted": retracted,
        "all_clear": all_clear,
        "occurred_at": occurred_at,
        "uncertainty": uncertainty,
        "time_basis": basis,
    }
