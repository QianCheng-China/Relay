# -*- coding: utf-8 -*-
"""
HappySynthesizer
pip install flask waitress dnslib requests
Developer ：HighspeedG2304
"""
import csv, logging, os, re, secrets, socket, sqlite3, subprocess, threading, time
from datetime import datetime
from html import escape as esc
from socketserver import ThreadingMixIn

import requests as http
from flask import Flask, g, make_response, redirect, request
from dnslib import A, DNSRecord, QTYPE, RR
from dnslib.server import BaseResolver, DNSServer, DNSLogger, TCPServer, UDPServer
from waitress import serve
from werkzeug.security import check_password_hash, generate_password_hash

# ==================== 配置区（部署时只改这里） ====================
APP_NAME     = "HappySynthesizer"
DEVELOPER    = "HighspeedG2304"
PORTAL_IP    = "192.168.137.1"      # 热点网关 IP（Windows 热点默认，不要改）
DNS_BIND     = "127.0.0.1"          # 配合热点 DNS 代理，见部署第 4 步
UPSTREAM_DNS = "223.5.5.5"
HIJACK_ALL   = True                 # 封闭网络：所有域名都指向门户
WHITELIST    = {                    # 不劫持（服务器自己要正常解析）
    "open.bigmodel.cn",
    "time.windows.com", "ntp.aliyun.com",
    # "xxx.school.edu.cn",           # 需要的校内系统域名加在这里
}
HTTP_PORT, HTTP_THREADS = 80, 64

AI_API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
AI_API_KEY = "在这里填你的KEY"
AI_MODEL   = "glm-4-flash"
AI_TIMEOUT = 30
AI_CONCURRENCY = 15                 # 同时发给 AI 的最大请求数，其余自动排队
MAX_ANSWER, MAX_QUERY = 400, 500

ADMIN_PASSWORD = "admin123"         # 教师管理页密码，务必修改！
COOKIE_NAME, COOKIE_DEV, COOKIE_MAX_AGE = "ps", "pd", 180*24*3600
BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE, "happysynthesizer.db")
QA_CSV   = os.path.join(BASE, "happysynthesizer_qa.csv")
LOG_PATH = os.path.join(BASE, "happysynthesizer.log")
# =================================================================

FOOTER = "<p class='tip'>%s · Developer  %s</p>" % (APP_NAME, DEVELOPER)

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()])
log = logging.getLogger(APP_NAME)

# ---------- 数据库（WAL，多线程安全） ----------
def dbq(sql, args=()):
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.isolation_level = None
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=8000")
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()

def init_db():
    dbq("""CREATE TABLE IF NOT EXISTS accounts(
        username TEXT PRIMARY KEY, pw_hash TEXT NOT NULL,
        device_id TEXT, bound_at TEXT, last_seen TEXT, last_query TEXT)""")
    dbq("""CREATE TABLE IF NOT EXISTS sessions(
        token TEXT PRIMARY KEY, username TEXT NOT NULL,
        device_id TEXT NOT NULL, created_at TEXT)""")

# ---------- 设备识别：优先取平板真实 MAC ----------
# Android 9 默认不做 MAC 随机化，同一平板在认证窗口内的 MAC 稳定，可作设备指纹
_arp_cache, _arp_lock = {}, threading.Lock()
def get_mac(ip):
    with _arp_lock:
        hit = _arp_cache.get(ip)
        if hit and time.time() - hit[1] < 600:
            return hit[0]
    try:
        subprocess.run(["ping", "-n", "1", "-w", "200", ip], capture_output=True)
        out = subprocess.run(["arp", "-a", ip], capture_output=True,
                             text=True, timeout=5).stdout
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
    tok = request.cookies.get(COOKIE_DEV)          # 兜底：设备 Cookie
    if tok:
        return "c:" + tok
    if not hasattr(g, "new_dev"):
        g.new_dev = secrets.token_hex(16)
    return "c:" + g.new_dev

# ---------- AI 中转 ----------
_ai_sem = threading.BoundedSemaphore(AI_CONCURRENCY)
_ai = http.Session()
_ai.mount("https://", http.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=32))
_csv_lock = threading.Lock()

def ask_ai(question):
    with _ai_sem:
        r = _ai.post(AI_API_URL,
            headers={"Authorization": "Bearer " + AI_API_KEY},
            json={"model": AI_MODEL,
                  "messages": [
                      {"role": "system", "content":
                          "你是课堂学习助手。用不超过500字的简体中文纯文本回答，"
                          "禁止使用任何Markdown符号。"},
                      {"role": "user", "content": question[:MAX_QUERY]}],
                  "max_tokens": 320, "temperature": 0.5},
            timeout=AI_TIMEOUT)
    r.raise_for_status()
    ans = r.json()["choices"][0]["message"]["content"].strip()
    ans = re.sub(r"[#*`>|]", "", ans)
    return ans if len(ans) <= MAX_ANSWER else ans[:MAX_ANSWER] + "……"

# ---------- 页面（纯 HTML，兼容老旧 WebView；无 JS） ----------
STYLE = ("<meta name='viewport' content='width=device-width,initial-scale=1'>"
         "<style>body{font-family:sans-serif;max-width:640px;margin:14px auto;"
         "padding:0 12px;font-size:17px}"
         "h3{margin:6px 0}"
         "input,textarea,button{font-size:17px;width:100%;box-sizing:border-box;"
         "padding:10px;margin:5px 0}"
         "button{background:#2b6cb0;color:#fff;border:0;border-radius:6px;padding:12px}"
         "button.mini{width:auto;padding:4px 8px;margin:0;font-size:13px;background:#888}"
         "pre{white-space:pre-wrap;word-wrap:break-word;background:#f4f6f8;"
         "padding:10px;border-radius:6px}"
         ".err{color:#c0392b}.tip{color:#666;font-size:14px}</style>")
REOPEN_TIP = ("<p class='tip'>若不慎关闭本页面，尝试开关 WLAN 开关，"
              "本页面应该会自动重新弹出</p>")

def login_page(err=""):
    e = "<p class='err'>" + esc(err) + "</p>" if err else ""
    return (f"<!doctype html><html><head><meta charset='utf-8'>{STYLE}"
            f"<title>{APP_NAME}</title></head><body><h3>{APP_NAME} · 登录</h3>"
            f"{e}<form method='post' action='/login'>"
            "<input name='u' placeholder='账号' autocapitalize='off' autocomplete='off'>"
            "<input name='p' type='password' placeholder='密码'>"
            "<label class='tip'><input type='checkbox' name='rebind' value='1' "
            "style='width:auto'> 若账号已绑定其他设备，轻触此项以将绑定迁移到本设备</label>"
            "<button type='submit'>登 录</button></form>"
            "<p class='tip'>一个账号仅允许与一台设备绑定</p>"
            + REOPEN_TIP + FOOTER + "</body></html>")

def qa_page(user, ans=None, qtext=""):
    a = "<h4>回答</h4><pre>" + esc(ans) + "</pre>" if ans else ""
    return (f"<!doctype html><html><head><meta charset='utf-8'>{STYLE}"
            f"<title>{APP_NAME}</title></head><body><h3>{APP_NAME} · AI Chat</h3>"
            f"<p class='tip'>当前账号：{esc(user)} 　<a href='/unbind'>解绑本设备</a></p>"
            "<form method='post' action='/ask'>"
            f"<textarea name='q' rows='3' placeholder='输入问题，轻触发送…'>{esc(qtext)}</textarea>"
            "<button type='submit'>发 送</button></form>"
            f"{a}" + REOPEN_TIP + FOOTER + "</body></html>")

def msg_page(text, back="/"):
    return (f"<!doctype html><html><head><meta charset='utf-8'>{STYLE}<title>提示</title>"
            f"</head><body><p>{text}</p><p><a href='{back}'>返回</a></p>"
            + FOOTER + "</body></html>")

# ---------- 路由 ----------
app = Flask(__name__)

@app.after_request
def finish(resp):
    if getattr(g, "new_dev", None) and not request.cookies.get(COOKIE_DEV):
        resp.set_cookie(COOKIE_DEV, g.new_dev, max_age=COOKIE_MAX_AGE,
                        httponly=True, path="/")
    resp.headers["Cache-Control"] = "no-store"
    return resp

def current_user():
    tok = request.cookies.get(COOKIE_NAME)
    if not tok:
        return None
    dev = device_id()
    rows = dbq("SELECT s.username, s.device_id, a.device_id FROM sessions s "
               "JOIN accounts a ON a.username=s.username WHERE s.token=?", (tok,))
    if not rows:
        return None
    user, sdev, adev = rows[0]
    return user if (sdev == dev and adev == dev) else None

@app.route("/")
def home():
    user = current_user()
    return qa_page(user) if user else login_page()

@app.route("/favicon.ico")
def favicon():
    return "", 204

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        user = current_user()
        return qa_page(user) if user else login_page()
    u = request.form.get("u", "").strip()
    p = request.form.get("p", "")
    dev = device_id()
    rows = dbq("SELECT pw_hash, device_id, bound_at FROM accounts WHERE username=?", (u,))
    if not rows or not check_password_hash(rows[0][0], p):
        log.info("登录失败 user=%s dev=%s", u, dev)
        return login_page("账号或密码错误")
    bound, bound_at = rows[0][1], rows[0][2] or "?"
    if bound and bound != dev and request.form.get("rebind") != "1":
        return login_page("该账号已绑定其他设备（绑定时间 " + bound_at + "）。"
                          "要将绑定转移到本设备，请轻触“迁移绑定”后重新登录")
    tok = secrets.token_hex(24)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    dbq("DELETE FROM sessions WHERE username=? AND device_id<>?", (u, dev))
    dbq("DELETE FROM sessions WHERE device_id=?", (dev,))
    dbq("INSERT INTO sessions VALUES (?,?,?,?)", (tok, u, dev, now))
    dbq("UPDATE accounts SET device_id=?, bound_at=? WHERE username=?", (dev, now, u))
    log.info("登录成功 user=%s dev=%s", u, dev)
    resp = make_response(qa_page(u))
    resp.set_cookie(COOKIE_NAME, tok, max_age=COOKIE_MAX_AGE, httponly=True, path="/")
    return resp

@app.route("/ask", methods=["POST", "GET"])
def ask():
    user = current_user()
    if not user:
        return login_page("需要登录")
    src = request.values if request.method == "GET" else request.form
    qtext = src.get("q", "").strip()
    if not qtext:
        return home()
    try:
        ans = ask_ai(qtext)
    except Exception as e:
        log.warning("AI 调用失败 user=%s err=%s", user, e)
        return qa_page(user, ans="暂时无法连接 AI，请稍后再试（%s）" % type(e).__name__,
                       qtext=qtext)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    dbq("UPDATE accounts SET last_seen=?, last_query=? WHERE username=?",
        (now, qtext[:80], user))
    with _csv_lock:
        with open(QA_CSV, "a", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow([datetime.now().isoformat(timespec="seconds"),
                                    user, device_id(), qtext, ans])
    return qa_page(user, ans=ans, qtext=qtext)

@app.route("/unbind", methods=["GET", "POST"])
def unbind():
    user = current_user()
    if not user:
        return login_page("需要登录")
    if request.method == "GET":
        return (f"<!doctype html><html><head><meta charset='utf-8'>{STYLE}"
                "<title>解绑设备</title></head><body><h3>解绑设备</h3>"
                "<p class='tip'>解绑后，账号 " + esc(user) +
                " 与本设备解除绑定，可在其他设备登录</p>"
                "<form method='post' action='/unbind'>"
                "<input name='p' type='password' placeholder='输入密码以授权此操作'>"
                "<button type='submit'>确认解绑</button></form>" + FOOTER +
                "</body></html>")
    rows = dbq("SELECT pw_hash FROM accounts WHERE username=?", (user,))
    if not rows or not check_password_hash(rows[0][0], request.form.get("p", "")):
        return msg_page("<span class='err'>密码错误，解绑失败</span>", "/unbind")
    dbq("DELETE FROM sessions WHERE username=?", (user,))
    dbq("UPDATE accounts SET device_id=NULL, bound_at=NULL WHERE username=?", (user,))
    log.info("解绑 user=%s", user)
    return msg_page("解绑成功", "/login")

@app.route("/<path:rest>")     # 接管一切探测/未知路径——必须始终返回 HTML，
def anypath(rest):             # 绝不能返回 204，否则认证窗口不再弹出
    user = current_user()
    return qa_page(user) if user else login_page()

# ---------- 教师管理（仅本机） ----------
_admin_tokens = set()

def admin_login_page(err=""):
    e = "<p class='err'>" + esc(err) + "</p>" if err else ""
    return (f"<!doctype html><html><head><meta charset='utf-8'>{STYLE}<title>管理</title>"
            f"</head><body><h3>{APP_NAME} · 教师管理</h3>{e}"
            "<form method='post' action='/admin'>"
            "<input name='pw' type='password' placeholder='管理密码'>"
            "<button type='submit'>登录</button></form>" + FOOTER + "</body></html>")

def admin_page(msg=None):
    m = "<p class='err'>" + esc(msg) + "</p>" if msg else ""
    rows = dbq("SELECT username, device_id, bound_at, last_seen, last_query "
               "FROM accounts ORDER BY username")
    trs = ""
    for r in rows:
        dev = (r[1] or "未绑定")[:17]
        trs += (f"<tr><td>{esc(r[0])}</td><td>{esc(dev)}</td>"
                f"<td>{esc(r[2] or '')}</td><td>{esc(r[3] or '')}</td>"
                f"<td class='tip'>{esc(r[4] or '')}</td><td>"
                f"<form method='post' action='/admin' style='display:inline'>"
                f"<input type='hidden' name='act' value='unbind'>"
                f"<input type='hidden' name='u' value='{esc(r[0])}'>"
                "<button class='mini'>解绑</button></form> "
                f"<form method='post' action='/admin' style='display:inline'>"
                f"<input type='hidden' name='act' value='del'>"
                f"<input type='hidden' name='u' value='{esc(r[0])}'>"
                "<button class='mini'>删除</button></form></td></tr>")
    body = (f"{m}<table border='1' cellpadding='4' style='font-size:14px;"
            "border-collapse:collapse'><tr><th>账号</th><th>绑定设备</th>"
            "<th>绑定时间</th><th>最后活跃</th><th>最近提问</th><th>操作</th></tr>"
            f"{trs}</table>"
            "<h4>添加账号</h4><form method='post' action='/admin'>"
            "<input type='hidden' name='act' value='add'>"
            "<input name='u' placeholder='账号' style='width:45%'>"
            "<input name='p' placeholder='密码' style='width:45%'>"
            "<button type='submit'>添加</button></form>"
            "<h4>批量导入（每行：账号 密码）</h4><form method='post' action='/admin'>"
            "<input type='hidden' name='act' value='bulk'>"
            "<textarea name='list' rows='6' "
            "placeholder='student01 123456&#10;student02 123456'></textarea>"
            "<button type='submit'>导入</button></form>"
            "<h4>重置密码（并强制下线）</h4><form method='post' action='/admin'>"
            "<input type='hidden' name='act' value='reset'>"
            "<input name='u' placeholder='账号' style='width:45%'>"
            "<input name='p' placeholder='新密码' style='width:45%'>"
            "<button type='submit'>重置</button></form>"
            "<p class='tip'><a href='/'>打开门户首页</a> 　<a href='/admin'>刷新</a></p>"
            + FOOTER)
    return (f"<!doctype html><html><head><meta charset='utf-8'>{STYLE}<title>教师管理"
            f"</title></head><body><h3>{APP_NAME} · 教师管理</h3>{body}</body></html>")

@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return msg_page("管理页仅限在服务器本机打开。", "/")
    if request.method == "POST" and "pw" in request.form:
        if request.form.get("pw") == ADMIN_PASSWORD:
            tok = secrets.token_hex(16)
            _admin_tokens.add(tok)
            resp = make_response(redirect("/admin"))
            resp.set_cookie("adm", tok, max_age=8*3600, httponly=True)
            return resp
        return admin_login_page("管理密码错误")
    tok = request.cookies.get("adm")
    if tok not in _admin_tokens:
        return admin_login_page()
    msg = ""
    act = request.form.get("act")
    try:
        if act == "add":
            u, p = request.form.get("u", "").strip(), request.form.get("p", "").strip()
            if u and p:
                dbq("INSERT OR REPLACE INTO accounts(username,pw_hash) VALUES(?,?)",
                    (u, generate_password_hash(p)))
                msg = "已保存账号 " + u
        elif act == "bulk":
            n = 0
            for line in request.form.get("list", "").splitlines():
                parts = re.split(r"[,，\s]+", line.strip())
                if len(parts) >= 2 and parts[0] and parts[1]:
                    dbq("INSERT OR REPLACE INTO accounts(username,pw_hash) VALUES(?,?)",
                        (parts[0], generate_password_hash(parts[1])))
                    n += 1
            msg = "批量导入 %d 个账号" % n
        elif act == "reset":
            u, p = request.form.get("u", "").strip(), request.form.get("p", "").strip()
            if u and p:
                dbq("UPDATE accounts SET pw_hash=? WHERE username=?",
                    (generate_password_hash(p), u))
                dbq("DELETE FROM sessions WHERE username=?", (u,))
                msg = "已重置 %s 的密码并强制下线" % u
        elif act == "unbind":
            u = request.form.get("u", "").strip()
            dbq("UPDATE accounts SET device_id=NULL, bound_at=NULL WHERE username=?", (u,))
            dbq("DELETE FROM sessions WHERE username=?", (u,))
            msg = "已解绑 " + u
        elif act == "del":
            u = request.form.get("u", "").strip()
            dbq("DELETE FROM accounts WHERE username=?", (u,))
            dbq("DELETE FROM sessions WHERE username=?", (u,))
            msg = "已删除 " + u
    except Exception as e:
        msg = "操作失败：%s" % e
    return admin_page(msg)

# ---------- DNS 劫持 ----------
def _pick(cls):
    """兼容新旧 dnslib：新版（0.9.23+）的 UDPServer/TCPServer 已内置
    ThreadingMixIn，直接用；旧版则动态包一层 ThreadingMixIn。"""
    if issubclass(cls, ThreadingMixIn):
        return cls
    return type("Threaded" + cls.__name__,
                (ThreadingMixIn, cls), {"daemon_threads": True})

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
        return reply   # AAAA/HTTPS 等类型一律空应答，防止绕过封闭网络

def start_dns():
    # 只保留 error 级日志，避免 60 台设备的查询记录刷爆控制台拖慢服务
    quiet = DNSLogger("-request,-reply,-truncated")
    try:
        d = DNSServer(Resolver(), address=DNS_BIND, port=53,
                      server=_pick(UDPServer), logger=quiet)
        threading.Thread(target=d.start, daemon=True).start()
        log.info("DNS(UDP) %s:53 已启动", DNS_BIND)
    except Exception as e:
        log.error("DNS(UDP) 启动失败：%s", e)
    try:
        d2 = DNSServer(Resolver(), address=DNS_BIND, port=53, tcp=True,
                       server=_pick(TCPServer), logger=quiet)
        threading.Thread(target=d2.start, daemon=True).start()
        log.info("DNS(TCP) %s:53 已启动", DNS_BIND)
    except Exception as e:
        log.error("DNS(TCP) 启动失败：%s", e)

if __name__ == "__main__":
    init_db()
    start_dns()
    log.info("%s 门户 http://0.0.0.0:%d （Waitress 线程 %d）—— Developer  %s",
             APP_NAME, HTTP_PORT, HTTP_THREADS, DEVELOPER)
    log.info("教师管理页 http://127.0.0.1:%d/admin", HTTP_PORT)
    serve(app, host="0.0.0.0", port=HTTP_PORT, threads=HTTP_THREADS)
