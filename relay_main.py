# -*- coding: utf-8 -*-
"""
Relay 3.0 · CTU 主程序

CTU 变更:
  - accounts 表补 last_seen / last_query / tokens_total 三列(assistant.py 的
    Token 统计与最近提问依赖;旧库自动迁移,列已存在则跳过)
  - 新增 /unbind 路由(Assistant 页「解绑本设备」此前是死链)
  - 新增全局 500 捕获:任何未处理异常 → 完整 traceback 写入 relay.log
    (关键字 HTTP 500 path=...)+ 友好错误页;投用后看日志即可定位,无需盲猜
Build 43 变更(保留):
  - DNS TCP/UDP 分流转发(修复大应答/HTTPS RR 截断导致教师机 Edge 无法上网)
  - waitress connection_limit=1024 / backlog=2048(默认 100 是 64 台接入的瓶颈)
  - probe 域名扩充(含 dns_spy 实测确认的 connectivitycheck.cbg-app.huawei.com.cn)
  - anypath 改 302(Hold v2 关键;认证 WebView 对 200+meta refresh 不可靠)
  - Hold 四件套:HOLD_URL_PREFIXES 含 /home · before_request 服务端记录 last_url
    · device_id cookie 优先(闪退后 MAC 失效不再踢回登录) · hold_loc 滚动恢复
  - 自定义劫持域名默认空(yunzuoye 系已确认非目标;旧默认值自动迁移清空)
  - DIRECT 投屏热点:7378 RPC(appId 已内置,提取自 util.js)· GO 检测 ·
    一键自愈 · watchdog 15 分钟自动自愈
  - AI 服务集中化:Key 在管理中心配置;assistant/resource 经 M.ai_post 共用
  - relay_apps.json 兼容 {"apps":[...]} 包装与 title/desc/admin_url 字段;
    模块蓝图自动注册;管理链接使用 admin_url
"""
import hashlib, json, logging, os, re, secrets, socket, socketserver, sqlite3, struct
import subprocess, threading, time, types
from datetime import datetime
from html import escape as esc
from flask import Flask, HTTPException, g, make_response, redirect, request
from waitress import serve
try:
    import dnslib
    from dnslib import DNSRecord, RR, QTYPE, A
except ImportError:
    dnslib = None
try:
    import requests as _rq
except ImportError:
    _rq = None

RELAY_VERSION = "3.0"
BUILD = "CTU"
ADMIN_PASSWORD = "admin123"          # 部署后务必修改
HTTP_PORT = 80
HTTP_THREADS = 128
DNS_BIND = "127.0.0.1"               # 勿改 0.0.0.0(与 ICS DNS 代理抢 53)
DNS_UPSTREAM = "223.5.5.5"
PORTAL_IP = "192.168.137.1"
SOFTAP_HOST, SOFTAP_PORT = "127.0.0.1", 7378

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DIR_COMMON = os.path.join(DATA_DIR, "common")
DIR_ASSISTANT = os.path.join(DATA_DIR, "assistant")
DIR_RESOURCE = os.path.join(DATA_DIR, "resource")
DIR_RC = os.path.join(DATA_DIR, "remoteclasswork")
DIR_DICT = os.path.join(DATA_DIR, "dictionary")
DIR_WEATHER = os.path.join(DATA_DIR, "weather")
DIR_MUSIC = os.path.join(DATA_DIR, "music")
for _d in (DIR_COMMON, DIR_ASSISTANT, DIR_RESOURCE, DIR_RC, DIR_DICT, DIR_WEATHER, DIR_MUSIC):
    os.makedirs(_d, exist_ok=True)
COMMON_DB = os.path.join(DIR_COMMON, "common.db")
APPS_JSON = os.path.join(BASE_DIR, "relay_apps.json")
RELAY_LOG = os.path.join(DIR_COMMON, "relay.log")

# Relay 出站域名白名单(网络诊断页连通性展示;模块通过 whitelist_add 注册)
_outbound_whitelist = ["open.bigmodel.cn"]

# 探测域名(CTU 沿用;cbg-app 行来自 dns_spy 实测)
_PROBE_HOSTS = set([
    "connectivitycheck.gstatic.com", "www.google.com", "connectivitycheck.android.com",
    "play.googleapis.com", "www.gstatic.cn", "dns.google",
    "captive.apple.com", "www.apple.com",
    "connect.rom.miui.com", "wifi.vivo.com.cn", "conn1.oppomobile.com",
    "connectivitycheck.platform.hicloud.com", "connectivitycheck.cloud.huawei.com.cn",
    "connectivitycheck.cbg-app.huawei.com.cn",
    "connectivitycheck.samsung.com", "connectivitycheck.samsung.com.cn",
    "samsung.com", "samsung.com.cn",
    "detectportal.firefox.com", "edge.microsoft.com",
    "www.msftconnecttest.com", "msftconnecttest.com", "www.baidu.com",
])
_PROBE_MARKS = ("generate_204", "gen_204", "connecttest", "connectivitycheck",
                "captive", "hotspot-detect", "detectportal", "dns-test",
                "msftconnecttest", "networkdetect", "wifi_check", "wxcheck",
                "wifi.vivo", "rom.miui", "hicloud", "samsung")

# 自定义劫持域名默认值:空。入口排查时:平板点一次「更新说明」→
# /admin/dns 日志出现新域名 → 点「劫持」
DEFAULT_HIJACK = ""
_OLD_DEFAULT_HIJACK = ("xh-res.ztytech.com\n"
                       "xuehaifile.oss-cn-hangzhou.aliyuncs.com\n"
                       "appstore.yunzuoye.net\n"
                       "*.yunzuoye.net")

COOKIE_USER = "relay_u"
COOKIE_DEV = "relay_d"

# ---------- 日志 ----------
log = logging.getLogger("relay.main")
log.setLevel(logging.INFO)
if not log.handlers:
    _h = logging.FileHandler(RELAY_LOG, encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_h)
log.propagate = False

def _mklog(name, path):
    lg = logging.getLogger(name)
    lg.setLevel(logging.INFO)
    if not lg.handlers:
        h = logging.FileHandler(path, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        lg.addHandler(h)
    lg.propagate = False
    return lg

# ---------- DB / settings ----------
def dbq(path, sql, args=()):
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        cur = conn.execute(sql, args)
        if sql.strip().upper().startswith(("SELECT", "PRAGMA")):
            return cur.fetchall()
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()

def _init_common_db():
    dbq(COMMON_DB, """CREATE TABLE IF NOT EXISTS accounts(
        username TEXT PRIMARY KEY, password TEXT NOT NULL,
        device_id TEXT DEFAULT '', last_url TEXT DEFAULT '',
        last_loc TEXT DEFAULT '', last_loc_ts REAL DEFAULT 0,
        hold_key TEXT DEFAULT '', created_at TEXT DEFAULT '',
        last_seen TEXT DEFAULT '', last_query TEXT DEFAULT '',
        tokens_total INTEGER DEFAULT 0)""")
    # CTU:旧库迁移——assistant.py 的 Token 统计/最近提问依赖这三列
    for _col, _typ in (("last_seen", "TEXT DEFAULT ''"),
                       ("last_query", "TEXT DEFAULT ''"),
                       ("tokens_total", "INTEGER DEFAULT 0")):
        try:
            dbq(COMMON_DB, "ALTER TABLE accounts ADD COLUMN %s %s" % (_col, _typ))
        except Exception:
            pass  # 列已存在
    try:
        dbq(COMMON_DB, "ALTER TABLE accounts ADD COLUMN hold_key TEXT DEFAULT ''")
    except Exception:
        pass
    dbq(COMMON_DB, """CREATE TABLE IF NOT EXISTS settings(
        k TEXT PRIMARY KEY, v TEXT DEFAULT '')""")
    rows = dbq(COMMON_DB, "SELECT v FROM settings WHERE k=?", ("hijack_domains",))
    if not rows:
        dbq(COMMON_DB, "INSERT INTO settings(k,v) VALUES(?,?)", ("hijack_domains", DEFAULT_HIJACK))
    elif (rows[0][0] or "").strip() == _OLD_DEFAULT_HIJACK.strip():
        # 一次性迁移:库里仍是旧默认名单(含 yunzuoye)→ 清空;用户自定义过则不动
        dbq(COMMON_DB, "UPDATE settings SET v='' WHERE k='hijack_domains'")
        log.info("hijack_domains 已从旧默认值迁移为空(yunzuoye 移除)")

def get_setting(key, default=""):
    try:
        rows = dbq(COMMON_DB, "SELECT v FROM settings WHERE k=?", (key,))
        return rows[0][0] if rows else default
    except Exception:
        return default

def set_setting(key, value):
    dbq(COMMON_DB, "INSERT INTO settings(k,v) VALUES(?,?) "
                   "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))

_init_common_db()

# ---------- 页面公共样式 ----------
STYLE = ("<style>"
 "*{box-sizing:border-box;font-family:'Segoe UI','Microsoft YaHei',sans-serif}"
 "body{max-width:760px;margin:0 auto;padding:12px;background:#f0f2f5;color:#1f2937;font-size:16px}"
 "h3,h4{margin:14px 0 8px}"
 "input,textarea{width:100%;padding:10px;margin:5px 0;border:1px solid #cbd5e1;border-radius:8px;font-size:16px}"
 "button{width:100%;padding:12px;margin:6px 0;border:0;border-radius:8px;"
 "background:#2563eb;color:#fff;font-size:16px}"
 "button.mini{width:auto;padding:6px 12px;font-size:14px;background:#475569}"
 "a.btn,.btn{display:block;text-align:center;background:#fff;border:1px solid #cbd5e1;"
 "border-radius:10px;padding:12px;margin:8px 0;text-decoration:none;color:#1f2937}"
 ".card{display:block;background:#fff;border-radius:12px;padding:14px;margin:8px 0;"
 "text-decoration:none;color:#1f2937;box-shadow:0 1px 3px rgba(0,0,0,.08)}"
 ".card .mi{font-size:26px;margin-right:10px}"
 "table{width:100%;border-collapse:collapse;background:#fff;font-size:14px}"
 "td,th{border:1px solid #cbd5e1;padding:6px}"
 ".tip{color:#64748b;font-size:14px}"
 ".err{background:#fee2e2;color:#991b1b;border-radius:8px;padding:10px}"
 ".chip{display:inline-block;background:#dbeafe;color:#1d4ed8;border-radius:10px;"
 "padding:2px 10px;font-size:13px}"
 ".chip.red{background:#fee2e2;color:#991b1b}.chip.green{background:#dcfce7;color:#166534}"
 ".mcard{display:flex;align-items:center;background:#fff;border-radius:14px;padding:16px;"
 "margin:8px 0;text-decoration:none;color:#1f2937;box-shadow:0 1px 3px rgba(0,0,0,.08)}"
 ".mcard .mi{font-size:30px;margin-right:14px}.mcard .mn{font-size:17px;font-weight:600}"
 "details summary{cursor:pointer}"
 "</style>")
ANIM_STYLE = ("<style>@keyframes fin{from{opacity:0;transform:translateY(6px)}"
 "to{opacity:1;transform:none}}body{animation:fin .25s ease}</style>")
FOOTER = ("<p style='text-align:center;color:#94a3b8;font-size:13px;margin:20px 0 6px'>"
          "Relay %s · %s</p>" % (RELAY_VERSION, BUILD))
REOPEN_TIP = ("<p class='tip' style='text-align:center'>闪退/断网重开后将自动回到当前位置</p>")

def back_btn(path):
    return "<a class='btn' href='%s'>‹ 返回</a>" % path

# ---------- App(必须先于一切 @app.route) ----------
app = Flask(__name__)

# ---------- 设备识别 / 用户 ----------
_mac_cache = {}
_mac_lock = threading.Lock()

def _arp_lookup(ip):
    try:
        out = subprocess.run(["arp", "-a", ip], capture_output=True, timeout=4).stdout
        txt = out.decode("gbk", "ignore") + out.decode("utf-8", "ignore")
        m = re.search(r"([0-9a-fA-F]{2}[-:]){5}[0-9a-fA-F]{2}", txt)
        if m:
            return m.group(0).replace("-", ":").lower()
    except Exception:
        pass
    return None

def get_mac(ip):
    with _mac_lock:
        v = _mac_cache.get(ip)
        if v and time.time() - v[1] < 60:
            return v[0]
    mac = _arp_lookup(ip)
    with _mac_lock:
        _mac_cache[ip] = (mac, time.time())
        if len(_mac_cache) > 512:
            _mac_cache.clear()
    return mac

def device_id():
    """CTU 沿用 Build 43:cookie 优先(跨 WLAN 开关/闪退稳定);MAC 兜底;随机兜底。"""
    tok = request.cookies.get(COOKIE_DEV)
    if tok:
        return "c:" + tok
    ip = request.remote_addr or ""
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
        mac = get_mac(ip)
        if mac:
            return mac
    if not hasattr(g, "new_dev"):
        g.new_dev = secrets.token_hex(16)
    return "c:" + g.new_dev

def current_user():
    u = request.cookies.get(COOKIE_USER)
    if not u:
        return None
    try:
        rows = dbq(COMMON_DB, "SELECT device_id FROM accounts WHERE username=?", (u,))
        if rows and rows[0][0] == device_id():
            return u
    except Exception:
        pass
    return None

def _device_bind_value():
    tok = request.cookies.get(COOKIE_DEV)
    if tok:
        return "c:" + tok, tok
    ip = request.remote_addr or ""
    mac = get_mac(ip) if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip) else None
    if mac:
        return mac, None
    tok = secrets.token_hex(16)
    return "c:" + tok, tok

# ---------- Hold v2 ----------
APPS = []
HOLD_URL_PREFIXES = ()

def hold_page(key):
    u = current_user()
    if not u:
        return
    try:
        dbq(COMMON_DB, "UPDATE accounts SET hold_key=? WHERE username=?", (key, u))
    except Exception:
        pass

def hold_loc_js(u):
    return ("<script>(function(){var t=null,sent=false;"
            "function send(sy){var x=new XMLHttpRequest();x.open('POST','/hold/loc',true);"
            "x.setRequestHeader('Content-Type','application/x-www-form-urlencoded');"
            "x.send('loc='+Math.round(window.scrollY||0));}"
            "window.addEventListener('scroll',function(){if(t)return;"
            "t=setTimeout(function(){t=null;send();},1500);},false);"
            "window.addEventListener('pagehide',function(){"
            "var x=new XMLHttpRequest();x.open('POST','/hold/loc',false);"
            "x.send('loc='+Math.round(window.scrollY||0));},false);"
            "try{fetch('/hold/loc?get=1').then(function(r){return r.text()})"
            ".then(function(v){var y=parseInt(v||'0');"
            "if(y>50&&!sent){sent=true;setTimeout(function(){window.scrollTo(0,y);},300);}});}catch(e){}"
            "})();</script>")

@app.route("/hold/loc", methods=["GET", "POST"])
def hold_loc():
    u = current_user()
    if not u:
        return ("", 204)
    if request.method == "GET":
        try:
            rows = dbq(COMMON_DB, "SELECT last_loc FROM accounts WHERE username=?", (u,))
            return (rows[0][0] or "0", 200, {"Content-Type": "text/plain"})
        except Exception:
            return ("0", 200, {"Content-Type": "text/plain"})
    loc = request.form.get("loc", "")
    try:
        dbq(COMMON_DB, "UPDATE accounts SET last_loc=?, last_loc_ts=? WHERE username=?",
            (loc[:60], time.time(), u))
    except Exception:
        pass
    return ("", 204)

def shell(title, body, extra_head=""):
    u = current_user()
    js = hold_loc_js(u) if u else ""
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            + extra_head + STYLE + ANIM_STYLE
            + "<title>" + esc(title) + "</title></head><body>" + body + REOPEN_TIP
            + FOOTER + js + "</body></html>")

def login_page(msg=""):
    body = ("<h3 style='text-align:center'>Relay 学习门户</h3>"
            + ("<p class='err'>%s</p>" % esc(msg) if msg else "")
            + "<form method='post' action='/login'>"
              "<input name='u' placeholder='账号' required>"
              "<input name='p' type='password' placeholder='密码' required>"
              "<button>登录</button></form>"
              "<p class='tip'>提示:如页面记忆未恢复,请重新登录一次(设备绑定机制已升级)。</p>")
    return shell("登录", body)

# ---------- 路由 ----------
@app.route("/health")
def health():
    return "ok"

@app.route("/favicon.ico")
def favicon():
    return ("", 204)

@app.route("/")
def root():
    u = current_user()
    if u:
        try:
            rows = dbq(COMMON_DB, "SELECT last_url FROM accounts WHERE username=?", (u,))
            last = (rows[0][0] or "").strip() if rows else ""
        except Exception:
            last = ""
        if last and any(last == p or last.startswith(p + "?") or last.startswith(p + "/")
                        for p in HOLD_URL_PREFIXES):
            return redirect(last, 302)
        return redirect("/home", 302)
    return login_page()

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return login_page()
    u = request.form.get("u", "").strip()
    p = request.form.get("p", "")
    rows = dbq(COMMON_DB, "SELECT password, last_url FROM accounts WHERE username=?", (u,))
    if not rows or rows[0][0] != p:
        return login_page("账号或密码错误")
    dev, tok = _device_bind_value()
    dbq(COMMON_DB, "UPDATE accounts SET device_id=? WHERE username=?", (dev, u))
    last = (rows[0][1] or "").strip()
    target = last if (last and any(last == pp or last.startswith(pp + "?")
                                   for pp in HOLD_URL_PREFIXES)) else "/home"
    resp = make_response(redirect(target, 302))
    resp.set_cookie(COOKIE_USER, u, max_age=90 * 24 * 3600)
    if tok:
        resp.set_cookie(COOKIE_DEV, tok, max_age=365 * 24 * 3600)
    log.info("登录 %s dev=%s", u, dev[:20])
    return resp

@app.route("/logout")
def logout():
    resp = make_response(redirect("/", 302))
    resp.delete_cookie(COOKIE_USER)
    return resp

@app.route("/unbind")
def unbind():
    """CTU 新增:Assistant 页「解绑本设备」——清除设备绑定并退出登录。"""
    u = current_user()
    if u:
        try:
            dbq(COMMON_DB, "UPDATE accounts SET device_id='' WHERE username=?", (u,))
            log.info("解绑设备 user=%s", u)
        except Exception:
            pass
    resp = make_response(redirect("/", 302))
    resp.delete_cookie(COOKIE_USER)
    resp.delete_cookie(COOKIE_DEV)
    return resp

@app.route("/home")
def home():
    u = current_user()
    if not u:
        return login_page()
    cards = ""
    for a in APPS:
        k = a.get("key", "")
        nm = a.get("name") or k
        ds = a.get("desc") or ""
        cards += ("<a class='mcard' href='/%s'><div><span class='mn'>%s</span>%s</div></a>"
                  % (esc(k), esc(nm),
                     ("<div class='tip' style='margin-top:2px'>%s</div>" % esc(ds)) if ds else ""))
    try:
        rows = dbq(COMMON_DB, "SELECT last_url, hold_key FROM accounts WHERE username=?", (u,))
        last = (rows[0][0] or "").strip() if rows else ""
        hkey = (rows[0][1] or "") if rows else ""
    except Exception:
        last, hkey = "", ""
    if last and any(last == pp or last.startswith(pp + "?") for pp in HOLD_URL_PREFIXES):
        cards += ("<a class='btn' href='%s'>↻ 继续上次:%s</a>"
                  % (esc(last), esc(hkey or last.split("?")[0])))
    return shell("Relay", "<h3>Relay 学习门户</h3>" + cards)

# ---------- before_request:服务端记录 last_url(不依赖 JS 心跳) ----------
_last_url_mem = {}
_last_url_lock = threading.Lock()

@app.before_request
def track_module_url():
    try:
        if request.method != "GET":
            return
        p = request.path
        if p.startswith("/hold") or p in ("/health", "/favicon.ico", "/logout", "/unbind"):
            return
        if not any(p == pf or p.startswith(pf + "?") or p.startswith(pf + "/")
                   for pf in HOLD_URL_PREFIXES):
            return
        u = current_user()
        if not u:
            return
        qs = request.query_string.decode("utf-8", "ignore")
        url = p + (("?" + qs) if qs else "")
        if len(url) > 300:
            return
        with _last_url_lock:
            if _last_url_mem.get(u) == url:
                return
            _last_url_mem[u] = url
        dbq(COMMON_DB, "UPDATE accounts SET last_url=?, last_loc_ts=? WHERE username=?",
            (url, time.time(), u))
    except Exception:
        pass

@app.after_request
def finish(resp):
    try:
        ct = resp.headers.get("Content-Type", "")
        if "text/html" in ct:
            resp.headers["Cache-Control"] = "no-store"
        elif "audio/" in ct or ct.startswith("image/"):
            resp.headers["Cache-Control"] = "public, max-age=86400"
    except Exception:
        pass
    return resp

# ---------- CTU:全局 500 捕获(traceback 落 relay.log + 友好错误页) ----------
@app.errorhandler(Exception)
def _err_log_and_page(e):
    """任何未捕获异常 → relay.log 记完整 traceback(搜 'HTTP 500 path=')→
    返回友好错误页。404 等 HTTPException 原样返回,不受影响。"""
    if isinstance(e, HTTPException):
        return e
    try:
        log.error("HTTP 500 path=%s", request.path,
                  exc_info=(type(e), e, e.__traceback__))
    except Exception:
        pass
    return ("<!doctype html><meta charset='utf-8'><title>错误</title>"
            "<body style='font-family:sans-serif;max-width:600px;margin:40px auto'>"
            "<h3>服务器内部错误</h3>"
            "<p>详情已记录到 data/common/relay.log——把含 'HTTP 500 path="
            + esc(request.path) + "' 的最后一段日志发给开发者即可定位。</p>"
            "<p><a href='/'>返回门户</a></p></body>", 500)

# ---------- 管理体系 ----------
def _admin_token():
    return hashlib.sha256(("relay-admin:" + ADMIN_PASSWORD).encode()).hexdigest()

def admin_ok():
    return request.cookies.get("relay_admin", "") == _admin_token()

def admin_login_page(msg=""):
    body = ("<h3>管理员登录</h3>"
            + ("<p class='err'>%s</p>" % esc(msg) if msg else "")
            + "<form method='post' action='/admin'>"
              "<input name='p' type='password' placeholder='管理密码'>"
              "<button>进入管理中心</button></form>"
              "<p><a href='/home'>返回门户</a></p>")
    return shell("管理登录", body)

@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        if request.form.get("p", "") == ADMIN_PASSWORD:
            resp = make_response(redirect("/admin", 302))
            resp.set_cookie("relay_admin", _admin_token(), max_age=8 * 3600)
            return resp
        return admin_login_page("密码错误")
    if not admin_ok():
        return admin_login_page()
    users = dbq(COMMON_DB, "SELECT username, device_id, last_url FROM accounts "
                           "ORDER BY username LIMIT 200")
    urows = ""
    for un, dev, lu in users:
        urows += ("<tr><td>%s</td><td class='tip'>%s</td>"
                  "<td><form method='post' action='/admin/users' style='display:inline'>"
                  "<input type='hidden' name='act' value='del'>"
                  "<input type='hidden' name='u' value='%s'>"
                  "<button class='mini'>删</button></form></td></tr>"
                  % (esc(un), esc((dev or "")[:14]), esc(un)))
    modlinks = ""
    for a in APPS:
        aurl = a.get("admin_url") or ("/admin/" + (a.get("key") or ""))
        modlinks += ("<a class='card' href='%s'><b>%s 管理</b> · %s</a>"
                     % (esc(aurl), esc(a.get("name") or a.get("key", "")),
                        esc(a.get("admin_desc") or a.get("key", ""))))
    ai_key_set = "已配置" if get_setting("ai_api_key", "") else "未配置"
    body = (back_btn("/home") + "<h3>管理中心</h3>"
            + modlinks
            + "<a class='card' href='/admin/dns'><b>网络诊断 / DNS / DIRECT 扩容</b></a>"
            + "<h4>AI 服务</h4>"
              "<form method='post' action='/admin/aikey'>"
              "<input name='ai_api_key' type='password' "
              "placeholder='GLM API Key(必填,否则 AI 问答/资源索引不可用)'>"
              "<input name='ai_model' value='%s' placeholder='模型名'>"
              "<button class='mini'>保存 AI 配置</button></form>"
              "<p class='tip'>当前状态:%s · 模型:%s</p>"
              % (esc(get_setting("ai_model", "glm-4-flash")), ai_key_set,
                 esc(get_setting("ai_model", "glm-4-flash")))
            + "<h4>账号管理</h4>"
            "<form method='post' action='/admin/users'>"
            "<input type='hidden' name='act' value='add'>"
            "<input name='u' placeholder='账号'><input name='p' placeholder='密码'>"
            "<button class='mini'>添加账号</button></form>"
            "<form method='post' action='/admin/users'>"
            "<input type='hidden' name='act' value='bulk'>"
            "<textarea name='bulk' rows='5' placeholder='批量导入:每行「账号 密码」'></textarea>"
            "<button class='mini'>批量导入</button></form>"
            "<table><tr><th>账号</th><th>设备</th><th></th></tr>"
            + (urows or "") + "</table>"
            "<h4>危险操作</h4>"
            "<form method='post' action='/admin/users'>"
            "<input type='hidden' name='act' value='clearhold'>"
            "<button class='mini' style='background:#dc2626'>清空全部 Hold 数据</button></form>"
            "<p class='tip'><a href='/logout'>退出管理</a></p>" + FOOTER)
    return shell("管理", body)

@app.route("/admin/users", methods=["POST"])
def admin_users():
    if not admin_ok():
        return admin_login_page()
    act = request.form.get("act", "")
    try:
        if act == "add":
            u = request.form.get("u", "").strip()
            p = request.form.get("p", "").strip()
            if u and p:
                dbq(COMMON_DB, "INSERT OR REPLACE INTO accounts(username,password,created_at) "
                               "VALUES(?,?,?)", (u, p, datetime.now().strftime("%Y-%m-%d %H:%M")))
        elif act == "del":
            dbq(COMMON_DB, "DELETE FROM accounts WHERE username=?", (request.form.get("u", ""),))
        elif act == "bulk":
            n = 0
            for line in request.form.get("bulk", "").splitlines():
                parts = re.split(r"[,\s]+", line.strip())
                if len(parts) >= 2 and parts[0] and parts[1]:
                    dbq(COMMON_DB, "INSERT OR REPLACE INTO accounts(username,password,created_at) "
                                   "VALUES(?,?,?)",
                        (parts[0], parts[1], datetime.now().strftime("%Y-%m-%d %H:%M")))
                    n += 1
            log.info("批量导入 %d 账号", n)
        elif act == "clearhold":
            clearhold_all()
    except Exception as e:
        log.exception("admin_users 失败")
        return admin_login_page("操作失败:%s" % e)
    return redirect("/admin", 302)

@app.route("/admin/aikey", methods=["POST"])
def admin_aikey():
    if not admin_ok():
        return admin_login_page()
    k = request.form.get("ai_api_key", "").strip()
    m = request.form.get("ai_model", "").strip()
    if k:
        set_setting("ai_api_key", k[:200])
    if m:
        set_setting("ai_model", m[:60])
    log.info("AI 配置已更新(model=%s)", m or "(未改)")
    return redirect("/admin", 302)

def clearhold_all():
    dbq(COMMON_DB, "UPDATE accounts SET last_url='', last_loc='', last_loc_ts=0, hold_key=''")
    try:
        p = os.path.join(DIR_MUSIC, "music.db")
        if os.path.exists(p):
            dbq(p, "DELETE FROM progress")
            dbq(p, "DELETE FROM prefs")
    except Exception:
        pass
    log.info("clearhold_all 已执行")

# ---------- AI 服务(assistant / resource 共用;Key 在管理中心配置) ----------
AI_BASE_DEFAULT = "https://open.bigmodel.cn/api/paas/v4"
AI_MODEL = "glm-4-flash"

def ai_post(session, payload, timeout):
    key = get_setting("ai_api_key", "")
    if not key:
        raise RuntimeError("AI API Key 未配置(管理中心 → AI 服务)")
    base = get_setting("ai_base", AI_BASE_DEFAULT).rstrip("/")
    try:
        payload = dict(payload)
        payload["model"] = get_setting("ai_model", AI_MODEL)
    except Exception:
        pass
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    return session.post(base + "/chat/completions", json=payload,
                        headers=headers, timeout=timeout)

def msg_page(message, back="/home"):
    body = (back_btn(back) + "<h3>提示</h3>"
            "<p class='err'>%s</p>" % esc(str(message)))
    return shell("提示", body)

# ---------- DNS 服务器 ----------
_dns_log = []
_dns_log_lock = threading.Lock()
_probe_seen = {}
_probe_lock = threading.Lock()
_dns_mode_cache = {"v": "probe", "ts": 0.0}
_hijack_cache = {"ts": 0.0, "exact": set(), "suffix": []}

def _log_dns(ip, host, hij):
    with _dns_log_lock:
        _dns_log.append((time.time(), ip, host, hij))
        del _dns_log[:-200]

def dns_mode():
    now = time.time()
    if now - _dns_mode_cache["ts"] > 30:
        _dns_mode_cache["v"] = get_setting("dns_mode", "probe")
        _dns_mode_cache["ts"] = now
    return _dns_mode_cache["v"]

def custom_hijacks():
    now = time.time()
    if now - _hijack_cache["ts"] > 60:
        ex, su = set(), []
        for line in get_setting("hijack_domains", "").splitlines():
            d = line.strip().lower().lstrip(".")
            if not d:
                continue
            if d.startswith("*."):
                su.append("." + d[2:])
            else:
                ex.add(d)
        _hijack_cache["ts"] = now
        _hijack_cache["exact"] = ex
        _hijack_cache["suffix"] = su
    return _hijack_cache["exact"], _hijack_cache["suffix"]

def should_hijack(host):
    if not host:
        return False
    h = host.lower().rstrip(".")
    mode = dns_mode()
    if mode == "hijack":
        return True
    if mode == "pass":
        return False
    if h in _PROBE_HOSTS:
        return True
    ex, su = custom_hijacks()
    if h in ex:
        return True
    for s in su:
        if h.endswith(s):
            return True
    return False

def _mk_reply(q, hij):
    r = q.reply()
    if hij and q.q.qtype == QTYPE.A:
        r.add_answer(RR(q.q.qname, QTYPE.A, rdata=A(PORTAL_IP), ttl=10))
    return r

def _forward_tcp(data):
    try:
        s = socket.create_connection((DNS_UPSTREAM, 53), timeout=4)
        s.sendall(struct.pack(">H", len(data)) + data)
        hdr = b""
        while len(hdr) < 2:
            c = s.recv(2 - len(hdr))
            if not c:
                return None
            hdr += c
        ln = struct.unpack(">H", hdr)[0]
        buf = b""
        while len(buf) < ln:
            c = s.recv(min(65535, ln - len(buf)))
            if not c:
                break
            buf += c
        s.close()
        return buf if len(buf) == ln else None
    except Exception:
        return None

class _TUDP(socketserver.ThreadingUDPServer):
    allow_reuse_address = True

class _TTCP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True

class UDPH(socketserver.BaseRequestHandler):
    def handle(self):
        if not dnslib:
            return
        data, sock = self.request
        try:
            q = DNSRecord.parse(data)
        except Exception:
            return
        host = str(q.q.qname).rstrip(".").lower() if q.questions else ""
        hij = should_hijack(host)
        _log_dns(self.client_address[0], host, hij)
        if hij:
            payload = _mk_reply(q, True).pack()
        else:
            payload = None
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.settimeout(4)
                s.sendto(data, (DNS_UPSTREAM, 53))
                payload, _a = s.recvfrom(65535)
                s.close()
            except Exception:
                pass
            if not payload:
                payload = _mk_reply(q, False).pack()
        try:
            sock.sendto(payload, self.client_address)
        except Exception:
            pass

class TCPH(socketserver.BaseRequestHandler):
    def handle(self):
        if not dnslib:
            return
        try:
            hdr = b""
            while len(hdr) < 2:
                c = self.request.recv(2 - len(hdr))
                if not c:
                    return
                hdr += c
            ln = struct.unpack(">H", hdr)[0]
            data = b""
            while len(data) < ln:
                c = self.request.recv(min(65535, ln - len(data)))
                if not c:
                    return
                data += c
        except Exception:
            return
        try:
            q = DNSRecord.parse(data)
        except Exception:
            return
        host = str(q.q.qname).rstrip(".").lower() if q.questions else ""
        hij = should_hijack(host)
        _log_dns(self.client_address[0], host, hij)
        if hij:
            payload = _mk_reply(q, True).pack()
        else:
            # Build 43 关键修复:TCP 查询用 TCP 上游转发(2 字节长度前缀协议)
            payload = _forward_tcp(data)
            if not payload:
                payload = _mk_reply(q, False).pack()
        try:
            self.request.sendall(struct.pack(">H", len(payload)) + payload)
        except Exception:
            pass

def start_dns_servers():
    if not dnslib:
        log.error("dnslib 未安装,DNS 服务未启动")
        return
    try:
        udp = _TUDP((DNS_BIND, 53), UDPH)
        threading.Thread(target=udp.serve_forever, daemon=True).start()
        log.info("DNS(UDP) %s:53 已启动", DNS_BIND)
    except OSError as e:
        log.error("DNS(UDP) 启动失败:%s", e)
    try:
        tcp = _TTCP((DNS_BIND, 53), TCPH)
        threading.Thread(target=tcp.serve_forever, daemon=True).start()
        log.info("DNS(TCP) %s:53 已启动", DNS_BIND)
    except OSError as e:
        log.error("DNS(TCP) 启动失败:%s", e)

# ---------- DIRECT 投屏热点(扩容通道) ----------
SOFTAP_APPID_DEFAULT = "c9de950d-0806-4717-9ae1-81218db0b2bf"  # 提取自投屏软件 util.js

def _softap_rpc(method, param=None, timeout=4):
    """希沃投屏热点控制服务 127.0.0.1:7378 的 JSON-RPC。
    报文与 util.js 逐字段一致:
      - 端点:POST http://127.0.0.1:7378 (根路径)
      - GetSoftApInfo 无 param 字段;SetSoftApEnable 带 param
      - 成功判定:HTTP 200(util.js 只检查状态码)
    appId 已内置默认值;管理页 appId 输入框仅在需要覆盖时使用。"""
    if not _rq:
        return False, None, "requests 未安装"
    appid = get_setting("softap_appid", "") or SOFTAP_APPID_DEFAULT
    body = {"version": 1, "method": method, "appId": appid}
    if param is not None:
        body["param"] = param
    raw = json.dumps(body)
    last_err = ""
    for path in ("/", "/rpc"):           # "/" 为 util.js 实际路径;"/rpc" 兜底
        try:
            r = _rq.post("http://%s:%d%s" % (SOFTAP_HOST, SOFTAP_PORT, path),
                         data=raw, headers={"Content-Type": "application/json"},
                         timeout=timeout)
            if r.status_code == 200:
                try:
                    return True, r.json(), ""
                except ValueError:
                    return True, {"raw": r.text[:500]}, ""
            last_err = "HTTP %d" % r.status_code
        except Exception as e:
            last_err = str(e)[:120]
    return False, None, last_err

def _softap_set(enable):
    for param in ({"enable": enable}, {"Enable": enable}, {"status": 1 if enable else 0}):
        ok, _d, _e = _softap_rpc("SetSoftApEnable", param)
        if ok:
            return True, param
    return False, None

def _find_go_adapter():
    """解析 ipconfig /all,找 Wi-Fi Direct GO 虚拟适配器及其 IPv4。"""
    try:
        out = subprocess.run(["ipconfig", "/all"], capture_output=True, timeout=8).stdout
        txt = out.decode("gbk", "ignore")
    except Exception:
        return None, None
    cur_name, is_go, ip = None, False, None
    for line in txt.splitlines():
        if line and not line.startswith((" ", "\t")) and ("适配器" in line or "adapter" in line.lower()):
            if is_go and ip:
                return cur_name, ip
            cur_name = line.split(":", 1)[0].replace(":", "").strip()
            low = line.lower()
            is_go = ("wi-fi direct" in low) or ("wlan direct" in low) or \
                    ("direct" in low and "virtual" in low)
            ip = None
        elif is_go and cur_name and not ip:
            m = re.search(r"(?:IPv4 地址|IPv4 Address)[.。:：\s]*(\d+\.\d+\.\d+\.\d+)", line)
            if m:
                ip = m.group(1)
    if is_go and ip:
        return cur_name, ip
    return None, None

def _go_dns_check(go_ip, timeout=3):
    if not dnslib or not go_ip:
        return "n/a", ""
    try:
        q = DNSRecord.question("connectivitycheck.gstatic.com", "A")
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        s.sendto(q.pack(), (go_ip, 53))
        resp, _ = s.recvfrom(4096)
        s.close()
        r = DNSRecord.parse(resp)
        for rr in r.rr:
            if rr.rtype == 1:
                ip = str(rr.rdata)
                return ("hijack-ok" if ip == PORTAL_IP else "forwarded"), ip
        return "forwarded", ""
    except Exception:
        return "timeout", ""

def _go_selfheal(auto=False):
    results = []
    try:
        r1 = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                             "-File", os.path.join(BASE_DIR, "hostednet_ics.ps1"),
                             "-DnsTakeover"], capture_output=True, timeout=90)
        results.append("DNS重设:" + ("ok" if r1.returncode == 0 else "fail"))
    except Exception as e:
        results.append("DNS重设异常:%s" % str(e)[:60])
    ok1, _p1 = _softap_set(False)
    time.sleep(3)
    ok2, _p2 = _softap_set(True)
    results.append("热点重启:%s→%s" % (ok1, ok2))
    time.sleep(5)
    go_ip, _n = _find_go_adapter()
    st, ans = _go_dns_check(go_ip)
    results.append("复检:%s(%s)" % (st, go_ip or "未检测到GO"))
    log.info("DIRECT 自愈(%s):%s", "auto" if auto else "manual", " | ".join(results))
    return results

def _go_watch():
    while True:
        time.sleep(900)
        try:
            if get_setting("go_autoheal", "1") != "1":
                continue
            go_ip, _n = _find_go_adapter()
            if not go_ip:
                continue
            st, _a = _go_dns_check(go_ip)
            if st != "hijack-ok":
                _go_selfheal(auto=True)
        except Exception:
            log.exception("DIRECT 自动自愈异常")

# ---------- 网络诊断页 ----------
def _lan_clients():
    try:
        out = subprocess.run(["arp", "-a"], capture_output=True, timeout=6).stdout
        txt = out.decode("gbk", "ignore") + out.decode("utf-8", "ignore")
    except Exception:
        return []
    rows = []
    for m in re.finditer(r"(192\.168\.137\.\d+)\s+((?:[0-9a-fA-F]{2}[-:]){5}[0-9a-fA-F]{2})\s+(\S+)",
                         txt):
        rows.append((m.group(1), m.group(2).replace("-", ":").lower(), m.group(3)))
    return rows

def _probe_outbound():
    res = []
    for d in _outbound_whitelist:
        try:
            ip = socket.gethostbyname(d)
            res.append((d, ip, "ok"))
        except Exception as e:
            res.append((d, "", str(e)[:40]))
    return res

@app.route("/admin/dns", methods=["GET", "POST"])
def admin_dns():
    if not admin_ok():
        return admin_login_page()
    msg = ""
    if request.method == "POST":
        act = request.form.get("act", "")
        if act == "mode":
            m = request.form.get("mode", "probe")
            if m in ("probe", "hijack", "pass"):
                set_setting("dns_mode", m)
                _dns_mode_cache["ts"] = 0
                msg = "DNS 模式已切换为 %s" % m
        elif act == "savehijack":
            set_setting("hijack_domains", request.form.get("domains", "")[:2000])
            _hijack_cache["ts"] = 0
            msg = "自定义劫持域名已保存"
        elif act == "addhijack":
            d = request.form.get("d", "").strip().lower()
            if d:
                cur = get_setting("hijack_domains", "")
                if d not in cur:
                    set_setting("hijack_domains", (cur + "\n" + d).strip())
                _hijack_cache["ts"] = 0
                msg = "已加入劫持:%s" % d
        elif act == "appid":
            set_setting("softap_appid", request.form.get("appid", "").strip()[:80])
            msg = "appId 已保存(留空则使用内置默认值)"
        elif act == "goheal":
            msg = " / ".join(_go_selfheal())
        elif act == "goon":
            ok, _p = _softap_set(True)
            msg = "开启投屏热点:%s" % ("指令已发送" if ok else "RPC 失败(检查投屏软件是否运行)")
        elif act == "gooff":
            ok, _p = _softap_set(False)
            msg = "关闭投屏热点:%s" % ("指令已发送" if ok else "RPC 失败")
        elif act == "autoheal":
            v = "1" if request.form.get("v", "") == "1" else "0"
            set_setting("go_autoheal", v)
            msg = "DIRECT 自动自愈:%s" % ("开" if v == "1" else "关")
    mode = dns_mode()
    ex, su = custom_hijacks()
    logrows = ""
    with _dns_log_lock:
        snap = list(_dns_log[-60:])
    for ts, ip, host, hij in reversed(snap):
        logrows += ("<tr><td class='tip'>%s</td><td>%s</td><td>%s</td>"
                    "<td><span class='chip %s'>%s</span></td>"
                    "<td><form method='post' action='/admin/dns' style='display:inline'>"
                    "<input type='hidden' name='act' value='addhijack'>"
                    "<input type='hidden' name='d' value='%s'>"
                    "<button class='mini'>劫持</button></form></td></tr>"
                    % (time.strftime("%H:%M:%S", time.localtime(ts)), esc(ip), esc(host[:40]),
                       "red" if hij else "green", "已劫持" if hij else "转发",
                       esc(host)))
    macs = _lan_clients()
    mrows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (esc(a), esc(b), esc(c))
                    for a, b, c in macs)
    orows = "".join("<tr><td>%s</td><td>%s</td><td class='%s'>%s</td></tr>"
                    % (esc(d), esc(ip), "tip" if s != "ok" else "chip", esc(s))
                    for d, ip, s in _probe_outbound())
    # DIRECT 状态
    ok, data, err = _softap_rpc("GetSoftApInfo")
    go_name, go_ip = _find_go_adapter()
    st, ans = _go_dns_check(go_ip)
    raw = ""
    if data is not None:
        raw = json.dumps(data, ensure_ascii=False)[:600]
    color = {"hijack-ok": "green", "forwarded": "red", "timeout": "red", "n/a": "red"}.get(st, "red")
    body = (back_btn("/admin") + ("<p class='err'>%s</p>" % esc(msg) if msg else "")
            + "<h3>网络诊断</h3>"
            "<h4>DNS 模式(当前:%s)</h4>"
            "<form method='post' action='/admin/dns'>"
            "<input type='hidden' name='act' value='mode'>"
            "<select name='mode' style='width:100%%;padding:10px'>"
            "<option value='probe' %s>probe(常态:仅探测/劫持名单,教师机可上网)</option>"
            "<option value='hijack' %s>hijack(全部劫持:仅临时触发认证,教师机也会被劫)</option>"
            "<option value='pass' %s>pass(诊断:全部转发;此模式下自检 WARN 属预期)</option>"
            "</select><button class='mini'>切换</button></form>"
            "<h4>自定义劫持域名(每行一个;「.」开头=后缀通配;默认空)</h4>"
            "<form method='post' action='/admin/dns'>"
            "<input type='hidden' name='act' value='savehijack'>"
            "<textarea name='domains' rows='5'>%s</textarea>"
            "<button class='mini'>保存劫持列表</button></form>"
            "<p class='tip'>入口排查:平板点一次「更新说明」→ 下面日志出现新域名 → 点「劫持」→ "
            "再点一次「更新说明」:弹出门户=HTTP 可用;白屏=HTTPS,换入口域。</p>"
            "<h4>最近 DNS 查询</h4>"
            "<table><tr><th>时间</th><th>来源</th><th>域名</th><th>状态</th><th></th></tr>"
            + (logrows or "<tr><td colspan='5' class='tip'>暂无</td></tr>") + "</table>"
            "<h4>承载网络客户端(%d)</h4>"
            "<table><tr><th>IP</th><th>MAC</th><th>类型</th></tr>"
            + (mrows or "<tr><td colspan='3' class='tip'>无</td></tr>") + "</table>"
            "<h4>教师机出站连通(白名单)</h4>"
            "<table><tr><th>域名</th><th>解析</th><th>状态</th></tr>" + (orows or "") + "</table>"
            "<h4>投屏热点(DIRECT 扩容)</h4>"
            "<p>RPC:%s · GO 接口:%s · GO IP:%s · DNS 自检:"
            "<span class='chip %s'>%s %s</span></p>"
            "<p class='tip'>原始返回:%s</p>"
            "<form method='post' action='/admin/dns' style='display:inline'>"
            "<input type='hidden' name='act' value='goheal'>"
            "<button class='mini'>一键自愈</button></form> "
            "<form method='post' action='/admin/dns' style='display:inline'>"
            "<input type='hidden' name='act' value='goon'>"
            "<button class='mini'>开启热点</button></form> "
            "<form method='post' action='/admin/dns' style='display:inline'>"
            "<input type='hidden' name='act' value='gooff'>"
            "<button class='mini'>关闭热点</button></form> "
            "<form method='post' action='/admin/dns' style='display:inline'>"
            "<input type='hidden' name='act' value='autoheal'>"
            "<input type='hidden' name='v' value='%s'>"
            "<button class='mini'>自动自愈:%s</button></form>"
            "<form method='post' action='/admin/dns'>"
            "<input type='hidden' name='act' value='appid'>"
            "<input name='appid' value='%s' placeholder='appId(留空=内置默认,无需填写)'>"
            "<button class='mini'>保存 appId 覆盖</button></form>"
            "<p class='tip'>自愈流程:重设教师机 DNS → RPC 重启投屏热点(RPC 失败则仅重设 DNS)→ 复检。"
            "自愈会让投屏闪断数秒,投屏进行中请先关自动自愈。</p>"
            "<p class='tip'><a href='/admin'>返回管理中心</a></p>" + FOOTER
            ) % (mode,
                 "selected" if mode == "probe" else "", "selected" if mode == "hijack" else "",
                 "selected" if mode == "pass" else "",
                 esc(get_setting("hijack_domains", "")),
                 len(macs),
                 "ok" if ok else "fail(%s)" % esc(err[:40]),
                 esc(go_name or "未检测到"),
                 esc(go_ip or "-"), color, esc(st), esc(ans[:40]),
                 esc(raw or err or "无"),
                 "1" if get_setting("go_autoheal", "1") == "1" else "0",
                 "开" if get_setting("go_autoheal", "1") == "1" else "关",
                 esc(get_setting("softap_appid", "")))
    return shell("网络诊断", body)

# ---------- anypath(catch-all) ----------
@app.route("/<path:rest>")
def anypath(rest):
    low = rest.lower()
    if any(m in low for m in _PROBE_MARKS):
        key = (request.remote_addr, low)
        now = time.time()
        with _probe_lock:
            if now - _probe_seen.get(key, 0) > 300:
                _probe_seen[key] = now
                if len(_probe_seen) > 512:
                    _probe_seen.clear()
                need_log = True
            else:
                need_log = False
        if need_log:
            log.info("网络探测 ip=%s path=/%s", request.remote_addr, rest[:80])
        u = current_user()
        return redirect("/home" if u else "/", 302)
    u = current_user()
    if u:
        return redirect("/home", 302)
    return login_page()

# ---------- 模块 API 与加载 ----------
MODULE_API = types.SimpleNamespace(
    dbq=dbq, _mklog=_mklog, COMMON_DB=COMMON_DB,
    get_setting=get_setting, set_setting=set_setting,
    DIR_COMMON=DIR_COMMON,
    DIR_AST=DIR_ASSISTANT, DIR_ASSISTANT=DIR_ASSISTANT,   # assistant.py 用 DIR_AST
    DIR_RES=DIR_RESOURCE, DIR_RESOURCE=DIR_RESOURCE,     # resource.py 用 DIR_RES
    DIR_RC=DIR_RC, DIR_DICT=DIR_DICT, DIR_WEATHER=DIR_WEATHER, DIR_MUSIC=DIR_MUSIC,
    BASE=BASE_DIR, BASE_DIR=BASE_DIR,                    # resource.py 用 BASE
    AI_MODEL=AI_MODEL, ai_post=ai_post,
    STYLE=STYLE, ANIM_STYLE=ANIM_STYLE, FOOTER=FOOTER,
    REOPEN_TIP=REOPEN_TIP, back_btn=back_btn,
    login_page=login_page, msg_page=msg_page,
    current_user=current_user, device_id=device_id,
    admin_ok=admin_ok, admin_login_page=admin_login_page,
    hold_page=hold_page, hold_loc_js=hold_loc_js,
    whitelist_add=lambda d: (_outbound_whitelist.append(d)
                             if d and d not in _outbound_whitelist else None),
    RELAY_VERSION=RELAY_VERSION, BUILD=BUILD, PORTAL_IP=PORTAL_IP,
    HTTP_PORT=HTTP_PORT, DNS_BIND=DNS_BIND, APPS_JSON=APPS_JSON,
)

def _load_apps():
    global APPS, HOLD_URL_PREFIXES
    raw = None
    try:
        with open(APPS_JSON, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        log.error("relay_apps.json 加载失败:%s", e)
        raw = None
    if isinstance(raw, dict):                 # 兼容 {"apps":[...]} 包装
        raw = raw.get("apps") or []
    APPS = []
    for it in (raw or []):
        if not isinstance(it, dict):
            continue
        spec = dict(it)
        # 字段归一:name ← title ← key(json 用 title;代码统一读 name)
        spec["name"] = spec.get("name") or spec.get("title") or spec.get("key") or "?"
        APPS.append(spec)
    log.info("relay_apps.json 已加载:%s", "、".join(a.get("key", "?") for a in APPS))
    # key -> 模块名别名(json 的 rc 条目实际模块文件是 remoteclasswork.py)
    _ALIAS = {"rc": "remoteclasswork"}
    loaded = {}
    for spec in APPS:
        key = spec.get("key", "")
        mod_name = spec.get("module") or _ALIAS.get(key) or key
        try:
            loaded[key] = __import__(mod_name)
        except Exception:
            log.exception("模块 %s import 失败", mod_name)
    ast = loaded.get("assistant")
    for spec in APPS:
        key = spec.get("key", "")
        mod_name = spec.get("module") or _ALIAS.get(key) or key
        mod = loaded.get(key)
        if not mod:
            log.info("模块 %s(%s)已跳过(import 失败)", key, mod_name)
            continue
        try:
            mod.init(MODULE_API, ast=ast)
            bp = getattr(mod, "bp", None)
            if bp is not None:
                app.register_blueprint(bp)    # 不注册则模块路由全部 404
            log.info("模块 %s (%s) 已挂载", key, mod_name)
        except Exception:
            log.exception("模块 %s init 失败——已跳过", key)
    HOLD_URL_PREFIXES = tuple(["/" + a.get("key", "") for a in APPS] + ["/home"])
    return loaded

# ---------- main ----------
if __name__ == "__main__":
    log.info("=" * 46)
    _modules = _load_apps()
    for _m in _modules.values():
        _fn = getattr(_m, "start_background", None)
        if callable(_fn):
            try:
                _fn()
            except Exception:
                log.exception("模块 start_background 失败")
    start_dns_servers()
    threading.Thread(target=_go_watch, daemon=True).start()
    log.info("Relay %s · %s 门户 http://0.0.0.0:%d", RELAY_VERSION, BUILD, HTTP_PORT)
    serve(app, host="0.0.0.0", port=HTTP_PORT, threads=HTTP_THREADS,
          connection_limit=1024, channel_timeout=120, backlog=2048)
