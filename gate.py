# -*- coding: utf-8 -*-
r"""
gate.py - Relay 候选版本门禁。用法：python gate.py <候选server文件>
通过 -> 退出码 0；拒绝 -> 非 0 并输出原因。
检查项：1 语法编译  2 __version__（Canary 前缀可选）存在  3 禁改函数逐字一致
4 配置区一致  5 冒烟测试（备用端口起真实进程：/health、首页含 Relay、
未知路径不得 204、DNS 劫持应答）  6 测试实例日志无 ERROR
"""
import ast, os, py_compile, re, shutil, socket, subprocess, sys, tempfile, time
import urllib.request
from dnslib import DNSRecord, QTYPE

LOCKED_FUNCS = ["get_mac", "device_id", "_enter_queue", "_q_grant", "_q_abandon",
                "_q_release", "_busy_try", "_busy_free", "current_user", "_finalize", "_ai_worker"]
T_HTTP, T_DNS = 18080, 15353
BASE = os.path.dirname(os.path.abspath(__file__))
fails = []

if len(sys.argv) < 2:
    print("用法：python gate.py <候选server文件>"); sys.exit(2)
CAND = os.path.abspath(sys.argv[1])

def fail(msg):
    fails.append(msg); print("[FAIL]", msg)

def func_sources(path):
    src = open(path, encoding="utf-8").read()
    out = {}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name in LOCKED_FUNCS:
            out[node.name] = ast.get_source_segment(src, node)
    return out

def config_block(path):
    src = open(path, encoding="utf-8").read()
    m = re.search(r"# =+ 配置区.*?\n# =+\n", src, re.S)
    return re.sub(r"\s+", " ", m.group(0)) if m else None

# 1 语法
try:
    py_compile.compile(CAND, doraise=True); print("[OK] 语法编译")
except Exception as e:
    fail("编译失败：%s" % e)

# 2 版本行（Canary 前缀可选，兼容 Stable 基线）
if not re.search(r'__version__ = "(?:Canary)?\d+\.\d+\.\d+"', open(CAND, encoding="utf-8").read()):
    fail("缺少 __version__ 行")
else:
    print("[OK] __version__ 存在")

# 3 禁改函数
cur, cand = func_sources(os.path.join(BASE, "server.py")), func_sources(CAND)
for fn in LOCKED_FUNCS:
    if fn in cur and cand.get(fn) != cur[fn]:
        fail("禁改函数被修改：%s" % fn)
print("[OK] 禁改函数检查完成")

# 4 配置区
if config_block(os.path.join(BASE, "server.py")) != config_block(CAND):
    fail("配置区被修改（AI 不得触碰部署配置）")
else:
    print("[OK] 配置区一致")

# 5 6 冒烟测试：临时数据目录 + 备用端口，跑真实进程
tmp = tempfile.mkdtemp(prefix="relaygate_")
env = dict(os.environ, HS_HTTP_PORT=str(T_HTTP), HS_DNS_PORT=str(T_DNS), HS_DATA_DIR=tmp)
errf = open(os.path.join(tmp, "stderr.log"), "wb")
proc = subprocess.Popen([sys.executable, CAND], cwd=BASE, env=env,
                        stdout=subprocess.DEVNULL, stderr=errf)
errf.close()
ready = False
try:
    t0 = time.time()
    while time.time() - t0 < 25:
        if proc.poll() is not None:
            fail("冒烟进程提前退出（exit=%s）" % proc.returncode); break
        try:
            if urllib.request.urlopen("http://127.0.0.1:%d/health" % T_HTTP, timeout=2).status == 200:
                ready = True
                print("[OK] /health 就绪（%.1f 秒）" % (time.time() - t0)); break
        except Exception:
            time.sleep(0.5)
    else:
        fail("25 秒内 /health 未就绪")
    if ready and proc.poll() is None:
        try:
            html = urllib.request.urlopen("http://127.0.0.1:%d/" % T_HTTP, timeout=5).read().decode("utf-8", "replace")
            if "Relay" in html: print("[OK] 首页返回 Relay 门户 HTML")
            else: fail("首页未返回 Relay 门户 HTML")
        except Exception as e:
            fail("首页请求失败：%s" % e)
        try:
            rr = urllib.request.urlopen("http://127.0.0.1:%d/__gate__/not/exist" % T_HTTP, timeout=5)
            if rr.status == 204: fail("未知路径返回 204（认证窗口将无法弹出——核心不变量被破坏）")
            elif rr.status == 200: print("[OK] 未知路径返回 200（非 204）")
            else: fail("未知路径返回 %d" % rr.status)
        except Exception as e:
            fail("未知路径请求失败：%s" % e)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(4)
            s.sendto(DNSRecord.question("captive.apple.com", QTYPE.A).pack(), ("127.0.0.1", T_DNS))
            rec = DNSRecord.parse(s.recvfrom(4096)[0])
            ip = str(rec.a.rdata) if rec.a else ""
            if ip == "192.168.137.1": print("[OK] DNS 劫持应答 = %s" % ip)
            else: fail("DNS 劫持应答异常：%r" % ip)
            s.close()
        except Exception as e:
            fail("DNS 探测失败：%s" % e)
finally:
    try:
        proc.kill(); proc.wait(timeout=10)
    except Exception:
        pass
    try:
        for ln in open(os.path.join(tmp, "relay.log"), encoding="utf-8", errors="replace"):
            if "[ERROR]" in ln and "API Key" not in ln:
                fail("测试实例日志含 ERROR：%s" % ln.strip()[:150])
    except OSError:
        pass
    shutil.rmtree(tmp, ignore_errors=True)

if fails:
    print("门禁结论：REJECT（%d 项问题）" % len(fails)); sys.exit(1)
print("门禁结论：PASS"); sys.exit(0)
