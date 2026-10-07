# -*- coding: utf-8 -*-
"""
launcher.py - Relay 常驻监护进程（由 NSSM 以系统服务方式运行）
职责：拉起/重启 server.py；健康监护；新版本 120 秒验收（通过则更新 LASTGOOD，异常则自动回退）；
执行 /admin 回退请求（ROLLBACK_REQ）；每日夜间窗口调用 autoupdate.py。
日志：launcher.log（自身）、launcher_service.log（server 子进程输出）
"""
import json, os, re, shutil, subprocess, sys, time, urllib.request
from collections import deque
from datetime import date, datetime

BASE          = os.path.dirname(os.path.abspath(__file__))
SERVER_PATH   = os.path.join(BASE, "server.py")
VERSIONS_DIR  = os.path.join(BASE, "versions")
LASTGOOD      = os.path.join(BASE, "LASTGOOD")
RESTART_REQ   = os.path.join(BASE, "RESTART_REQ")
ROLLBACK_REQ  = os.path.join(BASE, "ROLLBACK_REQ")
BLOCKED_PATH  = os.path.join(BASE, "blocked.json")
CHANGES_PATH  = os.path.join(BASE, "CHANGES.md")
HTTP_PORT     = int(os.environ.get("HS_HTTP_PORT", "80"))
CHECK_INTERVAL, FAILS_TO_RESTART = 10, 3
HEALTH_GRACE  = 120                       # 新版本切换后的验收等待期（秒）
CRASH_WINDOW, CRASH_MAX = 600, 3          # 10 分钟内崩 3 次 -> 回退
AUTO_AT, AUTO_UNTIL = "22:30", "05:30"    # 夜间修复窗口（跨午夜，可改）

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.FileHandler(os.path.join(BASE, "launcher.log"), encoding="utf-8")])
log = logging.getLogger("launcher")

def read_version(src):
    m = re.search(r'__version__ = "([^"]+)"', src)
    return m.group(1) if m else "Canary0.0.0"

def ensure_bootstrap():
    os.makedirs(VERSIONS_DIR, exist_ok=True)
    if not os.path.exists(LASTGOOD):
        ver = read_version(open(SERVER_PATH, encoding="utf-8").read())
        name = "server_v%s.py" % ver
        shutil.copyfile(SERVER_PATH, os.path.join(VERSIONS_DIR, name))
        open(LASTGOOD, "w").write(name)
        log.info("首次引导：当前 server.py (v%s) 已归档为 %s", ver, name)

def health_ok():
    try:
        r = urllib.request.urlopen("http://127.0.0.1:%d/health" % HTTP_PORT, timeout=3)
        return r.status == 200
    except Exception:
        return False

class Supervisor:
    def __init__(self):
        self.proc = None
    def start(self):
        out = open(os.path.join(BASE, "launcher_service.log"), "ab")
        self.proc = subprocess.Popen([sys.executable, SERVER_PATH], cwd=BASE,
                                     stdout=out, stderr=out)
        log.info("server 子进程已启动 pid=%s", self.proc.pid)
    def stop(self):
        if self.proc and self.proc.poll() is None:
            try: self.proc.kill()
            except Exception: pass
            try: self.proc.wait(timeout=10)
            except Exception: pass
            log.info("server 子进程已停止")
    def restart(self):
        self.stop(); time.sleep(1); self.start()

def blocked_add(cand, reason):
    try:
        d = json.load(open(BLOCKED_PATH, encoding="utf-8")) if os.path.exists(BLOCKED_PATH) else {}
    except Exception:
        d = {}
    d[os.path.basename(cand)] = {"reason": str(reason)[-200:],
                                 "ts": datetime.now().isoformat(timespec="seconds")}
    json.dump(d, open(BLOCKED_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

def changes_note(line):
    with open(CHANGES_PATH, "a", encoding="utf-8") as f:
        f.write("- %s\n" % line)

def do_rollback(sv, reason, cand=None):
    good = open(LASTGOOD).read().strip()
    shutil.copyfile(os.path.join(VERSIONS_DIR, good), SERVER_PATH)
    sv.restart()
    if cand:
        blocked_add(cand, reason)
        changes_note("[AUTO-REJECTED] %s - %s" % (cand, reason))
    log.warning("已回退到 %s（原因：%s）", good, reason)

def promote(cand):
    open(LASTGOOD, "w").write(os.path.basename(cand))
    log.info("新版本 %s 验收通过，已标记为 LASTGOOD", cand)

def in_window():
    t = time.strftime("%H:%M")
    return t >= AUTO_AT or t <= AUTO_UNTIL

def main():
    ensure_bootstrap()
    sv = Supervisor(); sv.start()
    pending = None            # (候选文件名, 验收截止时刻)
    fails, crashes = 0, deque()
    last_check = 0
    au, au_date = None, None
    log.info("监护启动：健康检查每 %d 秒 · 验收期 %d 秒 · 崩溃循环 %d 秒内 %d 次 -> 回退 · 夜间窗口 %s 至 %s",
             CHECK_INTERVAL, HEALTH_GRACE, CRASH_WINDOW, CRASH_MAX, AUTO_AT, AUTO_UNTIL)
    while True:
        time.sleep(1); now = time.time()
        # ---- 1) 请求文件 ----
        if os.path.exists(ROLLBACK_REQ):
            os.remove(ROLLBACK_REQ); pending = None
            do_rollback(sv, "教师从管理页请求回退"); continue
        if os.path.exists(RESTART_REQ):
            cand = open(RESTART_REQ).read().strip(); os.remove(RESTART_REQ)
            if os.path.exists(os.path.join(VERSIONS_DIR, cand)):
                sv.restart(); pending = (cand, now + HEALTH_GRACE)
                log.info("已切换到候选版本 %s，%d 秒后验收", cand, HEALTH_GRACE)
            else:
                log.error("RESTART_REQ 指向不存在的版本：%s", cand)
            continue
        # ---- 2) 子进程退出 ----
        if sv.proc.poll() is not None:
            crashes.append(now)
            while crashes and now - crashes[0] > CRASH_WINDOW: crashes.popleft()
            log.warning("server 进程退出（近 %d 秒内第 %d 次）", CRASH_WINDOW, len(crashes))
            if len(crashes) >= CRASH_MAX:
                crashes.clear(); pending = None
                do_rollback(sv, "崩溃循环：现状无法维持"); continue
            sv.restart(); continue
        # ---- 3) 新版本验收期 ----
        if pending:
            cand, deadline = pending
            if now >= deadline:
                if health_ok(): promote(cand)
                else: do_rollback(sv, "新版本验收未通过（启动或健康检查失败）", cand)
                pending = None
            continue
        # ---- 4) 常规健康监护 ----
        if now - last_check >= CHECK_INTERVAL:
            last_check = now
            if not health_ok():
                fails += 1
                log.warning("健康检查失败 %d/%d", fails, FAILS_TO_RESTART)
                if fails >= FAILS_TO_RESTART:
                    fails = 0; crashes.append(now)
                    while crashes and now - crashes[0] > CRASH_WINDOW: crashes.popleft()
                    if len(crashes) >= CRASH_MAX:
                        crashes.clear(); do_rollback(sv, "健康检查持续失败"); continue
                    sv.restart()
            else:
                fails = 0
        # ---- 5) 夜间自动修复调度 ----
        if au is not None:
            if au.poll() is not None:
                au = None; log.info("夜间修复流程结束")
        elif in_window() and au_date != date.today().isoformat():
            au_date = date.today().isoformat()
            log.info("进入夜间修复窗口，启动 autoupdate.py")
            au = subprocess.Popen([sys.executable, os.path.join(BASE, "autoupdate.py")], cwd=BASE,
                                  stdout=open(os.path.join(BASE, "autoupdate_stdout.log"), "ab"),
                                  stderr=subprocess.STDOUT)

if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("launcher 自身异常退出")
        raise
