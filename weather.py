# -*- coding: utf-8 -*-
"""
Relay Weather 模块 · Open-Meteo 数据源(免费无 Key)
Relay 3.0 · CTU
  geocoding-api.open-meteo.com   城市搜索(中文)
  api.open-meteo.com             当前 + 24h 逐时 + 15 天预报
  air-quality-api.open-meteo.com 空气质量(US AQI / PM2.5 / UV)
缓存节流 10 分钟;Hold v2 支持;苹果天气风格页面。
CTU 修复:
  - 补上缺失的 import requests(此前版本 _http_json/搜索/后台刷新全部 NameError,
    /admin/weather 带搜索词访问与 /weather 数据拉取 500 的根因)
  - 城市搜索改用 urllib.parse.quote(不再依赖 requests.utils 内部路径)
"""
import json, os, threading, time
from datetime import datetime
from html import escape as esc
from urllib.parse import quote as _urlquote
import requests as _rq
from flask import Blueprint, redirect, request

M = None
WEATHER_DB = WEATHER_LOG = None
log = None

def init(m, ast=None):
    global M, WEATHER_DB, WEATHER_LOG, log
    M = m
    WEATHER_DB = os.path.join(M.DIR_WEATHER, "weather.db")
    WEATHER_LOG = os.path.join(M.DIR_WEATHER, "weather.log")
    log = M._mklog("relay.weather", WEATHER_LOG)
    M.dbq(WEATHER_DB, """CREATE TABLE IF NOT EXISTS city(
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, lat REAL, lon REAL,
        picked INTEGER DEFAULT 0)""")
    M.dbq(WEATHER_DB, """CREATE TABLE IF NOT EXISTS cache(
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, data TEXT, ts REAL)""")
    M.whitelist_add("api.open-meteo.com")
    M.whitelist_add("air-quality-api.open-meteo.com")
    M.whitelist_add("geocoding-api.open-meteo.com")

bp = Blueprint("weather", __name__)

FORECAST_URL = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
 "&current=temperature_2m,relative_humidity_2m,apparent_temperature,is_day,precipitation,"
 "weather_code,cloud_cover,pressure_msl,wind_speed_10m,wind_direction_10m,wind_gusts_10m"
 "&hourly=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation_probability,"
 "weather_code,wind_speed_10m,wind_direction_10m,is_day,visibility"
 "&daily=weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,uv_index_max,"
 "precipitation_sum,precipitation_probability_max,wind_speed_10m_max"
 "&timezone=Asia%2FShanghai&forecast_days=15")
AQI_URL = ("https://air-quality-api.open-meteo.com/v1/air-quality?latitude={lat}"
 "&longitude={lon}&current=pm10,pm2_5,us_aqi,european_aqi,uv_index"
 "&timezone=Asia%2FShanghai")
GEO_URL = ("https://geocoding-api.open-meteo.com/v1/search?name={q}&language=zh&count=8")

WMO = {0: ("晴", "☀️"), 1: ("大体晴", "🌤️"), 2: ("多云", "⛅"), 3: ("阴", "☁️"),
       45: ("雾", "🌫️"), 48: ("雾凇", "🌫️"), 51: ("小毛雨", "🌦️"), 53: ("毛雨", "🌦️"),
       55: ("大毛雨", "🌦️"), 56: ("冻毛雨", "🌧️"), 57: ("强冻毛雨", "🌧️"),
       61: ("小雨", "🌦️"), 63: ("中雨", "🌧️"), 65: ("大雨", "🌧️"),
       66: ("冻雨", "🌧️"), 67: ("强冻雨", "🌧️"),
       71: ("小雪", "🌨️"), 73: ("中雪", "🌨️"), 75: ("大雪", "❄️"), 77: ("雪粒", "🌨️"),
       80: ("阵雨", "🌦️"), 81: ("中阵雨", "🌧️"), 82: ("强阵雨", "⛈️"),
       85: ("小阵雪", "🌨️"), 86: ("大阵雪", "❄️"),
       95: ("雷雨", "⛈️"), 96: ("雷雨伴冰雹", "⛈️"), 99: ("强雷雨伴冰雹", "⛈️")}

def wmo(c):
    return WMO.get(int(c or 0), ("未知", "🌡️"))

def wind_level(kmh):
    v = kmh or 0
    for i, b in enumerate([1, 5, 11, 19, 28, 38, 49, 61, 74, 88, 102, 117]):
        if v < b:
            return i
    return 12

def wind_dir(deg):
    if deg is None:
        return ""
    names = ["北", "东北偏北", "东北", "东北偏东", "东", "东南偏东", "东南", "东南偏南",
             "南", "西南偏南", "西南", "西南偏西", "西", "西北偏西", "西北", "西北偏北"]
    return names[int((deg % 360) / 22.5) % 16]

def aqi_level(v):
    v = v or 0
    if v <= 50:
        return ("优", "#43a047")
    if v <= 100:
        return ("良", "#f9a825")
    if v <= 150:
        return ("轻度污染", "#fb8c00")
    if v <= 200:
        return ("中度污染", "#e53935")
    if v <= 300:
        return ("重度污染", "#8e24aa")
    return ("严重污染", "#6a1b9a")

def uv_level(v):
    v = v or 0
    if v < 3:
        return "低"
    if v < 6:
        return "中等"
    if v < 8:
        return "高"
    if v < 11:
        return "很高"
    return "极高"

def picked_city():
    rows = M.dbq(WEATHER_DB, "SELECT id,name,lat,lon FROM city WHERE picked=1 LIMIT 1")
    if not rows:
        return None
    return {"id": rows[0][0], "name": rows[0][1], "lat": rows[0][2], "lon": rows[0][3]}

def _http_json(url, timeout=8):
    r = _rq.get(url, timeout=timeout, headers={"User-Agent": "Relay/3.0 CTU"})
    r.raise_for_status()
    return r.json()

def _cache_put(kind, data):
    M.dbq(WEATHER_DB, "INSERT INTO cache(kind,data,ts) VALUES(?,?,?)", (kind, data, time.time()))
    M.dbq(WEATHER_DB, "DELETE FROM cache WHERE kind=? AND id NOT IN "
                      "(SELECT id FROM cache WHERE kind=? ORDER BY id DESC LIMIT 2)", (kind, kind))

def _cache_get(kind):
    rows = M.dbq(WEATHER_DB, "SELECT data,ts FROM cache WHERE kind=? ORDER BY id DESC LIMIT 1",
                 (kind,))
    if not rows:
        return None, 0
    try:
        return json.loads(rows[0][0]), rows[0][1]
    except Exception:
        return None, 0

_fetch_lock = threading.Lock()
_last_fetch = 0.0

def fetch_all(force=False):
    global _last_fetch
    with _fetch_lock:
        if not force and time.time() - _last_fetch < 600:
            return True, ""
        city = picked_city()
        if not city:
            return False, "未选择城市"
        try:
            fc = _http_json(FORECAST_URL.format(lat=city["lat"], lon=city["lon"]))
            _cache_put("fc", json.dumps(fc, ensure_ascii=False))
            try:
                aq = _http_json(AQI_URL.format(lat=city["lat"], lon=city["lon"]))
                _cache_put("aq", json.dumps(aq, ensure_ascii=False))
            except Exception as e:
                log.warning("AQI 拉取失败:%s", e)
            _last_fetch = time.time()
            log.info("天气拉取成功:%s", city["name"])
            return True, ""
        except Exception as e:
            log.exception("天气拉取失败")
            return False, str(e)[:200]

WX_STYLE = ("<style>"
 ".wxhero{background:linear-gradient(160deg,#3d7bd9,#1f4e96);border-radius:16px;color:#fff;"
 "padding:18px;margin:10px 0;text-align:center}"
 ".wxhero .big{font-size:72px;font-weight:100;line-height:1}"
 ".wxhero .sub{opacity:.92;font-size:15px;margin-top:6px}"
 ".hrow{display:flex;overflow-x:auto;gap:8px;padding:4px 0;-webkit-overflow-scrolling:touch}"
 ".wcard{flex:0 0 auto;min-width:88px;background:#eef3fa;border-radius:12px;"
 "padding:10px 8px;text-align:center}"
 ".wcard .lab{font-size:12px;color:#5a6b80}.wcard .val{font-size:16px;margin-top:4px}"
 ".sec{margin-top:14px}"
 ".drow{display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid #e3e8ef}"
 ".drow .dname{width:54px;font-size:14px}.drow .dico{width:30px;text-align:center}"
 ".drow .dpop{width:44px;font-size:12px;color:#2563eb;text-align:right}"
 ".bar{flex:1;height:6px;border-radius:3px;background:#e3e8ef;position:relative}"
 ".bar span{position:absolute;height:6px;border-radius:3px;"
 "background:linear-gradient(90deg,#5aa9e6,#f7b32b)}"
 ".aqi{border-radius:14px;padding:14px;color:#fff;margin:10px 0;display:flex;"
 "align-items:center;justify-content:space-between}"
 ".aqi .big{font-size:34px;font-weight:700}"
 "</style>")

def _f(v, nd=0):
    try:
        return ("%." + str(nd) + "f") % float(v)
    except Exception:
        return "-"

def _render(fc, aq, ts, city):
    cur = fc.get("current") or {}
    hourly = fc.get("hourly") or {}
    daily = fc.get("daily") or {}
    t = cur.get("temperature_2m")
    code = cur.get("weather_code", 0)
    desc, icon = wmo(code)
    tmax = (daily.get("temperature_2m_max") or [None])[0]
    tmin = (daily.get("temperature_2m_min") or [None])[0]
    hero = ("<div class='wxhero'><div style='font-size:17px'>%s · %s</div>"
            "<div class='big'>%s°</div>"
            "<div class='sub'>%s %s · 最高 %s° / 最低 %s°</div>"
            "<div class='sub'>体感 %s° · 更新于 %s</div></div>"
            % (esc(str(city["name"])), icon, _f(t, 1), desc, icon,
               _f(tmax, 1), _f(tmin, 1), _f(cur.get("apparent_temperature"), 1),
               datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "-"))
    cards = [("体感", _f(cur.get("apparent_temperature"), 1) + "°"),
             ("湿度", _f(cur.get("relative_humidity_2m")) + "%"),
             ("风", "%s%s %d级" % (wind_dir(cur.get("wind_direction_10m")),
                                   _f(cur.get("wind_speed_10m")) + "km/h",
                                   wind_level(cur.get("wind_speed_10m")))),
             ("阵风", _f(cur.get("wind_gusts_10m")) + "km/h"),
             ("气压", _f(cur.get("pressure_msl")) + "hPa"),
             ("云量", _f(cur.get("cloud_cover")) + "%"),
             ("降水", _f(cur.get("precipitation"), 1) + "mm"),
             ("UV(今日)", uv_level((daily.get("uv_index_max") or [None])[0]))]
    chtml = "".join("<div class='wcard'><div class='lab'>%s</div>"
                    "<div class='val'>%s</div></div>" % (esc(k), esc(v)) for k, v in cards)
    body = hero + "<div class='hrow'>" + chtml + "</div>"
    hts = hourly.get("time") or []
    try:
        idx = hts.index(datetime.now().strftime("%Y-%m-%dT%H:00"))
    except ValueError:
        idx = 0
    hs = ""
    for j in range(idx, min(idx + 24, len(hts))):
        hh = (hts[j].split("T")[-1] or "")[:5]
        c2 = wmo((hourly.get("weather_code") or [0] * len(hts))[j])[1]
        tp = (hourly.get("temperature_2m") or [None] * len(hts))[j]
        pp = (hourly.get("precipitation_probability") or [None] * len(hts))[j]
        hs += ("<div class='wcard'><div class='lab'>%s</div><div style='font-size:22px'>%s</div>"
               "<div class='val'>%s°</div><div class='lab'>降水 %s%%</div></div>"
               % (esc(hh), c2, _f(tp, 0), esc(str(pp if pp is not None else "-"))))
    body += ("<div class='sec'><h4>24 小时预报</h4><div class='hrow'>" + hs + "</div></div>")
    dts = daily.get("time") or []
    gmax = max([v for v in (daily.get("temperature_2m_max") or []) if v is not None] or [0])
    gmin = min([v for v in (daily.get("temperature_2m_min") or []) if v is not None] or [0])
    span = max(1.0, gmax - gmin)
    wk = "一二三四五六日"
    ds = ""
    for j in range(len(dts)):
        try:
            dn = "周" + wk[datetime.strptime(dts[j], "%Y-%m-%d").weekday()]
        except Exception:
            dn = dts[j]
        c2 = wmo((daily.get("weather_code") or [0] * len(dts))[j])
        hi = (daily.get("temperature_2m_max") or [None] * len(dts))[j]
        lo = (daily.get("temperature_2m_min") or [None] * len(dts))[j]
        pop = (daily.get("precipitation_probability_max") or [None] * len(dts))[j]
        try:
            l = ((lo or gmin) - gmin) / span * 100
            w = ((hi or gmax) - (lo or gmin)) / span * 100
        except Exception:
            l, w = 0, 30
        ds += ("<div class='drow'><div class='dname'>%s</div><div class='dico'>%s</div>"
               "<div class='dpop'>%s%%</div><div class='tip' style='width:34px'>%s°</div>"
               "<div class='bar'><span style='left:%.0f%%;width:%.0f%%'></span></div>"
               "<div style='width:34px;text-align:right'>%s°</div></div>"
               % (esc(dn), c2[1], esc(str(pop if pop is not None else "-")),
                  _f(lo, 0), l, w, _f(hi, 0)))
    body += ("<div class='sec'><h4>15 天预报</h4>" + ds + "</div>")
    if aq:
        ac = aq.get("current") or {}
        av = ac.get("us_aqi")
        lv, col = aqi_level(av)
        body += ("<div class='aqi' style='background:%s'><div>"
                 "<div style='font-size:13px;opacity:.9'>空气质量 · US AQI</div>"
                 "<div class='big'>%s %s</div></div>"
                 "<div style='text-align:right;font-size:13px;line-height:1.7'>"
                 "PM2.5 %s<br>PM10 %s<br>UV %s</div></div>"
                 % (col, esc(_f(av, 0)), esc(lv),
                    esc(_f(ac.get("pm2_5"), 1)), esc(_f(ac.get("pm10"), 1)),
                    esc(_f(ac.get("uv_index"), 1))))
    sr = (daily.get("sunrise") or [""])[0].split("T")[-1]
    ss = (daily.get("sunset") or [""])[0].split("T")[-1]
    body += ("<div class='sec'><div class='hrow'>"
             "<div class='wcard'><div class='lab'>日出</div><div class='val'>%s</div></div>"
             "<div class='wcard'><div class='lab'>日落</div><div class='val'>%s</div></div>"
             "</div></div>" % (esc(sr), esc(ss)))
    return body

@bp.route("/weather")
def weather_home():
    u = M.current_user()
    if not u:
        return M.login_page()
    M.hold_page("weather")
    city = picked_city()
    if not city:
        body = (M.back_btn("/home") + "<h3>Weather</h3>"
                "<p class='tip'>教师尚未在管理页选择城市。</p>")
        return ("<html><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                + M.STYLE + WX_STYLE + "</head><body>" + body + M.REOPEN_TIP + M.FOOTER
                + M.hold_loc_js(u) + "</body></html>")
    ok, err = fetch_all()
    fc, ts = _cache_get("fc")
    aq, _t2 = _cache_get("aq")
    inner = _render(fc, aq, ts, city) if fc else \
        "<p class='err'>暂无数据:%s(教师机需可访问 api.open-meteo.com)</p>" % esc(err)
    body = (M.back_btn("/home") + "<h3>Weather · %s</h3>" % esc(city["name"]) + inner
            + "<p class='tip'><a href='/weather?r=1'>⟳ 刷新</a></p>")
    if request.args.get("r"):
        fetch_all(force=True)
        fc, ts = _cache_get("fc")
        aq, _t2 = _cache_get("aq")
        body = (M.back_btn("/home") + "<h3>Weather · %s</h3>" % esc(city["name"])
                + (_render(fc, aq, ts, city) if fc else "<p class='err'>刷新失败</p>"))
    return ("<html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            + M.STYLE + M.ANIM_STYLE + WX_STYLE + "</head><body>"
            + body + M.REOPEN_TIP + M.FOOTER + M.hold_loc_js(u) + "</body></html>")

@bp.route("/admin/weather", methods=["GET", "POST"])
def admin_weather():
    if not M.admin_ok():
        return M.admin_login_page()
    msg = ""
    if request.method == "POST":
        act = request.form.get("act", "")
        if act == "pick":
            M.dbq(WEATHER_DB, "UPDATE city SET picked=0")
            M.dbq(WEATHER_DB, "INSERT INTO city(name,lat,lon,picked) VALUES(?,?,?,1)",
                  (request.form.get("name", "")[:60], request.form.get("lat", ""),
                   request.form.get("lon", "")))
            ok, err = fetch_all(force=True)
            msg = "已选用城市,拉取:" + ("成功" if ok else "失败 " + err)
        elif act == "fetch":
            ok, err = fetch_all(force=True)
            msg = "拉取:" + ("成功" if ok else "失败 " + err)
    city = picked_city()
    fc, ts = _cache_get("fc")
    tsline = ("最近拉取:" + datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")) if ts \
        else "最近拉取:从未成功"
    results_html = ""
    q = request.args.get("q", "").strip()
    if q and _rq:
        try:
            data = _http_json(GEO_URL.format(q=_urlquote(q)))
            for r in (data.get("results") or [])[:8]:
                results_html += ("<tr><td>%s · %s · %s</td><td>%s,%s</td>"
                                 "<td><form method='post' action='/admin/weather' "
                                 "style='display:inline'>"
                                 "<input type='hidden' name='act' value='pick'>"
                                 "<input type='hidden' name='name' value='%s'>"
                                 "<input type='hidden' name='lat' value='%s'>"
                                 "<input type='hidden' name='lon' value='%s'>"
                                 "<button class='mini'>选用</button></form></td></tr>"
                                 % (esc(r.get("name", "")), esc(r.get("admin1", "")),
                                    esc(r.get("country", "")), r.get("lat"), r.get("lon"),
                                    esc(r.get("name", "")), r.get("lat"), r.get("lon")))
        except Exception as e:
            msg = "搜索失败:%s" % str(e)[:120]
    body = (M.back_btn("/admin") + ("<p class='err'>%s</p>" % esc(msg) if msg else "")
            + "<h3>Weather 管理</h3>"
            "<p>当前城市:%s · %s · 缓存:%s</p>"
            "<form method='post' action='/admin/weather'>"
            "<input type='hidden' name='act' value='fetch'>"
            "<button class='mini'>立即拉取测试</button></form>"
            "<h4>城市搜索(Open-Meteo,中文)</h4>"
            "<form method='get' action='/admin/weather'>"
            "<input name='q' value='%s' placeholder='如:金华'><button class='mini'>搜索</button></form>"
            "<table><tr><th>候选</th><th>坐标</th><th></th></tr>%s</table>"
            "<p class='tip'>数据源 Open-Meteo(完全免费无 Key);含当前/24h/15天/US AQI/日出日落。</p>"
            "<p class='tip'><a href='/admin'>返回管理中心</a> · <a href='/weather'>打开 Weather</a></p>"
            + M.FOOTER) % (esc(city["name"]) if city else "未选择", tsline,
                           "有" if fc else "空", esc(q), results_html or
                           "<tr><td colspan='3' class='tip'>输入城市名搜索</td></tr>")
    return ("<html><head><meta charset='utf-8'>" + M.STYLE
            + "<title>Weather 管理</title></head><body><h3>Weather 管理</h3>"
            + body + "</body></html>")

def start_background():
    def _loop():
        while True:
            time.sleep(600)
            try:
                fetch_all()
            except Exception:
                log.exception("后台天气刷新失败")
    threading.Thread(target=_loop, daemon=True).start()
