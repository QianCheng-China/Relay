# -*- coding: utf-8 -*-
"""
autoupdate.py - Relay 夜间自动修复流水线（由 launcher.py 在窗口期调用，也可手动运行）
流程：解析 relay.log 自上次运行以来的新增 ERROR -> 环境类问题只记录建议 ->
脱敏后连同完整 server.py 交给 AI（模型/地址读 key.txt）-> 解析完整新文件 ->
Canary 修订号递增 -> 归档 versions/ -> gate.py 门禁 -> 通过则覆盖 server.py 并请求重启验收。
只修 ERROR 对应的缺陷；无新 ERROR 时不动任何代码。产出始终为 Canary 通道版本。
用法：
python autoupdate.py            正常运行
python autoupdate.py --dry-run  演练：只做解析+脱敏，不调 AI、不落盘
python autoupdate.py --force    链路演练：无错误时用合成错误走全流程（会真实调 AI 并可能切版，
                                演练后可用 copy /y versions\server_vCanary0.1.0.py server.py 还原）
"""
import hashlib, json, os, re, shutil, subprocess, sys, time
from datetime import datetime
import requests as http

BASE          = os.path.dirname(os.path.abspath(__file__))
LOG_PATH      = os.path.join(BASE, "relay.log")   # 必须与 server.py 的 LOG_PATH 一致
STATE_PATH    = os.path.join(BASE, "autoupdate_state.json")
BLOCKED_PATH  = os.path.join(BASE, "blocked.json")
CHANGES_PATH  = os.path.join(BASE, "CHANGES.md")
SUGGEST_PATH  = os.path.join(BASE, "suggestions.md")
RESTART_REQ   = os.path.join(BASE, "RESTART_REQ")
VERSIONS_DIR  = os.path.join(BASE, "versions")
SERVER_PATH   = os.path.join(BASE, "server.py")
KEY_PATH      = os.path.join(BASE, "key.txt")
GATE_PATH     = os.path.join(BASE, "gate.py")
MAX_LOG_CHARS = 12000          # 送 AI 的日志上下文上限（字符）
CTX_BEFORE, CTX_AFTER = 60, 30 # 错误前后日志上下文行数
AI_TIMEOUT    = 300
AI_RETRIES    = 3

# 代码围栏在运行时拼出，源文件中不含任何字面三反引号
FENCE3  = chr(96) * 3
PYBLOCK = FENCE3 + "python"

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.FileHandler(os.path.join(BASE, "autoupdate.log"), encoding="utf-8")])
log = logging.getLogger("autoupdate")

# ---------------- key.txt（与 server.py 同规则：3 行 = key / 模型 / 地址） ----------------
def load_cfg():
    key, model, url = "", "glm-4.7-flash", "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    try:
        vals = [l.strip() for l in open(KEY_PATH, encoding="utf-8-sig")
                if l.strip() and not l.strip().startswith("#")]
        if vals: key = vals[0]
        if len(vals) > 1 and vals[1]: model = vals[1]
        if len(vals) > 2 and vals[2]: url = vals[2]
    except OSError:
        pass
    return key, model, url

# ---------------- 脱敏（学生隐私不出本机前先处理） ----------------
def sanitize(text):
    text = re.sub(r"user=\S+", "user=<U>", text)
    text = re.sub(r"dev=\S+", "dev=<D>", text)
    text = re.sub(r"(student\w*\d+)", "<U>", text, flags=re.I)
    return text

# 环境类问题关键词：命中则不改代码，只写建议
ENV_KEYS = ["address already in use", "10048", "10013", "端口", "防火墙", "firewall", "netsh",
            "key.txt", "icssvc", "热点", "wlan", "arp", "ping", "nssm", "权限",
            "拒绝访问", "access is denied", "mobile hotspot"]

LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} [\d:,]+) \[(\w+)\] (.*)$")

def parse_events(lines):
    """按行解析日志 -> [{idx, level, msg, trace}]"""
    events, cur = [], None
    for i, ln in enumerate(lines):
        m = LINE_RE.match(ln)
        if m:
            if cur: events.append(cur); cur = None
            if m.group(2) in ("ERROR", "WARNING"):
                cur = {"idx": i, "level": m.group(2), "msg": m.group(3), "trace": []}
        elif cur is not None:
            cur["trace"].append(ln.rstrip())
    if cur: events.append(cur)
    return events

def context_text(lines, ev):
    seg = lines[max(0, ev["idx"] - CTX_BEFORE): ev["idx"] + CTX_AFTER + len(ev["trace"])]
    return sanitize("\n".join(seg))[-MAX_LOG_CHARS:]

SYSTEM_PROMPT = ("你是资深 Python 后端工程师，负责对 Windows 局域网课堂答疑系统 Relay 的 "
                 "server.py 做保守的缺陷修复。原则：改动最小、宁可不改也绝不引入新问题。")

def build_prompt(src, logctx):
    return ("【任务】修复当前 server.py 中导致以下日志错误的缺陷（仅此一项）。\n\n"
            "【错误日志（已脱敏）】\n" + logctx + "\n\n"
            "【修复规范（逐条强制）】\n"
            "1. 只做修复该错误所需的最小改动；禁止新增功能、禁止重构、禁止调整无关代码风格。\n"
            "2. 禁止修改文件顶部「配置区」内任何内容。\n"
            "3. 禁止修改以下函数的实现：get_mac, device_id, _enter_queue, _q_grant, _q_abandon, "
            "_q_release, _busy_try, _busy_free, current_user, _finalize, _ai_worker。\n"
            "4. 禁止删除或改名任何路由；禁止修改日志格式与 logging 配置；禁止引入新的第三方库"
            "（仅可使用已导入的 flask/waitress/dnslib/requests/markdown/werkzeug 及标准库）。\n"
            "5. 数据库变更仅允许 CREATE TABLE IF NOT EXISTS 或 ALTER TABLE ADD COLUMN，"
            "禁止删列、改列类型、破坏性迁移。\n"
            "6. 保持 Windows 兼容：subprocess 的 creationflags/CREATE_NO_WINDOW 调用方式不得改变，"
            "不得改成 POSIX 写法。\n"
            "7. 保留全部原有注释；保留 __version__ 行不动（版本号由流水线统一递增）。\n\n"
            "【当前完整源码】\n" + PYBLOCK + "\n" + src + "\n" + FENCE3 + "\n\n"
            "【输出格式（严格遵守）】先输出修改后的完整文件，放在 " + PYBLOCK +
            " 代码块中；随后另起一行输出 ===CHANGES===，再给不超过 120 字的中文修改说明。"
            "不要输出其他内容。")

def call_ai(prompt):
    key, model, url = load_cfg()
    if not key:
        raise RuntimeError("key.txt 中没有 API Key")
    payload = {"model": model, "temperature": 0.1, "max_tokens": 32768,
               "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": prompt}]}
    delay = 10
    for attempt in range(AI_RETRIES):
        try:
            r = http.post(url, headers={"Authorization": "Bearer " + key},
                          json=payload, timeout=AI_TIMEOUT)
            if r.status_code == 429 or r.status_code >= 500:
                raise RuntimeError("HTTP %d" % r.status_code)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            log.warning("AI 调用失败（第 %d/%d 次，模型 %s）：%s", attempt + 1, AI_RETRIES, model, e)
            if attempt == AI_RETRIES - 1: raise
            time.sleep(delay); delay *= 3

def parse_patch(resp):
    m = re.search(re.escape(PYBLOCK) + r"\s*\n(.*?)" + re.escape(FENCE3), resp, re.S)
    code = m.group(1) if m else (resp if resp.lstrip().startswith("# -*- coding") else None)
    if not code:
        raise RuntimeError("AI 响应中未找到 Python 代码块")
    changes = resp.split("===CHANGES===", 1)[1].strip() if "===CHANGES===" in resp else "(无说明)"
    return code.strip() + "\n", changes[:200]

def bump_version(src):
    """Canary 通道递增：Canary0.1.0 -> Canary0.1.1。
    若当前是无前缀的 Stable 版本（如 0.2.0），产出 Canary0.2.1（自动修复永远走 Canary 通道）。"""
    m = re.search(r'__version__ = "(?:Canary)?(\d+)\.(\d+)\.(\d+)"', src)
    if not m:
        raise RuntimeError("源码缺少 __version__ 行")
    a, b, c = map(int, m.groups())
    return "Canary%d.%d.%d" % (a, b, c + 1)

def set_version(src, ver):
    return re.sub(r'__version__ = "[^"]+"', '__version__ = "%s"' % ver, src, count=1)

def load_blocked():
    try: return json.load(open(BLOCKED_PATH, encoding="utf-8"))
    except Exception: return {}

def save_blocked(d):
    json.dump(d, open(BLOCKED_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

def changes_append(text):
    with open(CHANGES_PATH, "a", encoding="utf-8") as f:
        f.write(text + "\n")

def run_gate(cand):
    p = subprocess.run([sys.executable, GATE_PATH, cand], capture_output=True, text=True, timeout=180)
    return p.returncode == 0, ((p.stdout or "") + (p.stderr or ""))[-800:]

def save_offset(n):
    json.dump({"offset": n}, open(STATE_PATH, "w", encoding="utf-8"))

def main():
    dry, force = "--dry-run" in sys.argv, "--force" in sys.argv
    if not os.path.exists(CHANGES_PATH):
        changes_append("# CHANGES - Relay 自动修复变更日志（每个 Canary 版本一条）\n")
    state = {}
    if os.path.exists(STATE_PATH):
        try: state = json.load(open(STATE_PATH, encoding="utf-8"))
        except Exception: state = {}
    offset = state.get("offset", 0)
    lines = open(LOG_PATH, encoding="utf-8", errors="replace").read().splitlines() if os.path.exists(LOG_PATH) else []
    events = [e for e in parse_events(lines) if e["idx"] >= offset]
    errors = [e for e in events if e["level"] == "ERROR"]

    if force and not errors:
        errors = [{"idx": max(0, len(lines) - 1), "level": "ERROR",
                   "msg": "ValueError: demo_force（演练用合成错误）",
                   "trace": ["Traceback (most recent call last):", "ValueError: demo_force"]}]

    if not errors:
        log.info("自上次运行以来无新 ERROR，不触发修复（检查起点 offset=%d，日志共 %d 行）",
                 offset, len(lines))
        if not dry and not force:
            save_offset(len(lines))
        return

    ev = errors[-1]  # 小步修复：每次只处理最新一条 ERROR
    target = ev["msg"] + "\n" + "\n".join(ev["trace"])
    if any(k in target.lower() for k in ENV_KEYS):
        with open(SUGGEST_PATH, "a", encoding="utf-8") as f:
            f.write("\n[%s] 环境类问题（未修改代码，请按提示人工处理）：\n%s\n"
                    % (datetime.now().isoformat(timespec="seconds"), sanitize(target)[:1000]))
        log.info("命中的是环境类问题（端口/防火墙/key.txt 等），已写入 suggestions.md，不修改代码")
        if not dry and not force:
            save_offset(len(lines))
        return

    fp = hashlib.md5((ev["level"] + ev["msg"][:150]).encode()).hexdigest()
    blocked = load_blocked()
    if fp in blocked:
        log.info("该错误已被封锁（此前修复被拒：%s），跳过。清理办法：编辑 blocked.json 删除该指纹。",
                 blocked[fp].get("reason", "?"))
        if not dry and not force:
            save_offset(len(lines))
        return

    src = open(SERVER_PATH, encoding="utf-8").read()
    newver = bump_version(src)
    prompt = build_prompt(src, context_text(lines, ev))

    if dry:
        print("===== 演练：将发送给 AI 的日志上下文（已脱敏，前 2000 字） =====")
        print(context_text(lines, ev)[:2000])
        print("===== 目标版本将为 v%s；本次不调用 AI、不落盘 =====" % newver)
        return

    log.info("开始修复：目标错误 = %s", ev["msg"][:120])
    try:
        resp = call_ai(prompt)
        code, changes = parse_patch(resp)
    except Exception as e:
        log.error("AI 环节失败：%s", e); return
    if "__version__" not in code:
        log.error("AI 返回的文件缺少 __version__ 行，放弃"); return
    code = set_version(code, newver)

    os.makedirs(VERSIONS_DIR, exist_ok=True)
    cand = os.path.join(VERSIONS_DIR, "server_v%s.py" % newver)
    open(cand, "w", encoding="utf-8", newline="").write(code)
    changes_append("\n## %s  %s\n- 目标错误：%s\n- AI 说明：%s"
                   % (newver, datetime.now().isoformat(timespec="seconds"),
                      ev["msg"][:100], changes))

    ok, out = run_gate(cand)
    if not ok:
        blocked[fp] = {"file": os.path.basename(cand), "reason": out[-200:],
                       "ts": datetime.now().isoformat(timespec="seconds")}
        save_blocked(blocked)
        changes_append("- 门禁：REJECTED -> 已加入 blocked.json（同一问题自动回退后不再尝试）\n  摘要：%s"
                       % out[-300:].replace("\n", " "))
        log.error("门禁拒绝候选版本 %s：%s", cand, out[-300:])
        return

    shutil.copyfile(cand, SERVER_PATH)
    open(RESTART_REQ, "w").write(os.path.basename(cand))
    changes_append("- 门禁：PASS -> 已切换，等待监护进程验收")
    log.info("候选版本 %s 已通过门禁并切换，已请求 launcher 验收（验收失败将自动回退）", newver)
    save_offset(len(lines))

if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("autoupdate 异常退出")
        raise
