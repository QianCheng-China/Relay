## Relay 部署与自动修复配置指南（完整版）
### 第 0 部分：文件清单与架构
将以下 6 个文件放在同一目录（推荐 `C:\Relay`，代码全部使用相对路径，目录名可自定）：
| 文件 | 作用 |
|---|---|
| server.py | 主程序：热点门户、DNS 劫持、AI 答疑、教师管理 |
| launcher.py | 监护进程：由 NSSM 运行，负责拉起/重启/验收/回退/夜间调度 |
| autoupdate.py | 夜间修复流水线：日志分析、脱敏、调 AI、递增版本 |
| gate.py | 门禁：语法、禁改区、配置区、真实进程冒烟测试 |
| install.bat | NSSM 服务一键安装（服务名 Relay） |
| key.txt | AI 配置：第 1 行密钥、第 2 行模型名、第 3 行 API 地址 |
自动产生的文件：`versions\`（版本归档）、`LASTGOOD`（最近稳定版指针）、`CHANGES.md`（变更日志）、`blocked.json`（被拒修复封锁清单）、`suggestions.md`（环境类问题人工处理建议）、`relay.db`、`relay_qa.csv`、`relay.log`、`launcher.log`、`launcher_service.log`、`autoupdate.log`。
数据流：`relay.log` 出现新 ERROR，流水线脱敏后交 AI 生成完整新文件，门禁检测（含真实进程冒烟），通过则在夜间窗口切换并由监护进程 120 秒验收，验收失败自动回退；人工审查通过的版本即为 Stable 基线。
### 第 1 部分：从 HappySynthesizer 迁移（已有部署才需要）
1. 停止旧服务：管理员 CMD 执行 `nssm stop HappySynthesizer`，再 `nssm remove HappySynthesizer confirm`。手动运行的实例直接关闭窗口。
2. 新建 `C:\Relay`，将上面 6 个文件放入，并将旧目录中的 `key.txt` 内容按新格式核对（第 1 行密钥即可，第 2、3 行可省略）。
3. 保留历史数据（可选）：将旧目录中 `happysynthesizer.db`、`happysynthesizer_qa.csv`、`happysynthesizer.log` 分别重命名为 `relay.db`、`relay_qa.csv`、`relay.log` 后复制到 `C:\Relay`。不需要历史数据则跳过。
4. 全新部署直接进入第 2 部分。
### 第 2 部分：安装依赖
````
pip install flask waitress dnslib requests markdown
````
### 第 3 部分：系统设置（管理员 CMD）
````
netsh advfirewall firewall add rule name="Relay_HTTP" dir=in action=allow protocol=TCP localport=80 remoteip=192.168.137.0/24
netsh advfirewall firewall add rule name="Relay_DNS_U" dir=in action=allow protocol=UDP localport=53 remoteip=192.168.137.0/24
netsh advfirewall firewall add rule name="Relay_DNS_T" dir=in action=allow protocol=TCP localport=53 remoteip=192.168.137.0/24
reg add "HKLM\SYSTEM\CurrentControlSet\Services\icssvc\Settings" /v WifiMaxPeers /t REG_DWORD /d 64 /f
net stop icssvc & net start icssvc
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
````
### 第 4 部分：热点配置
1. 设置，网络和 Internet，移动热点：共享来源选"以太网"；SSID 建议 `Relay`；密码至少 8 位；频带优先 5 GHz；打开开关。
2. 执行 `ipconfig`，确认"本地连接\* x"的 IP 为 `192.168.137.1`（代码中 `PORTAL_IP` 与此对应，不要改）。
### 第 5 部分：DNS 接管
````
netsh interface show interface
netsh interface ip set dns name="以太网" static 127.0.0.1
````
第一条命令查看有线网卡实际名称，第二条将"以太网"替换为实际名称。
### 第 6 部分：key.txt 配置
用记事本编辑 `C:\Relay\key.txt`，共 3 行（`#` 开头为注释，可保留）：
````
你的智谱APIKey
glm-4.7-flash
https://open.bigmodel.cn/api/paas/v4/chat/completions
````
第 1 行为智谱开放平台密钥；第 2 行模型名（答疑用 glm-4-flash 亦可，自动修复建议 glm-4.7-flash，其代码修复能力显著更强）；第 3 行 API 地址。答疑与自动修复共用这一份配置。
### 第 7 部分：首次启动与自检
````
cd C:\Relay
python server.py
````
正常应看到以下日志行（顺序不限）：
- `API Key 已从 key.txt 读取（长度 XX 字符），模型 glm-4.7-flash`
- `DNS(UDP) 127.0.0.1:53 已启动`
- `DNS(TCP) 127.0.0.1:53 已启动`
- `Relay vCanary0.1.0 门户 http://0.0.0.0:80 （Waitress 线程 64）—— Developer HighspeedG2304`
- `教师管理页 http://127.0.0.1:80/admin`
另开 CMD 自检：
````
nslookup captive.apple.com 127.0.0.1
:: 应返回 192.168.137.1
nslookup open.bigmodel.cn 127.0.0.1
:: 应返回真实公网 IP
curl http://127.0.0.1/
:: 应返回登录页 HTML
curl http://127.0.0.1/health
:: 应返回 ok
````
### 第 8 部分：管理页与账号
1. 修改管理密码：编辑 `server.py` 配置区的 `ADMIN_PASSWORD = "admin123"`，改为强密码，保存后重启程序。注意：配置区允许人工修改（AI 被禁止修改该区域，且门禁会校验一致性）；建议在白天修改，避免与夜间修复窗口重叠。
2. 本机浏览器打开 `http://127.0.0.1/admin`，输入管理密码登录。页面底部会显示"当前版本 Relay Canary0.1.0"，以及"回退上一稳定版"按钮（此时点击无副作用，仅是同版本重启）。
3. 批量导入账号：每行格式 `student01 123456`。
### 第 9 部分：平板实测（先 1 台）
1. 平板连接 `Relay` 热点，等 3 至 15 秒认证窗口自动弹出（若没弹，下拉通知栏点"登录到网络"，或关开一次 WLAN）。
2. 登录 student01，提问，得到回答。
3. 关掉窗口，关开一次 WLAN，窗口重弹，免登录直接问答（Cookie 180 天有效，设备绑定基于 MAC）。
4. 换一台平板登 student01，提示已绑定，验证绑定逻辑。
5. 通过后全班铺开。
教育 ROM 若裁剪认证窗口导致弹窗异常，记录现象并对照 `relay.log` 对应时间段排查。
### 第 10 部分：自动修复体系验证
停掉第 7 部分手动运行的 server.py（Ctrl+C），然后依次执行：
````
python gate.py versions\server_vCanary0.1.0.py
python autoupdate.py --dry-run
python autoupdate.py --force
````
- 第 1 条：对当前版本跑完整门禁，应逐项输出 OK 并以"门禁结论：PASS"结束。首次运行时若 `versions\` 尚无文件，先随便运行一次 `python launcher.py` 再立即关闭（会自动归档 vCanary0.1.0 并写 LASTGOOD），或手工建目录复制一份。
- 第 2 条：正常情况输出"无新 ERROR，不触发修复"，这是正确行为（无错误不动代码）。
- 第 3 条：用合成错误走完整链路——真实调用 AI、生成 `server_vCanary0.1.1.py`、过门禁、写入切换请求。完成后检查 `CHANGES.md` 出现新条目、`versions\` 出现新文件。演练后还原基线：
````
copy /y versions\server_vCanary0.1.0.py server.py
del RESTART_REQ 2>nul
````
### 第 11 部分：监护进程手动验证
````
python launcher.py
````
观察 `launcher.log`：应出现"监护启动"与"server 子进程已启动"。浏览器打开管理页确认门户正常。点击"回退上一稳定版"，数秒后 `launcher.log` 出现"已回退到 server_vCanary0.1.0.py（原因：教师从管理页请求回退）"，管理页刷新后版本号可见。验证完毕 Ctrl+C 停止。
### 第 12 部分：NSSM 服务安装
1. `nssm.exe` 放入 `C:\Relay`。
2. 管理员 CMD 执行：
````
cd /d C:\Relay
install.bat
````
脚本会自动探测 python 路径，移除同名旧服务，安装并启动名为 `Relay` 的服务（运行 launcher.py）。此后开机自启、崩溃自动拉起、无人值守体系全部生效。
### 第 13 部分：日常运维
夜间修复窗口默认 22:30 至次日 05:30（launcher.py 顶部 `AUTO_AT`、`AUTO_UNTIL` 可改）。每天窗口内最多运行一次流水线，每次只处理最新一条 ERROR，产出至多一个 Canary 版本。
| 事项 | 操作 |
|---|---|
| 查看 AI 改了什么 | 打开 `CHANGES.md`，每个版本一条：目标错误、AI 说明、门禁结论 |
| 某错误反复修不好 | 已自动进 `blocked.json`，同一指纹不再尝试；想再试则编辑该文件删除对应条目 |
| 日志报端口、防火墙、key.txt、热点类问题 | 看 `suggestions.md`，按提示做系统操作；代码不会被修改 |
| 上课时发现异常 | 管理页点"回退上一稳定版"，数秒生效 |
| 完全重置版本体系 | 停服务，删除 `versions\`、`LASTGOOD`、`blocked.json`、`autoupdate_state.json`，重启服务自动重新引导 |
| 换 AI 平台 | 只改 key.txt 第 2、3 行 |
| 修改管理密码、白名单等配置 | 白天在 server.py 配置区修改后重启服务；避免在夜间窗口前几分钟改动，防止在途候选版本与配置区校验冲突 |
| 晋升 Stable（人工审查） | 你回到电脑时：取 `versions\` 中验收通过的最新 Canary 版本，对照 `CHANGES.md` 审查，合入你的开发基线；如需标记 Stable，可将该版本 `__version__` 改为无前缀形式（如 `0.2.0`）作为新基线。此后 AI 的修复自动产出版本为 `Canary0.2.1` 等，始终与 Stable 分层 |
### 第 14 部分：故障排查
| 现象 | 处理 |
|---|---|
| 启动即退出，relay.log 有"DNS 启动失败" | 53 端口被占（其他 DNS 服务或残留进程），用 `netstat -ano | findstr :53` 找到并结束，或重启后重试 |
| 启动即退出，报 HTTP 端口占用 | 80 端口被 IIS 或其他服务占用，改配置区 `HTTP_PORT` 并同步调整防火墙规则 |
| 认证窗口不弹 | 先开关 WLAN；仍不弹则检查 relay.log 是否有异常，以及平板是否拿到了 192.168.137.x 网段 |
| AI 答疑报"暂时无法连接" | 核对 key.txt 第 1 行密钥、第 3 行地址；确认 `open.bigmodel.cn` 在白名单内且服务器自身 DNS 解析正常 |
| autoupdate.log 报"AI 响应中未找到 Python 代码块" | 模型未按格式输出，流水线自动放弃，下个窗口重试；若频繁出现，将 key.txt 第 2 行换为 glm-4.7-flash |
| 门禁频繁 REJECT | 属正常保护动作；查 `CHANGES.md` 中 REJECTED 摘要与 `blocked.json`；对应问题将转人工 |
| 服务起了但平板连不上 | 检查热点开关、防火墙三条规则、`ipconfig` 的 192.168.137.1 |
### 第 15 部分：安全与隐私
- 管理密码 `ADMIN_PASSWORD` 部署后必须修改（配置区内人工改）。
- key.txt 含 API 密钥，不要外传；答疑与修复共用，泄露会导致额度被盗用。
- 送 AI 的日志已经脱敏：用户名替换为 `<U>`、设备 MAC 替换为 `<D>`、匹配 `student` 加数字的账号替换为 `<U>`；学生提问原文不写日志，不会进入修复流程。
- 封闭网络属性不变：除白名单域名外所有解析均指向门户，自动修复复用已在白名单内的 API 域名，无需新增放行。
- AI 补丁被限制在禁改清单之外（配置区、设备识别、并发锁、会话校验、落盘统计等核心函数逐字节校验），数据库仅允许加列，Windows 调用方式不得改动；门禁冒烟测试会真实启动候选版本并验证门户 HTML、未知路径非 204、DNS 劫持应答三大不变量。
---
最后再次提醒两处复制要点：② 中 `_results` 行必须带 `threading.Lock()`（上文已单独给出修正行）；④ 中所有 `\\n` 应为单层 `\n`（上文已给出三个函数的权威版本，可直接整体替换）。如运行报错，把报错原文和对应文件名发我即可。
