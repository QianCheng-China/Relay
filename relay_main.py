# -*- coding: utf-8 -*-
"""
Relay 1.0 Beta 主程序（壳）
------------------------
组成：relay_main.py(本文件) + assistant.py(AI问答) + resource.py(课件)
      + remoteclasswork.py(班级作业) 四 Blueprint，waitress 生产服务器。
运行：python relay_main.py （监听 0.0.0.0:80，128 线程）
依赖：pip install flask waitress dnslib requests markdown ziamath pymupdf pywin32 winrt
说明：
- v1.0 Beta (HGH)：新增 RemoteClasswork；全部闪退缓解机制统一命名为 Relay Hold
  （hold_page / HOLD_MAP / hold_draft_* / resource hold_get/hold_save 等），
  数据库表与列名保持不变以保证已有数据兼容。
- v0.2.0 起移除内置自修复体系；外部 autoupdate.py / gate.py / 监护进程请自行下线。
- 数据目录：data/common(账号+主日志) data/assistant data/resource data/remoteclasswork。
- 旧 relay.db / relay_qa.csv / relay.log 已弃用，不再读写。
- 认证弹窗探测兜底路由保留（/<path:rest>），拆分不影响 Captive Portal 判定。
"""
import logging, os, re, secrets, socket, sqlite3, subprocess, sys, threading, time
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
RELAY_VERSION = "1.0 Beta"
RELAY_CODENAME = "HGH"
RELAY_BUILD = 37
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
COMMON_DB = os.path.join(DIR_COMMON, "accounts.db")
LOG_PATH = os.path.join(DIR_COMMON, "relay.log")
KEY_PATH = os.path.join(BASE, "key.txt")
for _d in (DIR_COMMON, DIR_AST, DIR_RES, DIR_RC):
    os.makedirs(_d, exist_ok=True)

# ---------- 配置区 ----------
PORTAL_IP = "192.168.137.1"
DNS_BIND = os.environ.get("HS_DNS_BIND", "127.0.0.1")
DNS_PORT = int(os.environ.get("HS_DNS_PORT", "53"))
UPSTREAM_DNS = "223.5.5.5"
WHITELIST = {
    "open.bigmodel.cn", "time.windows.com", "ntp.aliyun.com",
    "kv-service.wuyuan.dev",     # RemoteClasswork 作业数据（必加）
    "openid.zero-cat.dev",       # 仅当使用 refresh_token 续期时需要
    # "kv.houlang.cloud",        # 仅当 BaseURL 改用此实例时需要
}

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

# ---------- key.txt（Assistant 与 Resource 索引共用） ----------
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
    """AI 请求唯一出口（日后接本地模型只改这里）。"""
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

def init_common_db():
    # accounts.last_page 列沿用旧名（数据兼容）；代码层统一以 hold_page 访问
    dbq(COMMON_DB, """CREATE TABLE IF NOT EXISTS accounts(
        username TEXT PRIMARY KEY, pw_hash TEXT NOT NULL, device_id TEXT,
        bound_at TEXT, last_seen TEXT, last_query TEXT,
        tokens_total INTEGER NOT NULL DEFAULT 0, last_page TEXT DEFAULT 'home')""")
    dbq(COMMON_DB, """CREATE TABLE IF NOT EXISTS sessions(
        token TEXT PRIMARY KEY, username TEXT NOT NULL, device_id TEXT NOT NULL,
        created_at TEXT)""")
    try:
        dbq(COMMON_DB, "ALTER TABLE accounts ADD COLUMN last_page TEXT DEFAULT 'home'")
    except sqlite3.OperationalError:
        pass

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

# ---------- Relay Hold：功能页位置记忆（闪退后 / 直接跳回原功能） ----------
HOLD_MAP = {"assistant": "/assistant", "resource": "/resource", "rc": "/rc"}

def hold_page(key):
    u = current_user()
    if u:
        try:
            dbq(COMMON_DB, "UPDATE accounts SET last_page=? WHERE username=?", (key, u))
        except Exception:
            pass

# ---------- 公共 UI ----------
STYLE = ("<meta name='viewport' content='width=device-width,initial-scale=1'>"
 "<style>body{font-family:sans-serif;max-width:640px;margin:14px auto;"
 "padding:0 12px;font-size:17px}"
 "h3{margin:6px 0}h4{margin:10px 0 4px}"
 "input,textarea,button,select{font-size:17px;width:100%;box-sizing:border-box;"
 "padding:10px;margin:5px 0}"
 "label{display:block;margin:8px 0}"
 "input[type=checkbox]{width:auto;padding:0;margin:0 8px 0 0;vertical-align:middle}"
 "button{background:#2b6cb0;color:#fff;border:0;border-radius:6px;padding:12px}"
 "button.mini{width:auto;padding:4px 8px;margin:0;font-size:13px;background:#888}"
 "a.btn2{display:block;background:#e2e8f0;color:#2d3748;border-radius:6px;"
 "padding:12px;margin:5px 0;text-align:center;text-decoration:none;font-size:17px}"
 "pre{white-space:pre-wrap;word-wrap:break-word;background:#f4f6f8;"
 "padding:10px;border-radius:6px}"
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
    resp.headers["Cache-Control"] = "private, max-age=86400" if mt.startswith("image/") else "no-store"
    return resp

# ---------- 基础路由 ----------
@app.route("/")
def root():
    u = current_user()
    if not u:
        return login_page()
    try:
        r = dbq(COMMON_DB, "SELECT last_page FROM accounts WHERE username=?", (u,))
        lp = r[0][0] if r and r[0][0] else "home"
    except Exception:
        lp = "home"
    return redirect(HOLD_MAP.get(lp, "/home"))

@app.route("/home")
def home():
    u = current_user()
    if not u:
        return login_page()
    hold_page("home")
    body = (f"<p class='tip'>当前账号：{esc(u)} <a href='/unbind'>解绑本设备</a></p>"
            "<a class='card' href='/assistant'><b>Assistant</b>"
            "<span>向 GLM 4 Flash 提问</span></a>"
            "<a class='card' href='/resource'><b>Resource</b>"
            "<span>查看图像，文档和演示文稿</span></a>"
            "<a class='card' href='/rc'><b>RemoteClasswork</b>"
            "<span>查看并编辑班级作业（与班级大屏实时同步）</span></a>"
            + REOPEN_TIP + FOOTER)
    return (f"<!doctype html><html><head><meta charset='utf-8'>{STYLE}"
            f"<title>{APP_NAME}</title></head><body><h3>{APP_NAME} · 功能列表</h3>{body}</body></html>")

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
    body = ("<p>选择管理模块：</p>"
            "<a class='card' href='/admin/common'><b>通用管理</b>"
            "<span>用户账号：添加 / 批量导入 / 重置 / 解绑 / 删除 / Token 统计</span></a>"
            "<a class='card' href='/admin/assistant'><b>Assistant 管理</b>"
            "<span>询问记录 · Token 用量 · 联网搜索 · 对话上下文</span></a>"
            "<a class='card' href='/admin/resource'><b>Resource 管理</b>"
            "<span>课件文件夹 · 学科 · 索引计划 · 手动扫描 / 重建 / 预热</span></a>"
            "<a class='card' href='/admin/remoteclasswork'><b>RemoteClasswork 管理</b>"
            "<span>KV 连接（令牌 / 鉴权双模式）· 学科权限 · 缓存与 Hold 维护</span></a>"
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
        elif act == "zerotok":
            u = request.form.get("u", "").strip()
            dbq(COMMON_DB, "UPDATE accounts SET tokens_total=0 WHERE username=?", (u,))
            msg = "已清零 %s 的 Token 统计" % u
        elif act == "zerotok_all":
            dbq(COMMON_DB, "UPDATE accounts SET tokens_total=0")
            msg = "已清零全部 Token 统计"
    except Exception as e:
        msg = "操作失败：%s" % e
    rows = dbq(COMMON_DB, "SELECT username, device_id, bound_at, last_seen, last_query, tokens_total "
                          "FROM accounts ORDER BY username")
    trs = ""
    for r in rows:
        dev = (r[1] or "未绑定")[:17]
        tok = int(r[5] or 0)
        trs += (f"<tr><td>{esc(r[0])}</td><td>{esc(dev)}</td><td>{esc(r[2] or '')}</td>"
                f"<td>{esc(r[3] or '')}</td><td class='tip'>{esc(r[4] or '')}</td><td>{tok:,}</td><td>"
                f"<form method='post' action='/admin/common' style='display:inline'>"
                f"<input type='hidden' name='act' value='unbind'><input type='hidden' name='u' value='{esc(r[0])}'>"
                f"<button class='mini'>解绑</button></form> "
                f"<form method='post' action='/admin/common' style='display:inline'>"
                f"<input type='hidden' name='act' value='del'><input type='hidden' name='u' value='{esc(r[0])}'>"
                f"<button class='mini'>删除</button></form> "
                f"<form method='post' action='/admin/common' style='display:inline'>"
                f"<input type='hidden' name='act' value='zerotok'><input type='hidden' name='u' value='{esc(r[0])}'>"
                f"<button class='mini'>Token清零</button></form></td></tr>")
    head = back_btn("/admin") + (("<p class='err'>" + esc(msg) + "</p>") if msg else "")
    body = (head + "<p>AI 联网搜索等 Assistant 选项请前往 Assistant 管理；账号 Token 用量统计亦在其页查看。</p>"
            "<table border='1'><tr><th>账号</th><th>绑定设备</th><th>绑定时间</th><th>最后活跃</th>"
            "<th>最近提问</th><th>累计tokens</th><th>操作</th></tr>" + trs + "</table>"
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
            "<p class='tip'><a href='/admin'>返回管理中心</a></p>" + FOOTER)
    return (f"<!doctype html><html><head><meta charset='utf-8'>{STYLE}<title>通用管理</title>"
            f"</head><body><h3>{APP_NAME} · 通用管理</h3>{body}</body></html>")

# ---------- DNS 劫持（原逻辑原样保留） ----------
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
        name = str(request.q.qname).lower().rstrip(".")
        if name in WHITELIST:
            try:
                return DNSRecord.parse(_upstream(request.pack()))
            except Exception:
                return request.reply()
        reply = request.reply()
        if request.q.qtype == QTYPE.A:
            reply.add_answer(RR(request.q.qname, QTYPE.A, rdata=A(PORTAL_IP), ttl=10))
        return reply

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

# ===== 挂载子模块（init 注入依赖，避免循环导入） =====
import assistant as _ast_mod
import resource as _res_mod
import remoteclasswork as _rc_mod
_ast_mod.init(sys.modules[__name__])
_res_mod.init(sys.modules[__name__], _ast_mod)
_rc_mod.init(sys.modules[__name__])
app.register_blueprint(_ast_mod.bp)
app.register_blueprint(_res_mod.bp)
app.register_blueprint(_rc_mod.bp)

# 探测兜底 + 未知路径回门户（具体路由优先于本通配规则）
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
        _res_mod.start_background()
        log.info("Relay %s · %s · Build %d 门户 http://0.0.0.0:%d （waitress 线程 %d）—— Developer %s",
                 RELAY_VERSION, RELAY_CODENAME, RELAY_BUILD, HTTP_PORT, HTTP_THREADS, DEVELOPER)
        log.info("管理 http://127.0.0.1:%d/admin · 模块 assistant/resource/remoteclasswork 已挂载 · DNS %s:%d",
                 HTTP_PORT, DNS_BIND, DNS_PORT)
        serve(app, host="0.0.0.0", port=HTTP_PORT, threads=HTTP_THREADS)
    except Exception:
        log.exception("服务异常退出")
        raise
