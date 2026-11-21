# -*- coding: utf-8 -*-
"""
Relay Assistant 模块（原 Canary0.1.5 AI 问答全量迁移）
数据：data/assistant/{assistant.db, assistant.log, relay_qa.csv}

Relay 3.0 · CTU 变更：
 - 修复 _gc_started 未在模块级初始化（init 首次读取即 NameError → 模块被
   跳过 → /assistant 全部 404），并删除原 if 块之后的无条件误复位。
   其余逻辑与 Build 43 一致。

Build 40 (v1.1) 变更：
 - 问答状态落库（jobs 表）：服务重启/平板闪退双免疫；/assistant 检测未完成请求
   自动跳 /result；超时自动解锁；后台 GC 清理过期记录。
 - LaTeX 渲染排查增强（问题9）：失败不再静默，完整 traceback 入 assistant.log；
   新增管理页"渲染自检"；\( \) / \[ \] / \boxed 归一化。
 - Relay Hold v2：草稿 0.8s 防抖保留；页面位置由主程序全局心跳接管。
"""
import csv, os, re, threading, time
from datetime import datetime
from html import escape as esc
import requests as http
from flask import Blueprint, redirect, request

M = None
AST_DB = QA_CSV = AST_LOG = None
log = None
_gc_started = False    # CTU 修复:必须在模块级初始化(原代码缺失)

def init(m, ast=None):
    global M, AST_DB, QA_CSV, AST_LOG, log, _gc_started
    M = m
    AST_DB = os.path.join(M.DIR_AST, "assistant.db")
    QA_CSV = os.path.join(M.DIR_AST, "relay_qa.csv")
    AST_LOG = os.path.join(M.DIR_AST, "assistant.log")
    log = M._mklog("relay.assistant", AST_LOG)
    init_db()
    # CTU 修复:GC 线程只启动一次,不再误复位
    if not _gc_started:
        _gc_started = True
        threading.Thread(target=_jobs_gc, daemon=True).start()

bp = Blueprint("assistant", __name__)

try:
    import markdown as _md
except ImportError:
    _md = None
try:
    import ziamath as _zm
except ImportError:
    _zm = None

AI_TIMEOUT = 45
AI_CONCURRENCY = 32
AI_WEBSEARCH_DEFAULT = True
CTX_ROUNDS_DEFAULT = 3
CTX_ROUNDS_MAX = 10
CTX_CHAR_BUDGET = 6000
CTX_MSG_CHARS = 1500
CTX_STORE_CHARS = 4000
CTX_DISPLAY_ROUNDS = 3
BREAKER_PAUSE_AT = 64
BREAKER_RESUME_AT = 32
MAX_ANSWER, MAX_QUERY = 700, 500
RESULT_TTL = 1800
REFRESH_SECONDS = 3
DRAFT_MAX_CHARS = 2000
QUEUE_WAIT = 300
BUSY_STALE = QUEUE_WAIT + AI_TIMEOUT + 30

def init_db():
    M.dbq(AST_DB, "CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
    M.dbq(AST_DB, """CREATE TABLE IF NOT EXISTS drafts(
        user TEXT PRIMARY KEY, q TEXT NOT NULL, ts REAL NOT NULL)""")
    M.dbq(AST_DB, """CREATE TABLE IF NOT EXISTS ctx(
        user TEXT NOT NULL, q TEXT NOT NULL, a TEXT NOT NULL, ts REAL NOT NULL)""")
    M.dbq(AST_DB, """CREATE TABLE IF NOT EXISTS jobs(
        user TEXT PRIMARY KEY, state TEXT NOT NULL, qtext TEXT NOT NULL,
        answer TEXT DEFAULT '', note TEXT DEFAULT '', err TEXT DEFAULT '',
        ts REAL NOT NULL)""")

def setting_get(key, default):
    rows = M.dbq(AST_DB, "SELECT value FROM settings WHERE key=?", (key,))
    return rows[0][0] if rows else default

def setting_set(key, value):
    M.dbq(AST_DB, "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)))

# ---------------- 排队计数与熔断 ----------------
_q_lock = threading.Lock()
_waiting, _active, _paused = 0, 0, False

def _breaker_state():
    with _q_lock:
        return _waiting, _active, _paused

def ai_busy_state():
    """供 Resource Librarian 让路判断（只读）。"""
    w, a, p = _breaker_state()
    return {"waiting": w, "active": a, "limit": AI_CONCURRENCY, "paused": p}

def _enter_queue():
    global _paused, _waiting
    with _q_lock:
        if _paused:
            return False
        if _waiting >= BREAKER_PAUSE_AT:
            _paused = True
            log.warning("熔断触发：排队 %d >= %d，暂停接受请求", _waiting, BREAKER_PAUSE_AT)
            return False
        _waiting += 1
        return True

def _q_grant():
    global _waiting, _active
    with _q_lock:
        _waiting -= 1
        _active += 1

def _q_abandon():
    global _waiting, _paused
    with _q_lock:
        _waiting -= 1
        if _paused and _waiting < BREAKER_RESUME_AT:
            _paused = False
            log.info("熔断恢复：排队 %d < %d", _waiting, BREAKER_RESUME_AT)

def _q_release():
    global _active, _paused
    with _q_lock:
        _active -= 1
        if _paused and _waiting < BREAKER_RESUME_AT:
            _paused = False
            log.info("熔断恢复：排队 %d < %d", _waiting, BREAKER_RESUME_AT)

# ---------------- AI 调用 ----------------
_ai_sem = threading.BoundedSemaphore(AI_CONCURRENCY)
_ai = http.Session()
_ai.mount("https://", http.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=64))

def _build_payload(question, allow_web, history=None):
    msgs = [{"role": "system", "content": "你是课堂学习助手。用不超过600字的简体中文回答。"
             "可用简洁的 Markdown：用**加粗**标注关键词，用 - 列点；"
             "不要使用表格、代码块和标题符号。"
             "如需数学公式，用 LaTeX 语法：行内公式用 $x^2+y^2$ 包裹，"
             "独立成行的公式用 $$\\int_0^1 f(x)dx$$ 包裹，"
             "禁止使用 \\( \\)、\\[ \\] 或其他分隔符；"
             "公式不要包裹在反引号或代码块内。"}]
    for q, a in (history or []):
        msgs.append({"role": "user", "content": q})
        msgs.append({"role": "assistant", "content": a})
    msgs.append({"role": "user", "content": question[:MAX_QUERY]})
    payload = {"model": M.AI_MODEL, "messages": msgs, "max_tokens": 600, "temperature": 0.5}
    if allow_web:
        payload["tools"] = [{"type": "web_search", "web_search": {"enable": True}}]
    return payload

def _estimate_tokens(q, a):
    return max(1, int((len(q) + len(a)) / 1.6) + 40)

# ---------------- LaTeX -> SVG ----------------
_MATH_BLOCK_RE = re.compile(r'\$\$(.+?)\$\$', re.S)
_MATH_INLINE_RE = re.compile(r'(?<!\$)\$([^\$\n]+?)\$(?!\$)', re.S)
_MATH_TOKEN = "⟦M{}Q⟧"
_math_cache, _math_cache_lock = {}, threading.Lock()
MATH_CACHE_MAX = 256

def _norm_math(text):
    """兼容模型实际输出习惯：\\(...\\)→$...$，\\[...\\]→$$...$$，\\boxed{...} 剥壳。"""
    text = text.replace("\\(", "$").replace("\\)", "$")
    text = text.replace("\\[", "$$").replace("\\]", "$$")
    text = re.sub(r"\\boxed\{([^{}]*)\}", r"\1", text)
    return text

def _render_latex_svg(expr):
    # ── 问题9 排查注释（勿删）────────────────────────────────────────────
    # 此函数曾静默吞掉所有异常，导致"公式渲染失效"无从排查。
    # 头号嫌疑：Relay 服务实际运行的 python.exe 与 pip 安装 ziamath 的解释器不是
    # 同一个（与 resource.log 中 pymupdf 缺失同款先例，已证实该模式会发生）。
    # 验证：sc qc Relay 查看服务所用 python 路径 → 用它执行
    # "<python路径>" -c "import ziamath;print(ziamath.__version__)"
    # 若 ImportError：用该解释器 pip install ziamath 即可修复。
    # 亦可到 Assistant 管理页点"公式渲染自检"现场查看版本与 traceback。
    # ────────────────────────────────────────────────────────────────────
    if _zm is None:
        log.warning("[LaTeX] ziamath 未导入（_zm=None）——请核对服务所用解释器是否已安装 ziamath")
        return None
    with _math_cache_lock:
        if expr in _math_cache:
            return _math_cache[expr]
    try:
        svg = _zm.Latex(expr).svg()
    except Exception:
        log.exception("[LaTeX] 渲染失败 expr=%r（详见上方 traceback；"
                      "若为模块内部错误请连同日志反馈）", expr[:60])
        return None
    with _math_cache_lock:
        if len(_math_cache) >= MATH_CACHE_MAX:
            _math_cache.pop(next(iter(_math_cache)))
        _math_cache[expr] = svg
    return svg

def md_to_html(text):
    if not text:
        return ""
    text = _norm_math(text.strip())
    snippets = []

    def _stash(m, display):
        idx = len(snippets)
        snippets.append((m.group(1).strip(), display))
        return _MATH_TOKEN.format(idx)

    text = _MATH_BLOCK_RE.sub(lambda m: _stash(m, True), text)
    text = _MATH_INLINE_RE.sub(lambda m: _stash(m, False), text)
    text = esc(text)
    if _md is not None:
        try:
            html = _md.markdown(text, extensions=["nl2br"])
        except Exception:
            html = "".join("<p>%s</p>" % p for p in text.splitlines() if p.strip())
    else:
        html = "".join("<p>%s</p>" % p for p in text.splitlines() if p.strip())
    for idx, (expr, display) in enumerate(snippets):
        token = esc(_MATH_TOKEN.format(idx))
        svg = _render_latex_svg(expr)
        if svg is None:
            repl = "<code class='mathfall'>%s</code>" % esc(expr)
        elif display:
            repl = "<span class='mathblk'>%s</span>" % svg
        else:
            repl = "<span class='mathin'>%s</span>" % svg
        html = html.replace(token, repl)
    return html

def _render_selftest():
    out = []
    try:
        import ziamath
        out.append("ziamath 版本：%s（模块位置：%s）"
                   % (getattr(ziamath, "__version__", "?"),
                      getattr(ziamath, "__file__", "?")))
    except Exception as e:
        out.append("ziamath 导入失败：%r" % (e,))
        out.append("→ 排查：Relay 服务运行的 python.exe 可能与 pip 安装目标不一致。")
        out.append(" 步骤：sc qc Relay 查看 BINARY_PATH_NAME 中的 python 路径，然后用该解释器执行：")
        out.append(' "<该python路径>" -c "import ziamath;print(ziamath.__version__)"')
        out.append(" 报 ImportError 则用该解释器执行 pip install ziamath")
        return out
    try:
        svg = _render_latex_svg("x^2+\\frac{1}{2}")
        if svg:
            out.append("渲染测试：成功（SVG 长度 %d 字符）" % len(svg))
        else:
            out.append("渲染测试：返回空 —— 详细原因见 assistant.log 中 [LaTeX] 条目")
    except Exception as e:
        out.append("渲染测试异常：%r" % (e,))
    return out

# ---------------- 落盘 ----------------
_csv_lock = threading.Lock()

def _qa_log(user, dev, q, ans, tokens, allow_web):
    with _csv_lock:
        new = not os.path.exists(QA_CSV)
        with open(QA_CSV, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["时间", "账号", "设备", "提问", "回答", "tokens", "联网"])
            w.writerow([datetime.now().isoformat(timespec="seconds"), user, dev,
                        q, ans, tokens, "是" if allow_web else "否"])

def _finalize(user, dev, qtext, answer, usage, allow_web, t0):
    try:
        ans = answer.strip()
        if len(ans) > MAX_ANSWER:
            ans = ans[:MAX_ANSWER] + "……"
        tokens = usage or _estimate_tokens(qtext, answer)
        exact = "API" if usage else "估算"
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        M.dbq(M.COMMON_DB, "UPDATE accounts SET last_seen=?, last_query=?, "
                           "tokens_total=tokens_total+? WHERE username=?",
              (now, qtext[:80], tokens, user))
        _qa_log(user, dev, qtext, ans, tokens, allow_web)
        dur = max(1, int(time.time() - t0))
        return "回答长度 %d · 折算 tokens %d（%s）· 用时 %d s" % (len(ans), tokens, exact, dur)
    except Exception:
        log.exception("问答记录写入失败")
        return ""

# ---------------- 问答任务（jobs 表，重启/闪退双免疫） ----------------
def _job_get(user):
    try:
        r = M.dbq(AST_DB, "SELECT state,qtext,answer,note,err,ts FROM jobs WHERE user=?", (user,))
        if r:
            return {"state": r[0][0], "qtext": r[0][1], "answer": r[0][2],
                    "note": r[0][3], "err": r[0][4], "ts": float(r[0][5] or 0)}
    except Exception:
        pass
    return None

def _job_set(user, state, qtext, answer="", note="", err="", ts=None):
    try:
        M.dbq(AST_DB, "INSERT OR REPLACE INTO jobs VALUES(?,?,?,?,?,?,?)",
              (user, state, (qtext or "")[:MAX_QUERY], (answer or "")[:MAX_ANSWER],
               (note or "")[:200], (err or "")[:200], ts or time.time()))
    except Exception:
        log.exception("jobs 写入失败 user=%s", user)

def _jobs_gc():
    while True:
        time.sleep(120)
        try:
            M.dbq(AST_DB, "DELETE FROM jobs WHERE state='done' AND ts < ?",
                  (time.time() - RESULT_TTL,))
        except Exception:
            pass

def _ai_worker(user, dev, qtext, allow_web):
    t0 = time.time()
    granted = False
    try:
        if not _ai_sem.acquire(timeout=QUEUE_WAIT):
            log.warning("排队超时 user=%s（%ds）", user, QUEUE_WAIT)
            _job_set(user, "done", qtext, err="排队超时，请稍后重新发送。")
            return
        granted = True
        _q_grant()
        usage, answer, err = None, "", None
        try:
            history = ctx_load(user, ctx_rounds())
            r = M.ai_post(_ai, _build_payload(qtext, allow_web, history), AI_TIMEOUT)
            r.raise_for_status()
            obj = r.json()
            if obj.get("usage"):
                usage = obj["usage"].get("total_tokens")
            ch = obj.get("choices") or [{}]
            answer = (ch[0].get("message") or {}).get("content") or ""
        except Exception as e:
            err = "暂时无法连接 AI，请稍后再试（%s）。" % type(e).__name__
            log.warning("AI 调用失败 user=%s err=%s", user, e)
        if err is None:
            note = _finalize(user, dev, qtext, answer, usage, allow_web, t0)
            if answer:
                ctx_append(user, qtext, answer)
                _job_set(user, "done", qtext, answer=answer, note=note)
            else:
                _job_set(user, "done", qtext, err=err or "AI 未返回内容，请重试。")
        else:
            _job_set(user, "done", qtext, err=err)
    except Exception:
        log.exception("AI 工作线程异常 user=%s", user)
        try:
            _job_set(user, "done", qtext, err="服务器内部错误，请重试。")
        except Exception:
            pass
    finally:
        if granted:
            _q_release()
            _ai_sem.release()
        else:
            _q_abandon()

# ---------------- Relay Hold：输入草稿（0.8s 防抖，闪退恢复） ----------------
_draft_lock = threading.Lock()

def hold_draft_save(user, q):
    try:
        with _draft_lock:
            M.dbq(AST_DB, "INSERT OR REPLACE INTO drafts(user,q,ts) VALUES(?,?,?)",
                  (user, q[:DRAFT_MAX_CHARS], time.time()))
    except Exception as e:
        log.warning("Hold草稿保存失败 user=%s err=%s", user, e)

def hold_draft_load(user):
    try:
        rows = M.dbq(AST_DB, "SELECT q FROM drafts WHERE user=?", (user,))
        return rows[0][0] if rows and rows[0][0] else ""
    except Exception:
        return ""

def hold_draft_clear(user):
    try:
        with _draft_lock:
            M.dbq(AST_DB, "DELETE FROM drafts WHERE user=?", (user,))
    except Exception:
        pass

def hold_draft_clear_all():
    try:
        with _draft_lock:
            M.dbq(AST_DB, "DELETE FROM drafts")
    except Exception:
        pass

# ---------------- 对话上下文 ----------------
def ctx_rounds():
    try:
        v = int(setting_get("ctx_rounds", str(CTX_ROUNDS_DEFAULT)))
    except (TypeError, ValueError):
        v = CTX_ROUNDS_DEFAULT
    return max(0, min(CTX_ROUNDS_MAX, v))

def ctx_append(user, q, a):
    try:
        M.dbq(AST_DB, "INSERT INTO ctx(user,q,a,ts) VALUES(?,?,?,?)",
              (user, q[:MAX_QUERY], a[:CTX_STORE_CHARS], time.time()))
    except Exception as e:
        log.warning("上下文写入失败 user=%s err=%s", user, e)

def ctx_load(user, rounds):
    if rounds <= 0 or not user:
        return []
    try:
        rows = M.dbq(AST_DB, "SELECT q,a FROM ctx WHERE user=? ORDER BY rowid DESC LIMIT ?",
                     (user, rounds))
    except Exception:
        return []
    turns, budget = [], CTX_CHAR_BUDGET
    for q, a in reversed(rows):
        q, a = q[:CTX_MSG_CHARS], a[:CTX_MSG_CHARS]
        cost = len(q) + len(a) + 16
        if cost > budget:
            break
        turns.append((q, a))
        budget -= cost
    return turns

def ctx_clear(user):
    try:
        M.dbq(AST_DB, "DELETE FROM ctx WHERE user=?", (user,))
    except Exception:
        pass

def ctx_count(user):
    try:
        rows = M.dbq(AST_DB, "SELECT COUNT(*) FROM ctx WHERE user=?", (user,))
        return int(rows[0][0] or 0)
    except Exception:
        return 0

# ---------------- 页面 ----------------
def web_checkbox():
    if setting_get("websearch", "1" if AI_WEBSEARCH_DEFAULT else "0") != "1":
        return ""
    return ("<label class='tip'><input type='checkbox' name='web' value='1'>联网搜索</label>")

_DRAFT_JS = ("<script>(function(){"
             "var ta=document.getElementById('q');if(!ta)return;"
             "var timer=null,submitting=false;"
             "function save(){var x=new XMLHttpRequest();"
             "x.open('POST','/hold/draft',true);"
             "x.setRequestHeader('Content-Type','application/x-www-form-urlencoded');"
             "x.send('q='+encodeURIComponent(ta.value));}"
             "ta.addEventListener('input',function(){if(timer)clearTimeout(timer);timer=setTimeout(save,800);});"
             "var f=document.getElementById('askform');"
             "if(f)f.addEventListener('submit',function(){submitting=true;});"
             "window.addEventListener('pagehide',function(){if(!submitting)save();});"
             "})();</script>")

def _ctx_html(user):
    rounds = ctx_rounds()
    if rounds <= 0 or not user:
        return ""
    total = ctx_count(user)
    turns = ctx_load(user, min(rounds, CTX_DISPLAY_ROUNDS))
    if not turns:
        if total:
            return ("<p class='tip'>对话上下文：已积累 %d 轮（提问时将携带最近 %d 轮）</p>"
                    % (total, rounds))
        return ""
    parts = ["<h4>对话上下文（共 %d 轮 · 提问时携带最近 %d 轮）</h4><div class='ctx'>"
             % (total, min(rounds, CTX_DISPLAY_ROUNDS))]
    for q, a in turns:
        parts.append("<p class='ctxq'>%s</p><div class='md'>%s</div>" % (esc(q), md_to_html(a)))
    parts.append("</div>")
    return "".join(parts)

def qa_page(user, ans_html=None, qtext="", note=None, ta_text=None, show_busy=True,
            extra_head="", show_ctx=True, waiting=False):
    w, a, paused = _breaker_state()
    rounds = ctx_rounds()
    qline = ("排队 %d 人 · 并行 %d/%d" % (w, a, AI_CONCURRENCY)
             + (" · 上下文 %d 轮" % rounds if rounds > 0 else "")
             + (" · 熔断中，暂停接受请求" if paused else ""))
    j = _job_get(user) if user else None
    busy_fresh = bool(j) and j["state"] == "wait" and (time.time() - j["ts"] < BUSY_STALE)
    busy = show_busy and busy_fresh and not waiting
    busy_line = ("<p class='queue'><span class='spinner sm'></span>"
                 "存在请求正在排队/回答，完成前不允许发送新请求</p>") if busy else ""
    if waiting:
        ah = ("<div class='fadein'><p class='queue'><span class='spinner'></span>正在排队/生成回答"
              "<span class='dots'><i></i><i></i><i></i></span></p></div>")
    else:
        ah = ("<h4>回答</h4><div class='md fadein'>" + ans_html + "</div>") if ans_html else ""
    nh = ("<p class='tip'>" + esc(note) + "</p>") if note else ""
    qh = ("<h4>问题</h4><pre>" + esc(qtext) + "</pre>") if (qtext and (ans_html or waiting)) else ""
    ta = esc(ta_text if ta_text is not None else (hold_draft_load(user) if user else ""))
    has_result = bool(j) and (time.time() - j["ts"] < RESULT_TTL * 2)
    btn_last = "<a class='btn2' href='/result'>查看上次回答</a>" if has_result else ""
    ctxhtml = _ctx_html(user) if (show_ctx and user) else ""
    ctxbtn = ("<form method='post' action='/clearctx' style='display:inline'>"
              "<button class='mini'>清除我的上下文</button></form>") if (rounds > 0 and user) else ""
    return (f"<!doctype html><html><head><meta charset='utf-8'>{extra_head}{M.STYLE}{M.ANIM_STYLE}"
            f"<title>Assistant</title></head><body>{M.back_btn('/home')}<h3>Assistant · AI Chat</h3>"
            f"<p class='tip'>当前账号：{esc(user)} <a href='/unbind'>解绑本设备</a> {ctxbtn}</p>"
            "<form method='post' action='/ask' id='askform'>"
            f"<textarea name='q' id='q' rows='3' "
            f"placeholder='输入问题，轻触发送以询问AI（输入自动保存草稿）'>{ta}</textarea>"
            + web_checkbox() +
            "<button type='submit'>发 送</button></form>"
            + btn_last +
            f"<p class='queue'>{esc(qline)}</p>{busy_line}{qh}{ah}{nh}"
            + ctxhtml + M.REOPEN_TIP + M.FOOTER + M.hold_loc_js(user) + _DRAFT_JS
            + "</body></html>")

# ---------------- 路由 ----------------
@bp.route("/assistant")
def chat_home():
    u = M.current_user()
    if not u:
        return M.login_page()
    M.hold_page("assistant")
    # jobs 落库后：未完成请求自动跳回 /result（服务重启/闪退双免疫）
    j = _job_get(u)
    extra = ""
    if j and j["state"] == "wait" and (time.time() - j["ts"] < BUSY_STALE):
        extra = "<meta http-equiv='refresh' content='1;url=/result'>"
    return qa_page(u, extra_head=extra)

@bp.route("/ask", methods=["POST", "GET"])
def ask():
    u = M.current_user()
    if not u:
        return M.login_page()
    src = request.values if request.method == "GET" else request.form
    qtext = (src.get("q") or "").strip()
    if not qtext:
        j = _job_get(u)
        return redirect("/result", 303) if j else redirect("/assistant", 303)
    j = _job_get(u)
    if j and j["state"] == "wait" and (time.time() - j["ts"] < BUSY_STALE):
        return redirect("/result", 303)  # 单飞：已有进行中的请求
    if not _enter_queue():
        w, a, _ = _breaker_state()
        log.warning("熔断拒绝 user=%s 排队=%d", u, w)
        return qa_page(u, ans_html=("<p class='err'>系统繁忙：当前排队 %d，正在回答 %d/%d，"
                                    "已暂停接受新请求，请稍后重新发送。</p>" % (w, a, AI_CONCURRENCY)),
                       qtext=qtext, ta_text=qtext)
    hold_draft_clear(u)  # Relay Hold：进入队列，草稿使命完成
    dev = M.device_id()
    allow_web = (src.get("web") == "1") and setting_get("websearch", "1") == "1"
    _job_set(u, "wait", qtext)
    threading.Thread(target=_ai_worker, args=(u, dev, qtext, allow_web), daemon=True).start()
    log.info("提交问题 user=%s len=%d web=%s", u, len(qtext), allow_web)
    return redirect("/result", 303)

@bp.route("/hold/draft", methods=["POST"])
def hold_draft():
    u = M.current_user()
    if not u:
        return "", 204
    q = request.form.get("q") or ""
    if len(q) > DRAFT_MAX_CHARS:
        q = q[:DRAFT_MAX_CHARS]
    hold_draft_save(u, q)
    return "", 204

@bp.route("/clearctx", methods=["POST"])
def clearctx():
    u = M.current_user()
    if not u:
        return M.login_page()
    ctx_clear(u)
    log.info("用户清除上下文 user=%s", u)
    return redirect("/assistant", 303)

@bp.route("/result")
def result():
    u = M.current_user()
    if not u:
        return M.login_page()
    j = _job_get(u)
    if j is None:
        return redirect("/assistant", 303)
    if j["state"] == "wait":
        if time.time() - j["ts"] > BUSY_STALE:
            _job_set(u, "done", j["qtext"], err="本次请求超时未完成，请重新发送。")
            j = _job_get(u)
        else:
            # 等待页轮询：不写 Hold 页码（避免频繁 DB 写；全局心跳除外）
            return qa_page(u, qtext=j["qtext"], show_busy=False,
                           extra_head=("<meta http-equiv='refresh' content='%d;url=/result'>"
                                       % REFRESH_SECONDS),
                           show_ctx=False, waiting=True)
    M.hold_page("assistant")
    if j.get("err"):
        return qa_page(u, ans_html="<p class='err'>" + esc(j["err"]) + "</p>", qtext=j["qtext"])
    return qa_page(u, ans_html=md_to_html(j["answer"]) if j["answer"] else "",
                   qtext=j["qtext"], note=j["note"] or None)

# ---------------- Assistant 管理子页 ----------------
@bp.route("/admin/assistant", methods=["GET", "POST"])
def admin_assistant():
    if not M.admin_ok():
        return M.admin_login_page()
    msg = ""
    act = request.form.get("act")
    test_out = None
    try:
        if act == "webtoggle":
            cur = setting_get("websearch", "1" if AI_WEBSEARCH_DEFAULT else "0")
            setting_set("websearch", "0" if cur == "1" else "1")
            msg = "已切换 AI 联网搜索选项"
        elif act == "ctxset":
            try:
                v = int(request.form.get("ctx", ""))
            except ValueError:
                v = -1
            if 0 <= v <= CTX_ROUNDS_MAX:
                setting_set("ctx_rounds", v)
                msg = "上下文保留已设为 %d 轮（立即生效）" % v
            else:
                msg = "轮数无效（允许 0-%d）" % CTX_ROUNDS_MAX
        elif act == "ctxclear":
            u2 = request.form.get("u", "").strip()
            ctx_clear(u2)
            msg = "已清除 %s 的对话上下文" % u2
        elif act == "ctxclear_all":
            M.dbq(AST_DB, "DELETE FROM ctx")
            msg = "已清空全部用户的对话上下文"
        elif act == "zerotok":
            u2 = request.form.get("u", "").strip()
            M.dbq(M.COMMON_DB, "UPDATE accounts SET tokens_total=0 WHERE username=?", (u2,))
            msg = "已清零 %s 的 Token 统计" % u2
        elif act == "zerotok_all":
            M.dbq(M.COMMON_DB, "UPDATE accounts SET tokens_total=0")
            msg = "已清零全部 Token 统计"
        elif act == "rendertest":
            test_out = _render_selftest()
    except Exception as e:
        msg = "操作失败：%s" % e
    ws_state = "开" if setting_get("websearch", "1" if AI_WEBSEARCH_DEFAULT else "0") == "1" else "关"
    cr = ctx_rounds()
    try:
        cnt, tot = M.dbq(M.COMMON_DB,
                         "SELECT COUNT(*), IFNULL(SUM(tokens_total),0) FROM accounts")[0]
    except Exception:
        cnt, tot = 0, 0
    acc_rows = ""
    try:
        for u2, t, lq in M.dbq(M.COMMON_DB, "SELECT username, tokens_total, last_query "
                                            "FROM accounts ORDER BY username"):
            acc_rows += (f"<tr><td>{esc(u2)}</td><td style='text-align:right'>{int(t or 0):,}</td>"
                         f"<td class='tip'>{esc(lq or '')}</td><td>"
                         f"<form method='post' action='/admin/assistant' style='display:inline'>"
                         f"<input type='hidden' name='act' value='zerotok'>"
                         f"<input type='hidden' name='u' value='{esc(u2)}'>"
                         f"<button class='mini'>Token清零</button></form></td></tr>")
    except Exception:
        pass
    top = ""
    try:
        for u2, t in M.dbq(M.COMMON_DB, "SELECT username, tokens_total FROM accounts "
                                        "WHERE tokens_total>0 ORDER BY tokens_total DESC LIMIT 10"):
            top += "<tr><td>%s</td><td style='text-align:right'>%s</td></tr>" \
                   % (esc(u2), f"{int(t or 0):,}")
    except Exception:
        pass
    rec_rows = []
    head = []
    try:
        with open(QA_CSV, "r", encoding="utf-8-sig") as f:
            allr = list(csv.reader(f))
        head = allr[0] if allr else []
        rec_rows = allr[-100:] if len(allr) > 100 else allr[1:]
    except OSError:
        pass
    rec = ("<table border='1'><tr>" + "".join("<th>%s</th>" % esc(h) for h in head) + "</tr>")
    for row in rec_rows:
        tds = "".join("<td>%s</td>" % esc((c[:60] + "…") if len(c) > 60 else c) for c in row)
        rec += "<tr>%s</tr>" % tds
    rec += "</table><p class='tip'>仅显示最近 100 条，完整记录见 relay_qa.csv</p>"
    test_html = ""
    if test_out is not None:
        test_html = "<h4>渲染自检结果</h4><pre>" + esc("\n".join(test_out)) + "</pre>"
    body = (M.back_btn("/admin") + ("<p class='err'>" + esc(msg) + "</p>" if msg else "")
            + f"<p>账号数 <b>{cnt}</b> · 累计 tokens <b>{int(tot):,}</b></p>"
            + "<h4>账号数据（Token 用量 / 最近提问）</h4>"
            + "<table border='1'><tr><th>账号</th><th>累计tokens</th>"
              "<th>最近提问</th><th>操作</th></tr>"
            + (acc_rows or "<tr><td colspan='4' class='tip'>暂无账号</td></tr>")
            + "</table>"
            + "<form method='post' action='/admin/assistant'>"
              "<input type='hidden' name='act' value='zerotok_all'>"
              "<button class='mini'>全部账号 Token 清零</button></form>"
            + "<h4>Token 用量 Top10</h4><table border='1'><tr><th>账号</th><th>tokens</th></tr>"
            + (top or "<tr><td colspan='2' class='tip'>暂无数据</td></tr>")
            + "</table>"
            + "<h4>公式渲染自检（问题9 排查工具）</h4>"
              "<form method='post' action='/admin/assistant'>"
              "<input type='hidden' name='act' value='rendertest'>"
              "<button class='mini'>立即自检（ziamath 版本 + 现场渲染）</button></form>"
              "<p class='tip'>若自检失败：先用 sc qc Relay 确认服务所用 python.exe，"
              "再用该解释器安装 ziamath（详见自检输出与 assistant.py 头部注释）</p>"
            + test_html
            + "<p>AI 联网搜索选项：当前【" + ws_state + "】"
              "<form method='post' action='/admin/assistant' style='display:inline'>"
              "<input type='hidden' name='act' value='webtoggle'>"
              "<button class='mini'>切换</button></form></p>"
            + "<p>对话上下文：当前保留 <b>" + str(cr) + "</b> 轮（0=关闭）"
              "<form method='post' action='/admin/assistant' style='display:inline'>"
              "<input type='hidden' name='act' value='ctxset'>"
              f"<input name='ctx' type='number' min='0' max='{CTX_ROUNDS_MAX}' value='{cr}' "
              "style='width:70px'>"
              "<button class='mini'>保存轮数</button></form> "
              "<form method='post' action='/admin/assistant' style='display:inline'>"
              "<input type='hidden' name='act' value='ctxclear_all'>"
              "<button class='mini'>清空全部用户上下文</button></form></p>"
            + "<p>清除单用户上下文：<form method='post' action='/admin/assistant' "
              "style='display:inline'>"
              "<input type='hidden' name='act' value='ctxclear'>"
              "<input name='u' placeholder='账号' style='width:160px'>"
              "<button class='mini'>清除</button></form></p>"
            + "<h4>询问记录</h4>"
            + rec
            + "<p class='tip'><a href='/admin'>返回管理中心</a></p>"
            + M.FOOTER)
    return (f"<!doctype html><html><head><meta charset='utf-8'>{M.STYLE}"
            f"<title>Assistant 管理</title></head><body><h3>Assistant 管理</h3>{body}</body></html>")
