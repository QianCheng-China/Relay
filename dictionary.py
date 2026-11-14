# -*- coding: utf-8 -*-
"""
Relay Dictionary 模块：mobi/prc 词典查询
----------------------------------------------------------------
数据：data/dictionary/{dict.db, dict.log, dictsource/, last_extract_*.html}
      词典源文件(.mobi/.prc/.azw)由教师放入 dictsource 目录，管理页导入。
管线（基于四份真实样例适配，零第三方依赖——自带 MOBI/PalmDOC 解析器）：
  BOOKMOBI 解析（record0：压缩类型[0:2]/文本记录数[4:6]/编码[24:28]/extra flags[258:260]）
  → 逐记录剥 trailing entries（先 multibyte overlap 再逐标志位 varint，KindleUnpack 规范）
  → PalmDOC 解压 → 拼接正文 → 剥 head/guide/frameset/pagebreak
  → 三级自适应词条切分：
      ① idx:entry/idx:orth（标准 Kindle 词典）
      ② <h1>-<h4> 标题分段（牛津系 / 古汉语常用字字典）
      ③ <hr/> 分段 + 段首 <b> 词条头（现代汉英词典式）
  → 清洗（script/style/img/iframe 剔除、a/filepos 剥壳留字）、伪词条过滤
  → SQLite 入库。同一头词多条（多音字）全部保留。
查询：精确 → 前缀 → 包含 三级合并（互相排他去重），最多 20 条；首 3 条展开，其余 <details> 折叠。
Relay Hold v2：查询词在 ?q= 由全局 URL 心跳恢复；滚动由全局滚动心跳恢复；查询历史按用户存 10 条。
"""
import html as _htmlmod
import os, re, sqlite3, threading, time
from datetime import datetime
from html import escape as esc
from urllib.parse import quote
from flask import Blueprint, redirect, request

M = None
DICT_DB = DICT_LOG = DICT_SRC = None
log = None

SRC_EXTS = (".mobi", ".prc", ".azw", ".azw3")
MAX_FILE_MB = 120
IMPORT_COMMIT = 2000
MIN_BODY_TEXT = 10     # 剥标签后正文少于该字符数的段落视为伪词条，跳过
MIN_ENTRIES = 10       # 有效词条少于该数判定解析失败（并写诊断文件）

def init(m, ast=None):
    global M, DICT_DB, DICT_LOG, DICT_SRC, log
    M = m
    DICT_DB = os.path.join(M.DIR_DICT, "dict.db")
    DICT_LOG = os.path.join(M.DIR_DICT, "dict.log")
    DICT_SRC = os.path.join(M.DIR_DICT, "dictsource")
    os.makedirs(DICT_SRC, exist_ok=True)
    log = M._mklog("relay.dict", DICT_LOG)
    init_db()

bp = Blueprint("dict", __name__)

def init_db():
    M.dbq(DICT_DB, """CREATE TABLE IF NOT EXISTS books(
        book_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE,
        entries INTEGER DEFAULT 0, imported_at TEXT DEFAULT '')""")
    M.dbq(DICT_DB, """CREATE TABLE IF NOT EXISTS entries(
        book_id INTEGER NOT NULL, headword TEXT NOT NULL,
        headword_norm TEXT NOT NULL, content_html TEXT NOT NULL)""")
    M.dbq(DICT_DB, """CREATE TABLE IF NOT EXISTS history(
        user TEXT NOT NULL, q TEXT NOT NULL, ts REAL NOT NULL)""")
    try:
        M.dbq(DICT_DB, "CREATE INDEX IF NOT EXISTS idx_norm ON entries(headword_norm)")
    except Exception:
        pass

# ================= MOBI / PalmDOC 解析器（自带，零依赖） =================
def _pdb_records(data):
    num = int.from_bytes(data[76:78], "big")
    offs = []
    for i in range(num):
        offs.append(int.from_bytes(data[78 + i * 8:82 + i * 8], "big"))
    offs.append(len(data))
    return [(offs[i], offs[i + 1]) for i in range(num)]

def _palmdec(data):
    """PalmDOC LZ77 变体解压。"""
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        b = data[i]; i += 1
        if b == 0:
            out.append(0)
        elif b <= 8:
            out += data[i:i + b]; i += b
        elif b <= 0x7F:
            out.append(b)
        elif b <= 0xBF:
            if i >= n:
                break
            b2 = data[i]; i += 1
            dist = (((b << 8) | b2) >> 3) & 0x7FF
            ln = (b2 & 7) + 3
            for _ in range(ln):
                out.append(out[-dist])
        else:
            out.append(0x20)
            out.append(b ^ 0x80)
    return bytes(out)

def _trailing_entry_size(data, size):
    """从 data[size-1] 起向前读 varint（KindleUnpack 规范：高位字节在前遇到即止）。"""
    bitpos, result = 0, 0
    while size > 0:
        v = data[size - 1]
        result |= (v & 0x7F) << bitpos
        bitpos += 7
        size -= 1
        if (v & 0x80) or bitpos >= 28 or size == 0:
            return result
    return result

def _strip_extra(rec, flags):
    """剥记录尾部 extra data：先 multibyte overlap（bit0），再按 flags>>1 逐位剥 trailing entry。
    词典的 filepos 索引就在 trailing entries 中，必须在解压前剥离。"""
    if not rec:
        return rec
    off = 0
    if flags & 1:
        off = (rec[len(rec) - 1] & 3) + 1
    f = flags >> 1
    while f:
        if f & 1:
            off += _trailing_entry_size(rec, len(rec) - off)
        f >>= 1
    if off and off < len(rec):
        return rec[:len(rec) - off]
    return rec

def extract_text(path):
    """返回 MOBI 正文 HTML 字符串（支持未压缩与 PalmDOC 压缩；HUFF/CDIC 类 AZW3 不支持）。
    record0 字段偏移均为“record0 起始”绝对偏移（KindleUnpack 布局）：
      [0:2] 压缩类型 · [8:10] 文本记录数 · [16:20] 'MOBI' 魔数 · [28:32] 文本编码
      [36:40] 版本 · [242:244](0xF2) extra data flags（版本 ≥5 才存在）"""
    if os.path.getsize(path) > MAX_FILE_MB * 1024 * 1024:
        raise ValueError("文件超过 %dMB，不予处理" % MAX_FILE_MB)
    with open(path, "rb") as f:
        data = f.read()
    if data[60:68] != b"BOOKMOBI":
        raise ValueError("不是 MOBI/PRC 格式（缺少 BOOKMOBI 魔数）")
    recs = _pdb_records(data)
    if not recs or recs[0][1] <= recs[0][0]:
        raise ValueError("PDB 记录表异常（文件可能损坏）")
    maxn = (len(data) - 78) // 8
    num = int.from_bytes(data[76:78], "big")
    if num > maxn:
        raise ValueError("PDB 记录数异常（文件可能损坏）")
    r0 = data[recs[0][0]:recs[0][1]]
    if len(r0) < 40:
        raise ValueError("record0 过短，文件可能损坏")
    compression = int.from_bytes(r0[0:2], "big")
    text_count = int.from_bytes(r0[8:10], "big")       # PalmDOC 记录数
    encoding = int.from_bytes(r0[28:32], "big")        # 65001=UTF-8 / 1252=CP1252
    if compression == 17480:
        raise ValueError("HUFF/CDIC 压缩（AZW3/KF8）暂不支持，请使用 .mobi/.prc 源文件")
    if compression not in (1, 2):
        raise ValueError("不支持的压缩类型 %d" % compression)
    codec = "utf-8" if encoding in (65001, 0) else "cp1252"
    flags = 0
    if len(r0) >= 244 and r0[16:20] == b"MOBI":
        ver = int.from_bytes(r0[36:40], "big")
        if ver >= 5:
            flags = int.from_bytes(r0[242:244], "big") & 0xFFFF
    chunks = []
    for idx in range(1, min(text_count, len(recs) - 1) + 1):
        s, e = recs[idx]
        rec = data[s:e]
        if not rec:
            continue
        rec = _strip_extra(rec, flags)     # 先剥 trailing entries（含词典 filepos）
        if compression == 2:
            rec = _palmdec(rec)
        chunks.append(rec.decode(codec, "ignore"))
    html = "".join(chunks)
    if len(html.strip()) < 200:
        raise ValueError("解压后正文过短，文件可能损坏")
    return html


# ================= 清洗与词条切分 =================
_TAG_RE = re.compile(r"<[^>]+>")

def _strip_tags(s):
    return _htmlmod.unescape(_TAG_RE.sub("", s or "")).strip()

def _prep(html):
    """正文预处理：取 body、剥 head/guide/frameset/pagebreak。"""
    m = re.search(r"(?is)<body[^>]*>", html)
    if m:
        html = html[m.end():]
    html = re.sub(r"(?is)<head.*?</head>", "", html)
    html = re.sub(r"(?is)<guide.*?</guide>", "", html)
    html = re.sub(r"(?is)</?frameset[^>]*>", "", html)
    html = re.sub(r"(?is)</?frame[^>]*>", "", html)
    html = re.sub(r"(?is)<mbp:pagebreak\s*/?>", "", html)
    return html

def _clean_body(h):
    h = re.sub(r"(?is)<script.*?</script>", "", h)
    h = re.sub(r"(?is)<style.*?</style>", "", h)
    h = re.sub(r"(?is)<iframe.*?</iframe>", "", h)
    h = re.sub(r"(?is)<img[^>]*>", "", h)      # Q2：不显示图片
    h = re.sub(r"(?is)</?a[^>]*>", "", h)      # filepos 链接剥壳留字
    h = re.sub(r"(?is)<link[^>]*>", "", h)
    h = re.sub(r"(?is)<meta[^>]*>", "", h)
    return h.strip()

def _valid(hw, body):
    if not hw or len(hw) > 60:
        return False
    if len(_strip_tags(body)) < MIN_BODY_TEXT:
        return False
    return True

def parse_entries(html):
    """三级自适应切分，返回 [(headword, body_html), ...]；同头词条目全部保留（多音字）。"""
    html = _prep(html)
    # ① 标准 Kindle 词典 idx:entry
    ents = []
    for m in re.finditer(r"(?is)<idx:entry[^>]*>(.*?)</idx:entry>", html):
        blk = m.group(1)
        mo = re.search(r"(?is)<idx:orth[^>]*?value=[\"']([^\"']+)[\"']", blk)
        if mo:
            hw = _strip_tags(mo.group(1))
            body = _clean_body(blk)
            if _valid(hw, body):
                ents.append((hw, body))
    if len(ents) >= MIN_ENTRIES:
        return ents
    # ② 标题分段（牛津高阶 / 古汉语常用字字典均属此类）
    ents = []
    heads = list(re.finditer(r"(?is)<h[1-4][^>]*>(.*?)</h[1-4]>", html))
    if len(heads) >= MIN_ENTRIES:
        for i, m in enumerate(heads):
            start = m.end()
            end = heads[i + 1].start() if i + 1 < len(heads) else len(html)
            hw = _strip_tags(m.group(1))
            body = _clean_body(html[start:end])
            if _valid(hw, body) and len(hw) <= 40:
                ents.append((hw, body))
        if len(ents) >= MIN_ENTRIES:
            return ents
    # ③ hr 分段 + 段首 b 词条头（现代汉英词典式）
    ents = []
    parts = re.split(r"(?i)<hr\s*/?>", html)
    if len(parts) >= MIN_ENTRIES:
        for part in parts:
            bm = re.search(r"(?is)<b[^>]*>(.*?)</b>", part)
            if bm:
                hw = _strip_tags(bm.group(1))
                body = _clean_body(part)
                if _valid(hw, body) and len(hw) <= 40:
                    ents.append((hw, body))
        if len(ents) >= MIN_ENTRIES:
            return ents
    return ents

# ================= 导入 =================
_import_lock = threading.Lock()
_imp = {"state": "idle", "book": "", "count": 0, "err": "", "ts": 0.0}

def _set_imp(**kw):
    with _import_lock:
        _imp.update(kw)
        _imp["ts"] = time.time()

def _imp_snapshot():
    with _import_lock:
        return dict(_imp)

def _norm_hw(hw):
    return re.sub(r"\s+", " ", hw).strip().lower()

def _import_one(path):
    name = os.path.splitext(os.path.basename(path))[0]
    _set_imp(state="running", book=name, count=0, err="")
    try:
        html = extract_text(path)
        entries = parse_entries(html)
        if len(entries) < MIN_ENTRIES:
            dump = os.path.join(os.path.dirname(DICT_DB), "last_extract_%s.html" % name)
            try:
                with open(dump, "w", encoding="utf-8", errors="ignore") as f:
                    f.write(html[:200000])
                dump_name = os.path.basename(dump)
            except OSError:
                dump_name = "（诊断文件写入失败）"
            raise ValueError(
                "仅解析出 %d 个有效词条（低于 %d）。已将解压正文样例保存为 data/dictionary/%s，"
                "请将该文件发给开发者以补充解析策略" % (len(entries), MIN_ENTRIES, dump_name))
        conn = sqlite3.connect(DICT_DB, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            conn.execute("DELETE FROM entries WHERE book_id IN (SELECT book_id FROM books WHERE name=?)",
                         (name,))
            conn.execute("DELETE FROM books WHERE name=?", (name,))
            cur = conn.execute("INSERT INTO books(name, entries, imported_at) VALUES(?,0,?)",
                               (name, datetime.now().strftime("%Y-%m-%d %H:%M")))
            book_id = cur.lastrowid
            batch = []
            for hw, body in entries:
                batch.append((book_id, hw, _norm_hw(hw), body))
                if len(batch) >= IMPORT_COMMIT:
                    conn.executemany("INSERT INTO entries VALUES(?,?,?,?)", batch)
                    batch = []
            if batch:
                conn.executemany("INSERT INTO entries VALUES(?,?,?,?)", batch)
            conn.execute("UPDATE books SET entries=? WHERE book_id=?", (len(entries), book_id))
            conn.commit()
        finally:
            conn.close()
        _set_imp(state="done", book=name, count=len(entries), err="")
        log.info("词典导入完成 %s：%d 词条", name, len(entries))
    except Exception as e:
        log.exception("词典导入失败 %s", path)
        _set_imp(state="error", book=name, err=str(e)[:300])

def _scan_files(force=False):
    existing = set()
    try:
        existing = {r[0] for r in M.dbq(DICT_DB, "SELECT name FROM books")}
    except Exception:
        pass
    todo = []
    try:
        names = sorted(os.listdir(DICT_SRC))
    except OSError:
        return todo
    for fn in names:
        p = os.path.join(DICT_SRC, fn)
        ext = os.path.splitext(fn)[1].lower()
        if ext not in SRC_EXTS or not os.path.isfile(p):
            continue
        if not force and os.path.splitext(fn)[0] in existing:
            continue
        todo.append(p)
    return todo

def _import_worker(paths):
    for p in paths:
        _import_one(p)

# ================= 查询 =================
_BASE = ("SELECT e.headword, e.content_html, b.name FROM entries e "
         "JOIN books b ON b.book_id=e.book_id WHERE ")

def _search(q, cap=20):
    norm = _norm_hw(q)
    if not norm:
        return []
    out = []
    def _q(where, args):
        n = cap - len(out)
        if n <= 0:
            return
        rows = M.dbq(DICT_DB, _BASE + where + (" LIMIT %d" % n), args)
        for hw, body, book in rows:
            out.append((hw, body, book))
    _q("e.headword_norm=?", (norm,))
    if len(out) < cap:
        _q("e.headword_norm LIKE ? AND e.headword_norm<>?", (norm + "%", norm))
    if len(out) < cap:
        _q("e.headword_norm LIKE ? AND e.headword_norm NOT LIKE ? AND e.headword_norm<>?",
           ("%" + norm + "%", norm + "%", norm))
    return out

# ================= 查询历史（Q3） =================
def _hist_add(u, q):
    try:
        M.dbq(DICT_DB, "INSERT INTO history(user,q,ts) VALUES(?,?,?)", (u, q[:60], time.time()))
        M.dbq(DICT_DB, "DELETE FROM history WHERE user=? AND rowid NOT IN "
                       "(SELECT rowid FROM history WHERE user=? ORDER BY ts DESC LIMIT 10)", (u, u))
    except Exception:
        pass

def _hist_list(u):
    try:
        return [r[0] for r in M.dbq(DICT_DB, "SELECT q FROM history WHERE user=? "
                                            "ORDER BY ts DESC LIMIT 10", (u,))]
    except Exception:
        return []

# ================= 页面 =================
DICT_STYLE = ("<style>"
 ".dentry{background:#f4f6f8;border-radius:6px;padding:10px;margin:8px 0}"
 ".dentry .md{margin-top:6px}"
 "details.dentry summary{cursor:pointer;font-size:15px;color:#2b6cb0}"
 ".dentry .md b{color:#1a4c8b}.dentry .md i{color:#4a5568}"
 ".hist a{display:inline-block;background:#e2e8f0;border-radius:6px;"
 "padding:6px 10px;margin:4px 6px 0 0;text-decoration:none;color:#2d3748;font-size:15px}"
 "</style>")

def _shell(title, body, extra_head=""):
    u = M.current_user()
    js = M.hold_loc_js(u) if u else ""
    return (f"<!doctype html><html><head><meta charset='utf-8'>{extra_head}{M.STYLE}{M.ANIM_STYLE}"
            f"{DICT_STYLE}<title>{esc(title)}</title></head><body>{body}" + js + "</body></html>")

@bp.route("/dictionary")
def dict_home():
    u = M.current_user()
    if not u:
        return M.login_page()
    M.hold_page("dictionary")
    try:
        nb = M.dbq(DICT_DB, "SELECT COUNT(*) FROM books")[0][0]
        ne = M.dbq(DICT_DB, "SELECT IFNULL(SUM(entries),0) FROM books")[0][0]
    except Exception:
        nb, ne = 0, 0
    q = request.args.get("q", "").strip()
    body = (M.back_btn("/home") + "<h3>Dictionary · 词典查询</h3>"
            "<form method='get' action='/dictionary' style='display:flex;gap:6px'>"
            f"<input name='q' value='{esc(q)}' placeholder='输入单词、词语或汉字'>"
            "<button type='submit' style='width:auto;padding:10px 16px'>查询</button></form>"
            f"<p class='tip'>已装词典 {nb} 部 · 共 {ne:,} 词条</p>")
    if q:
        _hist_add(u, q)
        results = _search(q)
        if not results:
            body += "<p class='tip'>词典中未找到“%s”（可尝试其他写法或繁简体）</p>" % esc(q)
        else:
            parts = ["<p class='tip'>命中 %d 条（前 3 条已展开，其余轻触展开）</p>" % len(results)]
            for i, (hw, body_html, book) in enumerate(results):
                if i < 3:
                    parts.append("<div class='dentry'><div class='fname'>%s"
                                 "<span class='chip'>%s</span></div><div class='md'>%s</div></div>"
                                 % (esc(hw), esc(book), body_html))
                else:
                    summary = _strip_tags(body_html)[:80]
                    parts.append("<details class='dentry'><summary><b>%s</b> "
                                 "<span class='chip'>%s</span> — %s…</summary>"
                                 "<div class='md'>%s</div></details>"
                                 % (esc(hw), esc(book), esc(summary), body_html))
            body += "".join(parts)
    hist = _hist_list(u)
    if hist:
        links = "".join("<a href='/dictionary?q=%s'>%s</a>" % (quote(h, safe=""), esc(h))
                        for h in hist)
        body += "<h4>最近查询</h4><div class='hist'>" + links + "</div>"
    return _shell("Dictionary", body + M.REOPEN_TIP + M.FOOTER)

@bp.route("/admin/dictionary", methods=["GET", "POST"])
def admin_dict():
    if not M.admin_ok():
        return M.admin_login_page()
    msg = ""
    act = request.form.get("act")
    try:
        if act == "scan":
            todo = _scan_files(force=False)
            if not todo:
                msg = "dictsource 目录中没有待导入的新词典"
            elif _imp_snapshot()["state"] == "running":
                msg = "已有导入在进行中，请稍候"
            else:
                threading.Thread(target=_import_worker, args=(todo,), daemon=True).start()
                msg = "已开始导入 %d 个词典（后台执行，页面数秒后自动刷新进度）" % len(todo)
        elif act == "reimport":
            todo = _scan_files(force=True)
            if not todo:
                msg = "dictsource 目录中没有词典文件"
            elif _imp_snapshot()["state"] == "running":
                msg = "已有导入在进行中，请稍候"
            else:
                threading.Thread(target=_import_worker, args=(todo,), daemon=True).start()
                msg = "已重新导入全部 %d 个词典（后台执行）" % len(todo)
        elif act == "delbook":
            bid = request.form.get("id", "")
            M.dbq(DICT_DB, "DELETE FROM entries WHERE book_id=?", (bid,))
            M.dbq(DICT_DB, "DELETE FROM books WHERE book_id=?", (bid,))
            msg = "词典已删除"
        elif act == "clearhist":
            M.dbq(DICT_DB, "DELETE FROM history")
            msg = "查询历史已清空"
    except Exception as e:
        msg = "操作失败：%s" % e
    st = _imp_snapshot()
    st_line = "导入状态：%s" % st["state"]
    if st["state"] == "running":
        st_line += "（%s）" % st["book"]
    elif st["state"] == "done":
        st_line += "（%s：%d 词条）" % (st["book"], st["count"])
    elif st["state"] == "error":
        st_line += "（%s：%s）" % (st["book"], st["err"])
    books = M.dbq(DICT_DB, "SELECT book_id, name, entries, imported_at FROM books ORDER BY book_id")
    brows = ""
    for bid, nm, cnt, at in books:
        brows += ("<tr><td>%s</td><td style='text-align:right'>%s</td><td>%s</td><td>"
                  "<form method='post' action='/admin/dictionary' style='display:inline'>"
                  "<input type='hidden' name='act' value='delbook'>"
                  "<input type='hidden' name='id' value='%s'>"
                  "<button class='mini'>删除</button></form></td></tr>"
                  % (esc(nm), f"{int(cnt or 0):,}", esc(at or ""), bid))
    try:
        names = sorted(os.listdir(DICT_SRC))
    except OSError:
        names = []
    existing = {b[1] for b in books}
    frows = ""
    for fn in names:
        ext = os.path.splitext(fn)[1].lower()
        if ext not in SRC_EXTS:
            continue
        tag = "已导入" if os.path.splitext(fn)[0] in existing else "待导入"
        frows += "<tr><td>%s</td><td><span class='chip'>%s</span></td></tr>" % (esc(fn), tag)
    extra = "<meta http-equiv='refresh' content='5'>" if st["state"] == "running" else ""
    body = (M.back_btn("/admin") + ("<p class='err'>" + esc(msg) + "</p>" if msg else "")
            + "<p>" + esc(st_line) + "</p>"
            "<h4>操作</h4><p>"
            "<form method='post' action='/admin/dictionary' style='display:inline'>"
            "<input type='hidden' name='act' value='scan'>"
            "<button class='mini'>扫描并导入新词典</button></form> "
            "<form method='post' action='/admin/dictionary' style='display:inline'>"
            "<input type='hidden' name='act' value='reimport'>"
            "<button class='mini'>重新导入全部</button></form> "
            "<form method='post' action='/admin/dictionary' style='display:inline'>"
            "<input type='hidden' name='act' value='clearhist'>"
            "<button class='mini'>清空查询历史</button></form></p>"
            "<p class='tip'>把 .mobi/.prc 词典文件放入 data/dictionary/dictsource/ 后点"
            "“扫描并导入”；导入为后台执行，本页数秒自动刷新。"
            "解析失败时会在 data/dictionary/ 生成 last_extract_*.html 诊断文件。</p>"
            "<h4>已装词典</h4><table border='1'><tr><th>名称</th><th>词条数</th><th>导入时间</th>"
            "<th></th></tr>" + (brows or "<tr><td colspan='4' class='tip'>暂无</td></tr>") + "</table>"
            "<h4>dictsource 目录中的源文件</h4>"
            "<table border='1'>" + (frows or "<tr><td class='tip'>空</td></tr>") + "</table>"
            "<p class='tip'><a href='/admin'>返回管理中心</a> · <a href='/dictionary'>打开 Dictionary</a></p>"
            + M.FOOTER)
    return (f"<!doctype html><html><head><meta charset='utf-8'>{extra}{M.STYLE}"
            f"<title>Dictionary 管理</title></head><body><h3>Dictionary 管理</h3>{body}</body></html>")
