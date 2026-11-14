# -*- coding: utf-8 -*-
"""
Relay Weather 模块：小米天气（服务端单点拉取 + 全班共享缓存）
----------------------------------------------------------------
数据源：weatherapi.market.xiaomi.com/wtr-v3/weather/all（模板可在管理页改，
        含 {locationKey}/{days} 占位符——接口变更时教师只改配置不改代码）
架构：后台线程按管理页配置的间隔拉取一次存 weather.db，64 台平板只读缓存；
      失败保留旧缓存并显示数据年龄；页面 ⟳ 强制刷新 10 秒节流（后台执行不阻塞）。
数据展示（Q5：能取到的都显示）：当前温度/天气/体感/湿度/气压/能见度/紫外线/
      风向风力/发布时间、AQI、日出日落、逐小时预报（前 24 小时）、每日预报（7/15 天）。
管理页：城市搜索（调小米城市接口→候选点选）、天数、刷新间隔、API 模板、拉取测试。
Relay Hold v2：滚动由全局心跳恢复；?refresh=1 随 URL 心跳一并恢复（节流保护）。
"""
import json, os, threading, time
from html import escape as esc
from urllib.parse import quote
from flask import Blueprint, redirect, request

M = None
WX_DB = WX_LOG = None
log = None
bp = Blueprint("weather", __name__)

DEF_TEMPLATE = ("https://weatherapi.market.xiaomi.com/wtr-v3/weather/all"
                "?locationKey=weathercn:{locationKey}&days={days}"
                "&appKey=weather20151024&sign=zUFJoAR2ZVrDy1vF3D07"
                "&isGlobal=false&locale=zh_cn&latitude=0&longitude=0")
SEARCH_URL = ("https://weatherapi.market.xiaomi.com/wtr-v3/location/city/search"
              "?name={name}&locale=zh_cn")
REFRESH_GAP = 10          # 页面 ⟳ 强制刷新最小间隔（秒）

_cond_map = {0: "晴", 1: "多云", 2: "阴", 3: "阵雨", 4: "雷阵雨", 5: "雷阵雨伴冰雹",
             6: "小雨", 7: "中雨", 8: "大雨", 9: "暴雨", 10: "大暴雨", 11: "特大暴雨",
             12: "冻雨", 13: "沙尘暴", 14: "小到中雨", 15: "中到大雨", 16: "大到暴雨",
             17: "暴雨到大暴雨", 18: "大暴雨到特大暴雨", 19: "小雪", 20: "中雪", 21: "大雪",
             22: "暴雪", 23: "小到中雪", 24: "中到大雪", 25: "大到暴雪", 26: "暴雪到大暴雪",
             27: "浮尘", 28: "扬沙", 29: "沙尘暴", 30: "强沙尘暴", 31: "雾", 32: "霾"}

def init(m, ast=None):
    global M, WX_DB, WX_LOG, log
    M = m
    WX_DB = os.path.join(M.DIR_WX, "weather.db")
    WX_LOG = os.path.join(M.DIR_WX, "weather.log")
    log = M._mklog("relay.weather", WX_LOG)
    init_db()


def init_db():
    M.dbq(WX_DB, "CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
    M.dbq(WX_DB, "CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, value TEXT, ts REAL)")
    _sdefault("url_template", DEF_TEMPLATE)
    _sdefault("location_key", "101210901")   # Q4：默认金华，管理页可改
    _sdefault("city_name", "金华")
    _sdefault("days", "7")
    _sdefault("interval_min", "30")

def sget(k, d=""):
    r = M.dbq(WX_DB, "SELECT value FROM settings WHERE key=?", (k,))
    return r[0][0] if r else d

def sset(k, v):
    M.dbq(WX_DB, "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, str(v)))

def _sdefault(k, v):
    if not M.dbq(WX_DB, "SELECT value FROM settings WHERE key=?", (k,)):
        sset(k, v)

# ---------------- 拉取与缓存 ----------------
_fetch_lock = threading.Lock()

def _http():
    import requests as http
    return http

def fetch_now():
    """同步拉取一次并落缓存；返回 (ok, msg)。"""
    tpl = sget("url_template") or DEF_TEMPLATE
    url = (tpl.replace("{locationKey}", quote(sget("location_key"), safe=""))
              .replace("{days}", str(sget("days") or "7")))
    try:
        r = _http().get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("天气拉取失败：%s", e)
        return False, "拉取失败：" + str(e)[:120]
    M.dbq(WX_DB, "INSERT OR REPLACE INTO cache(key,value,ts) VALUES('payload',?,?)",
          (json.dumps(data, ensure_ascii=False), time.time()))
    d = data.get("forecastDaily") or {}
    h = data.get("forecastHourly") or {}
    log.info("天气拉取成功 %s（daily=%s hourly=%s）", sget("city_name"),
             len(d.get("card") or []), len(h.get("card") or []))
    return True, "ok"

def _cache():
    rows = M.dbq(WX_DB, "SELECT value, ts FROM cache WHERE key='payload'")
    if not rows:
        return None, 0.0
    try:
        return json.loads(rows[0][0]), float(rows[0][1])
    except Exception:
        return None, 0.0

def _bg_loop():
    while True:
        time.sleep(60)
        try:
            try:
                iv = max(5, int(sget("interval_min") or 30))
            except ValueError:
                iv = 30
            row = M.dbq(WX_DB, "SELECT ts FROM cache WHERE key='payload'")
            age = (time.time() - row[0][0]) if (row and row[0][0]) else 1e12
            if age >= iv * 60:
                if not _fetch_lock.acquire(blocking=False):
                    continue
                try:
                    fetch_now()
                finally:
                    _fetch_lock.release()
        except Exception:
            log.exception("weather bg loop")

def start_background():
    threading.Thread(target=_bg_loop, daemon=True).start()
    log.info("Weather 后台线程已启动（每 %s 分钟自动刷新）", sget("interval_min"))

# ---------------- 解析辅助（防御式：字段缺失即不显示该格） ----------------
def _g(d, *keys):
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = d.get(k)
        if v is not None and v != "":
            return v
    return None

def _cond_name(code):
    try:
        return _cond_map.get(int(code), "天气(%s)" % code)
    except (TypeError, ValueError):
        return str(code) if code else ""

def _wind_dir(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    try:
        deg = float(v) % 360
        dirs = ("北", "东北", "东", "东南", "南", "西南", "西", "西北")
        return dirs[int((deg + 22.5) // 45) % 8] + "风"
    except (TypeError, ValueError):
        return ""

def _wind_level(kmh):
    try:
        s = float(kmh)
    except (TypeError, ValueError):
        return ""
    bounds = (1, 5, 11, 19, 28, 38, 49, 61, 74, 88, 102, 117)
    lv = 0
    for b in bounds:
        if s < b:
            break
        lv += 1
    return "%d级" % lv

def _aqi_level(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    for lim, name in ((50, "优"), (100, "良"), (150, "轻度污染"),
                      (200, "中度污染"), (300, "重度污染")):
        if n <= lim:
            return name
    return "严重污染"

def _time_of(x):
    if isinstance(x, dict):
        return str(x.get("time") or x.get("date") or "")
    return str(x) if x else ""

def _fmt_temp(v):
    if v is None or v == "":
        return ""
    return "%s℃" % v

def _summarize():
    data, _ = _cache()
    if not data:
        return "无数据"
    cur = data.get("current") or {}
    d = data.get("forecastDaily") or {}
    h = data.get("forecastHourly") or {}
    return "%s 当前 %s %s · 每日 %d 条 · 逐小时 %d 条" % (
        sget("city_name"), _fmt_temp(_g(cur, "temp", "temperature")),
        _cond_name(_g(cur, "condition", "conditionDay")),
        len(d.get("card") or []), len(h.get("card") or []))

# ---------------- 页面 ----------------
WX_STYLE = ("<style>"
 ".wxbig{font-size:44px;font-weight:bold;color:#1a365d}"
 ".wxcond{font-size:20px;color:#4a5568}"
 ".wxgrid{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}"
 ".wxcell{background:#eef4fb;border-radius:8px;padding:10px 14px;text-align:center;"
 "flex:1;min-width:calc(25% - 8px)}"
 ".wxcell b{display:block;font-size:15px;color:#1a365d}"
 ".wxcell span{font-size:13px;color:#718096}"
 ".wrow{padding:10px 4px;border-bottom:1px solid #e2e8f0}"
 ".wrow .fname{font-size:16px;font-weight:bold;color:#1a202c}"
 "</style>")

def _shell(title, body, extra_head=""):
    u = M.current_user()
    js = M.hold_loc_js(u) if u else ""
    return (f"<!doctype html><html><head><meta charset='utf-8'>{extra_head}{M.STYLE}{M.ANIM_STYLE}"
            f"{WX_STYLE}<title>{esc(title)}</title></head><body>{body}" + js + "</body></html>")

@bp.route("/weather")
def wx_home():
    u = M.current_user()
    if not u:
        return M.login_page()
    M.hold_page("weather")
    tip = ""
    if request.args.get("refresh") == "1":
        row = M.dbq(WX_DB, "SELECT ts FROM cache WHERE key='payload'")
        age = (time.time() - row[0][0]) if (row and row[0][0]) else 1e12
        if age >= REFRESH_GAP:
            threading.Thread(target=fetch_now, daemon=True).start()
            tip = "<p class='tip'>已提交后台刷新，稍后轻触 ⟳ 查看新数据</p>"
        else:
            tip = "<p class='tip'>刷新过于频繁（%d 秒间隔），稍后再试</p>" % REFRESH_GAP
    data, ts = _cache()
    data = data or {}
    cur = data.get("current") or {}
    temp = _fmt_temp(_g(cur, "temp", "temperature"))
    cond = _cond_name(_g(cur, "condition", "conditionDay"))
    feels = _fmt_temp(_g(cur, "feels_like", "feelsLike", "feels"))
    hum = _g(cur, "humidity")
    pres = _g(cur, "pressure")
    vis = _g(cur, "visibility")
    uv = _g(cur, "uv", "uvIndex")
    wind = cur.get("wind") if isinstance(cur.get("wind"), dict) else {}
    wspd = _g(wind, "speed") or _g(cur, "windSpeed")
    wdir = _g(wind, "direction") or _g(cur, "windDirectionDegree")
    dirtxt = _wind_dir(wdir)
    lvltxt = _wind_level(wspd)
    pub = _g(cur, "pub_time") or _g(data, "pub_time")
    aqi = data.get("aqi") if isinstance(data.get("aqi"), dict) else {}
    aqi_v = _g(aqi, "aqi", "value")
    aqi_n = aqi.get("name") or (_aqi_level(aqi_v) if aqi_v else "")
    sr = _time_of(data.get("sunrise"))
    ss = _time_of(data.get("sunset"))
    cells = ""
    for label, val in (("湿度", (str(hum) + "%") if hum not in (None, "") else ""),
                       ("气压", (str(pres) + "hPa") if pres not in (None, "") else ""),
                       ("能见度", (str(vis) + "km") if vis not in (None, "") else ""),
                       ("紫外线", str(uv or "")),
                       ("风", ((dirtxt + " ") if dirtxt else "") + lvltxt),
                       ("体感", feels)):
        if val:
            cells += "<div class='wxcell'><b>%s</b><span>%s</span></div>" % (label, esc(val))
    daily = (data.get("forecastDaily") or {}).get("card") or []
    hourly = (data.get("forecastHourly") or {}).get("card") or []
    dparts = []
    for i, it in enumerate(daily):
        date = str(_g(it, "date") or "")
        day_c = _cond_name(_g(it, "conditionDay", "condition"))
        night_c = _cond_name(it.get("conditionNight"))
        hi = _fmt_temp(_g(it, "high"))
        lo = _fmt_temp(_g(it, "low"))
        pp = _g(it, "precipProbability")
        seg = ("今天" if i == 0 else ("明天" if i == 1 else "第%d天" % (i + 1)))
        if len(date) >= 10:
            seg += " " + date[5:]
        trans = ("转" + night_c) if (night_c and night_c != day_c) else ""
        meta = "%s / %s" % (esc(hi), esc(lo))
        if pp not in (None, ""):
            meta += " · 降水 %s%%" % pp
        dparts.append("<div class='wrow'><div class='fname'>%s · %s%s</div>"
                      "<div class='fmeta'>%s</div></div>" % (esc(seg), esc(day_c), trans, meta))
    hparts = []
    for it in hourly[:24]:
        t = str(_g(it, "time") or "")
        if len(t) >= 16:
            t = t[11:16]
        tp = _fmt_temp(_g(it, "temp"))
        cnd = _cond_name(_g(it, "condition"))
        pp = _g(it, "precipProbability")
        meta = "%s · %s" % (esc(tp), esc(cnd))
        if pp not in (None, ""):
            meta += " · 降水 %s%%" % pp
        hparts.append("<div class='wrow'><div class='fname'>%s</div>"
                      "<div class='fmeta'>%s</div></div>" % (esc(t), meta))
    if ts:
        ageline = "数据更新于 %d 分钟前（来源：小米天气）" % int((time.time() - ts) / 60)
    else:
        ageline = "暂无数据（等待首次拉取，教师可在管理页测试）"
    aqiline = ""
    if aqi_v not in (None, ""):
        aqiline = "<p class='tip'>空气质量 AQI %s%s</p>" % (
            esc(aqi_v), (" · " + esc(aqi_n)) if aqi_n else "")
    sunline = ""
    if sr or ss:
        sunline = "<p class='tip'>日出 %s · 日落 %s%s</p>" % (
            esc(sr or "—"), esc(ss or "—"),
            (" · 发布 " + esc(str(pub))) if pub else "")
    body = (f"<div class='rowline'>{M.back_btn('/home')}"
            f"<a class='slink' href='/weather?refresh=1'>⟳ 刷新</a></div>"
            f"<h3>Weather · 天气</h3>"
            f"<p class='tip'>{esc(sget('city_name'))} · {esc(ageline)}</p>"
            f"{tip}<div class='wxbig'>{esc(temp)}</div><div class='wxcond'>{esc(cond)}</div>"
            + (("<div class='wxgrid'>" + cells + "</div>") if cells else "")
            + aqiline + sunline
            + ("<h4>逐小时预报（未来 24 小时）</h4>" + "".join(hparts) if hparts else "")
            + ("<h4>未来 %d 天预报</h4>" % len(daily) + "".join(dparts) if dparts else "")
            + M.REOPEN_TIP + M.FOOTER)
    return _shell("Weather", body)

# ---------------- Weather 管理 ----------------
def _city_search(name):
    url = SEARCH_URL.replace("{name}", quote(name))
    try:
        r = _http().get(url, timeout=10)
        r.raise_for_status()
        j = r.json()
    except Exception as e:
        return False, [], "搜索接口请求失败：" + str(e)[:120]
    lst = None
    if isinstance(j, list):
        lst = j
    elif isinstance(j, dict):
        d = j.get("data") if isinstance(j.get("data"), (list, dict)) else j
        if isinstance(d, dict):
            for k in ("list", "cities", "result", "items"):
                if isinstance(d.get(k), list):
                    lst = d[k]
                    break
            if lst is None and isinstance(d.get("city"), list):
                lst = d["city"]
        elif isinstance(d, list):
            lst = d
    cands = []
    for c in lst or []:
        if not isinstance(c, dict):
            continue
        key = _g(c, "locationKey", "location_key", "key", "locationId")
        nm = _g(c, "name", "city", "cityName")
        parent = _g(c, "parent", "province", "district", "state") or ""
        if key and nm:
            cands.append((str(key), str(nm), str(parent)))
    if not cands:
        return False, [], "接口返回中未识别出候选城市（可把返回内容发我以适配字段）"
    return True, cands, ""

@bp.route("/admin/weather", methods=["GET", "POST"])
def admin_wx():
    if not M.admin_ok():
        return M.admin_login_page()
    msg = ""
    act = request.form.get("act")
    cands = None
    try:
        if act == "search":
            nm = request.form.get("name", "").strip()
            if nm:
                ok, cands, err = _city_search(nm)
                msg = err if err else ("找到 %d 个候选城市，轻触“选用”" % len(cands))
        elif act == "pick":
            key = request.form.get("key", "").strip()
            if key.lower().startswith("weathercn:"):
                key = key[len("weathercn:"):]   # 模板已含 weathercn: 前缀，防双重拼接
            sset("location_key", key)
            sset("city_name", request.form.get("name", "").strip() or "未命名城市")
            msg = "城市已切换为 %s；建议立即执行一次拉取测试" % sget("city_name")
        elif act == "save":
            days = request.form.get("days", "7")
            sset("days", "15" if days == "15" else "7")
            try:
                iv = max(5, int(request.form.get("interval", "30")))
            except ValueError:
                iv = 30
            sset("interval_min", str(iv))
            sset("url_template", request.form.get("tpl", "").strip() or DEF_TEMPLATE)
            msg = "设置已保存"
        elif act == "fetchnow":
            ok, m2 = fetch_now()
            msg = ("拉取成功：" + _summarize()) if ok else m2
        elif act == "clearcache":
            M.dbq(WX_DB, "DELETE FROM cache")
            msg = "缓存已清除（后台将按间隔自动重新拉取）"
    except Exception as e:
        msg = "操作失败：%s" % e
    data, ts = _cache()
    fetch_line = "最近拉取：从未成功" if not ts else ("最近拉取：%d 分钟前 · %s" % (
        int((time.time() - ts) / 60), _summarize()))
    cand_html = ""
    if cands is not None:
        if cands:
            rows = ""
            for key, nm, parent in cands[:30]:
                rows += ("<tr><td>%s</td><td class='tip'>%s</td><td class='tip'>%s</td><td>"
                         "<form method='post' action='/admin/weather' style='display:inline'>"
                         "<input type='hidden' name='act' value='pick'>"
                         "<input type='hidden' name='key' value='%s'>"
                         "<input type='hidden' name='name' value='%s'>"
                         "<button class='mini'>选用</button></form></td></tr>"
                         % (esc(nm), esc(parent), esc(key), esc(key), esc(nm)))
            cand_html = ("<h4>候选城市（轻触“选用”）</h4><table border='1'><tr><th>城市</th>"
                         "<th>上级行政区</th><th>locationKey</th><th></th></tr>" + rows + "</table>")
        else:
            cand_html = "<p class='err'>未找到候选城市，请尝试其他写法（如“金华”“金华市”）</p>"
    body = (M.back_btn("/admin") + ("<p class='err'>" + esc(msg) + "</p>" if msg else "")
            + f"<p>当前城市：<b>{esc(sget('city_name'))}</b>（locationKey {esc(sget('location_key'))}）"
            f" · 天数 {esc(sget('days'))} 天 · 间隔 {esc(sget('interval_min'))} 分钟</p>"
            f"<p class='tip'>{esc(fetch_line)}</p>"
            "<h4>城市设置（按名称搜索，候选点选）</h4>"
            "<form method='post' action='/admin/weather' style='display:flex;gap:6px'>"
            "<input type='hidden' name='act' value='search'>"
            "<input name='name' placeholder='城市名，如 金华'>"
            "<button type='submit' style='width:auto;padding:10px 16px'>搜索</button></form>"
            + cand_html +
            "<form method='post' action='/admin/weather'>"
            "<input type='hidden' name='act' value='save'>"
            "<h4>拉取天数</h4>"
            "<label><input type='radio' name='days' value='7'"
            + (" checked" if sget("days") != "15" else "") + ">7 天</label>"
            "<label><input type='radio' name='days' value='15'"
            + (" checked" if sget("days") == "15" else "") + ">15 天</label>"
            "<h4>自动刷新间隔（分钟，≥5）</h4>"
            f"<input name='interval' value='{esc(sget('interval_min'))}'>"
            "<h4>API 模板（高级；含 {locationKey} 与 {days} 占位符）</h4>"
            f"<textarea name='tpl' rows='4'>{esc(sget('url_template'))}</textarea>"
            "<button type='submit'>保存设置</button></form>"
            "<h4>手动操作</h4><p>"
            "<form method='post' action='/admin/weather' style='display:inline'>"
            "<input type='hidden' name='act' value='fetchnow'>"
            "<button class='mini'>立即拉取测试</button></form> "
            "<form method='post' action='/admin/weather' style='display:inline'>"
            "<input type='hidden' name='act' value='clearcache'>"
            "<button class='mini'>清除缓存</button></form></p>"
            "<p class='tip'><a href='/admin'>返回管理中心</a> · <a href='/weather'>打开 Weather</a></p>"
            + M.FOOTER)
    return (f"<!doctype html><html><head><meta charset='utf-8'>{M.STYLE}"
            f"<title>Weather 管理</title></head><body><h3>Weather 管理</h3>{body}</body></html>")
