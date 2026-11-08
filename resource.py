# -*- coding: utf-8 -*-
"""
Relay Resource 模块：课件查看 + Resource Librarian（扫描/索引双轨制）
数据：data/resource/{resource.db, resource.log, index/<fid>.json, cache/<fid>/page-NNN.png}
管线：Office(COM)->PDF->逐页PNG(1080px)；文本层提取；扫描件/图片走 Windows OCR；
      AI 总结学科+关键词（独立并发2，AI 空闲/夜间窗口运行，忙碌自动让路）。
Relay Hold：查看进度（hold_get/hold_save，路由 /resource/hold）——页码/缩放/平移闪退恢复。
注意：PPT 动画按 Q2 决策退化为页面翻页；认证轻量浏览器全程零外部依赖。
"""
import hashlib, json, os, queue, re, shutil, threading, time
from datetime import datetime
from html import escape as esc
from urllib.parse import quote
from flask import Blueprint, redirect, request, send_file

M = None
AST = None

def init(m, ast):
    global M, AST, RES_DB, RES_LOG, INDEX_DIR, CACHE_DIR, log
    M, AST = m, ast
    RES_DB = os.path.join(M.DIR_RES, "resource.db")
    RES_LOG = os.path.join(M.DIR_RES, "resource.log")
    INDEX_DIR = os.path.join(M.DIR_RES, "index")
    CACHE_DIR = os.path.join(M.DIR_RES, "cache")
    for d in (INDEX_DIR, CACHE_DIR):
        os.makedirs(d, exist_ok=True)
    log = M._mklog("relay.resource", RES_LOG)
    init_db()
    _sdefault("folders", json.dumps([os.path.join(M.BASE, "courseware")], ensure_ascii=False))
    _sdefault("subjects", json.dumps(["语文", "数学", "英语", "物理", "化学", "生物",
                                      "历史", "地理", "政治"], ensure_ascii=False))
    _sdefault("night_start", "23:30")
    _sdefault("night_end", "06:30")
    _sdefault("idle_index", "0")
    fd = json.loads(sget("folders"))[0] if json.loads(sget("folders")) else None
    if fd and not fd.startswith("\\\\"):
        try:
            os.makedirs(fd, exist_ok=True)
        except OSError:
            pass

bp = Blueprint("resource", __name__)

try:
    import fitz
    HAVE_FITZ = True
except ImportError:
    fitz = None
    HAVE_FITZ = False
try:
    import winrt.windows.media.ocr as _wocr
    import winrt.windows.globalization as _wglob
    import winrt.windows.storage.streams as _wstreams
    import winrt.windows.graphics.imaging as _wgim
    HAVE_OCR = True
except ImportError:
    HAVE_OCR = False

EXTS = {".png", ".jpg", ".jpeg", ".doc", ".docx", ".pdf", ".ppt", ".pptx"}
OFFICE = {".doc", ".docx", ".ppt", ".pptx"}
IMG = {".png", ".jpg", ".jpeg"}
PAGE_W, THUMB_W, TPP = 1080, 160, 24
OCR_PAGE_CAP = 60
CONV_REFRESH = 5

# ---------------- DB ----------------
def init_db():
    M.dbq(RES_DB, """CREATE TABLE IF NOT EXISTS files(
        fid TEXT PRIMARY KEY, name TEXT, ext TEXT, size INTEGER, mtime REAL, path TEXT,
        subject TEXT, pages INTEGER DEFAULT 0, conv TEXT DEFAULT 'none',
        idx TEXT DEFAULT 'pending', err TEXT DEFAULT '')""")
    M.dbq(RES_DB, "CREATE TABLE IF NOT EXISTS kw(fid TEXT, kw TEXT)")
    # progress 表沿用旧名（数据兼容）；代码层统一以 hold_* 访问（Relay Hold）
    M.dbq(RES_DB, """CREATE TABLE IF NOT EXISTS progress(
        user TEXT, fid TEXT, page INTEGER DEFAULT 1, scale REAL DEFAULT 1,
        pan_x REAL DEFAULT 0, pan_y REAL DEFAULT 0, ts REAL,
        PRIMARY KEY(user, fid))""")
    M.dbq(RES_DB, "CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")

def sget(k, d=""):
    r = M.dbq(RES_DB, "SELECT value FROM settings WHERE key=?", (k,))
    return r[0][0] if r else d

def sset(k, v):
    M.dbq(RES_DB, "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, str(v)))

def _sdefault(k, v):
    if not M.dbq(RES_DB, "SELECT value FROM settings WHERE key=?", (k,)):
        sset(k, v)

def get_folders():
    try:
        v = json.loads(sget("folders", "[]"))
        return v if isinstance(v, list) else []
    except Exception:
        return []

def get_subjects():
    try:
        v = json.loads(sget("subjects", "[]"))
        return [str(x) for x in v if str(x).strip()] or ["其他"]
    except Exception:
        return ["其他"]

_FKEYS = "fid name ext size mtime path subject pages conv idx err".split()

def frow(fid):
    r = M.dbq(RES_DB, "SELECT fid,name,ext,size,mtime,path,subject,pages,conv,idx,err "
                      "FROM files WHERE fid=?", (fid,))
    return dict(zip(_FKEYS, r[0])) if r else None

def fid_of(path):
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:16]

def human(n):
    n = int(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%dB" % n if unit == "B" else "%.1f%s" % (n, unit)
        n /= 1024.0
    return "%.1fGB" % n

def fmt_ts(t):
    try:
        return datetime.fromtimestamp(float(t)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "?"

# ---------------- OCR（Windows.Media.Ocr，离线） ----------------
_ocr_engine, _ocr_tried = None, False

def _get_engine():
    global _ocr_engine, _ocr_tried
    if not HAVE_OCR:
        return None
    if _ocr_tried:
        return _ocr_engine
    _ocr_tried = True
    try:
        lang = _wglob.Language("zh-Hans-CN")
        _ocr_engine = (_wocr.OcrEngine.try_create_from_language(lang)
                       or _wocr.OcrEngine.try_create_from_user_profile_language())
        if _ocr_engine is None:
            log.warning("Windows OCR 引擎不可用")
    except Exception:
        _ocr_engine = None
    return _ocr_engine

def ocr_bytes(data):
    eng = _get_engine()
    if not eng or not data:
        return ""
    try:
        stream = _wstreams.InMemoryRandomAccessStream()
        w = _wstreams.DataWriter(stream)
        w.write_bytes(bytes(data))
        w.store_async().get()
        w.flush_async().get()
        w.detach_stream()
        stream.seek(0)
        dec = _wgim.BitmapDecoder.create_async(stream).get()
        bmp = dec.get_software_bitmap_async().get()
        res = eng.recognize_async(bmp).get()
        return (res.text or "").strip()
    except Exception as e:
        log.warning("OCR 失败：%s", e)
        return ""

# ---------------- 转换 / 渲染 ----------------
def office_to_pdf(src, dst):
    try:
        import pythoncom
        import win32com.client
    except Exception as e:
        raise RuntimeError("缺少 pywin32/Office，无法转换文档：" + str(e))
    pythoncom.CoInitialize()
    app = None
    try:
        if src.lower().endswith((".ppt", ".pptx")):
            app = win32com.client.DispatchEx("PowerPoint.Application")
            pres = app.Presentations.Open(src, True, False, False)
            try:
                pres.SaveAs(dst, 32)
            except Exception:
                pres.SaveCopyAs(dst, 32)
            pres.Close()
        else:
            app = win32com.client.DispatchEx("Word.Application")
            try:
                app.DisplayAlerts = 0
            except Exception:
                pass
            doc = app.Documents.Open(src)
            try:
                doc.SaveAs2(dst, 17)
            except Exception:
                doc.SaveAs(dst, 17)
            doc.Close(False)
        if not os.path.exists(dst) or os.path.getsize(dst) == 0:
            raise RuntimeError("无转换产物（文件可能损坏或受密码保护）")
    finally:
        try:
            if app is not None:
                app.Quit()
        except Exception:
            pass

def render_pdf(fid, pdfsrc):
    if not HAVE_FITZ:
        raise RuntimeError("缺少 pymupdf，无法渲染页面")
    doc = fitz.open(pdfsrc)
    cdir = os.path.join(CACHE_DIR, fid)
    os.makedirs(cdir, exist_ok=True)
    n = 0
    try:
        for i, page in enumerate(doc):
            try:
                w = float(page.rect.width) or 792.0
            except Exception:
                w = 792.0
            z = max(0.3, min(4.0, PAGE_W / w))
            page.get_pixmap(matrix=fitz.Matrix(z, z)).save(
                os.path.join(cdir, "page-%03d.png" % (i + 1)))
            zt = max(0.15, min(2.0, THUMB_W / w))
            page.get_pixmap(matrix=fitz.Matrix(zt, zt)).save(
                os.path.join(cdir, "thumb-%03d.png" % (i + 1)))
            n += 1
    finally:
        doc.close()
    return n

# ---------------- 准备队列（转换+渲染，串行） ----------------
_q = queue.Queue()
_qset, _qset_lock = set(), threading.Lock()
_rl_lock = threading.Lock()
_render_locks = {}

def _rlock(fid):
    with _rl_lock:
        return _render_locks.setdefault(fid, threading.Lock())

def _ready(row):
    if row["ext"] in IMG:
        return True
    return row["conv"] == "ready" and int(row["pages"] or 0) > 0

def enqueue(row):
    fid = row["fid"]
    if row["conv"] == "none":
        M.dbq(RES_DB, "UPDATE files SET conv='queued', err='' WHERE fid=?", (fid,))
    with _qset_lock:
        if fid not in _qset:
            _qset.add(fid)
            _q.put(fid)

def _conv_one(fid):
    row = frow(fid)
    if not row:
        return
    try:
        if row["ext"] in OFFICE:
            M.dbq(RES_DB, "UPDATE files SET conv='converting' WHERE fid=?", (fid,))
            cdir = os.path.join(CACHE_DIR, fid)
            os.makedirs(cdir, exist_ok=True)
            pdf = os.path.join(cdir, "src.pdf")
            office_to_pdf(row["path"], pdf)
            n = render_pdf(fid, pdf)
            M.dbq(RES_DB, "UPDATE files SET conv='ready', pages=?, err='' WHERE fid=?", (n, fid))
            log.info("转换完成 %s（%d 页）", row["name"], n)
        elif row["ext"] == ".pdf":
            n = render_pdf(fid, row["path"])
            M.dbq(RES_DB, "UPDATE files SET conv='ready', pages=?, err='' WHERE fid=?", (n, fid))
            log.info("渲染完成 %s（%d 页）", row["name"], n)
    except Exception as e:
        log.exception("prepare 失败 fid=%s", fid)
        M.dbq(RES_DB, "UPDATE files SET conv='failed', err=? WHERE fid=?", (str(e)[:300], fid))

def _conv_loop():
    try:
        import pythoncom
        pythoncom.CoInitialize()
    except Exception:
        pass
    while True:
        fid = _q.get()
        try:
            _conv_one(fid)
        except Exception:
            log.exception("conv loop fid=%s", fid)
        finally:
            with _qset_lock:
                _qset.discard(fid)

def _wait_ready(fid, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        row = frow(fid)
        if not row:
            return False
        if row["conv"] == "failed":
            return False
        if _ready(row):
            return True
        time.sleep(2)
    return False

# ---------------- 扫描（轻量、无 AI、常开） ----------------
_scan_lock = threading.Lock()
_last_scan = [0.0]

def _purge(fid):
    shutil.rmtree(os.path.join(CACHE_DIR, fid), ignore_errors=True)
    try:
        os.remove(os.path.join(INDEX_DIR, fid + ".json"))
    except OSError:
        pass
    M.dbq(RES_DB, "DELETE FROM kw WHERE fid=?", (fid,))

def _do_scan():
    seen = {}
    for root in get_folders():
        if not os.path.isdir(root):
            continue
        for dp, dn, fn in os.walk(root):
            for f in fn:
                ext = os.path.splitext(f)[1].lower()
                if ext not in EXTS:
                    continue
                p = os.path.join(dp, f)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                seen[p] = (st.st_mtime, st.st_size)
    old = {r[1]: r for r in M.dbq(RES_DB, "SELECT fid,path,mtime,size FROM files")}
    for p, (mt, sz) in seen.items():
        fid = fid_of(p)
        ext = os.path.splitext(p)[1].lower()
        if p not in old:
            conv = "none" if ext in OFFICE else "ready"
            M.dbq(RES_DB, "INSERT OR REPLACE INTO files(fid,name,ext,size,mtime,path,subject,"
                          "pages,conv,idx,err) VALUES(?,?,?,?,?,?,NULL,0,?,'pending','')",
                  (fid, os.path.splitext(os.path.basename(p))[0], ext, sz, mt, p, conv))
            log.info("新增文件 %s", p)
        else:
            o = old[p]
            if abs(o[2] - mt) > 1 or o[3] != sz:
                _purge(fid)
                conv = "none" if ext in OFFICE else "ready"
                M.dbq(RES_DB, "UPDATE files SET size=?,mtime=?,subject=NULL,pages=0,conv=?,"
                              "idx='pending',err='' WHERE fid=?", (sz, mt, conv, fid))
                log.info("文件变更 %s", p)
    for p, o in old.items():
        if p not in seen:
            _purge(o[0])
            M.dbq(RES_DB, "DELETE FROM files WHERE fid=?", (o[0],))
            log.info("移除文件 %s", p)

def scan_now():
    with _scan_lock:
        try:
            _do_scan()
        except Exception:
            log.exception("scan")
        finally:
            _last_scan[0] = time.time()

def maybe_scan():
    if time.time() - _last_scan[0] > 60:
        threading.Thread(target=scan_now, daemon=True).start()

def _scan_loop():
    while True:
        scan_now()
        time.sleep(300)

# ---------------- 文本提取 ----------------
def _pdf_text(src):
    if not HAVE_FITZ:
        return ""
    try:
        doc = fitz.open(src)
        try:
            return "\n".join(page.get_text() for page in doc)
        finally:
            doc.close()
    except Exception as e:
        log.warning("PDF 文本提取失败 %s：%s", src, e)
        return ""

def _ocr_pages(fid, pages):
    out = []
    cdir = os.path.join(CACHE_DIR, fid)
    for n in range(1, min(int(pages or 0), OCR_PAGE_CAP) + 1):
        pf = os.path.join(cdir, "page-%03d.png" % n)
        if os.path.exists(pf):
            try:
                with open(pf, "rb") as f:
                    out.append(ocr_bytes(f.read()))
            except OSError:
                pass
    return "\n".join(out)

# ---------------- AI 总结（共用 key；独立小并发；让路） ----------------
_ai = None
_ai_sem = threading.BoundedSemaphore(2)
_ai_lock = threading.Lock()

def _ai_session():
    global _ai
    with _ai_lock:
        if _ai is None:
            import requests as http
            _ai = http.Session()
        return _ai

def _ai_idle():
    if not AST:
        return True
    try:
        st = AST.ai_busy_state()
    except Exception:
        return True
    return st["waiting"] < 8 and st["active"] < max(1, st["limit"] // 2)

def _ai_summary(name, text):
    subjects = get_subjects()
    prompt = ("以下是课件文件信息，请判断学科并提取关键词/主题。\n"
              "文件名：" + name + "\n可选学科：" + "、".join(subjects) + "\n"
              "内容摘录：\n" + text[:4000] +
              '\n只输出 JSON：{"subject":"学科名或 其他","keywords":["关键词",…]}，'
              "keywords 最多 8 个，每个不超过 10 字。")
    payload = {"model": M.AI_MODEL,
               "messages": [{"role": "system", "content": "你是文档分析器。只输出 JSON，不要输出其他文字。"},
                            {"role": "user", "content": prompt}],
               "max_tokens": 200, "temperature": 0.2}
    for _ in range(2):
        try:
            with _ai_sem:
                r = M.ai_post(_ai_session(), payload, 60)
            r.raise_for_status()
            content = (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
            mobj = re.search(r"\{.*\}", content, re.S)
            obj = json.loads(mobj.group(0))
            sub = str(obj.get("subject") or "其他").strip()
            if sub not in subjects and sub != "其他":
                sub = "其他"
            kws = [str(k).strip()[:20] for k in (obj.get("keywords") or []) if str(k).strip()][:8]
            return sub, kws
        except Exception as e:
            log.warning("AI 总结失败 %s：%s", name, e)
            time.sleep(2)
    return None

# ---------------- 索引器（仅夜间窗口 / AI 空闲；始终让路 Assistant） ----------------
_idx_state = {"running": False, "yielding": False, "current": ""}

def _hhmm(v, dflt):
    try:
        h, m = str(v).split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return dflt

def _in_night():
    a = _hhmm(sget("night_start", "23:30"), 1410)
    b = _hhmm(sget("night_end", "06:30"), 390)
    now = datetime.now()
    cur = now.hour * 60 + now.minute
    return (a <= cur < b) if a <= b else (cur >= a or cur < b)

def _should_index():
    if sget("idle_index", "0") == "1" and _ai_idle():
        return True
    return _in_night()

def _mark_idx(fid, st, err):
    M.dbq(RES_DB, "UPDATE files SET idx=?, err=? WHERE fid=?", (st, err, fid))

def _write_index(fid, row, sub, kws, text):
    obj = {"name": row["name"], "path": row["path"], "ext": row["ext"],
           "size": row["size"], "mtime": row["mtime"], "subject": sub,
           "keywords": kws, "text": text[:20000], "ts": time.time()}
    with open(os.path.join(INDEX_DIR, fid + ".json"), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    M.dbq(RES_DB, "DELETE FROM kw WHERE fid=?", (fid,))
    for k in kws:
        M.dbq(RES_DB, "INSERT INTO kw(fid,kw) VALUES(?,?)", (fid, k))
    M.dbq(RES_DB, "UPDATE files SET idx='done', subject=?, err='' WHERE fid=?", (sub, fid))

def _index_one(fid):
    row = frow(fid)
    if not row:
        return
    text = ""
    try:
        if row["ext"] in IMG:
            try:
                with open(row["path"], "rb") as f:
                    text = ocr_bytes(f.read())
            except OSError as e:
                raise RuntimeError("读取文件失败 " + str(e))
        else:
            if not _ready(row):
                enqueue(row)
                if not _wait_ready(fid):
                    _mark_idx(fid, "failed", "转换/渲染未就绪或失败，请在管理页重试")
                    return
                row = frow(fid)
            src = row["path"] if row["ext"] == ".pdf" else os.path.join(CACHE_DIR, fid, "src.pdf")
            text = _pdf_text(src)
            if len(text.strip()) < 100:
                text += "\n" + _ocr_pages(fid, row["pages"])
    except Exception as e:
        log.exception("文本提取失败 fid=%s", fid)
        _mark_idx(fid, "failed", "提取失败：" + str(e)[:120])
        return
    while AST and not _ai_idle():
        _idx_state["yielding"] = True
        time.sleep(30)
    _idx_state["yielding"] = False
    sub, kws = _ai_summary(row["name"], text)
    if sub is None:
        _mark_idx(fid, "text_only", "AI 总结失败（全文仍可检索）")
        log.warning("索引完成(无AI) %s", row["name"])
    else:
        _write_index(fid, row, sub, kws, text)
        log.info("索引完成 %s · %s · %s", row["name"], sub, ",".join(kws))

def _indexer_loop():
    while True:
        try:
            if not _should_index():
                _idx_state["running"] = False
                _idx_state["yielding"] = False
                time.sleep(30)
                continue
            r = M.dbq(RES_DB, "SELECT fid FROM files WHERE idx='pending' ORDER BY rowid LIMIT 1")
            if not r:
                _idx_state["running"] = False
                time.sleep(20)
                continue
            if AST and not _ai_idle():
                _idx_state["running"] = False
                _idx_state["yielding"] = True
                time.sleep(30)
                continue
            _idx_state["yielding"] = False
            _idx_state["running"] = True
            fid = r[0][0]
            row = frow(fid)
            _idx_state["current"] = row["name"] if row else fid
            _index_one(fid)
            _idx_state["running"] = False
            time.sleep(2)
        except Exception:
            log.exception("indexer loop")
            _idx_state["running"] = False
            time.sleep(15)

def _indicator():
    try:
        pend = M.dbq(RES_DB, "SELECT COUNT(*) FROM files WHERE idx='pending'")[0][0]
    except Exception:
        pend = 0
    if _idx_state["running"] and pend > 0:
        return "run", "索引正在进行"
    if pend > 0:
        return "pend", "索引已挂起"
    return "done", "索引已完成"

def start_background():
    threading.Thread(target=_scan_loop, daemon=True).start()
    threading.Thread(target=_indexer_loop, daemon=True).start()
    threading.Thread(target=_conv_loop, daemon=True).start()
    threading.Thread(target=lambda: (time.sleep(1), scan_now()), daemon=True).start()
    log.info("Resource 后台线程已启动（扫描/索引/转换队列）")

# ---------------- Relay Hold：查看进度（闪退恢复） ----------------
def hold_get(u, fid):
    r = M.dbq(RES_DB, "SELECT page,scale,pan_x,pan_y FROM progress WHERE user=? AND fid=?", (u, fid))
    if r:
        return {"page": max(1, int(r[0][0] or 1)), "scale": float(r[0][1] or 1),
                "pan_x": float(r[0][2] or 0), "pan_y": float(r[0][3] or 0)}
    return {"page": 1, "scale": 1.0, "pan_x": 0.0, "pan_y": 0.0}

def hold_save(u, fid, page=None, scale=None, pan_x=None, pan_y=None):
    cur = hold_get(u, fid)
    if page is not None:
        cur["page"] = page
    if scale is not None:
        cur["scale"] = min(5.0, max(1.0, scale))
    if pan_x is not None:
        cur["pan_x"] = max(-20000.0, min(20000.0, pan_x))
    if pan_y is not None:
        cur["pan_y"] = max(-20000.0, min(20000.0, pan_y))
    M.dbq(RES_DB, "INSERT OR REPLACE INTO progress VALUES(?,?,?,?,?,?,?)",
          (u, fid, cur["page"], cur["scale"], cur["pan_x"], cur["pan_y"], time.time()))

# ---------------- 页面片段 ----------------
VSTYLE = ("<style>.pv{width:100%;border:1px solid #e2e8f0;border-radius:6px;margin:6px 0;"
 "display:block}.thumbs{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}"
 ".tcell{width:calc(25% - 6px);text-align:center;text-decoration:none;color:#4a5568;font-size:12px}"
 ".tcell img{width:100%;border:1px solid #cbd5e0;border-radius:4px;display:block}"
 ".navbtns{display:flex;gap:8px}.navbtns a{flex:1}</style>")

_IMG_JS = ("<script>(function(){"
 "var s=__S__,px=__PX__,py=__PY__,dr=false,lx=0,ly=0,t=null;"
 "var im=document.getElementById('im'),w=document.getElementById('w');"
 "function ap(){im.style.transform='translate('+px+'px,'+py+'px) scale('+s+')';}"
 "function cl(){var W=im.clientWidth,H=im.clientHeight;"
 "px=Math.min(0,Math.max(W*(1-s),px));py=Math.min(0,Math.max(H*(1-s),py));}"
 "function sv(){if(t)clearTimeout(t);t=setTimeout(function(){var x=new XMLHttpRequest();"
 "x.open('POST','/resource/hold',true);"
 "x.setRequestHeader('Content-Type','application/x-www-form-urlencoded');"
 "x.send('fid=__FID__&page=1&scale='+s+'&pan_x='+px+'&pan_y='+py);},600);}"
 "function z(f){s=Math.min(5,Math.max(1,s*f));cl();ap();sv();}"
 "function rs(){s=1;px=0;py=0;ap();sv();}"
 "function ds(x,y){dr=true;lx=x;ly=y;}"
 "function dm(x,y){if(!dr)return;px+=x-lx;py+=y-ly;lx=x;ly=y;cl();ap();}"
 "function de(){if(dr){dr=false;sv();}}"
 "w.addEventListener('touchstart',function(e){ds(e.touches[0].clientX,e.touches[0].clientY);},false);"
 "w.addEventListener('touchmove',function(e){e.preventDefault();dm(e.touches[0].clientX,e.touches[0].clientY);},false);"
 "w.addEventListener('touchend',de,false);"
 "w.addEventListener('mousedown',function(e){ds(e.clientX,e.clientY);},false);"
 "w.addEventListener('mousemove',function(e){if(dr)dm(e.clientX,e.clientY);},false);"
 "w.addEventListener('mouseup',de,false);w.addEventListener('mouseleave',de,false);"
 "window.addEventListener('pagehide',sv);"
 "var a=document.getElementById('zi');if(a)a.onclick=function(){z(1.3);return false;};"
 "var b=document.getElementById('zo');if(b)b.onclick=function(){z(1/1.3);return false;};"
 "var c=document.getElementById('zr');if(c)c.onclick=function(){rs();return false;};"
 "cl();ap();})();</script>")

_PPT_JS = ("<script>(function(){var x=0,y=0;"
 "document.addEventListener('touchstart',function(e){x=e.touches[0].clientX;y=e.touches[0].clientY;},false);"
 "document.addEventListener('touchend',function(e){"
 "var dx=e.changedTouches[0].clientX-x,dy=e.changedTouches[0].clientY-y;"
 "if((dx<-60||dy<-60)&&__NH__)location.href=__NU__;else if((dx>60||dy>60)&&__PH__)location.href=__PU__;},false);})();</script>")

def _page_shell(title, body, extra_head=""):
    return (f"<!doctype html><html><head><meta charset='utf-8'>{extra_head}{M.STYLE}{M.ANIM_STYLE}"
            f"{VSTYLE}<title>{esc(title)}</title></head><body>{body}</body></html>")

def _slink(q, sf, sd, cur_f, cur_d, label):
    on = " on" if (sf == cur_f and sd == cur_d) else ""
    return ("<a class='slink%s' href='/resource?q=%s&amp;sf=%s&amp;sd=%s'>%s</a>"
            % (on, quote(q), sf, sd, label))

# ---------------- 路由：列表 ----------------
@bp.route("/resource")
def res_list():
    u = M.current_user()
    if not u:
        return M.login_page()
    maybe_scan()
    M.hold_page("resource")
    q = request.args.get("q", "").strip()
    sf = request.args.get("sf", "mtime")
    sd = request.args.get("sd", "desc")
    if sf not in ("name", "size", "mtime"):
        sf = "mtime"
    if sd not in ("asc", "desc"):
        sd = "desc"
    col = {"name": "name COLLATE NOCASE", "size": "size", "mtime": "mtime"}[sf]
    sql = "SELECT fid,name,ext,size,mtime,subject,conv,idx FROM files"
    args = []
    if q:
        like = "%" + q + "%"
        sql += (" WHERE name LIKE ? OR subject LIKE ? OR fid IN "
                "(SELECT DISTINCT fid FROM kw WHERE kw LIKE ?)")
        args = [like, like, like]
    sql += " ORDER BY " + col + " " + sd.upper()
    rows = M.dbq(RES_DB, sql, args)
    st, sttxt = _indicator()
    items = ""
    for r in rows:
        chip = ""
        if r[6] == "failed":
            chip = "<span class='chip' style='background:#fed7d7;color:#c53030'>转换失败</span>"
        if r[5]:
            chip += "<span class='chip'>" + esc(r[5]) + "</span>"
        items += (f"<a class='litem' href='/resource/view/{r[0]}'>"
                  f"<div class='fname'>{esc(r[1])}<span class='chip'>{esc((r[2] or '').lstrip('.').upper())}</span>{chip}</div>"
                  f"<div class='fmeta'>{fmt_ts(r[4])} · {human(r[3])}</div></a>")
    if not rows:
        items = "<p class='tip'>没有匹配的文件。教师可将课件放入管理页指定的文件夹。</p>"
    srch = ("<form method='get' action='/resource' style='display:flex;gap:6px'>"
            f"<input name='q' value='{esc(q)}' placeholder='搜索：文件名 / 学科 / 关键词'>"
            "<button type='submit' style='width:auto;padding:10px 16px'>搜索</button></form>"
            + (f"<p class='tip'>正在筛选“{esc(q)}” · <a href='/resource'>清除筛选</a></p>" if q else ""))
    sorts = "".join([_slink(q, "name", "asc", sf, sd, "名称↑"), _slink(q, "name", "desc", sf, sd, "名称↓"),
                     _slink(q, "size", "asc", sf, sd, "大小↑"), _slink(q, "size", "desc", sf, sd, "大小↓"),
                     _slink(q, "mtime", "asc", sf, sd, "时间↑"), _slink(q, "mtime", "desc", sf, sd, "时间↓")])
    body = (M.back_btn("/home") + "<h3>Resource · 课件资源</h3>" + srch +
            f"<div class='rowline'><span><span class='stchip {st}'>{sttxt}</span>"
            f"<a class='slink' href='/resource'>刷新</a></span><span>{sorts}</span></div>"
            f"<p class='tip'>共 {len(rows)} 个文件（全部文件夹合并列出）</p>" + items
            + M.REOPEN_TIP + M.FOOTER)
    return _page_shell("Resource", body)

# ---------------- 路由：查看器 ----------------
@bp.route("/resource/view/<fid>")
def res_view(fid):
    u = M.current_user()
    if not u:
        return M.login_page()
    row = frow(fid)
    if not row:
        return M.msg_page("文件不存在或已被移除。", "/resource")
    ext = row["ext"]

    if ext in IMG:
        prog = hold_get(u, fid)
        js = (_IMG_JS.replace("__S__", repr(prog["scale"]))
              .replace("__PX__", repr(prog["pan_x"])).replace("__PY__", repr(prog["pan_y"]))
              .replace("__FID__", fid))
        body = (M.back_btn("/resource") + "<h3>" + esc(row["name"]) + "</h3>"
                "<p><button class='mini' id='zi'>放大</button> "
                "<button class='mini' id='zo'>缩小</button> "
                "<button class='mini' id='zr'>还原</button> "
                "<span class='tip'>拖动以移动</span></p>"
                f"<div id='w' style='height:75vh;min-height:360px;overflow:hidden;position:relative;"
                f"background:#f4f6f8;border-radius:6px'>"
                f"<img id='im' src='/resource/raw/{fid}' draggable='false' "
                f"style='width:100%;display:block;transform-origin:0 0'></div>"
                + M.REOPEN_TIP + M.FOOTER + js)
        return _page_shell(row["name"], body)

    if row["conv"] == "failed" and request.args.get("retry") != "1":
        body = (M.back_btn("/resource") + "<h3>" + esc(row["name"]) + "</h3>"
                "<p class='err'>该文件转换失败：</p><pre>" + esc(row["err"] or "未知原因") + "</pre>"
                "<p class='tip'>可轻触下方按钮尝试重新转换，或联系管理员重试。</p>"
                f"<a class='btn2' href='/resource/view/{fid}?retry=1'>重试</a>" + M.FOOTER)
        return _page_shell(row["name"], body)
    if not _ready(row):
        if request.args.get("retry") == "1":
            if ext in OFFICE:
                M.dbq(RES_DB, "UPDATE files SET conv='none', err='' WHERE fid=?", (fid,))
            else:
                M.dbq(RES_DB, "UPDATE files SET pages=0, err='' WHERE fid=?", (fid,))
        enqueue(row)
        vurl = "/resource/view/" + fid
        body = (M.back_btn("/resource") + "<h3>" + esc(row["name"]) + "</h3>"
                "<div class='fadein'><p class='queue'><span class='spinner'></span>正在准备文件"
                "<span class='dots'><i></i><i></i><i></i></span></p>"
                "<p class='tip'>首次访问需要转换/渲染</p></div>" + M.FOOTER)
        return _page_shell(row["name"], body,
                           extra_head=f"<meta http-equiv='refresh' content='{CONV_REFRESH};url={vurl}'>")

    pages = int(row["pages"] or 0)
    prog = hold_get(u, fid)
    try:
        p = int(request.args.get("page", "") or prog["page"])
    except ValueError:
        p = prog["page"]
    p = max(1, min(pages, p))
    hold_save(u, fid, page=p)   # Relay Hold：页码记忆 -> 闪退恢复

    vurl = "/resource/view/" + fid
    if request.args.get("thumbs"):
        tpages = (pages + TPP - 1) // TPP
        try:
            tp = int(request.args.get("tp", "") or ((p - 1) // TPP + 1))
        except ValueError:
            tp = 1
        tp = max(1, min(tpages, tp))
        cells = ""
        for n in range((tp - 1) * TPP + 1, min(pages, tp * TPP) + 1):
            cells += (f"<a class='tcell' href='{vurl}?page={n}'>"
                      f"<img src='/resource/file/{fid}/thumb-{n:03d}.png' loading='lazy'>第 {n} 页</a>")
        nav = ""
        if tp > 1:
            nav += f"<a class='slink' href='{vurl}?thumbs=1&amp;tp={tp-1}'>‹ 上一屏</a>"
        if tp < tpages:
            nav += f"<a class='slink' href='{vurl}?thumbs=1&amp;tp={tp+1}'>下一屏 ›</a>"
        body = (M.back_btn("/resource") + "<h3>" + esc(row["name"]) + "</h3>"
                f"<p class='tip'>页面缩略图 · 第 {tp}/{tpages} 屏 {nav} · "
                f"<a class='slink' href='{vurl}?page={p}'>‹ 返回第 {p} 页阅读</a></p>"
                f"<div class='thumbs'>{cells}</div>" + M.FOOTER)
        return _page_shell(row["name"], body)

    prev = vurl + "?page=%d" % max(1, p - 1)
    nxt = vurl + "?page=%d" % min(pages, p + 1)
    jump = (f"<form method='get' action='{vurl}' style='display:inline'>"
            f"<input name='page' type='number' min='1' max='{pages}' value='{p}' "
            f"style='width:76px;display:inline-block;padding:4px'>"
            f"<button class='mini'>跳转</button></form>")
    body = (M.back_btn("/resource") + "<h3>" + esc(row["name"]) + "</h3>"
            "<div class='rowline'><span>"
            f"<a class='slink' href='{prev}'>‹ 上一页</a> · 第 {p}/{pages} 页 · "
            f"<a class='slink' href='{nxt}'>下一页 ›</a></span>"
            f"<span>{jump}<a class='slink' href='{vurl}?thumbs=1&amp;tp={(p-1)//TPP+1}'>缩略图</a></span></div>"
            f"<img class='pv' src='/resource/file/{fid}/page-{p:03d}.png'>"
            "<div class='navbtns'>"
            f"<a class='btn2' href='{prev}'>‹ 上一页</a>"
            f"<a class='btn2' href='{nxt}'>下一页 ›</a></div>"
            + ("<p class='tip'>左右或上下滑动屏幕以在页间切换</p>" if ext in (".ppt", ".pptx") else "")
            + M.REOPEN_TIP + M.FOOTER)
    if ext in (".ppt", ".pptx"):
        js = (_PPT_JS.replace("__NH__", "1" if p < pages else "0")
              .replace("__NU__", '"' + nxt + '"')
              .replace("__PH__", "1" if p > 1 else "0")
              .replace("__PU__", '"' + prev + '"'))
        body += js
    return _page_shell(row["name"], body)

# ---------------- 路由：文件服务 ----------------
def _send(path, mt, back):
    try:
        return send_file(path, mimetype=mt, conditional=True)
    except Exception:
        return M.msg_page("文件不存在或正在准备中。", back)

@bp.route("/resource/raw/<fid>")
def res_raw(fid):
    if not M.current_user():
        return M.login_page()
    row = frow(fid)
    if not row or row["ext"] not in IMG:
        return M.msg_page("文件不存在。", "/resource")
    mt = "image/png" if row["ext"] == ".png" else "image/jpeg"
    return _send(row["path"], mt, "/resource")

@bp.route("/resource/file/<fid>/page-<int:n>.png")
def res_page(fid, n):
    if not M.current_user():
        return M.login_page()
    row = frow(fid)
    if not row or row["ext"] in IMG:
        return M.msg_page("文件不存在。", "/resource")
    back = "/resource/view/" + fid
    if not (1 <= n <= int(row["pages"] or 0)):
        return M.msg_page("页码超出范围。", back)
    pf = os.path.join(CACHE_DIR, fid, "page-%03d.png" % n)
    if not os.path.exists(pf):
        with _rlock(fid):
            if not os.path.exists(pf):
                try:
                    src = row["path"] if row["ext"] == ".pdf" else os.path.join(CACHE_DIR, fid, "src.pdf")
                    m = render_pdf(fid, src)
                    M.dbq(RES_DB, "UPDATE files SET pages=? WHERE fid=?", (m, fid))
                except Exception as e:
                    log.exception("按需渲染失败 fid=%s", fid)
                    return M.msg_page("页面渲染失败，请稍后重试。", back)
    return _send(pf, "image/png", back)

@bp.route("/resource/file/<fid>/thumb-<int:n>.png")
def res_thumb(fid, n):
    if not M.current_user():
        return M.login_page()
    tf = os.path.join(CACHE_DIR, fid, "thumb-%03d.png" % n)
    if not os.path.exists(tf):
        return M.msg_page("缩略图未生成。", "/resource/view/" + fid + "?page=%d" % n)
    return _send(tf, "image/png", "/resource/view/" + fid + "?page=%d" % n)

@bp.route("/resource/hold", methods=["POST"])
def res_hold():
    u = M.current_user()
    if not u:
        return "", 204
    fid = request.form.get("fid", "")
    if not frow(fid):
        return "", 204
    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    def _i(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    hold_save(u, fid, page=_i(request.form.get("page")),
              scale=_f(request.form.get("scale")),
              pan_x=_f(request.form.get("pan_x")),
              pan_y=_f(request.form.get("pan_y")))
    return "", 204

# ---------------- Resource 管理子页 ----------------
@bp.route("/admin/resource", methods=["GET", "POST"])
def admin_resource():
    if not M.admin_ok():
        return M.admin_login_page()
    msg = ""
    act = request.form.get("act")
    try:
        if act == "save":
            folders = []
            for ln in request.form.get("folders", "").splitlines():
                ln = ln.strip()
                if ln and ln not in folders:
                    folders.append(ln)
            subjects = []
            for ln in request.form.get("subjects", "").replace("，", ",").replace("、", ",").split(","):
                ln = ln.strip()
                if ln and ln not in subjects:
                    subjects.append(ln)
            if not subjects:
                subjects = ["其他"]
            sset("folders", json.dumps(folders, ensure_ascii=False))
            sset("subjects", json.dumps(subjects, ensure_ascii=False))
            sset("night_start", request.form.get("ns", "23:30").strip() or "23:30")
            sset("night_end", request.form.get("ne", "06:30").strip() or "06:30")
            sset("idle_index", "1" if request.form.get("idle") == "1" else "0")
            for fd in folders:
                if not fd.startswith("\\\\"):
                    try:
                        os.makedirs(fd, exist_ok=True)
                    except OSError:
                        pass
            threading.Thread(target=scan_now, daemon=True).start()
            msg = "设置已保存，并已触发一次扫描"
        elif act == "scan":
            threading.Thread(target=scan_now, daemon=True).start()
            msg = "已触发扫描（片刻后刷新查看）"
        elif act == "warm":
            n = 0
            for r in M.dbq(RES_DB, "SELECT fid,ext,pages,conv FROM files"):
                if r[1] in OFFICE and r[3] in ("none", "failed"):
                    M.dbq(RES_DB, "UPDATE files SET conv='queued' WHERE fid=?", (r[0],))
                    enqueue(frow(r[0])); n += 1
                elif r[1] == ".pdf" and int(r[2] or 0) <= 0:
                    enqueue(frow(r[0])); n += 1
            msg = "已加入转换/渲染队列 %d 个文件（串行执行，可在文件列表观察进度）" % n
        elif act == "rebuild":
            for (fid,) in M.dbq(RES_DB, "SELECT fid FROM files"):
                try:
                    os.remove(os.path.join(INDEX_DIR, fid + ".json"))
                except OSError:
                    pass
            M.dbq(RES_DB, "UPDATE files SET idx='pending'")
            msg = "已请求重建全部索引（将在夜间窗口或 AI 空闲时执行）"
        elif act == "clearcache":
            for d in os.listdir(CACHE_DIR):
                shutil.rmtree(os.path.join(CACHE_DIR, d), ignore_errors=True)
            for r in M.dbq(RES_DB, "SELECT fid,ext FROM files"):
                conv = "none" if r[1] in OFFICE else "ready"
                M.dbq(RES_DB, "UPDATE files SET pages=0, conv=? WHERE fid=?", (conv, r[0]))
            msg = "渲染缓存已清空（首次访问将重新渲染）"
        elif act == "retry":
            fid = request.form.get("fid", "")
            row = frow(fid)
            if row:
                if row["ext"] in OFFICE:
                    M.dbq(RES_DB, "UPDATE files SET conv='none', err='' WHERE fid=?", (fid,))
                else:
                    M.dbq(RES_DB, "UPDATE files SET pages=0, err='' WHERE fid=?", (fid,))
                enqueue(row)
                msg = "已重新入队：" + row["name"]
    except Exception as e:
        msg = "操作失败：%s" % e

    folders = "\n".join(get_folders())
    subjects = "，".join(get_subjects())
    st, sttxt = _indicator()
    pend = M.dbq(RES_DB, "SELECT COUNT(*) FROM files WHERE idx='pending'")[0][0]
    cur = _idx_state.get("current") or "—"
    failed = M.dbq(RES_DB, "SELECT fid,name,err FROM files WHERE conv='failed' ORDER BY name")
    frows = "".join(
        f"<tr><td>{esc(r[1])}</td><td class='tip'>{esc(r[2] or '')[:120]}</td>"
        f"<td><form method='post' action='/admin/resource' style='display:inline'>"
        f"<input type='hidden' name='act' value='retry'><input type='hidden' name='fid' value='{r[0]}'>"
        f"<button class='mini'>重试</button></form></td></tr>" for r in failed)
    body = (M.back_btn("/admin") + ("<p class='err'>" + esc(msg) + "</p>" if msg else "")
            + f"<p>索引状态：<span class='stchip {st}'>{sttxt}</span> · 待索引 {pend} · "
            f"当前：{esc(cur)} · 让路中：{'是' if _idx_state['yielding'] else '否'}</p>"
            + "<form method='post' action='/admin/resource'>"
            "<input type='hidden' name='act' value='save'>"
            "<h4>课件文件夹（每行一个，支持本地盘与 UNC 路径）</h4>"
            f"<textarea name='folders' rows='4'>{esc(folders)}</textarea>"
            "<h4>学科列表（逗号分隔；判不出的归“其他”）</h4>"
            f"<input name='subjects' value='{esc(subjects)}'>"
            "<h4>夜间索引时间窗</h4>"
            f"<p>开始 <input name='ns' value='{esc(sget('night_start'))}' style='width:100px;display:inline-block;padding:6px'> "
            f"结束 <input name='ne' value='{esc(sget('night_end'))}' style='width:100px;display:inline-block;padding:6px'>（HH:MM，支持跨零点）</p>"
            "<label><input type='checkbox' name='idle' value='1' "
            + ("checked" if sget("idle_index", "0") == "1" else "")
            + "> AI 空闲时也更新索引（Assistant 忙碌时自动暂停）</label>"
            "<button type='submit'>保存设置</button></form>"
            "<h4>手动操作</h4><p>"
            "<form method='post' action='/admin/resource' style='display:inline'>"
            "<input type='hidden' name='act' value='scan'><button class='mini'>立即扫描</button></form> "
            "<form method='post' action='/admin/resource' style='display:inline'>"
            "<input type='hidden' name='act' value='warm'><button class='mini'>预热全部转换</button></form> "
            "<form method='post' action='/admin/resource' style='display:inline'>"
            "<input type='hidden' name='act' value='rebuild'><button class='mini'>重建全部索引</button></form> "
            "<form method='post' action='/admin/resource' style='display:inline'>"
            "<input type='hidden' name='act' value='clearcache'><button class='mini'>清空渲染缓存</button></form></p>"
            + ("<h4>转换失败的文件</h4><table border='1'><tr><th>文件</th><th>原因</th><th>操作</th></tr>"
               + frows + "</table>" if failed else "")
            + "<p class='tip'><a href='/admin'>返回管理中心</a> · <a href='/resource'>打开 Resource 列表</a></p>"
            + M.FOOTER)
    return (f"<!doctype html><html><head><meta charset='utf-8'>{M.STYLE}<title>Resource 管理</title>"
            f"</head><body><h3>Resource 管理</h3>{body}</body></html>")
