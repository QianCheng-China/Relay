# -*- coding: utf-8 -*-
"""
HappySynthesizer —— 班级 AI 门户服务器（单文件）
  DNS 劫持（封闭网络）+ 登录鉴权（账号-设备绑定）
  + AI 问答（阻塞式，服务端 Markdown 渲染）+ 排队显示 + 高峰熔断
  + 单账号同时仅一个请求（处理中拒绝新提问）
  + 用户可选 AI 联网搜索 + Token 用量统计（教师页可清零）+ 教师管理页
  入口：平板"网络认证窗口"（系统 WebView），无浏览器、无 JS 环境
  面向低性能平板优化：整页一次渲染、无脚本、无外部资源
  控制台：全部输出写入日志文件；将本文件命名为 *.pyw，双击运行即无控制台窗口
依赖：pip install flask waitress dnslib requests markdown
开发者：HighspeedG2304
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

try:
    import markdown as _md           # 服务端 Markdown 渲染；未安装时自动降级为纯文本
except ImportError:
    _md = None

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
AI_TIMEOUT = 45                      # 阻塞式：整个回答的超时
AI_CONCURRENCY = 15                  # 同时回答数，其余排队

AI_WEBSEARCH_DEFAULT = True          # "AI 联网搜索"选项是否开放给学生（教师页可随时开关）
BREAKER_PAUSE_AT  = 30               # 熔断：排队达到该人数 → 拒绝新提问
BREAKER_RESUME_AT = 10               # 恢复：排队回落到该人数以下 → 自动恢复

MAX_ANSWER, MAX_QUERY = 600, 500

ADMIN_PASSWORD = "admin123"         # 教师管理页密码，务必修改！
COOKIE_NAME, COOKIE_DEV, COOKIE_MAX_AGE = "ps", "pd", 180*24*3600
BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE, "happysynthesizer.db")
QA_CSV   = os.path.join(BASE, "happysynthesizer_qa.csv")
LOG_PATH = os.path.join(BASE, "happysynthesizer.log")
# =================================================================

FOOTER = "<p class='tip'>%s · Developer %s</p>" % (APP_NAME, DEVELOPER)

# 控制台静默：所有输出只写日志文件（配合 .pyw 双击无窗口运行）
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8")])
log = logging.getLogger(APP_NAME)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

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
        device_id TEXT, bound_at TEXT, last_seen TEXT, last_query TEXT,
        tokens_total INTEGER NOT NULL DEFAULT 0)""")
    dbq("""CREATE TABLE IF NOT EXISTS sessions(
        token TEXT PRIMARY KEY, username TEXT NOT NULL,
        device_id TEXT NOT NULL, created_at TEXT)""")
    dbq("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
    try:    # 旧库升级：补 tokens_total 列（已存在则忽略）
        dbq("ALTER TABLE accounts ADD COLUMN tokens_total INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass

def setting_get(key, default):
    rows = dbq("SELECT value FROM settings WHERE key=?", (key,))
    return rows[0][0] if rows else default

def setting_set(key, value):
    dbq("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)))

# ---------- 设备识别：优先取平板真实 MAC ----------
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
    tok = request.cookies.get(COOKIE_DEV)          # 兜底：设备 Cookie
    if tok:
        return "c:" + tok
    if not hasattr(g, "new_dev"):
        g.new_dev = secrets.token_hex(16)
    return "c:" + g.new_dev

# ---------- 排队计数与熔断 ----------
_q_lock = threading.Lock()
_waiting = 0        # 正在排队等待 AI 名额的请求数
_active  = 0        # 正在调用 AI 的请求数
_paused  = False    # 熔断状态

def _breaker_state():
    with _q_lock:
        return _waiting, _active, _paused

def _enter_queue():
    """请求进入排队。返回 False = 被熔断拒绝"""
    global _paused, _waiting
    with _q_lock:
        if _paused:
            return False
        if _waiting >= BREAKER_PAUSE_AT:
            _paused = True
            log.warning("熔断触发：排队 %d ≥ %d，暂停接受新提问", _waiting, BREAKER_PAUSE_AT)
            return False
        _waiting += 1
        return True

def _q_grant():
    global _waiting, _active
    with _q_lock:
        _waiting -= 1
        _active += 1
        return _active

def _q_abandon():
    global _waiting
    with _q_lock:
        _waiting -= 1

def _q_release():
    global _active, _paused
    with _q_lock:
        _active -= 1
        if _paused and _waiting < BREAKER_RESUME_AT:
            _paused = False
            log.info("熔断恢复：排队 %d < %d，重新接受提问", _waiting, BREAKER_RESUME_AT)

# ---------- 单账号请求锁：处理中不允许再提交 ----------
_busy, _busy_lock = set(), threading.Lock()

def _busy_try(user):
    """未占用则占用并返回 True；已在处理中返回 False"""
    with _busy_lock:
        if user in _busy:
            return False
        _busy.add(user)
        return True

def _busy_free(user):
    with _busy_lock:
        _busy.discard(user)     # discard 幂等，重复调用无害

# ---------- AI 中转（阻塞式） ----------
_ai_sem = threading.BoundedSemaphore(AI_CONCURRENCY)
_ai = http.Session()
_ai.mount("https://", http.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=32))
_csv_lock = threading.Lock()

def _build_payload(question, allow_web):
    payload = {
        "model": AI_MODEL,
        "messages": [
            {"role": "system", "content":
                "你是课堂学习助手。用不超过400字的简体中文回答。"
                "可用简洁的 Markdown：用**加粗**标注关键词，用 - 列点；"
                "不要使用表格、代码块和标题符号。"},
            {"role": "user", "content": question[:MAX_QUERY]}],
        "max_tokens": 500, "temperature": 0.5,
    }
    if allow_web:    # 用户选择联网：允许 AI 调用智谱 web_search 工具
        payload["tools"] = [{"type": "web_search", "web_search": {"enable": True}}]
    return payload

def _estimate_tokens(q, a):
    # 粗估：中文约 1.6 字符/token，另加系统提示与模板开销（API 返回 usage 时用精确值）
    return max(1, int((len(q) + len(a)) / 1.6) + 40)

def md_to_html(text):
    """整段 Markdown → HTML。先转义再渲染，杜绝 XSS；纯服务端，平板零计算。"""
    text = esc(text).strip()
    if not text:
        return ""
    if _md is not None:
        try:
            return _md.markdown(text, extensions=["nl2br"])
        except Exception:
            pass
    return "".join("<p>%s</p>" % p for p in text.splitlines() if p.strip())

def _qa_log(user, dev, q, ans, tokens, allow_web):
    with _csv_lock:
        new = not os.path.exists(QA_CSV)
        with open(QA_CSV, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["时间", "账号", "设备", "提问", "回答", "tokens", "联网"])
            w.writerow([datetime.now().isoformat(timespec="seconds"), user, dev,
                        q, ans, tokens, "是" if allow_web else "否"])

def _finalize(user, qtext, answer, usage, allow_web, t0):
    """落盘（DB + CSV），返回给用户看的统计说明"""
    try:
        ans = answer.strip()
        if len(ans) > MAX_ANSWER:
            ans = ans[:MAX_ANSWER] + "……"
        tokens = usage or _estimate_tokens(qtext, answer)
        exact = "API" if usage else "估算"
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        dbq("UPDATE accounts SET last_seen=?, last_query=?, tokens_total=tokens_total+? "
            "WHERE username=?", (now, qtext[:80], tokens, user))
        _qa_log(user, device_id(), qtext, ans, tokens, allow_web)
        dur = max(1, int(time.time() - t0))
        return "本次回答 %d 字 · 消耗 tokens %d（%s）· 用时 %d 秒" % (
            len(ans), tokens, exact, dur)
    except Exception:
        log.exception("问答记录写入失败")
        return ""

# ---------- 页面（纯 HTML，一次完整渲染；兼容老旧 WebView；无 JS） ----------
STYLE = ("<meta name='viewport' content='width=device-width,initial-scale=1'>"
         "<style>body{font-family:sans-serif;max-width:640px;margin:14px auto;"
         "padding:0 12px;font-size:17px}"
         "h3{margin:6px 0}h4{margin:10px 0 4px}"
         "input,textarea,button{font-size:17px;width:100%;box-sizing:border-box;"
         "padding:10px;margin:5px 0}"
         "label{display:block;margin:8px 0}"
         "input[type=checkbox]{width:auto;padding:0;margin:0 8px 0 0;"
         "vertical-align:middle}"
         "button{background:#2b6cb0;color:#fff;border:0;border-radius:6px;padding:12px}"
         "button.mini{width:auto;padding:4px 8px;margin:0;font-size:13px;background:#888}"
         "pre{white-space:pre-wrap;word-wrap:break-word;background:#f4f6f8;"
         "padding:10px;border-radius:6px}"
         ".err{color:#c0392b}.tip{color:#666;font-size:14px}"
         ".queue{background:#eef4fb;border-radius:6px;padding:6px 10px;"
         "font-size:14px;color:#2b6cb0}"
         ".md p{margin:6px 0}.md ul,.md ol{margin:4px 0 4px 20px;padding:0}"
         ".md li{margin:2px 0}.md strong{color:#1a4c8b}</style>")
REOPEN_TIP = ("<p class='tip'>若不慎关闭本页面，尝试开关 WLAN 开关，"
              "本页面应该会自动重新弹出</p>")

def web_checkbox():
    if setting_get("websearch", "1" if AI_WEBSEARCH_DEFAULT else "0") != "1":
        return ""
    return ("<label class='tip'><input type='checkbox' name='web' value='1'>"
            "允许 AI 联网搜索（信息更新，稍慢）</label>")

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

def qa_page(user, ans_html=None, qtext="", note=None, ta_text=None, show_busy=True):
    w, a, paused = _breaker_state()
    qline = ("排队 %d 人 · 并行 %d/%d" % (w, a, AI_CONCURRENCY)
             + (" · 熔断中，暂停接受新提问" if paused else ""))
    with _busy_lock:
        busy = show_busy and (user in _busy)
    busy_line = ("<p class='queue'>有一个问题正在排队/回答中，"
                 "完成前发送新问题会被拒绝</p>") if busy else ""
    ah = ("<h4>回答</h4><div class='md'>" + ans_html + "</div>") if ans_html else ""
    nh = ("<p class='tip'>" + esc(note) + "</p>") if note else ""
    qh = ("<h4>问题</h4><pre>" + esc(qtext) + "</pre>") if (qtext and ans_html) else ""
    ta = esc(ta_text if ta_text is not None else qtext)
    return (f"<!doctype html><html><head><meta charset='utf-8'>{STYLE}"
            f"<title>{APP_NAME}</title></head><body><h3>{APP_NAME} · AI Chat</h3>"
            f"<p class='tip'>当前账号：{esc(user)} <a href='/unbind'>解绑本设备</a></p>"
            "<form method='post' action='/ask'>"
            f"<textarea name='q' rows='3' placeholder='输入问题，轻触发送…'>{ta}</textarea>"
            + web_checkbox() +
            "<button type='submit'>发 送</button></form>"
            f"<p class='queue'>{esc(qline)}</p>{busy_line}{qh}{ah}{nh}"
            + REOPEN_TIP + FOOTER + "</body></html>")

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

    # 单账号请求锁：上一问未完成则拒绝（不占排队名额）。
    # 被拒时上一问确实仍在处理中，err 文案已说明情况，无需再显示忙碌行。
    if not _busy_try(user):
        w, a, _ = _breaker_state()
        return qa_page(user, ans_html=(
            "<p class='err'>上一个问题正在排队或回答中（当前排队 %d 人），"
            "请等它完成后再发送新问题。</p>" % w), qtext=qtext, show_busy=False)

    # 熔断检查
    if not _enter_queue():
        _busy_free(user)
        w, a, _ = _breaker_state()
        log.warning("熔断拒绝 user=%s 排队=%d", user, w)
        return qa_page(user, ans_html=(
            "<p class='err'>系统繁忙：当前排队 %d 人、正在回答 %d/%d，"
            "已暂停接受新提问，通常 1~2 分钟内自动恢复。请稍后重新发送。</p>"
            % (w, a, AI_CONCURRENCY)), qtext=qtext)

    granted = False
    t0 = time.time()
    try:
        _ai_sem.acquire()               # 阻塞等待 AI 名额（熔断已在此前拦截）
        granted = True
        act = _q_grant()
        try:
            allow_web = (src.get("web") == "1") and setting_get("websearch", "1") == "1"
            usage = None
            try:
                r = _ai.post(AI_API_URL,
                             headers={"Authorization": "Bearer " + AI_API_KEY},
                             json=_build_payload(qtext, allow_web),
                             timeout=AI_TIMEOUT)
                r.raise_for_status()
                obj = r.json()
                if obj.get("usage"):
                    usage = obj["usage"].get("total_tokens")
                ch = obj.get("choices") or [{}]
                answer = (ch[0].get("message") or {}).get("content") or ""
            except Exception as e:
                log.warning("AI 调用失败 user=%s err=%s", user, e)
                _busy_free(user)        # 先释放再渲染：错误页不显示忙碌提示
                return qa_page(user, ans_html=(
                    "<p class='err'>暂时无法连接 AI，请稍后再试（%s）。</p>"
                    % type(e).__name__), qtext=qtext)
            note = _finalize(user, qtext, answer, usage, allow_web, t0)
            _busy_free(user)            # 先释放再渲染：回答页不显示忙碌提示
            return qa_page(user, ans_html=md_to_html(answer), qtext=qtext,
                           note=note, ta_text="")
        finally:
            _q_release()
    finally:
        if not granted:
            _q_abandon()
        _busy_free(user)                # 兜底（discard 幂等）

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
    ws_state = "开" if setting_get("websearch", "1" if AI_WEBSEARCH_DEFAULT else "0") == "1" else "关"
    rows = dbq("SELECT username, device_id, bound_at, last_seen, last_query, tokens_total "
               "FROM accounts ORDER BY username")
    trs = ""
    for r in rows:
        dev = (r[1] or "未绑定")[:17]
        tok = int(r[5] or 0)
        trs += (f"<tr><td>{esc(r[0])}</td><td>{esc(dev)}</td>"
                f"<td>{esc(r[2] or '')}</td><td>{esc(r[3] or '')}</td>"
                f"<td class='tip'>{esc(r[4] or '')}</td><td>{tok:,}</td><td>"
                f"<form method='post' action='/admin' style='display:inline'>"
                f"<input type='hidden' name='act' value='unbind'>"
                f"<input type='hidden' name='u' value='{esc(r[0])}'>"
                "<button class='mini'>解绑</button></form> "
                f"<form method='post' action='/admin' style='display:inline'>"
                f"<input type='hidden' name='act' value='del'>"
                f"<input type='hidden' name='u' value='{esc(r[0])}'>"
                "<button class='mini'>删除</button></form> "
                f"<form method='post' action='/admin' style='display:inline'>"
                f"<input type='hidden' name='act' value='zerotok'>"
                f"<input type='hidden' name='u' value='{esc(r[0])}'>"
                "<button class='mini'>Token清零</button></form></td></tr>")
    body = (m
            + "<p>AI 联网搜索选项：当前【" + ws_state + "】"
            "<form method='post' action='/admin' style='display:inline'>"
            "<input type='hidden' name='act' value='webtoggle'>"
            "<button class='mini'>切换</button></form>　"
            "<form method='post' action='/admin' style='display:inline'>"
            "<input type='hidden' name='act' value='zerotok_all'>"
            "<button class='mini'>全部 Token 清零</button></form></p>"
            + "<table border='1' cellpadding='4' style='font-size:14px;"
            "border-collapse:collapse'><tr><th>账号</th><th>绑定设备</th>"
            "<th>绑定时间</th><th>最后活跃</th><th>最近提问</th><th>累计tokens</th>"
            "<th>操作</th></tr>" + trs + "</table>"
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
            "<p class='tip'><a href='/'>打开门户首页</a> <a href='/admin'>刷新</a></p>"
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
        elif act == "zerotok":
            u = request.form.get("u", "").strip()
            dbq("UPDATE accounts SET tokens_total=0 WHERE username=?", (u,))
            msg = "已清零 %s 的 Token 统计" % u
        elif act == "zerotok_all":
            dbq("UPDATE accounts SET tokens_total=0")
            msg = "已清零全部 Token 统计"
        elif act == "webtoggle":
            cur = setting_get("websearch", "1" if AI_WEBSEARCH_DEFAULT else "0")
            setting_set("websearch", "0" if cur == "1" else "1")
            msg = "已切换 AI 联网搜索选项"
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
    try:
        init_db()
        start_dns()
        log.info("%s 门户 http://0.0.0.0:%d （Waitress 线程 %d）—— Developer %s",
                 APP_NAME, HTTP_PORT, HTTP_THREADS, DEVELOPER)
        log.info("教师管理页 http://127.0.0.1:%d/admin", HTTP_PORT)
        log.info("熔断参数：排队≥%d 暂停 / <%d 恢复 · 控制台静默，输出见 %s",
                 BREAKER_PAUSE_AT, BREAKER_RESUME_AT, LOG_PATH)
        serve(app, host="0.0.0.0", port=HTTP_PORT, threads=HTTP_THREADS)
    except Exception:
        log.exception("服务异常退出")
        raise
