# -*- coding: utf-8 -*-
"""
Relay 1.2 主程序（壳）
------------------------
组成：功能模块由 relay_apps.json 声明并动态挂载（Build 41 起），
      内置 assistant/resource/remoteclasswork/dictionary/weather；
      waitress 生产服务器。
运行：python relay_main.py （监听 0.0.0.0:80，128 线程）
依赖：flask waitress dnslib requests markdown ziamath pymupdf（winrt 可选）
新增功能（Build 41）：
- relay_apps.json 配置文件化：功能名称/代码路径/描述/管理入口均由配置声明；
  新增 App = 写一个 .py + json 加一行 + 重启，relay_main 零改动；
  配置缺失/损坏时回退内置最小列表；单个模块加载失败仅记录日志并跳过。
- 新增 Dictionary（mobi/prc 词典查询，自带解析器零依赖）与 Weather（小米天气）。
其余（Hold v2 / DNS 模式 / 网络诊断）与 Build 40 一致，见各函数注释。
"""
import collections, importlib, json, logging, os, re, secrets, socket, sqlite3
import subprocess, sys, threading, time
from datetime import datetime
from html import escape as esc
from socketserver import ThreadingMixIn
import requests as http
from flask import Flask, g, make_response, redirect, request
from dnslib import A, DNSRecord, QTYPE, RR
from dnslib.server import BaseResolver, DNSServer, DNSLogger, TCPServer, UDPServer
from waitress import serve
from werkzeug.security import check_password_hash, generate_password_hash

# ---------- 版本（唯一管理处） ----------
RELAY_VERSION = "2.0"
RELAY_CODENAME = "CKG"
RELAY_BUILD = 42
__version__ = RELAY_VERSION

APP_NAME = "Relay"
DEVELOPER = "HighspeedG2304"

# ---------- 路径与目录 ----------
BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
DIR_COMMON = os.path.join(DATA, "common")
DIR_AST = os.path.join(DATA, "assistant")
DIR_RES = os.path.join(DATA, "resource")
DIR_RC = os.path.join(DATA, "remoteclasswork")
DIR_DICT = os.path.join(DATA, "dictionary")
DIR_WX = os.path.join(DATA, "weather")
COMMON_DB = os.path.join(DIR_COMMON, "accounts.db")
LOG_PATH = os.path.join(DIR_COMMON, "relay.log")
KEY_PATH = os.path.join(BASE, "key.txt")
APPS_PATH = os.path.join(BASE, "relay_apps.json")
for _d in (DIR_COMMON, DIR_AST, DIR_RES, DIR_RC, DIR_DICT, DIR_WX):
    os.makedirs(_d, exist_ok=True)

# ---------- 配置区 ----------
PORTAL_IP = "192.168.137.1"
DNS_BIND = os.environ.get("HS_DNS_BIND", "127.0.0.1")
DNS_PORT = int(os.environ.get("HS_DNS_PORT", "53"))
UPSTREAM_DNS = "223.5.5.5"
WHITELIST = {
    "open.bigmodel.cn", "time.windows.com", "ntp.aliyun.com",
    "kv-service.wuyuan.dev",           # RemoteClasswork
    "openid.zero-cat.dev",             # 仅 refresh_token 续期用
    "weatherapi.market.xiaomi.com",    # Weather 拉取（Build 41 新增，必保留）
}
DEFAULT_PROBE_HOSTS = [
    "connectivitycheck.gstatic.com", "www.google.com", "connectivitycheck.android.com",
    "play.googleapis.com", "www.gstatic.cn", "captive.apple.com",
    "connect.rom.miui.com", "wifi.vivo.com.cn", "conn1.oppomobile.com",
    "connectivitycheck.platform.hicloud.com",
]
DNS_GUARD = {"www.msftconnecttest.com", "dns.msftncsi.com", "ipv6.msftconnecttest.com"}
HOLD_URL_MAX_AGE = 7 * 24 * 3600
HTTP_PORT = int(os.environ.get("HS_HTTP_PORT", "80"))
HTTP_THREADS = 128
ADMIN_PASSWORD = "admin123"   # 管理密码（部署后务必修改）
COOKIE_NAME, COOKIE_DEV, COOKIE_MAX_AGE = "ps", "pd", 180*24*3600
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---------- 日志 ----------
def _mklog(name, path):
    lg = logging.getLogger(name)
    if not lg.handlers:
        h = logging.FileHandler(path, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        lg.addHandler(h); lg.setLevel(logging.INFO); lg.propagate = False
    return lg

log = _mklog("relay.main", LOG_PATH)

# ---------- App 配置（relay_apps.json，Build 41） ----------
_DEFAULT_APPS = [
    {"key": "assistant",  "module": "assistant",       "title": "Assistant",
     "desc": "向 GLM 4 Flash 提问",
     "admin_url": "/admin/assistant",
     "admin_desc": "账号数据/Token · 询问记录 · 联网搜索 · 上下文 · 公式渲染自检"},
    {"key": "resource",   "module": "resource",        "title": "Resource",
     "desc": "查看图像，文档和演示文稿",
     "admin_url": "/admin/resource",
     "admin_desc": "课件文件夹 · LibreOffice 路径 · 索引计划 · 手动扫描 / 重建 / 预热"},
    {"key": "rc",         "module": "remoteclasswork", "title": "RemoteClasswork",
     "desc": "查看并编辑班级作业（与班级大屏实时同步）",
     "admin_url": "/admin/remoteclasswork",
     "admin_desc": "KV 连接（令牌 / 鉴权双模式）· 学科权限 · 离线队列"},
    {"key": "dictionary", "module": "dictionary",      "title": "Dictionary",
     "desc": "查询词典",
     "admin_url": "/admin/dictionary",
     "admin_desc": "mobi 词典导入与管理"},
    {"key": "weather",    "module": "weather",         "title": "Weather",
     "desc": "查看天气变化与天气预报",
     "admin_url": "/admin/weather",
     "admin_desc": "城市配置 · 天数与刷新间隔 · 拉取测试"},
]
_APPKEY_RE = re.compile(r"^[a-z0-9]{1,24}$")



def _load_apps():
    try:
        with open(APPS_PATH, "r", encoding="utf-8-sig") as f:
            obj = json.load(f)
        apps, seen = [], set()
        for a in obj.get("apps", []):
            k = str(a.get("key") or "").strip().lower()
            mod = str(a.get("module") or "").strip()
            if not _APPKEY_RE.match(k) or not mod or k in seen:
                log.warning("relay_apps.json 中无效或重复的条目已忽略：%r", a)
                continue
            seen.add(k)
            apps.append({"key": k, "module": mod,
                         "title": str(a.get("title") or k),
                         "desc": str(a.get("desc") or ""),
                         "admin_url": str(a.get("admin_url") or ""),
                         "admin_desc": str(a.get("admin_desc") or "")})
        if apps:
            log.info("relay_apps.json 已加载：%s", "、".join(a["key"] for a in apps))
            return apps
        log.error("relay_apps.json 中无有效条目，使用内置列表")
    except FileNotFoundError:
        log.error("未找到 relay_apps.json，使用内置最小功能列表")
    except Exception as e:
        log.error("relay_apps.json 解析失败（%s），使用内置最小功能列表", e)
    return [dict(x) for x in _DEFAULT_APPS]

APPS = _load_apps()
# Hold v2：模块 key → 入口路径；URL 前缀白名单（含 /dictionary /weather，自动生成）
HOLD_MAP = {a["key"]: "/" + a["key"] for a in APPS}
HOLD_URL_PREFIXES = tuple("/" + a["key"] for a in APPS)


# ---------- key.txt ----------
def _load_config():
    vals = []
    try:
        for line in open(KEY_PATH, "r", encoding="utf-8-sig"):
            line = line.strip()
            if line and not line.startswith("#"):
                vals.append(line)
    except OSError:
        log.error("未找到 key.txt（%s），AI 功能不可用", KEY_PATH)
    vals += [""] * (3 - len(vals))
    if vals[0]:
        log.info("API Key 已读取（长度 %d），模型 %s", len(vals[0]), vals[1] or "默认")
    else:
        log.error("key.txt 中 API Key 为空，AI 功能不可用")
    return vals[0], vals[1] or "glm-4.7-flash", vals[2] or "https://open.bigmodel.cn/api/paas/v4/chat/completions"

AI_API_KEY, AI_MODEL, AI_API_URL = _load_config()

def ai_post(session, payload, timeout):
    return session.post(AI_API_URL, headers={"Authorization": "Bearer " + AI_API_KEY},
                        json=payload, timeout=timeout)

# ---------- 通用 DB ----------
def dbq(path, sql, args=()):
    conn = sqlite3.connect(path, timeout=15)
    conn.isolation_level = None
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()

def cget(key, default=""):
    r = dbq(COMMON_DB, "SELECT value FROM settings WHERE key=?", (key,))
    return r[0][0] if r else default

def cset(key, value):
    dbq(COMMON_DB, "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)))

def init_common_db():
    dbq(COMMON_DB, """CREATE TABLE IF NOT EXISTS accounts(
        username TEXT PRIMARY KEY, pw_hash TEXT NOT NULL, device_id TEXT,
        bound_at TEXT, last_seen TEXT, last_query TEXT,
        tokens_total INTEGER NOT NULL DEFAULT 0, last_page TEXT DEFAULT 'home')""")
    dbq(COMMON_DB, """CREATE TABLE IF NOT EXISTS sessions(
        token TEXT PRIMARY KEY, username TEXT NOT NULL, device_id TEXT NOT NULL,
        created_at TEXT)""")
    dbq(COMMON_DB, "CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
    try:
        dbq(COMMON_DB, "ALTER TABLE accounts ADD COLUMN last_page TEXT DEFAULT 'home'")
    except sqlite3.OperationalError:
        pass
    for stmt in ("ALTER TABLE accounts ADD COLUMN last_url TEXT DEFAULT ''",
                 "ALTER TABLE accounts ADD COLUMN last_scroll INTEGER DEFAULT 0",
                 "ALTER TABLE accounts ADD COLUMN last_loc_ts REAL DEFAULT 0"):
        try:
            dbq(COMMON_DB, stmt)
        except sqlite3.OperationalError:
            pass
    if not dbq(COMMON_DB, "SELECT key FROM settings WHERE key='probe_hosts'"):
        cset("probe_hosts", "\n".join(DEFAULT_PROBE_HOSTS))
    if not dbq(COMMON_DB, "SELECT key FROM settings WHERE key='dns_mode'"):
        cset("dns_mode", "probe")

# ---------- 设备识别 ----------
_arp_cache, _arp_lock = {}, threading.Lock()

def get_mac(ip):
    with _arp_lock:
        hit = _arp_cache.get(ip)
        if hit and time.time() - hit[1] < 600:
            return hit[0]
    try:
        subprocess.run(["ping", "-n", "1", "-w", "200", ip],
                       capture_output=True, creationflags=_NO_WINDOW)
        out = subprocess.run(["arp", "-a", ip], capture_output=True, text=True,
                             timeout=5, creationflags=_NO_WINDOW).stdout
        m = re.search(r"([0-9A-Fa-f]{2}[-:]){5}[0-9A-Fa-f]{2}", out)
        if m:
            mac = m.group(0).replace("-", ":").lower()
            with _arp_lock:
                _arp_cache[ip] = (mac, time.time())
            return mac
    except Exception:
        pass
    return None

def device_id():
    ip = request.remote_addr or ""
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
        mac = get_mac(ip)
        if mac:
            return mac
    tok = request.cookies.get(COOKIE_DEV)
    if tok:
        return "c:" + tok
    if not hasattr(g, "new_dev"):
        g.new_dev = secrets.token_hex(16)
    return "c:" + g.new_dev

def current_user():
    tok = request.cookies.get(COOKIE_NAME)
    if not tok:
        return None
    dev = device_id()
    rows = dbq(COMMON_DB, "SELECT s.username, s.device_id, a.device_id FROM sessions s "
                          "JOIN accounts a ON a.username=s.username WHERE s.token=?", (tok,))
    if not rows:
        return None
    user, sdev, adev = rows[0]
    return user if (sdev == dev and adev == dev) else None

# ---------- Relay Hold v2：功能页 + 完整 URL + 滚动位置 ----------
def hold_page(key):
    u = current_user()
    if u:
        try:
            dbq(COMMON_DB, "UPDATE accounts SET last_page=? WHERE username=?", (key, u))
        except Exception:
            pass

def _cur_url():
    qs = request.query_string.decode("utf-8", "ignore") if request.query_string else ""
    return request.path + (("?" + qs) if qs else "")

def hold_scroll_for(u):
    try:
        r = dbq(COMMON_DB, "SELECT last_url, last_scroll FROM accounts WHERE username=?", (u,))
        if r and r[0][0] and r[0][0] == _cur_url():
            return max(0, int(r[0][1] or 0))
    except Exception:
        pass
    return 0

HOLD_LOC_JS = ("<script>(function(){"
 "var lastU=null,lastY=-1,t=null;"
 "function cu(){return location.pathname+location.search;}"
 "function cy(){return window.pageYOffset||document.documentElement.scrollTop||0;}"
 "function send(){var a=cu(),b=Math.round(cy());"
 "if(a===lastU&&b===lastY)return;lastU=a;lastY=b;"
 "var x=new XMLHttpRequest();x.open('POST','/hold/loc',true);"
 "x.setRequestHeader('Content-Type','application/x-www-form-urlencoded');"
 "x.send('url='+encodeURIComponent(a)+'&scroll_y='+b);}"
 "window.addEventListener('scroll',function(){if(t)clearTimeout(t);t=setTimeout(send,300);},false);"
 "window.addEventListener('pagehide',function(){"
 "var x=new XMLHttpRequest();x.open('POST','/hold/loc',false);"
 "x.setRequestHeader('Content-Type','application/x-www-form-urlencoded');"
 "x.send('url='+encodeURIComponent(cu())+'&scroll_y='+Math.round(cy()));},false);"
 "setInterval(send,3000);"
 "var sy=__SCROLL__;"
 "if(sy>0){var n=0;"
 "function rs(){var v=cy();if(v>=sy-2||n>=4)return;n++;setTimeout(rs,400);}"
 "window.scrollTo(0,sy);setTimeout(rs,400);}"
 "})();</script>")

def hold_loc_js(u):
    try:
        sy = hold_scroll_for(u) if u else 0
    except Exception:
        sy = 0
    return HOLD_LOC_JS.replace("__SCROLL__", str(sy))

@app.route("/hold/loc", methods=["POST"])
def hold_loc():
    u = current_user()
    if not u:
        return "", 204
    url = (request.form.get("url") or "").strip()
    try:
        sy = int(float(request.form.get("scroll_y") or 0))
    except (TypeError, ValueError):
        sy = 0
    if not url or not url.startswith(HOLD_URL_PREFIXES):
        return "", 204
    sy = max(0, min(200000, sy))
    try:
        dbq(COMMON_DB, "UPDATE accounts SET last_url=?, last_scroll=?, last_loc_ts=? WHERE username=?",
            (url[:300], sy, time.time(), u))
    except Exception:
        pass
    return "", 204

# ---------- 公共 UI ----------
STYLE = ("<meta name='viewport' content='width=device-width,initial-scale=1'>"
 "<style>body{font-family:sans-serif;max-width:640px;margin:14px auto;"
 "padding:0 12px;font-size:17px}"
 "h3{margin:6px 0}h4{margin:10px 0 4px}"
 "input,textarea,button,select{font-size:17px;width:100%;box-sizing:border-box;"
 "padding:10px;margin:5px 0}"
 "label{display:block;margin:8px 0}"
 "input[type=checkbox]{width:auto;padding:0;margin:0 8px 0 0;vertical-align:middle}"
 "input[type=radio]{width:auto;padding:0;margin:0 8px 0 0;vertical-align:middle}"
 "button{background:#2b6cb0;color:#fff;border:0;border-radius:6px;padding:12px}"
 "button.mini{width:auto;padding:4px 8px;margin:0;font-size:13px;background:#888}"
 "a.btn2{display:block;background:#e2e8f0;color:#2d3748;border-radius:6px;"
 "padding:12px;margin:5px 0;text-align:center;text-decoration:none;font-size:17px}"
 "pre{white-space:pre-wrap;word-wrap:break-word;background:#f4f6f8;"
 "padding:10px;border-radius:6px;font-size:12px}"
 ".err{color:#c0392b}.tip{color:#666;font-size:14px}"
 ".queue{background:#eef4fb;border-radius:6px;padding:6px 10px;"
 "font-size:14px;color:#2b6cb0}"
 ".md p{margin:6px 0}.md ul,.md ol{margin:4px 0 4px 20px;padding:0}"
 ".md li{margin:2px 0}.md strong{color:#1a4c8b}"
 ".md .mathin{display:inline-block;max-width:100%}"
 ".md .mathin svg{vertical-align:middle}"
 ".md .mathblk{display:block;text-align:center;margin:8px 0;overflow-x:auto}"
 ".md .mathfall{background:#f4f6f8;padding:2px 4px;border-radius:4px;"
 "font-family:monospace;font-size:14px;color:#c0392b}"
 ".ctx{border-left:3px solid #cbd5e0;padding-left:10px;margin:6px 0}"
 ".ctxq{background:#f4f6f8;border-radius:6px;padding:6px 8px;"
 "margin:8px 0 2px;font-weight:bold}"
 "table{font-size:14px;border-collapse:collapse}td,th{padding:4px}"
 ".back{display:inline-block;color:#2b6cb0;text-decoration:none;font-size:15px;margin:2px 0 6px}"
 ".card{display:block;background:#eef4fb;border-radius:10px;padding:16px;margin:10px 0;"
 "text-decoration:none;color:#1a365d}"
 ".card b{font-size:19px}.card span{display:block;color:#4a5568;font-size:13px;margin-top:4px}"
 ".litem{display:block;padding:10px 4px;border-bottom:1px solid #e2e8f0;"
 "text-decoration:none;color:inherit}"
 ".fname{font-size:18px;font-weight:bold;color:#1a202c;word-break:break-all}"
 ".fmeta{font-size:12px;color:#a0aec0;margin-top:3px}"
 ".chip{display:inline-block;background:#e2e8f0;color:#2d3748;border-radius:4px;"
 "padding:1px 6px;font-size:12px;margin-left:6px;vertical-align:middle}"
 ".rowline{display:flex;justify-content:space-between;align-items:center;gap:8px;"
 "font-size:13px;color:#4a5568;margin:8px 0 2px;flex-wrap:wrap}"
 ".slink{color:#2b6cb0;text-decoration:none;margin-left:6px;white-space:nowrap}"
 ".slink.on{font-weight:bold;text-decoration:underline}"
 ".stchip{padding:2px 8px;border-radius:10px;font-size:12px;color:#fff;background:#888}"
 ".stchip.run{background:#2b6cb0}.stchip.pend{background:#b7791f}.stchip.done{background:#38a169}"
 "</style>")

ANIM_STYLE = ("<style>"
 "@-webkit-keyframes spin{to{-webkit-transform:rotate(360deg)}}"
 "@keyframes spin{to{transform:rotate(360deg)}}"
 ".spinner{display:inline-block;width:20px;height:20px;vertical-align:-4px;"
 "border:3px solid #dbe4ee;border-top-color:#2b6cb0;border-radius:50%;"
 "-webkit-animation:spin .9s linear infinite;animation:spin .9s linear infinite}"
 ".spinner.sm{width:13px;height:13px;border-width:2px;vertical-align:-2px;margin-right:6px}"
 "@-webkit-keyframes dotj{0%,60%,100%{-webkit-transform:translateY(0);opacity:.35}"
 "30%{-webkit-transform:translateY(-5px);opacity:1}}"
 "@keyframes dotj{0%,60%,100%{transform:translateY(0);opacity:.35}"
 "30%{transform:translateY(-5px);opacity:1}}"
 ".dots i{display:inline-block;width:7px;height:7px;margin-left:4px;"
 "background:#2b6cb0;border-radius:50%;-webkit-animation:dotj 1.2s infinite;animation:dotj 1.2s infinite}"
 ".dots i:nth-child(2){-webkit-animation-delay:.15s;animation-delay:.15s}"
 ".dots i:nth-child(3){-webkit-animation-delay:.3s;animation-delay:.3s}"
 "@-webkit-keyframes fadein{from{opacity:0;-webkit-transform:translateY(6px)}"
 "to{opacity:1;-webkit-transform:translateY(0)}}"
 "@keyframes fadein{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}"
 ".fadein{-webkit-animation:fadein .35s ease-out;animation:fadein .35s ease-out}"
 "button:active,a.btn2:active{-webkit-transform:scale(.97);transform:scale(.97)}"
 "@media (prefers-reduced-motion:reduce){.spinner,.dots i,.fadein{-webkit-animation:none;animation:none}}"
 "</style>")

FOOTER = ("<p class='tip'>Relay %s · %s · Build %d · Developer %s</p>"
          % (RELAY_VERSION, RELAY_CODENAME, RELAY_BUILD, DEVELOPER))
REOPEN_TIP = ("<p class='tip'>若不慎关闭本页面，尝试开关 WLAN 开关，"
              "本页面应该会自动重新弹出</p>")

def back_btn(href="/home", label="返回"):
    return "<a class='back' href='%s'>‹ %s</a>" % (href, label)

def msg_page(text, back="/"):
    return (f"<!doctype html><html><head><meta charset='utf-8'>{STYLE}<title>提示</title>"
            f"</head><body>{back_btn()}<p>{text}</p><p><a href='{back}'>返回</a></p>"
            + FOOTER + "</body></html>")

def login_page(err=""):
    e = "<p class='err'>" + esc(err) + "</p>" if err else ""
    return (f"<!doctype html><html><head><meta charset='utf-8'>{STYLE}"
            f"<title>{APP_NAME}</title></head><body><h3>{APP_NAME} · 登录</h3>"
            f"{e}<form method='post' action='/login'>"
            "<input name='u' placeholder='账号' autocapitalize='off' autocomplete='off'>"
            "<input name='p' type='password' placeholder='密码'>"
            "<label class='tip'><input type='checkbox' name='rebind' value='1'>"
            "若账号已绑定其他设备，轻触此项以将绑定迁移到本设备</label>"
            "<button type='submit'>登 录</button></form>"
            "<p class='tip'>一个账号仅允许与一台设备绑定</p>"
            + REOPEN_TIP + FOOTER + "</body></html>")

# ---------- App ----------
app = Flask(__name__)

@app.after_request
def finish(resp):
    if getattr(g, "new_dev", None) and not request.cookies.get(COOKIE_DEV):
        resp.set_cookie(COOKIE_DEV, g.new_dev, max_age=COOKIE_MAX_AGE, httponly=True, path="/")
    mt = resp.mimetype or ""
    resp.headers["Cache-Control"] = ("private, max-age=86400"
        if (mt.startswith("image/") or mt.startswith("audio/")) else "no-store")
    return resp

# ---------- 基础路由 ----------
@app.route("/")
def root():
    u = current_user()
    if not u:
        return login_page()
    try:
        r = dbq(COMMON_DB, "SELECT last_page, last_url, last_loc_ts FROM accounts WHERE username=?", (u,))
    except Exception:
        r = []
    lp, last_url, last_ts = "home", "", 0.0
    if r:
        lp = r[0][0] or "home"
        last_url = r[0][1] or ""
        try:
            last_ts = float(r[0][2] or 0)
        except (TypeError, ValueError):
            last_ts = 0.0
    if (last_url and last_url.startswith(HOLD_URL_PREFIXES)
            and time.time() - last_ts < HOLD_URL_MAX_AGE):
        return redirect(last_url)
    return redirect(HOLD_MAP.get(lp, "/home"))

@app.route("/home")
def home():
    u = current_user()
    if not u:
        return login_page()
    hold_page("home")
    cards = ""
    for a in APPS:
        cards += ("<a class='card' href='/%s'><b>%s</b><span>%s</span></a>"
                  % (esc(a["key"]), esc(a["title"]), esc(a["desc"])))
    body = (f"<p class='tip'>当前账号：{esc(u)} <a href='/unbind'>解绑本设备</a></p>"
            + cards + REOPEN_TIP + FOOTER)
    return (f"<!doctype html><html><head><meta charset='utf-8'>{STYLE}"
            f"<title>{APP_NAME}</title></head><body><h3>{APP_NAME} · 功能列表</h3>{body}"
            + hold_loc_js(u) + "</body></html>")

@app.route("/health")
def health():
    return "ok", 200

@app.route("/favicon.ico")
def favicon():
    return "", 204

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if current_user():
            return redirect("/")
        return login_page()
    u = request.form.get("u", "").strip()
    p = request.form.get("p", "")
    dev = device_id()
    rows = dbq(COMMON_DB, "SELECT pw_hash, device_id, bound_at FROM accounts WHERE username=?", (u,))
    if not rows or not check_password_hash(rows[0][0], p):
        log.info("登录失败 user=%s dev=%s", u, dev)
        return login_page("账号或密码错误")
    bound, bound_at = rows[0][1], rows[0][2] or "?"
    if bound and bound != dev and request.form.get("rebind") != "1":
        return login_page("该账号已绑定其他设备（绑定时间 " + bound_at + "）。"
                          "要将绑定转移到本设备，请轻触迁移绑定后重新登录")
    tok = secrets.token_hex(24)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    dbq(COMMON_DB, "DELETE FROM sessions WHERE username=? AND device_id<>?", (u, dev))
    dbq(COMMON_DB, "DELETE FROM sessions WHERE device_id=?", (dev,))
    dbq(COMMON_DB, "INSERT INTO sessions VALUES (?,?,?,?)", (tok, u, dev, now))
    dbq(COMMON_DB, "UPDATE accounts SET device_id=?, bound_at=? WHERE username=?", (dev, now, u))
    log.info("登录成功 user=%s dev=%s", u, dev)
    resp = make_response(redirect("/"))
    resp.set_cookie(COOKIE_NAME, tok, max_age=COOKIE_MAX_AGE, httponly=True, path="/")
    return resp

@app.route("/unbind", methods=["GET", "POST"])
def unbind():
    u = current_user()
    if not u:
        return login_page()
    if request.method == "GET":
        return (f"<!doctype html><html><head><meta charset='utf-8'>{STYLE}"
                "<title>解绑设备</title></head><body><h3>解绑设备</h3>"
                "<p class='tip'>解绑后，账号 " + esc(u) + " 与本设备解除绑定，可在其他设备登录</p>"
                "<form method='post' action='/unbind'>"
                "<input name='p' type='password' placeholder='输入密码以授权此操作'>"
                "<button type='submit'>确认解绑</button></form>" + FOOTER + "</body></html>")
    rows = dbq(COMMON_DB, "SELECT pw_hash FROM accounts WHERE username=?", (u,))
    if not rows or not check_password_hash(rows[0][0], request.form.get("p", "")):
        return msg_page("<span class='err'>密码错误，解绑失败</span>", "/unbind")
    dbq(COMMON_DB, "DELETE FROM sessions WHERE username=?", (u,))
    dbq(COMMON_DB, "UPDATE accounts SET device_id=NULL, bound_at=NULL WHERE username=?", (u,))
    log.info("解绑 user=%s", u)
    return msg_page("解绑成功", "/login")

# ---------- 管理中心 ----------
_admin_tokens = set()

def admin_ok():
    return request.cookies.get("adm") in _admin_tokens

def admin_login_page(err=""):
    e = "<p class='err'>" + esc(err) + "</p>" if err else ""
    return (f"<!doctype html><html><head><meta charset='utf-8'>{STYLE}<title>管理</title>"
            f"</head><body><h3>{APP_NAME} · 管理员管理</h3>{e}"
            "<form method='post' action='/admin'>"
            "<input name='pw' type='password' placeholder='管理密码'>"
            "<button type='submit'>登录</button></form>" + FOOTER + "</body></html>")

@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST" and "pw" in request.form:
        if request.form.get("pw") == ADMIN_PASSWORD:
            tok = secrets.token_hex(16)
            _admin_tokens.add(tok)
            resp = make_response(redirect("/admin"))
            resp.set_cookie("adm", tok, max_age=8*3600, httponly=True)
            return resp
        return admin_login_page("管理密码错误")
    if not admin_ok():
        return admin_login_page()
    app_cards = ""
    for a in APPS:
        if a.get("admin_url"):
            app_cards += ("<a class='card' href='%s'><b>%s 管理</b><span>%s</span></a>"
                          % (esc(a["admin_url"]), esc(a["title"]), esc(a["admin_desc"])))
    body = ("<p>选择管理模块：</p>"
            "<a class='card' href='/admin/common'><b>通用管理</b>"
            "<span>账号管理 · DNS 劫持模式 · Relay Hold 一键维护</span></a>"
            "<a class='card' href='/admin/dns'><b>网络诊断</b>"
            "<span>劫持列表 · 最近查询 · 承载网络关联客户端实时状态</span></a>"
            + app_cards +
            "<p class='tip'><a href='/'>打开门户首页</a> <a href='/admin'>刷新</a></p>" + FOOTER)
    return (f"<!doctype html><html><head><meta charset='utf-8'>{STYLE}<title>管理</title>"
            f"</head><body><h3>{APP_NAME} · 管理中心</h3>{body}</body></html>")

@app.route("/admin/common", methods=["GET", "POST"])
def admin_common():
    if not admin_ok():
        return admin_login_page()
    msg = ""
    act = request.form.get("act")
    try:
        if act == "add":
            u, p = request.form.get("u", "").strip(), request.form.get("p", "").strip()
            if u and p:
                dbq(COMMON_DB, "INSERT OR REPLACE INTO accounts(username,pw_hash) VALUES(?,?)",
                    (u, generate_password_hash(p)))
                msg = "已保存账号 " + u
        elif act == "bulk":
            n = 0
            for line in request.form.get("list", "").splitlines():
                parts = re.split(r"[,，\s]+", line.strip())
                if len(parts) >= 2 and parts[0] and parts[1]:
                    dbq(COMMON_DB, "INSERT OR REPLACE INTO accounts(username,pw_hash) VALUES(?,?)",
                        (parts[0], generate_password_hash(parts[1])))
                    n += 1
            msg = "批量导入 %d 个账号" % n
        elif act == "reset":
            u, p = request.form.get("u", "").strip(), request.form.get("p", "").strip()
            if u and p:
                dbq(COMMON_DB, "UPDATE accounts SET pw_hash=? WHERE username=?",
                    (generate_password_hash(p), u))
                dbq(COMMON_DB, "DELETE FROM sessions WHERE username=?", (u,))
                try:
                    import assistant as _a
                    _a.ctx_clear(u)
                except Exception:
                    pass
                msg = "已重置 %s 的密码并强制下线" % u
        elif act == "unbind":
            u = request.form.get("u", "").strip()
            dbq(COMMON_DB, "UPDATE accounts SET device_id=NULL, bound_at=NULL WHERE username=?", (u,))
            dbq(COMMON_DB, "DELETE FROM sessions WHERE username=?", (u,))
            msg = "已解绑 " + u
        elif act == "del":
            u = request.form.get("u", "").strip()
            dbq(COMMON_DB, "DELETE FROM accounts WHERE username=?", (u,))
            dbq(COMMON_DB, "DELETE FROM sessions WHERE username=?", (u,))
            msg = "已删除 " + u
        elif act == "setdns":
            m = request.form.get("mode", "probe")
            if m not in ("probe", "hijack", "pass"):
                m = "probe"
            cset("dns_mode", m)
            _dns_mode_cache[1] = 0.0
            msg = "DNS 劫持模式已切换为 " + m + "（即时生效）"
        elif act == "clearhold_all":
            # 直接按固定 DB 路径清理各模块 Hold 数据（模块无关，配置化后依然有效）
            for _p, _sql in ((os.path.join(DIR_AST, "assistant.db"), "DELETE FROM drafts"),
                             (os.path.join(DIR_AST, "assistant.db"), "DELETE FROM jobs"),
                             (os.path.join(DIR_RC, "rc.db"), "DELETE FROM holds"),
                             (os.path.join(DIR_RC, "rc.db"), "DELETE FROM drafts"),
                             (os.path.join(DIR_RES, "resource.db"), "DELETE FROM progress"),
                             (os.path.join(DATA, "music", "music.db"), "DELETE FROM progress"),
                             (os.path.join(DATA, "music", "music.db"), "DELETE FROM prefs")):

                try:
                    dbq(_p, _sql)
                except Exception:
                    pass
            dbq(COMMON_DB, "UPDATE accounts SET last_url='', last_scroll=0, "
                           "last_loc_ts=0, last_page='home'")
            msg = "已清空全部 Relay Hold 数据（草稿/页面位置/滚动位置），用户下次进入为全新状态"
    except Exception as e:
        msg = "操作失败：%s" % e
    rows = dbq(COMMON_DB, "SELECT username, device_id, bound_at, last_seen "
                          "FROM accounts ORDER BY username")
    trs = ""
    for r in rows:
        dev = (r[1] or "未绑定")[:17]
        trs += (f"<tr><td>{esc(r[0])}</td><td>{esc(dev)}</td><td>{esc(r[2] or '')}</td>"
                f"<td>{esc(r[3] or '')}</td><td>"
                f"<form method='post' action='/admin/common' style='display:inline'>"
                f"<input type='hidden' name='act' value='unbind'><input type='hidden' name='u' value='{esc(r[0])}'>"
                f"<button class='mini'>解绑</button></form> "
                f"<form method='post' action='/admin/common' style='display:inline'>"
                f"<input type='hidden' name='act' value='del'><input type='hidden' name='u' value='{esc(r[0])}'>"
                f"<button class='mini'>删除</button></form></td></tr>")
    head = back_btn("/admin") + (("<p class='err'>" + esc(msg) + "</p>") if msg else "")
    mode = cget("dns_mode", "probe")
    def _ck(v):
        return " checked" if mode == v else ""
    body = (head
            + "<p>账号 Token 用量与最近提问数据在 <a href='/admin/assistant'>Assistant 管理</a>；"
            "网络诊断见 <a href='/admin/dns'>网络诊断页</a>。</p>"
            "<table border='1'><tr><th>账号</th><th>绑定设备</th><th>绑定时间</th><th>最后活跃</th>"
            "<th>操作</th></tr>" + trs + "</table>"
            "<h4>添加账号</h4><form method='post' action='/admin/common'>"
            "<input type='hidden' name='act' value='add'>"
            "<input name='u' placeholder='账号' style='width:45%'>"
            "<input name='p' placeholder='密码' style='width:45%'>"
            "<button type='submit'>添加</button></form>"
            "<h4>批量导入（每行：账号 密码）</h4><form method='post' action='/admin/common'>"
            "<input type='hidden' name='act' value='bulk'>"
            "<textarea name='list' rows='6' placeholder='student01 123456&#10;student02 123456'></textarea>"
            "<button type='submit'>导入</button></form>"
            "<h4>重置密码（并强制下线）</h4><form method='post' action='/admin/common'>"
            "<input type='hidden' name='act' value='reset'>"
            "<input name='u' placeholder='账号' style='width:45%'>"
            "<input name='p' placeholder='新密码' style='width:45%'>"
            "<button type='submit'>重置</button></form>"
            "<h4>DNS 劫持模式</h4>"
            "<form method='post' action='/admin/common'>"
            "<input type='hidden' name='act' value='setdns'>"
            "<label><input type='radio' name='mode' value='probe'" + _ck("probe") + ">"
            "probe（推荐：仅劫持探测域名，教师机正常上网，平板连网弹出认证）</label>"
            "<label><input type='radio' name='mode' value='hijack'" + _ck("hijack") + ">"
            "hijack（全量劫持：所有域名指向门户）</label>"
            "<label><input type='radio' name='mode' value='pass'" + _ck("pass") + ">"
            "pass（全放行，排障用）</label>"
            "<button type='submit'>保存模式</button></form>"
            "<p class='tip'>探测域名列表与最近查询请见 <a href='/admin/dns'>网络诊断页</a></p>"
            "<h4>Relay Hold 维护</h4>"
            "<form method='post' action='/admin/common'>"
            "<input type='hidden' name='act' value='clearhold_all'>"
            "<button class='mini'>清空全部 Hold 数据（草稿/页面位置/滚动位置）</button></form>"
            "<p class='tip'>用于个别学生端 Hold 状态异常时的远程复位，影响所有用户</p>"
            "<p class='tip'><a href='/admin'>返回管理中心</a></p>" + FOOTER)
    return (f"<!doctype html><html><head><meta charset='utf-8'>{STYLE}<title>通用管理</title>"
            f"</head><body><h3>{APP_NAME} · 通用管理</h3>{body}</body></html>")

# ---------- DNS 模式系统 ----------
_dns_mode_cache = ["probe", 0.0]
_hosts_cache = [None, 0.0]
_dns_recent = collections.deque(maxlen=300)
_dns_recent_lock = threading.Lock()

def get_dns_mode():
    if time.time() - _dns_mode_cache[1] > 5:
        _dns_mode_cache[0] = cget("dns_mode", "probe") or "probe"
        _dns_mode_cache[1] = time.time()
    return _dns_mode_cache[0]

def get_hijack_hosts():
    if time.time() - _hosts_cache[1] > 5:
        raw = cget("probe_hosts", "") or ""
        _hosts_cache[0] = [h.strip().lower() for h in raw.splitlines() if h.strip()]
        _hosts_cache[1] = time.time()
    return _hosts_cache[0] or []

def _recent_add(name, hij):
    try:
        with _dns_recent_lock:
            _dns_recent.append((time.time(), name, hij))
    except Exception:
        pass

def _fwd(request):
    try:
        return DNSRecord.parse(_upstream(request.pack()))
    except Exception:
        return request.reply()

def _pick(cls):
    if issubclass(cls, ThreadingMixIn):
        return cls
    return type("Threaded" + cls.__name__, (ThreadingMixIn, cls), {"daemon_threads": True})

def _upstream(data):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(5)
    try:
        s.sendto(data, (UPSTREAM_DNS, 53))
        return s.recvfrom(4096)[0]
    finally:
        s.close()

class Resolver(BaseResolver):
    def resolve(self, request, handler):
        try:
            name = str(request.q.qname).lower().rstrip(".")
            if name in DNS_GUARD:
                return _fwd(request)
            if name in WHITELIST:
                return _fwd(request)
            mode = get_dns_mode()
            if mode == "pass":
                return _fwd(request)
            hij = True
            if mode == "probe":
                hosts = get_hijack_hosts()
                hij = any(name == h or name.endswith("." + h) for h in hosts)
            _recent_add(name, hij)
            if not hij:
                return _fwd(request)
            reply = request.reply()
            if request.q.qtype == QTYPE.A:
                reply.add_answer(RR(request.q.qname, QTYPE.A, rdata=A(PORTAL_IP), ttl=10))
            return reply
        except Exception:
            return request.reply()

def start_dns():
    quiet = DNSLogger("-request,-reply,-truncated")
    try:
        d = DNSServer(Resolver(), address=DNS_BIND, port=DNS_PORT, server=_pick(UDPServer), logger=quiet)
        threading.Thread(target=d.start, daemon=True).start()
        log.info("DNS(UDP) %s:%d 已启动", DNS_BIND, DNS_PORT)
    except Exception as e:
        log.error("DNS(UDP) 启动失败：%s", e)
    try:
        d2 = DNSServer(Resolver(), address=DNS_BIND, port=DNS_PORT, tcp=True, server=_pick(TCPServer), logger=quiet)
        threading.Thread(target=d2.start, daemon=True).start()
        log.info("DNS(TCP) %s:%d 已启动", DNS_BIND, DNS_PORT)
    except Exception as e:
        log.error("DNS(TCP) 启动失败：%s", e)

# ---------- 网络诊断页 ----------
def _hosted_status():
    macs, raw = [], ""
    try:
        raw = subprocess.run(["netsh", "wlan", "show", "hostednetwork"],
                             capture_output=True, text=True, timeout=8,
                             creationflags=_NO_WINDOW).stdout or ""
        seen = []
        for tok in re.findall(r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}", raw):
            t = tok.lower()
            if t not in seen:
                seen.append(t)
        macs = seen
    except Exception as e:
        raw = "获取失败：%s" % e
    return macs, raw

@app.route("/admin/dns", methods=["GET", "POST"])
def admin_dns():
    if not admin_ok():
        return admin_login_page()
    msg = ""
    act = request.form.get("act")
    try:
        if act == "setmode":
            m = request.form.get("mode", "probe")
            if m in ("probe", "hijack", "pass"):
                cset("dns_mode", m)
                _dns_mode_cache[1] = 0.0
                msg = "模式已切换为 " + m
        elif act == "addhost":
            h = request.form.get("host", "").strip().lower()
            if h and "." in h and h not in DNS_GUARD:
                hosts = get_hijack_hosts()
                if h not in hosts:
                    hosts.append(h)
                    cset("probe_hosts", "\n".join(hosts))
                    _hosts_cache[1] = 0.0
                    msg = "已加入劫持列表：" + h
                else:
                    msg = "已在列表中"
            elif h in DNS_GUARD:
                msg = "该域名受保护（Windows 自检），不可劫持"
        elif act == "delhost":
            h = request.form.get("host", "").strip().lower()
            hosts = [x for x in get_hijack_hosts() if x != h]
            cset("probe_hosts", "\n".join(hosts))
            _hosts_cache[1] = 0.0
            msg = "已移除：" + h
        elif act == "resethosts":
            cset("probe_hosts", "\n".join(DEFAULT_PROBE_HOSTS))
            _hosts_cache[1] = 0.0
            msg = "已恢复默认探测域名列表"
    except Exception as e:
        msg = "操作失败：%s" % e
    mode = get_dns_mode()
    hosts = get_hijack_hosts()
    hrows = ""
    for h in hosts:
        hrows += ("<tr><td>%s</td><td><form method='post' action='/admin/dns' style='display:inline'>"
                  "<input type='hidden' name='act' value='delhost'>"
                  "<input type='hidden' name='host' value='%s'>"
                  "<button class='mini'>移除</button></form></td></tr>" % (esc(h), esc(h)))
    with _dns_recent_lock:
        rec = list(_dns_recent)
    rrows = ""
    for ts, name, hij in reversed(rec[-60:]):
        try:
            t = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        except Exception:
            t = "?"
        rrows += ("<tr><td>%s</td><td>%s</td><td>%s</td></tr>"
                  % (t, esc(name), "<span style='color:#c0392b'>是</span>" if hij else "否"))
    macs, raw = _hosted_status()
    def _ck(v):
        return " checked" if mode == v else ""
    body = (back_btn("/admin") + ("<p class='err'>" + esc(msg) + "</p>" if msg else "")
            + "<h4>当前模式：" + esc(mode) + "（probe=仅探测域名 / hijack=全量 / pass=放行）</h4>"
            "<form method='post' action='/admin/dns'>"
            "<input type='hidden' name='act' value='setmode'>"
            "<label><input type='radio' name='mode' value='probe'" + _ck("probe") + ">probe</label>"
            "<label><input type='radio' name='mode' value='hijack'" + _ck("hijack") + ">hijack</label>"
            "<label><input type='radio' name='mode' value='pass'" + _ck("pass") + ">pass</label>"
            "<button type='submit'>切换模式</button></form>"
            "<p class='tip'>受保护域名（永不劫持）：" + esc("、".join(sorted(DNS_GUARD))) + "</p>"
            "<h4>劫持/探测域名列表</h4>"
            "<table border='1'>" + (hrows or "<tr><td class='tip'>空</td></tr>") + "</table>"
            "<form method='post' action='/admin/dns' style='display:flex;gap:6px'>"
            "<input type='hidden' name='act' value='addhost'>"
            "<input name='host' placeholder='新增域名，如 connectivitycheck.gstatic.com'>"
            "<button type='submit' style='width:auto;padding:10px 16px'>加入</button></form>"
            "<form method='post' action='/admin/dns'>"
            "<input type='hidden' name='act' value='resethosts'>"
            "<button class='mini'>恢复默认列表</button></form>"
            "<h4>最近 DNS 查询（最近 60 条，劫持=是 的域名即门户触发源）</h4>"
            "<table border='1'><tr><th>时间</th><th>域名</th><th>劫持</th></tr>"
            + (rrows or "<tr><td colspan='3' class='tip'>暂无记录</td></tr>") + "</table>"
            "<h4>承载网络实时状态（关联客户端 MAC " + str(len(macs)) + " 个，含 BSSID）</h4>"
            "<pre>" + esc(raw) + "</pre>"
            + "<p class='tip'><a href='/admin'>返回管理中心</a></p>" + FOOTER)
    return (f"<!doctype html><html><head><meta charset='utf-8'>{STYLE}<title>网络诊断</title>"
            f"</head><body><h3>{APP_NAME} · 网络诊断</h3>{body}</body></html>")

# ===== 按 relay_apps.json 挂载子模块（两轮：先 import 后 init/register） =====
# 统一 init 签名：def init(m, ast=None)；resource 使用 ast（Assistant 让路判断），
# 其余模块可忽略；assistant 建议在配置中排在首位。
_loaded = []
_ast_mod = None
for _a in APPS:
    try:
        _m = importlib.import_module(_a["module"])
        _loaded.append((_a, _m))
        if _a["key"] == "assistant":
            _ast_mod = _m
    except Exception:
        log.exception("模块 %s (%s) 加载失败，已跳过（不影响其他模块）", _a["key"], _a["module"])
for _a, _m in _loaded:
    try:
        _m.init(sys.modules[__name__], _ast_mod)
        app.register_blueprint(_m.bp)
        log.info("模块 %s (%s) 已挂载", _a["key"], _a["module"])
    except Exception:
        log.exception("模块 %s (%s) init 失败，已跳过", _a["key"], _a["module"])

# 探测兜底 + 未知路径回门户
_PROBE_MARKS = ("generate_204", "gen_204", "connecttest", "ncsi", "hotspot-detect")
_PROBE_SEEN = {}
_PROBE_SEEN_LOCK = threading.Lock()

@app.route("/<path:rest>")
def anypath(rest):
    low = rest.lower()
    if any(m in low for m in _PROBE_MARKS):
        key = (request.remote_addr, low)
        now = time.time()
        with _PROBE_SEEN_LOCK:
            if now - _PROBE_SEEN.get(key, 0) > 300:
                _PROBE_SEEN[key] = now
                if len(_PROBE_SEEN) > 512:
                    _PROBE_SEEN.clear()
                need_log = True
            else:
                need_log = False
        if need_log:
            log.info("网络探测 ip=%s path=/%s", request.remote_addr, rest[:60])
        return ("<!doctype html><html><head><meta charset='utf-8'>"
                "<meta http-equiv='refresh' content='0;url=/'>"
                "<title>Relay</title></head><body>Relay</body></html>", 200,
                {"Content-Type": "text/html; charset=utf-8"})
    u = current_user()
    return redirect("/home") if u else login_page()

if __name__ == "__main__":
    try:
        init_common_db()
        start_dns()
        for _a, _m in _loaded:
            if hasattr(_m, "start_background"):
                try:
                    _m.start_background()
                    log.info("模块 %s 后台线程已启动", _a["key"])
                except Exception:
                    log.exception("模块 %s 后台线程启动失败", _a["key"])
        log.info("Relay %s · %s · Build %d 门户 http://0.0.0.0:%d （waitress 线程 %d）—— Developer %s",
                 RELAY_VERSION, RELAY_CODENAME, RELAY_BUILD, HTTP_PORT, HTTP_THREADS, DEVELOPER)
        log.info("DNS 劫持模式：%s（probe=仅探测域名劫持；hijack=全量劫持；pass=全放行）", get_dns_mode())
        log.info("管理 http://127.0.0.1:%d/admin · 网络诊断 /admin/dns · "
                 "模块：%s · DNS %s:%d",
                 HTTP_PORT, "、".join(a["key"] for a in APPS), DNS_BIND, DNS_PORT)
        serve(app, host="0.0.0.0", port=HTTP_PORT, threads=HTTP_THREADS)
    except Exception:
        log.exception("服务异常退出")
        raise
