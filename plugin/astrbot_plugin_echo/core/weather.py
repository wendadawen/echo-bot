"""实时天气：wttr.in 免费接口，不需要 key。只依赖标准库。"""

import urllib.parse
import urllib.request

WEATHER_ZH = (
    ("smoky haze", "烟霾"),
    ("haze", "霾"),
    ("partly cloudy", "多云"),
    ("cloudy", "多云"),
    ("overcast", "阴"),
    ("light drizzle", "毛毛雨"),
    ("light rain", "小雨"),
    ("moderate rain", "中雨"),
    ("heavy rain", "大雨"),
    ("thunderstorm", "雷阵雨"),
    ("light snow", "小雪"),
    ("heavy snow", "大雪"),
    ("snow", "雪"),
    ("mist", "薄雾"),
    ("fog", "雾"),
    ("sunny", "晴"),
    ("clear", "晴"),
)


def translate(desc):
    key = str(desc or "").strip().lower()
    for pattern, zh in WEATHER_ZH:
        if pattern in key:
            return zh
    return str(desc or "").strip()


def lookup(city, timeout=15):
    """返回一行天气；查不到返回空串。"""
    city = str(city or "").strip()[:40]
    if not city:
        return ""
    query = urllib.parse.urlencode({"format": "%l|%C|%t|%h|%w", "lang": "zh"})
    url = "https://wttr.in/" + urllib.parse.quote(city) + "?" + query
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "ignore").strip()
    except Exception:
        return ""
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) < 5:
        return raw[:120]
    name, desc, temp, humidity, wind = parts[:5]
    if not name or not temp:
        return ""
    return "{}：{}，{}，湿度 {}，风 {}".format(
        name, translate(desc), temp, humidity, wind
    )
