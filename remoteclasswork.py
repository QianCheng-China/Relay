# -*- coding: utf-8 -*-
"""
Relay RemoteClasswork 模块（班级作业查看与编辑，基于 Classworks KV）
----------------------------------------------------------------
上游：https://kv-service.wuyuan.dev（管理页可改）· 文档 docs.wuyuan.dev/classworks
键格式：classworks-data-YYYYMMDD = {"homework":{"学科":{"content":"..."}}, "attendance":{...}}
安全边界：仅读写当天键的 homework[学科].content；attendance 与 classworks-list-* 一律不碰；
          写回 = GET 旧值 -> 合并 -> 整键 POST（上游为覆盖式写）。
认证双模式（互为备份，管理页切换；"直接令牌"非空时令牌模式优先）：
  - 鉴权模式：namespace + password + appId -> POST /apps/auth/token 换 JWT（自动续期）
  - 令牌模式：直接粘贴现成 Bearer token（可选 refresh_token 尽力续期，仅对 OAuth 账号令牌有效）
Relay Hold：功能页 + 滚动位置 + 编辑态 + 未提交草稿，闪退重开立即恢复。
v1.0 Beta 修复：_save_ts/_save_lock 上移并启用原子节流；无权限学科只读展开；
      _EDIT_JS 学科名 URL 编码；首屏 quick 拉取 + 防并发重复拉取；
      修复 f-string 表达式内反斜杠嵌套引号（Python<3.12 语法错误）。
注意：本文件必须命名为 remoteclasswork.py（与 relay_main.py 的 import 一致）。
"""
import base64, json, os, threading, time
from datetime import datetime
from html import escape as esc
from urllib.parse import quote
from flask import Blueprint, redirect, request

M = None

DEF_BASE = "https://kv-service.wuyuan.dev"
REFRESH_URL = "https://openid.zero-cat.dev/api/accounts/refresh"  # 尽力尝试，仅 OAuth 账号令牌有效
FORCE_REFRESH_MIN = 10     # 手动强制刷新最小间隔（秒）
FETCH_TTL = 30             # 缓存有效期（秒）
SAVE_GAP = 5               # 同一用户同一学科两次保存最小间隔（秒）

# 保存节流状态（必须在 rc_save 之前定义；锁保护原子"检查-设置"）
_save_ts = {}
_save_lock = threading.Lock()

class KVError(RuntimeError):
    pass

class TokenDead(KVError):
    pass

def init(m):
    global M, RC_DB, RC_LOG, CACHE_PATH, log
    M = m
    RC_DB = os.path.join(M.DIR_RC, "rc.db")
    RC_LOG = os.path.join(M.DIR_RC, "rc.log")
    CACHE_PATH = os.path.join(M.DIR_RC, "cache.json")
    log = M._mklog("relay.rc", RC_LOG)
    init_db()
    _sdefault("base_url", DEF_BASE)
    _sdefault("namespace", "")
    _sdefault("password", "")
    _sdefault("app_id", "")
    _sdefault("rc_token", "")
    _sdefault("refresh_token", "")
    _load_cache()

bp = Blueprint("rc", __name__)

# ---------------- DB ----------------
def init_db():
    M.dbq(RC_DB, "CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
    M.dbq(RC_DB, "CREATE TABLE IF NOT EXISTS subjects(name TEXT PRIMARY KEY)")
    M.dbq(RC_DB, """CREATE TABLE IF NOT EXISTS holds(
        user TEXT PRIMARY KEY, scroll_y INTEGER DEFAULT 0, edit_subject TEXT DEFAULT '')""")
    M.dbq(RC_DB, """CREATE TABLE IF NOT EXISTS drafts(
        user TEXT PRIMARY KEY, subject TEXT DEFAULT '', content TEXT DEFAULT '', ts REAL DEFAULT 0)""")

def sget(k, d=""):
    r = M.dbq(RC_DB, "SELECT value FROM settings WHERE key=?", (k,))
    return r[0][0] if r else d

def sset(k, v):
    M.dbq(RC_DB, "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, str(v)))

def _sdefault(k, v):
    if not M.dbq(RC_DB, "SELECT value FROM settings WHERE key=?", (k,)):
        sset(k, v)

# ---------------- KV 客户端（双模式） ----------------
_sess, _sess_lock = None, threading.Lock()
_tok = {"val": "", "exp": 0}
_tok_lock = threading.Lock()

def _http():
    global _sess
    with _sess_lock:
        if _sess is None:
            import requests as http
            _sess = http.Session()
        return _sess

def _mode():
    return "token" if sget("rc_token") else "auth"

def _token_exp(tok):
    """解析 JWT exp（明文 base64，仅作展示/判断，不验签）。"""
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        obj = json.loads(base64.urlsafe_b64decode(p.encode()).decode("utf-8", "ignore"))
        return float(obj.get("exp") or 0)
    except Exception:
        return 0

def _auth_fetch():
    ns, pw, aid = sget("namespace"), sget("password"), sget("app_id")
    if not (ns and pw and aid):
        raise KVError("鉴权模式缺少 namespace / password / appId（管理页补全，或改用令牌模式）")
    try:
        r = _http().post((sget("base_url") or DEF_BASE).rstrip("/") + "/apps/auth/token",
                         json={"namespace": ns, "password": pw, "appId": aid}, timeout=15)
    except Exception as e:
        raise KVError("鉴权请求失败：" + str(e)[:120])
    if r.status_code != 200:
        raise KVError("鉴权被拒（HTTP %d）：%s" % (r.status_code, r.text[:120]))
    tok = (r.json() or {}).get("token") or ""
    if not tok:
        raise KVError("鉴权响应缺少 token 字段")
    with _tok_lock:
        _tok.update({"val": tok, "exp": _token_exp(tok)})
    return tok

def _try_refresh():
    rt = sget("refresh_token")
    if not rt:
        return False
    try:
        r = _http().post(REFRESH_URL, json={"refresh_token": rt}, timeout=15)
        if r.ok:
            j = r.json() or {}
            tok = j.get("token") or j.get("access_token") or ""
            if tok:
                sset("rc_token", tok)
                with _tok_lock:
                    _tok.update({"val": tok, "exp": _token_exp(tok)})
                log.info("已通过 refresh_token 续期 KV 令牌")
                return True
    except Exception:
        pass
    return False

def _get_token():
    with _tok_lock:
        if _tok["val"] and (_tok["exp"] == 0 or time.time() < _tok["exp"] - 60):
            return _tok["val"]
    if _mode() == "token":
        return sget("rc_token")
    return _auth_fetch()

def _invalidate():
    with _tok_lock:
        _tok.update({"val": "", "exp": 0})

def kv_request(method, path, data=None, timeout=25, _tries=2):
    """返回 Response（404 由调用方判断）；最终 401 -> TokenDead；网络/429 重试后仍失败 -> KVError。"""
    base = (sget("base_url") or DEF_BASE).rstrip("/")
    r = None
    for attempt in range(_tries + 1):
        tok = _get_token()
        try:
            r = _http().request(method, base + path, json=data,
                                headers={"Authorization": "Bearer " + tok}, timeout=timeout)
        except Exception as e:
            if attempt < _tries:
                time.sleep(2)
                continue
            raise KVError("网络错误：" + str(e)[:120])
        if r.status_code == 401:
            if attempt < _tries:
                _invalidate()
                if _mode() == "token":
                    if not _try_refresh():
                        raise TokenDead("令牌已失效且无法续期，请在管理页更新令牌或改用鉴权模式")
                time.sleep(1)
                continue
            raise TokenDead("令牌已失效，请在管理页更新")
        if r.status_code == 429 and attempt < _tries:
            try:
                ra = float(r.headers.get("retry-after") or 3)
            except (TypeError, ValueError):
                ra = 3
            time.sleep(min(15, ra))
            continue
        return r
    raise KVError("请求重试后仍失败（HTTP %s）" % getattr(r, "status_code", "?"))

# ---------------- 数据缓存（cache.json 持久化，服务重启不丢） ----------------
CACHE = {"ts": 0.0, "date": "", "data": None, "err": ""}
_cache_lock = threading.Lock()
_fetch_lock = threading.Lock()
_last_force = [0.0]

def _load_cache():
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            j = json.load(f)
        CACHE.update({"ts": float(j.get("ts") or 0), "date": str(j.get("date") or ""),
                      "data": j.get("data")})
    except Exception:
        pass

def _persist_cache():
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"ts": CACHE["ts"], "date": CACHE["date"], "data": CACHE["data"]},
                      f, ensure_ascii=False)
    except Exception:
        pass

def _today():
    return datetime.now().strftime("%Y%m%d")

def _register_subjects(names):
    for n in names or []:
        n = (n or "").strip()
        if n:
            try:
                M.dbq(RC_DB, "INSERT OR IGNORE INTO subjects(name) VALUES(?)", (n,))
            except Exception:
                pass

def fetch_today(force=False, quick=False):
    """拉取当天作业。quick=True：短超时少重试（首屏同步路径用）；force=True：绕过 TTL。"""
    today = _today()
    with _cache_lock:
        fresh = (CACHE["data"] is not None and CACHE["date"] == today
                 and time.time() - CACHE["ts"] < FETCH_TTL)
    if fresh and not force:
        with _cache_lock:
            return dict(CACHE), CACHE.get("err") or ""
    # 防并发重复拉取：非强制请求拿不到锁直接回缓存；强制请求最多等 15s
    if not _fetch_lock.acquire(timeout=15 if force else 0.2):
        with _cache_lock:
            return dict(CACHE), CACHE.get("err") or ""
    try:
        r = kv_request("GET", "/kv/classworks-data-" + today,
                       timeout=8 if quick else 25, _tries=1 if quick else 2)
        if r.status_code == 404:
            data = {"homework": {}, "attendance": {}}
        elif r.status_code == 200:
            data = r.json() or {}
        else:
            raise KVError("读取作业失败（HTTP %d）" % r.status_code)
        with _cache_lock:
            CACHE.update({"ts": time.time(), "date": today, "data": data, "err": ""})
            _persist_cache()
        _register_subjects(list((data.get("homework") or {}).keys()))
    except TokenDead as e:
        with _cache_lock:
            CACHE["err"] = str(e)
        return dict(CACHE), str(e)
    except Exception as e:
        with _cache_lock:
            CACHE["err"] = str(e)[:200]
        return dict(CACHE), CACHE["err"]
    with _cache_lock:
        return dict(CACHE), ""

def ensure_fresh():
    with _cache_lock:
        empty = CACHE["data"] is None
        stale = CACHE["date"] != _today() or time.time() - CACHE["ts"] >= FETCH_TTL
    if empty:
        fetch_today(quick=True)   # 首次同步快速拉取（保证首屏有内容，最坏 ~20s）
    elif stale:
        threading.Thread(target=fetch_today, daemon=True).start()

def list_subjects(cache_data):
    rows = [r[0] for r in M.dbq(RC_DB, "SELECT name FROM subjects ORDER BY rowid")]
    if cache_data:
        for n in (cache_data.get("homework") or {}).keys():
            if n and n not in rows:
                rows.append(n)
    return rows

# ---------------- 权限 ----------------
def perm_list(subject):
    raw = sget("perm_" + subject, "")
    return [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()]

def has_perm(u, subject):
    lst = perm_list(subject)
    return ("*" in lst) or (u in lst)

# ---------------- Relay Hold：滚动 / 编辑态 / 草稿 ----------------
def hold_get(u):
    r = M.dbq(RC_DB, "SELECT scroll_y, edit_subject FROM holds WHERE user=?", (u,))
    if r:
        return {"scroll": int(r[0][0] or 0), "edit": r[0][1] or ""}
    return {"scroll": 0, "edit": ""}

def hold_set(u, scroll=None, edit=None):
    cur = hold_get(u)
    if scroll is not None:
        cur["scroll"] = max(0, min(200000, int(scroll)))
    if edit is not None:
        cur["edit"] = edit
    M.dbq(RC_DB, "INSERT OR REPLACE INTO holds VALUES(?,?,?)", (u, cur["scroll"], cur["edit"]))

def hold_draft_save(u, subject, content):
    try:
        M.dbq(RC_DB, "INSERT OR REPLACE INTO drafts VALUES(?,?,?,?)",
              (u, subject, content[:20000], time.time()))
    except Exception as e:
        log.warning("Hold草稿保存失败 user=%s err=%s", u, e)

def hold_draft_load(u):
    try:
        r = M.dbq(RC_DB, "SELECT subject, content FROM drafts WHERE user=?", (u,))
        if r:
            return {"subject": r[0][0] or "", "content": r[0][1] or ""}
    except Exception:
        pass
    return {"subject": "", "content": ""}

def hold_draft_clear(u):
    try:
        M.dbq(RC_DB, "DELETE FROM drafts WHERE user=?", (u,))
    except Exception:
        pass

# ---------------- 写回作业 ----------------
def save_subject(subject, content):
    key = "/kv/classworks-data-" + _today()
    try:
        r = kv_request("GET", key)
        if r.status_code == 200:
            data = r.json() or {}
        elif r.status_code == 404:
            data = {"homework": {}, "attendance": {}}
        else:
            return False, "读取原作业失败（HTTP %d）" % r.status_code
        hw = dict(data.get("homework") or {})
        entry = dict(hw.get(subject) or {})   # 保留该学科除 content 外的其他字段
        entry["content"] = content
        hw[subject] = entry
        data["homework"] = hw                  # attendance 原样保留
        pr = kv_request("POST", key, data)
        if pr.status_code not in (200, 201):
            return False, "写回失败（HTTP %d）：%s" % (pr.status_code, pr.text[:100])
        with _cache_lock:
            CACHE.update({"ts": time.time(), "date": _today(), "data": data, "err": ""})
            _persist_cache()
        _register_subjects([subject])
        log.info("作业已写回 subject=%s（attendance 等字段原样保留）", subject)
        return True, ""
    except TokenDead as e:
        return False, str(e)
    except Exception as e:
        return False, "保存失败：" + str(e)[:150]

# ---------------- 连接测试 ----------------
def test_connection():
    out = []
    m = _mode()
    out.append("认证模式：" + ("令牌模式（直接粘贴的 token）" if m == "token" else "鉴权模式（namespace+password+appId）"))
    try:
        tok = _get_token()
        exp = _token_exp(tok) if m == "token" else _tok.get("exp", 0)
        if exp:
            rem = exp - time.time()
            if rem <= 0:
                out.append("⚠ 令牌已过期（KV 读写将失败）")
            else:
                out.append("获取令牌：成功（剩余 %.1f 天）" % (rem / 86400))
        else:
            out.append("获取令牌：成功（未解析到有效期）")
        r = kv_request("GET", "/kv/_keys")
        if r.status_code != 200:
            out.append("列出键名：失败（HTTP %d）%s" % (r.status_code, r.text[:80]))
            return out
        try:
            keys = r.json()
        except Exception:
            keys = []
        if isinstance(keys, dict):
            keys = keys.get("keys") or keys.get("data") or []
        cwk = [k for k in keys if isinstance(k, str) and k.startswith("classworks-data-")]
        out.append("列出键名：成功（共 %d 键，其中作业键 %d 个）" % (len(keys), len(cwk)))
        if cwk:
            latest = sorted(cwk)[-1]
            r2 = kv_request("GET", "/kv/" + latest)
            if r2.status_code == 200:
                d = r2.json() or {}
                hw = d.get("homework") or {}
                out.append("试读 %s：成功（学科 %d 个，attendance 字段%s）"
                           % (latest, len(hw), "存在" if "attendance" in d else "不存在"))
            else:
                out.append("试读 %s：HTTP %d" % (latest, r2.status_code))
        else:
            out.append("尚未发现 classworks-data-* 键（可能是新班级）；写权限以首次保存为准")
    except TokenDead as e:
        out.append("失败：" + str(e))
    except Exception as e:
        out.append("失败：" + str(e)[:150])
    return out

# ---------------- 页面 ----------------
RC_STYLE = ("<style>"
 ".rcbar{display:flex;justify-content:space-between;align-items:center;margin:2px 0 8px}"
 ".rrefresh{color:#2b6cb0;text-decoration:none;font-size:15px}"
 ".subcard{display:block;padding:12px 14px;border-radius:10px;margin:8px 0;"
 "background:#eef0f3;text-decoration:none;color:inherit}"
 ".subcard.perm{background:#dbeafe}"
 ".subcard .fname{font-size:18px;font-weight:bold;color:#1a202c}"
 ".subcard .fmeta{font-size:13px;color:#718096;margin-top:4px;word-break:break-all}"
 ".editor textarea{min-height:340px;line-height:1.5}"
 ".ro{background:#f4f6f8;border-radius:6px;padding:10px;white-space:pre-wrap;"
 "word-wrap:break-word;max-height:60vh;overflow-y:auto}"
 "</style>")

# Relay Hold JS-1：滚动位置上报/恢复（ES5 + XHR，500ms 节流）
_SCROLL_JS = ("<script>(function(){"
 "var y=__Y__,t=null;"
 "function ry(){return window.pageYOffset||document.documentElement.scrollTop||0;}"
 "function rp(){var x=new XMLHttpRequest();x.open('POST','/rc/hold',true);"
 "x.setRequestHeader('Content-Type','application/x-www-form-urlencoded');"
 "x.send('scroll_y='+ry());}"
 "window.scrollTo(0,y);"
 "window.addEventListener('scroll',function(){if(t)clearTimeout(t);t=setTimeout(rp,500);},false);"
 "window.addEventListener('pagehide',rp);})();</script>")

# Relay Hold JS-2：编辑草稿上报（0.8s 防抖；submitting 防止保存跳转回写）
_EDIT_JS = ("<script>(function(){"
 "var ta=document.getElementById('content');if(!ta)return;"
 "var t=null,subm=false;"
 "function sv(){var x=new XMLHttpRequest();x.open('POST','/rc/draft',true);"
 "x.setRequestHeader('Content-Type','application/x-www-form-urlencoded');"
 "x.send('subject=__SUBJ__&content='+encodeURIComponent(ta.value));}"
 "ta.addEventListener('input',function(){if(t)clearTimeout(t);t=setTimeout(sv,800);});"
 "var f=document.getElementById('svform');"
 "if(f)f.addEventListener('submit',function(){subm=true;});"
 "window.addEventListener('pagehide',function(){if(!subm)sv();});})();</script>")

def _rc_shell(title, body, extra_head=""):
    return (f"<!doctype html><html><head><meta charset='utf-8'>{extra_head}{M.STYLE}{M.ANIM_STYLE}"
            f"{RC_STYLE}<title>{esc(title)}</title></head><body>{body}</body></html>")

# ---------------- 路由：作业总览 / 编辑 ----------------
@bp.route("/rc")
def rc_home():
    u = M.current_user()
    if not u:
        return M.login_page()
    M.hold_page("rc")
    # 手动强制刷新（节流 FORCE_REFRESH_MIN 秒）
    if request.args.get("refresh") == "1":
        if time.time() - _last_force[0] >= FORCE_REFRESH_MIN:
            _last_force[0] = time.time()
            fetch_today(force=True)
    ensure_fresh()
    with _cache_lock:
        cache = dict(CACHE)
    err = cache.get("err") or ""
    data = cache.get("data") or {}
    hw = data.get("homework") or {}

    h = hold_get(u)
    # 展开状态（open_subj = 本页展开的学科）：
    #   显式 ?edit=学科 且有权限 -> 进入编辑态并记入 Hold；
    #   显式 ?edit=（空）        -> 收起，清除 Hold 的编辑态；
    #   显式 ?edit=学科 但无权限 -> 仅本次只读展开（Q4），不改动 Hold；
    #   无 ?edit= 参数           -> 恢复 Hold 的编辑态（闪退恢复路径）。
    if "edit" in request.args:
        open_subj = request.args.get("edit", "").strip()
        if open_subj and has_perm(u, open_subj):
            hold_set(u, edit=open_subj)
        elif not open_subj:
            hold_set(u, edit="")
    else:
        open_subj = h["edit"] if (h["edit"] and has_perm(u, h["edit"])) else ""
    can_edit_open = bool(open_subj) and has_perm(u, open_subj)

    # 提示横幅：保存错误 / 令牌临期 / 缓存异常
    banner = ""
    serr = request.args.get("serr", "")
    if serr:
        banner += "<p class='err'>" + esc(serr) + "</p>"
    if _mode() == "token" and sget("rc_token"):
        rem = _token_exp(sget("rc_token")) - time.time()
        if rem <= 0:
            banner += ("<p class='err'>KV 令牌已过期：当前仅能查看缓存内容，编辑不可用。"
                       "请教师在管理页更新令牌（或改用鉴权模式）。</p>")
        elif rem < 86400:
            banner += ("<p class='tip' style='color:#b7791f'>提示：KV 令牌将在 %.1f 小时后过期，"
                       "届时需要教师更新。</p>" % (rem / 3600))
    if err:
        banner += "<p class='tip'>远端同步异常（%s），当前显示本地缓存</p>" % esc(err[:80])

    date_line = "今日作业 · " + datetime.strptime(_today(), "%Y%m%d").strftime("%Y-%m-%d")
    if cache.get("ts"):
        date_line += " · 数据更新于 %d 秒前" % int(time.time() - cache["ts"])

    items = ""
    for name in list_subjects(data):
        qname = quote(name, safe="")   # 预计算：f-string 表达式内不得出现反斜杠/同型引号（Python<3.12）
        entry = hw.get(name) or {}
        content = str(entry.get("content") or "")
        if open_subj == name and can_edit_open:
            # ---- 可编辑：textarea + 草稿恢复（Relay Hold） ----
            draft = hold_draft_load(u)
            ta_val = content
            draft_tip = ""
            if draft["subject"] == name and draft["content"]:
                ta_val = draft["content"]
                draft_tip = "<p class='tip' style='color:#b7791f'>已恢复未保存的草稿（Relay Hold）</p>"
            items += (f"<div class='subcard perm' style='padding:8px 12px'>"
                      f"<div class='fname'>{esc(name)} · 编辑中</div></div>"
                      "<div class='editor'>"
                      "<form method='post' action='/rc/save' id='svform'>"
                      f"<input type='hidden' name='subject' value='{esc(name)}'>"
                      f"<textarea name='content' id='content'>{esc(ta_val)}</textarea>"
                      "<button type='submit'>保 存</button></form>"
                      "<a class='btn2' href='/rc?edit='>收起（不保存）</a>"
                      + draft_tip +
                      "<p class='tip'>输入自动保存草稿（Relay Hold），闪退后重开自动恢复</p></div>"
                      + _EDIT_JS.replace("__SUBJ__", qname))
        elif open_subj == name:
            # ---- 无权限：只读展开（Q4） ----
            items += (f"<div class='subcard' style='padding:8px 12px'>"
                      f"<div class='fname'>{esc(name)} · 只读</div></div>"
                      f"<div class='ro'>{esc(content or '（暂无内容）')}</div>"
                      "<a class='btn2' href='/rc'>收起</a>")
        else:
            # ---- 折叠卡片：有权限浅蓝 / 无权限浅灰 ----
            meta = (content[:60] + "…") if len(content) > 60 else (content or "（暂无内容）")
            cls = "subcard perm" if has_perm(u, name) else "subcard"
            items += (f"<a class='{cls}' href='/rc?edit={qname}'>"
                      f"<div class='fname'>{esc(name)}</div>"
                      f"<div class='fmeta'>{esc(meta)}</div></a>")
    if not items:
        items = "<p class='tip'>暂无作业数据。首次使用请教师到管理页完成连接测试。</p>"

    body = (f"<div class='rcbar'>{M.back_btn('/home')}"
            f"<a class='rrefresh' href='/rc?refresh=1'>⟳ 刷新</a></div>"
            f"<h3>RemoteClasswork · 班级作业</h3>"
            f"{banner}<p class='tip'>{esc(date_line)}（浅蓝=有编辑权限 · 浅灰=仅查看）</p>"
            + items + M.REOPEN_TIP + M.FOOTER
            + _SCROLL_JS.replace("__Y__", str(h["scroll"])))
    return _rc_shell("RemoteClasswork", body)

@bp.route("/rc/save", methods=["POST"])
def rc_save():
    u = M.current_user()
    if not u:
        return M.login_page()
    subject = request.form.get("subject", "").strip()
    content = request.form.get("content", "")
    if not subject or not has_perm(u, subject):
        return M.msg_page("无该学科编辑权限", "/rc")
    now = time.time()
    blocked, wait = False, 0
    with _save_lock:                       # 原子"检查-设置"，防并发线程绕过节流
        last = _save_ts.get((u, subject), 0.0)
        if now - last < SAVE_GAP:
            blocked, wait = True, int(SAVE_GAP - (now - last)) + 1
        else:
            _save_ts[(u, subject)] = now
    if blocked:
        return redirect("/rc?edit=" + quote(subject, safe="") +
                        "&serr=" + quote("保存过于频繁，请 %d 秒后再试" % wait))
    ok, serr = save_subject(subject, content)
    if ok:
        hold_draft_clear(u)
        hold_set(u, edit="")
        return redirect("/rc")
    hold_set(u, edit=subject)              # 失败：保持编辑态与草稿，回到编辑页
    return redirect("/rc?edit=" + quote(subject, safe="") + "&serr=" + quote(serr))

@bp.route("/rc/hold", methods=["POST"])
def rc_hold():
    u = M.current_user()
    if not u:
        return "", 204
    try:
        y = int(float(request.form.get("scroll_y") or 0))
    except (TypeError, ValueError):
        y = 0
    hold_set(u, scroll=y)
    return "", 204

@bp.route("/rc/draft", methods=["POST"])
def rc_draft():
    u = M.current_user()
    if not u:
        return "", 204
    subj = request.form.get("subject", "").strip()
    if not subj:
        return "", 204
    hold_draft_save(u, subj, request.form.get("content", ""))
    return "", 204

# ---------------- RemoteClasswork 管理子页 ----------------
@bp.route("/admin/remoteclasswork", methods=["GET", "POST"])
def admin_rc():
    if not M.admin_ok():
        return M.admin_login_page()
    msg = ""
    act = request.form.get("act")
    test_out = None
    try:
        if act == "save":
            sset("base_url", request.form.get("base_url", "").strip() or DEF_BASE)
            sset("namespace", request.form.get("namespace", "").strip())
            sset("password", request.form.get("password", "").strip())
            sset("app_id", request.form.get("app_id", "").strip())
            sset("rc_token", request.form.get("rc_token", "").strip())
            sset("refresh_token", request.form.get("refresh_token", "").strip())
            for ln in request.form.get("newsubj", "").replace("，", ",").replace("、", ",").split(","):
                n = ln.strip()
                if n:
                    _register_subjects([n])
            for (n,) in M.dbq(RC_DB, "SELECT name FROM subjects"):
                v = request.form.get("perm_" + n)
                if v is not None:
                    sset("perm_" + n, v.strip())
            msg = "设置已保存。当前模式：" + ("令牌模式" if _mode() == "token" else "鉴权模式")
        elif act == "test":
            test_out = test_connection()
        elif act == "delsubj":
            n = request.form.get("name", "").strip()
            M.dbq(RC_DB, "DELETE FROM subjects WHERE name=?", (n,))
            msg = "已从列表移除学科：" + n
        elif act == "clearhold":
            M.dbq(RC_DB, "DELETE FROM drafts")
            M.dbq(RC_DB, "DELETE FROM holds")
            msg = "已清空全部 Hold 草稿与页面位置"
        elif act == "clearcache":
            with _cache_lock:
                CACHE.update({"ts": 0.0, "date": "", "data": None, "err": ""})
            try:
                os.remove(CACHE_PATH)
            except OSError:
                pass
            msg = "本地缓存已清除（下次访问重新拉取）"
    except Exception as e:
        msg = "操作失败：%s" % e

    with _cache_lock:
        cache_err, cache_ts = CACHE.get("err") or "", CACHE.get("ts") or 0.0
    st_rows = ""
    for (n,) in M.dbq(RC_DB, "SELECT name FROM subjects ORDER BY rowid"):
        st_rows += ("<tr><td>%s</td><td><input name='perm_%s' value='%s'></td></tr>"
                    % (esc(n), esc(n), esc(sget("perm_" + n))))
    del_rows = ""
    for (n,) in M.dbq(RC_DB, "SELECT name FROM subjects ORDER BY rowid"):
        del_rows += ("<tr><td>%s</td><td><form method='post' action='/admin/remoteclasswork'>"
                     "<input type='hidden' name='act' value='delsubj'>"
                     "<input type='hidden' name='name' value='%s'>"
                     "<button class='mini'>移除</button></form></td></tr>" % (esc(n), esc(n)))
    mode_line = "令牌模式（直接令牌优先）" if _mode() == "token" else "鉴权模式（appId 自动换发）"
    cache_age = "从未获取" if not cache_ts else "%d 秒前" % int(time.time() - cache_ts)
    tok_rem = ""
    if sget("rc_token"):
        rem = _token_exp(sget("rc_token")) - time.time()
        tok_rem = "已过期" if rem <= 0 else "剩余 %.1f 天" % (rem / 86400)
    test_html = ""
    if test_out is not None:
        test_html = "<h4>连接测试结果</h4><pre>" + esc("\n".join(test_out)) + "</pre>"
    body = (M.back_btn("/admin") + ("<p class='err'>" + esc(msg) + "</p>" if msg else "")
            + f"<p>当前模式：<b>{mode_line}</b> · 令牌剩余：{tok_rem or '—（鉴权模式自动续期）'} · "
            f"缓存：{cache_age}{(' · 最近异常：' + esc(cache_err)[:80]) if cache_err else ''}</p>"
            + "<form method='post' action='/admin/remoteclasswork'>"
            "<input type='hidden' name='act' value='save'>"
            "<h4>KV 连接（双模式互备）</h4>"
            "<p class='tip'>规则：直接令牌非空 → 令牌模式；留空 → 鉴权模式（namespace+password+appId 自动换发并续期）</p>"
            f"<label>Base URL<input name='base_url' value='{esc(sget('base_url'))}'></label>"
            f"<label>命名空间 namespace<input name='namespace' value='{esc(sget('namespace'))}'></label>"
            f"<label>密码 password<input name='password' value='{esc(sget('password'))}'></label>"
            f"<label>appId（鉴权模式必填）<input name='app_id' value='{esc(sget('app_id'))}'></label>"
            f"<label>直接令牌 token（粘贴现成 Bearer 后的一长串）"
            f"<textarea name='rc_token' rows='3'>{esc(sget('rc_token'))}</textarea></label>"
            f"<label>refresh_token（选填；仅对 OAuth 账号令牌有效的尽力续期）"
            f"<textarea name='refresh_token' rows='2'>{esc(sget('refresh_token'))}</textarea></label>"
            "<h4>学科权限（逗号分隔用户名，* 表示全部账号；留空=无人可编辑）</h4>"
            "<table border='1'><tr><th>学科</th><th>允许编辑的用户名</th></tr>"
            + (st_rows or "<tr><td colspan='2' class='tip'>暂无学科（保存作业或下方添加后出现）</td></tr>")
            + "</table>"
            "<h4>新增学科（逗号分隔）</h4>"
            "<input name='newsubj' placeholder='如：美术，信息技术'>"
            "<button type='submit'>保存全部设置</button></form>"
            + test_html +
            "<h4>测试连接</h4><form method='post' action='/admin/remoteclasswork'>"
            "<input type='hidden' name='act' value='test'>"
            "<button type='submit'>立即测试（鉴权/取键/试读）</button></form>"
            "<h4>移除学科（仅从列表隐藏，不影响 KV 数据）</h4>"
            "<table border='1'>" + (del_rows or "<tr><td class='tip'>暂无</td></tr>") + "</table>"
            "<h4>数据维护</h4><p>"
            "<form method='post' action='/admin/remoteclasswork' style='display:inline'>"
            "<input type='hidden' name='act' value='clearhold'>"
            "<button class='mini'>清空全部 Hold 草稿与位置</button></form> "
            "<form method='post' action='/admin/remoteclasswork' style='display:inline'>"
            "<input type='hidden' name='act' value='clearcache'>"
            "<button class='mini'>清除本地缓存</button></form></p>"
            + "<p class='tip'><a href='/admin'>返回管理中心</a> · <a href='/rc'>打开 RemoteClasswork</a></p>"
            + M.FOOTER)
    return (f"<!doctype html><html><head><meta charset='utf-8'>{M.STYLE}<title>RemoteClasswork 管理</title>"
            f"</head><body><h3>RemoteClasswork 管理</h3>{body}</body></html>")
