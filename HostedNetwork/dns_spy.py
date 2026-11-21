# -*- coding: utf-8 -*-
# dns_spy v2 — DNS 查询侦探(修复 v1 域名全显示 "?" 的解析 bug)
# 用法:sudo python3 dns_spy.py     日志:dns_spy.log(同时打印到终端)
# v2 变更:
#   - 修复:域名解析改 ASCII 解码 + 逐段容错(v1 的 idna 解码在部分环境下
#     对所有标签抛异常,被整体 except 吞掉,导致全部显示 "?")
#   - 新增 TCP/53 监听(部分客户端用 TCP 查询)
#   - 记录查询类型(A/AAAA/HTTPS——HTTPS 记录是判断协议倾向的线索)
#   - 智通云/学海关键词命中行打 ★★★
#   - 60 秒重复查询去重(★★★ 命中不去重),压制路由器改 DNS 后的重解析洪泛
import socket, threading, time, sys

UP = ("223.5.5.5", 53)
KEYWORDS = ("zty", "xuehai", "xh-", "yunzuoye", "zhthjy", "zhixue",
            "zhiyun", "ztytech", "aliyuncs")
LOG = open("dns_spy.log", "a", encoding="utf-8")
LOCK = threading.Lock()
_seen = {}
_seen_lock = threading.Lock()

def name_of(data):
    try:
        i, parts = 12, []
        while i < len(data):
            n = data[i]
            if n == 0:
                break
            if n & 0xC0:
                break                      # 压缩指针(查询报文不该出现,防御)
            part = data[i + 1:i + 1 + n].decode("ascii", "ignore").strip()
            if part:
                parts.append(part)
            i += 1 + n
        return ".".join(parts) if parts else "?"
    except Exception:
        return "?"

_QTYPES = {1: "A", 2: "NS", 5: "CNAME", 12: "PTR", 16: "TXT", 28: "AAAA",
           33: "SRV", 65: "HTTPS", 255: "ANY"}

def qtype_of(data):
    try:
        i = 12
        while i < len(data):
            n = data[i]
            if n == 0:
                i += 1
                break
            if n & 0xC0:
                i += 2
                break
            i += 1 + n
        t = (data[i] << 8) | data[i + 1]
        return _QTYPES.get(t, str(t))
    except Exception:
        return "?"

def write_line(line):
    with LOCK:
        LOG.write(line + "\n")
        LOG.flush()
        try:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except Exception:
            pass

def is_dup(ip, name, qt, hit):
    if hit:
        return False
    key = (ip, name, qt)
    now = time.time()
    with _seen_lock:
        if now - _seen.get(key, 0) < 60:
            return True
        _seen[key] = now
        if len(_seen) > 2048:
            _seen.clear()
        return False

def process(data, addr, proto, reply):
    name = name_of(data)
    qt = qtype_of(data)
    hit = any(k in name.lower() for k in KEYWORDS)
    if not is_dup(addr[0], name, qt, hit):
        mark = "  ★★★" if hit else ""
        write_line("%s  %-15s  %-3s %-5s  %s%s"
                   % (time.strftime("%H:%M:%S"), addr[0], proto, qt, name, mark))
    try:
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.settimeout(5)
        u.sendto(data, UP)
        resp, _ = u.recvfrom(4096)
        u.close()
        reply(resp)
    except Exception:
        pass

def udp_worker(data, addr, sock):
    process(data, addr, "udp", lambda r: sock.sendto(r, addr))

def tcp_worker(c, addr):
    try:
        c.settimeout(6)
        hdr = b""
        while len(hdr) < 2:
            ch = c.recv(2 - len(hdr))
            if not ch:
                return
            hdr += ch
        ln = int.from_bytes(hdr, "big")
        data = b""
        while len(data) < ln:
            ch = c.recv(min(65535, ln - len(data)))
            if not ch:
                return
            data += ch
        process(data, addr, "tcp",
                lambda r: c.sendall(len(r).to_bytes(2, "big") + r))
    except Exception:
        pass
    finally:
        try:
            c.close()
        except Exception:
            pass

def tcp_server():
    t = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    t.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    t.bind(("0.0.0.0", 53))
    t.listen(20)
    while True:
        c, a = t.accept()
        threading.Thread(target=tcp_worker, args=(c, a), daemon=True).start()

def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", 53))
    print("dns_spy v2 就绪:udp:53 + tcp:53;日志 dns_spy.log;★★★ = 关键词命中")
    threading.Thread(target=tcp_server, daemon=True).start()
    while True:
        data, addr = s.recvfrom(4096)
        threading.Thread(target=udp_worker, args=(data, addr, s), daemon=True).start()

if __name__ == "__main__":
    main()
