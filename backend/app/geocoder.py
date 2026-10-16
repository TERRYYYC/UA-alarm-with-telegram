"""OSM Nominatim 自动地理编码:未收录地名 → 坐标,无需人工、无需 API key。

使用政策:≤1 请求/秒、自定义 User-Agent。结果由管线写入地名库(持久化),
后续同名单词不再需要网络。
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request


class Geocoder:
    def __init__(self, base_url: str | None = None, user_agent: str | None = None, min_interval: float = 1.05):
        self.base_url = base_url or "https://nominatim.openstreetmap.org/search"
        self.user_agent = user_agent or "situation-monitor/0.1 (local war-monitoring research)"
        self.min_interval = min_interval
        self._last = 0.0

    def search(self, query: str, context_admin: str | None = None) -> dict | None:
        """返回 {name, lat, lng, type, importance, admin1} 或 None。"""
        q = query
        if context_admin:
            q = f"{query}, {context_admin}"
        params = {
            "q": q,
            "countrycodes": "ua",
            "format": "json",
            "limit": 1,
            "accept-language": "uk",
        }
        url = f"{self.base_url}?{urllib.parse.urlencode(params)}"
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            self._last = time.monotonic()
        except Exception:
            self._last = time.monotonic()
            return None
        if not data:
            return None
        r = data[0]
        try:
            return {
                "name": r.get("display_name", "").split(",")[0].strip(),
                "lat": float(r["lat"]),
                "lng": float(r["lon"]),
                "type": r.get("type", ""),
                "importance": float(r.get("importance", 0.0)),
                "admin1": context_admin,
            }
        except (TypeError, ValueError):
            return None
