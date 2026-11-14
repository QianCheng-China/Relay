# -*- coding: utf-8 -*-
"""
Relay Music 模块：音频播放（听力材料 / 音乐）
----------------------------------------------------------------
数据：data/music/{music.db, music.log}
      音频文件由教师放入管理页配置的一个或多个目录（递归子文件夹）。
管线：后台扫描（300s 节流 + 管理页手动）→ files 表（含 path；ID3 元数据可选）
      → 列表页全库扁平列出（每页 100 条）→ 播放页原生 <audio controls> +
      上一首/下一首 + 连播开关（按用户存服务器端）+ 进度心跳。
技术栈（认证 WebView 复核）：编解码走 Android 系统 media 框架；Range 请求由
      send_file(conditional=True) 支持（拖动进度/边下边播）；JS 全 ES5+XHR，
      渐进增强（JS 失效仅丢续播/连播）；恢复不自动出声（定位后由用户轻触播放）。
格式：.mp3 .m4a .wav .ogg（.flac 兼容性差，需要时改 MUSIC_EXTS）。
依赖：mutagen（Q1 已装；未装也能跑——退化为文件名+浏览器端时长）。
Relay Hold v2：所在页由全局 URL 心跳恢复；列表滚动由全局滚动心跳恢复；
      播放进度专用心跳（4s 节流 + 暂停/离页同步）；连播偏好按用户存库。
"""
import hashlib, json, os, threading, time
from html import escape as esc
from urllib.parse import quote
from flask import Blueprint, redirect, request, send_file

M = None
MUSIC_DB = MUSIC_LOG = None
log = None
bp = Blueprint("music", __name__)

try:
    import mutagen as _mg
    HAVE_MUTAGEN = True
except ImportError:
    _mg = None
    HAVE_MUTAGEN = False

MUSIC_EXTS = (".mp3", ".m4a", ".wav", ".ogg")
MIME = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav", "ogg": "audio/ogg"}
PAGE_SIZE = 100
HOLD_GAP = 10            # 进度超过该秒数才在重开时恢复
TICK_SEND = 4            # 播放中进度心跳间隔（秒）

def init(m, ast=None):
    global M, MUSIC_DB, MUSIC_LOG, log
    M = m
    MUSIC_DB = os.path.join(m.DATA, "music", "music.db")
    MUSIC_LOG = os.path.join(m.DATA, "music", "music.log")
    os.makedirs(os.path.dirname(MUSIC_DB), exist_ok=True)
    log = m._mklog("relay.music", MUSIC_LOG)
    init_db()
    _sdefault("folders", json.dumps([os.path.join(m.BASE, "music")], ensure_ascii=False))
    try:
        fd = json.loads(sget("folders"))[0]
    except Exception:
        fd = None
    if fd and not fd.startswith("\\\\"):
        try:
            os.makedirs(fd, exist_ok=True)
        except OSError:
            pass

def init_db():
    M.dbq(MUSIC_DB, """CREATE TABLE IF NOT EXISTS files(
        fid TEXT PRIMARY KEY, name TEXT, title TEXT, artist TEXT, path TEXT,
        ext TEXT, size INTEGER, mtime REAL, seconds REAL)""")
    for col, typ in (("path", "TEXT"), ("title", "TEXT DEFAULT ''"),
                     ("artist", "TEXT DEFAULT ''"), ("seconds", "REAL")):
        try:
            M.dbq(MUSIC_DB, "ALTER TABLE files ADD COLUMN %s %s" % (col, typ))
        except Exception:
            pass
    M.dbq(MUSIC_DB, """CREATE TABLE IF NOT EXISTS progress(
        user TEXT, fid TEXT, progress REAL DEFAULT 0, ts REAL,
        PRIMARY KEY(user, fid))""")
    M.dbq(MUSIC_DB, """CREATE TABLE IF NOT EXISTS prefs(
        user TEXT PRIMARY KEY, autoplay INTEGER DEFAULT 1)""")
    M.dbq(MUSIC_DB, "CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")

def sget(k, d=""):
    r = M.dbq(MUSIC_DB, "SELECT value FROM settings WHERE key=?", (k,))
    return r[0][0] if r else d

def sset(k, v):
    M.dbq(MUSIC_DB, "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, str(v)))

def _sdefault(k, v):
    if not M.dbq(MUSIC_DB, "SELECT value FROM settings WHERE key=?", (k,)):
        sset(k, v)

def get_folders():
    try:
        v = json.loads(sget("folders", "[]"))
        return v if isinstance(v, list) else []
    except Exception:
        return []

def fid_of(path):
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:16]

def human(n):
    n = int(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%dB" % n if unit == "B" else "%.1f%s" % (n, unit)
        n /= 1024.0
    return "%.1fGB" % n

def fmt_len(sec):
    try:
        sec = int(float(sec))
    except (TypeError, ValueError):
        return ""
    return "%d:%02d" % (sec // 60, sec % 60)

# ---------------- 元数据（mutagen 可选，防御式） ----------------
def read_meta(path):
    """返回 (title, artist, seconds)；任何失败都退化为 ('', '', None)。"""
    if not HAVE_MUTAGEN:
        return "", "", None
    try:
        f = _mg.File(path, easy=True)
        if f is None:
            return "", "", None
        tags = f.tags or {}
        title = (tags.get("title") or [""])[0] if tags.get("title") else ""
        artist = (tags.get("artist") or [""])[0] if tags.get("artist") else ""
        secs = None
        if f.info is not None and getattr(f.info, "length", None):
            secs = float(f.info.length)
        return str(title).strip(), str(artist).strip(), secs
    except Exception:
        return "", "", None

# ---------------- 扫描 ----------------
_scan_lock = threading.Lock()
_last_scan = [0.0]

def _scan():
    seen = {}
    for root in get_folders():
        if not os.path.isdir(root):
            continue
        for dp, dn, fn in os.walk(root):
            for f in fn:
                ext = os.path.splitext(f)[1].lower()
                if ext not in MUSIC_EXTS or f.startswith("~$") or f.startswith("."):
                    continue
                p = os.path.join(dp, f)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                seen[p] = (st.st_mtime, st.st_size)
    old = {r[1]: r for r in M.dbq(MUSIC_DB, "SELECT fid,path,mtime,size FROM files")}
    for p, (mt, sz) in seen.items():
        fid = fid_of(p)
        ext = os.path.splitext(p)[1].lower()
        if p not in old:
            title, artist, secs = read_meta(p)
            M.dbq(MUSIC_DB, "INSERT INTO files(fid,name,title,artist,path,ext,size,mtime,"
                            "seconds) VALUES(?,?,?,?,?,?,?,?,?)",
                  (fid, os.path.basename(p), title, artist, p, ext, sz, mt, secs))
        elif abs(old[p][2] - mt) > 1 or old[p][3] != sz:
            title, artist, secs = read_meta(p)
            M.dbq(MUSIC_DB, "UPDATE files SET name=?,title=?,artist=?,path=?,size=?,"
                            "mtime=?,seconds=? WHERE fid=?",
                  (os.path.basename(p), title, artist, p, sz, mt, secs, fid))
    for p, o in old.items():
        if p not in seen:
            M.dbq(MUSIC_DB, "DELETE FROM files WHERE fid=?", (o[0],))
            M.dbq(MUSIC_DB, "DELETE FROM progress WHERE fid=?", (o[0],))

def scan_now():
    with _scan_lock:
        try:
            _scan()
        except Exception:
            log.exception("scan")
        finally:
            _last_scan[0] = time.time()

def _scan_loop():
    while True:
        scan_now()
        time.sleep(300)

def start_background():
    threading.Thread(target=_scan_loop, daemon=True).start()
    threading.Thread(target=lambda: (time.sleep(1), scan_now()), daemon=True).start()
    log.info("Music 后台线程已启动（mutagen：%s）",
             "已启用 ID3 元数据" if HAVE_MUTAGEN else "未安装（仅文件名）")

# ---------------- Relay Hold：播放进度 + 连播偏好 ----------------
def hold_get(u, fid):
    r = M.dbq(MUSIC_DB, "SELECT progress FROM progress WHERE user=? AND fid=?", (u, fid))
    try:
        return max(0.0, float(r[0][0])) if r else 0.0
    except (TypeError, ValueError):
        return 0.0

def hold_save(u, fid, progress):
    M.dbq(MUSIC_DB, "INSERT OR REPLACE INTO progress VALUES(?,?,?,?)",
          (u, fid, max(0.0, min(86400.0, progress)), time.time()))

def autoplay_get(u):
    r = M.dbq(MUSIC_DB, "SELECT autoplay FROM prefs WHERE user=?", (u,))
    return (int(r[0][0]) == 1) if r else True

def autoplay_set(u, on):
    M.dbq(MUSIC_DB, "INSERT OR REPLACE INTO prefs(user,autoplay) VALUES(?,?)",
          (u, 1 if on else 0))

# ---------------- 页面 ----------------
MUSIC_STYLE = ("<style>"
 ".navbtns{display:-webkit-flex;display:flex;gap:8px}"
 ".navbtns a,.navbtns form{flex:1}"
 ".navbtns .btn2{margin:0}"
 ".plname{font-size:22px;font-weight:bold;color:#1a202c;word-break:break-all}"
 ".plartist{font-size:15px;color:#718096;margin:2px 0 10px}"
 "audio{width:100%;margin:6px 0}"
 ".swline{display:-webkit-flex;display:flex;justify-content:space-between;"
 "align-items:center;margin:10px 0;flex-wrap:wrap}"
 "</style>")

def _shell(title, body, extra_head=""):
    u = M.current_user()
    js = M.hold_loc_js(u) if u else ""
    return (f"<!doctype html><html><head><meta charset='utf-8'>{extra_head}{M.STYLE}"
            f"{M.ANIM_STYLE}{MUSIC_STYLE}<title>{esc(title)}</title></head><body>{body}"
            + js + "</body></html>")

_SORT_SQL = " ORDER BY COALESCE(NULLIF(title,''), name) COLLATE NOCASE, name COLLATE NOCASE"

@bp.route("/music")
def music_list():
    u = M.current_user()
    if not u:
        return M.login_page()
    M.hold_page("music")
    q = request.args.get("q", "").strip()
    try:
        page = max(1, int(request.args.get("page", "") or 1))
    except ValueError:
        page = 1
    where, args = "", []
    if q:
        like = "%" + q + "%"
        where = " WHERE title LIKE ? OR name LIKE ? OR artist LIKE ?"
        args = [like, like, like]
    try:
        total = int(M.dbq(MUSIC_DB, "SELECT COUNT(*) FROM files" + where, args)[0][0])
        totsec = M.dbq(MUSIC_DB, "SELECT IFNULL(SUM(seconds),0) FROM files" + where,
                       args)[0][0] or 0
        rows = M.dbq(MUSIC_DB, "SELECT fid,title,artist,name,ext,size,seconds FROM files"
                     + where + _SORT_SQL + " LIMIT ? OFFSET ?",
                     args + [PAGE_SIZE, (page - 1) * PAGE_SIZE])
    except Exception:
        log.exception("music list")
        total, totsec, rows = 0, 0, []
    items = ""
    for fid, title, artist, name, ext, size, secs in rows:
        disp = title or os.path.splitext(name or "")[0]
        meta_bits = []
        if artist:
            meta_bits.append(str(artist))
        if secs:
            meta_bits.append(fmt_len(secs))
        meta_bits.append(human(size))
        chip = ("<span class='chip'>%s</span>" % esc(str(ext).lstrip(".").upper())) if ext else ""
        items += (f"<a class='litem' href='/music/play/{esc(fid)}'>"
                  f"<div class='fname'>{esc(disp)}{chip}</div>"
                  f"<div class='fmeta'>{esc(' · '.join(meta_bits))}</div></a>")
    if not rows:
        items = "<p class='tip'>没有匹配的音频。教师可在管理页配置音乐目录并点“立即扫描”。</p>"
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    pager = ""
    if pages > 1:
        pager = "<div class='rowline'><span>第 %d / %d 页</span><span>" % (page, pages)
        if page > 1:
            pager += "<a class='slink' href='/music?q=%s&amp;page=%d'>‹ 上一页</a>" % (
                quote(q, safe=""), page - 1)
        if page < pages:
            pager += "<a class='slink' href='/music?q=%s&amp;page=%d'>下一页 ›</a>" % (
                quote(q, safe=""), page + 1)
        pager += "</span></div>"
    hours, mins = int(totsec // 3600), int(totsec % 3600 // 60)
    sumline = "共 %d 首" % total + (" · 总时长 %d 小时 %d 分" % (hours, mins) if total else "")
    body = (M.back_btn("/home") + "<h3>Music · 音乐</h3>"
            "<form method='get' action='/music' style='display:-webkit-flex;display:flex;gap:6px'>"
            f"<input name='q' value='{esc(q)}' placeholder='搜索：标题 / 歌手 / 文件名'>"
            "<button type='submit' style='width:auto;padding:10px 16px'>搜索</button></form>"
            + (f"<p class='tip'>正在筛选“{esc(q)}” · <a href='/music'>清除筛选</a></p>" if q else "")
            + f"<p class='tip'>{esc(sumline)}（上一首/下一首按全库标题顺序）</p>"
            + items + pager + M.REOPEN_TIP + M.FOOTER)
    return _shell("Music", body)

# 播放页进度心跳（ES5）：timeupdate 4s 节流 + pause/ended/pagehide 同步；
# 恢复：loadedmetadata 后定位到上次位置（不自动出声，规避自动播放策略）
_PLAY_JS = ("<script>(function(){"
 "var a=document.getElementById('au');if(!a)return;"
 "var lastT=-1,lastSend=0,resume=__RESUME__,fid='__FID__',auto=__AUTO__,nxt=__NEXT__,done=false;"
 "function sv(){var t=Math.round(a.currentTime||0);if(t===lastT)return;lastT=t;"
 "var x=new XMLHttpRequest();x.open('POST','/music/hold',true);"
 "x.setRequestHeader('Content-Type','application/x-www-form-urlencoded');"
 "x.send('fid='+fid+'&p='+t);}"
 "a.addEventListener('timeupdate',function(){var n=new Date().getTime();"
 "if(n-lastSend>=__TICK__000){lastSend=n;sv();}},false);"
 "a.addEventListener('pause',sv,false);"
 "a.addEventListener('ended',function(){sv();if(auto&&nxt)location.href=nxt;},false);"
 "window.addEventListener('pagehide',function(){"
 "var x=new XMLHttpRequest();x.open('POST','/music/hold',false);"
 "x.setRequestHeader('Content-Type','application/x-www-form-urlencoded');"
 "x.send('fid='+fid+'&p='+Math.round(a.currentTime||0));},false);"
 "if(resume>__GAP__){"
 "a.addEventListener('loadedmetadata',function(){if(done)return;done=true;"
 "try{a.currentTime=resume;}catch(e){}},false);"
 "setTimeout(function(){if(done)return;done=true;"
 "try{a.currentTime=resume;}catch(e){}},1200);}"
 "})();</script>")


@bp.route("/music/play/<fid>")
def music_play(fid):
    u = M.current_user()
    if not u:
        return M.login_page()
    M.hold_page("music")
    row = M.dbq(MUSIC_DB, "SELECT fid,title,artist,name,seconds FROM files WHERE fid=?", (fid,))
    if not row:
        return M.msg_page("音频不存在或已被移除。", "/music")
    _, title, artist, name, secs = row[0]
    disp = title or os.path.splitext(name or "")[0]
    # “从头播放” / 连播开关：处理即重定向到干净 URL，
    # 避免全局 URL 心跳把 ?t=0 / ?autoplay= 固化（?t=0 固化会在下次恢复时误清进度）
    if request.args.get("t") == "0":
        hold_save(u, fid, 0.0)
        return redirect("/music/play/" + fid, 302)
    if request.args.get("autoplay") in ("0", "1"):
        autoplay_set(u, request.args.get("autoplay") == "1")
        return redirect("/music/play/" + fid, 302)
    resume = hold_get(u, fid)
    resumed = resume > HOLD_GAP
    auto = autoplay_get(u)
    order = [r[0] for r in M.dbq(MUSIC_DB, "SELECT fid FROM files" + _SORT_SQL)]
    try:
        idx = order.index(fid)
    except ValueError:
        idx = -1
    prev = order[idx - 1] if idx > 0 else None
    nxt = order[idx + 1] if 0 <= idx < len(order) - 1 else None
    nav = ""
    if prev:
        nav += "<a class='btn2' href='/music/play/%s'>‹ 上一首</a>" % prev
    else:
        nav += "<a class='btn2' style='opacity:.4'>‹ 上一首</a>"
    nav += "<a class='btn2' href='/music/play/%s?t=0'>从头播放</a>" % fid
    if nxt:
        nav += "<a class='btn2' href='/music/play/%s'>下一首 ›</a>" % nxt
    else:
        nav += "<a class='btn2' style='opacity:.4'>下一首 ›</a>"
    sw = "自动连播：" + ("<b>开</b>" if auto else "<b>关</b>")
    swbtn = ("<form method='get' action='/music/play/%s' style='display:inline'>"
             "<input type='hidden' name='autoplay' value='%s'>"
             "<button class='mini'>%s</button></form>"
             % (fid, "0" if auto else "1", "关闭连播" if auto else "开启连播"))
    meta_bits = []
    if artist:
        meta_bits.append(str(artist))
    if secs:
        meta_bits.append("时长 " + fmt_len(secs))
    meta = " · ".join(meta_bits)
    resume_tip = ("<p class='tip' style='color:#b7791f'>已恢复至上次位置 %s，"
                  "轻触播放按钮继续（或点“从头播放”）</p>" % fmt_len(resume)) if resumed else ""
    js = (_PLAY_JS.replace("__RESUME__", str(int(resume)))
          .replace("__FID__", esc(fid))
          .replace("__AUTO__", "1" if auto else "0")
          .replace("__NEXT__", ('"/music/play/%s"' % nxt) if nxt else '""')
          .replace("__TICK__", str(TICK_SEND))
          .replace("__GAP__", str(HOLD_GAP)))
    body = (M.back_btn("/music") + "<h3>Music · 正在播放</h3>"
            f"<div class='plname'>{esc(disp)}</div>"
            f"<div class='plartist'>{esc(meta) if meta else ''}</div>"
            f"<audio id='au' controls preload='metadata' src='/music/audio/{esc(fid)}'></audio>"
            + resume_tip +
            "<div class='navbtns'>" + nav + "</div>"
            f"<div class='swline'><span class='tip'>{sw}</span><span>{swbtn}</span></div>"
            "<p class='tip'>首次轻触播放后可拖动进度条；连播开启时，一首结束后自动播放下一首"
            "（浏览器禁止网页自动出声，需先手动播放一次）</p>"
            + M.REOPEN_TIP + M.FOOTER + js)
    return _shell(disp, body)

@bp.route("/music/audio/<fid>")
def music_audio(fid):
    if not M.current_user():
        return M.login_page()
    row = M.dbq(MUSIC_DB, "SELECT ext, path FROM files WHERE fid=?", (fid,))
    if not row:
        return M.msg_page("音频不存在。", "/music")
    ext, p = row[0]
    if not p or not os.path.exists(p):
        return M.msg_page("音频文件未找到，请教师在管理页重新扫描。", "/music")
    mt = MIME.get(ext, "application/octet-stream")
    try:
        return send_file(p, mimetype=mt, conditional=True)   # conditional=True → 支持 Range
    except Exception:
        return M.msg_page("文件读取失败。", "/music")

@bp.route("/music/hold", methods=["POST"])
def music_hold():
    u = M.current_user()
    if not u:
        return "", 204
    fid = request.form.get("fid", "")
    try:
        p = float(request.form.get("p") or 0)
    except (TypeError, ValueError):
        p = 0.0
    if M.dbq(MUSIC_DB, "SELECT 1 FROM files WHERE fid=?", (fid,)):
        hold_save(u, fid, p)
    return "", 204

# ---------------- Music 管理子页 ----------------
@bp.route("/admin/music", methods=["GET", "POST"])
def admin_music():
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
            sset("folders", json.dumps(folders, ensure_ascii=False))
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
        elif act == "clearhold":
            M.dbq(MUSIC_DB, "DELETE FROM progress")
            M.dbq(MUSIC_DB, "DELETE FROM prefs")
            msg = "已清空全部播放进度与连播偏好"
    except Exception as e:
        msg = "操作失败：%s" % e
    folders = "\n".join(get_folders())
    try:
        n = int(M.dbq(MUSIC_DB, "SELECT COUNT(*) FROM files")[0][0])
        tot = M.dbq(MUSIC_DB, "SELECT IFNULL(SUM(seconds),0) FROM files")[0][0] or 0
        nid3 = int(M.dbq(MUSIC_DB, "SELECT COUNT(*) FROM files WHERE title<>''")[0][0])
    except Exception:
        n, tot, nid3 = 0, 0, 0
    hours, mins = int(tot // 3600), int(tot % 3600 // 60)
    stat = "共 %d 首 · 总时长 %d 小时 %d 分 · 含标题元数据 %d 首" % (n, hours, mins, nid3)
    mg_flag = ("已启用（ID3 标题/歌手/精确时长）" if HAVE_MUTAGEN else
               "未安装 mutagen（仅显示文件名）；pip install mutagen 后重新扫描即可启用")
    body = (M.back_btn("/admin") + ("<p class='err'>" + esc(msg) + "</p>" if msg else "")
            + f"<p>{esc(stat)}<br><span class='tip'>元数据：{mg_flag}</span></p>"
            "<form method='post' action='/admin/music'>"
            "<input type='hidden' name='act' value='save'>"
            "<h4>音乐目录（每行一个，支持本地盘与 UNC 路径，递归子文件夹）</h4>"
            f"<textarea name='folders' rows='4'>{esc(folders)}</textarea>"
            "<button type='submit'>保存并扫描</button></form>"
            "<h4>手动操作</h4><p>"
            "<form method='post' action='/admin/music' style='display:inline'>"
            "<input type='hidden' name='act' value='scan'>"
            "<button class='mini'>立即扫描</button></form> "
            "<form method='post' action='/admin/music' style='display:inline'>"
            "<input type='hidden' name='act' value='clearhold'>"
            "<button class='mini'>清空播放进度与连播偏好</button></form></p>"
            "<p class='tip'><a href='/admin'>返回管理中心</a> · <a href='/music'>打开 Music</a></p>"
            + M.FOOTER)
    return (f"<!doctype html><html><head><meta charset='utf-8'>{M.STYLE}"
            f"<title>Music 管理</title></head><body><h3>Music 管理</h3>{body}</body></html>")
