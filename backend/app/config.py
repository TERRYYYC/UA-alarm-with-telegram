import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/app.db")
GAZETTEER_SEED = os.getenv("GAZETTEER_SEED", "data/gazetteer_seed.json")
MERGE_WINDOW_MINUTES = int(os.getenv("MERGE_WINDOW_MINUTES", "60"))
FUZZY_THRESHOLD = float(os.getenv("FUZZY_THRESHOLD", "92"))

# 自动地理编码(Nominatim;未收录地名自动解析入库,无需人工)
GEOCODER_ENABLED = os.getenv("GEOCODER_ENABLED", "1") == "1"
GEOCODER_MIN_IMPORTANCE = float(os.getenv("GEOCODER_MIN_IMPORTANCE", "0.15"))

# 自动核验:多源交叉印证阈值(独立来源数 / 报告数,满足其一即自动确认)
AUTO_CONFIRM_MIN_SOURCES = int(os.getenv("AUTO_CONFIRM_MIN_SOURCES", "2"))
AUTO_CONFIRM_MIN_REPORTS = int(os.getenv("AUTO_CONFIRM_MIN_REPORTS", "3"))

# 波次级印证:两个来源在"邻近 + 同类型 + 时间窗内"各报一处时,视为同一波攻击而互相印证。
# 只影响核验状态,不合并点位(两个城镇仍是地图上两个点)。设 WAVE_CORROBORATION=0 关闭。
WAVE_CORROBORATION = os.getenv("WAVE_CORROBORATION", "1").lower() not in ("0", "false", "no")
WAVE_RADIUS_KM = float(os.getenv("WAVE_RADIUS_KM", "50"))
WAVE_WINDOW_MIN = int(os.getenv("WAVE_WINDOW_MIN", "60"))

# 发布器(未配置 PUBLISH_OUT_DIR 时不启用)
PUBLISH_OUT_DIR = os.getenv("PUBLISH_OUT_DIR", "")
PUBLISH_MIN_INTERVAL_S = int(os.getenv("PUBLISH_MIN_INTERVAL_S", "300"))
PUBLISH_RENDER_URL = os.getenv("PUBLISH_RENDER_URL", "http://127.0.0.1:5173/?export=1")

# 真实发送渠道(可选;未配置时回退 FileSender)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_PUBLISH_CHAT = os.getenv("TELEGRAM_PUBLISH_CHAT", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
