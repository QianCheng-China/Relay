## 部署流程

**☐ 1. 装依赖**

```cmd
pip install flask waitress dnslib requests markdown
```

**☐ 2. 系统设置**（管理员 CMD）

```cmd
netsh advfirewall firewall add rule name="HS_HTTP"   dir=in action=allow protocol=TCP localport=80  remoteip=192.168.137.0/24
netsh advfirewall firewall add rule name="HS_DNS_U"  dir=in action=allow protocol=UDP localport=53  remoteip=192.168.137.0/24
netsh advfirewall firewall add rule name="HS_DNS_T"  dir=in action=allow protocol=TCP localport=53  remoteip=192.168.137.0/24
reg add "HKLM\SYSTEM\CurrentControlSet\Services\icssvc\Settings" /v WifiMaxPeers /t REG_DWORD /d 64 /f
net stop icssvc & net start icssvc
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
```

**☐ 3. 开热点**：设置 → 网络和 Internet → 移动热点 → 共享来源选“**以太网**” → SSID 建议 `HappySynthesizer` → 密码 ≥8 位 → 频带**优先 5 GHz** → 打开 → `ipconfig` 确认“本地连接\* x” IP = 192.168.137.1
**☐ 4. DNS 接管**

```cmd
netsh interface show interface                        :: 查看有线网卡名
netsh interface ip set dns name="以太网" static 127.0.0.1
```

**☐ 5. 启动 + 自检**

```cmd
cd C:\HappySynthesizer
python server.py
```

✔ 无 WARNING；看到 `DNS(UDP) 已启动`、`HappySynthesizer 门户`。
另开 CMD：

```cmd
nslookup captive.apple.com 127.0.0.1     :: 应返回 192.168.137.1
nslookup open.bigmodel.cn 127.0.0.1      :: 应返回真实公网 IP
curl http://127.0.0.1/                   :: 应返回登录页 HTML
```

**☐ 6. 建账号**：一体机本机浏览器开 `http://127.0.0.1/admin` → 改过的管理密码 → 批量导入（每行 `student01 123456`）
**☐ 7. 平板实测（先 1 台）**

1. 连 `HappySynthesizer` → 等 3~15 秒**认证窗口自动弹出**（若没弹，下拉通知栏点“登录到网络”，或关开一次 WLAN）
2. 登录 student01 → 提问 → 得到回答
3. 关掉窗口 → **关开一次 WLAN** → 窗口重弹 → **免登录直接问答**（记忆生效）
4. 换一台平板登 student01 → 提示已绑定 → 验证绑定逻辑
5. 通过后全班铺开
   **☐ 8. 开机自启**（NSSM）：`nssm.exe` 放入 `C:\HappySynthesizer`，管理员 CMD：

```cmd
where python
C:\HappySynthesizer\nssm.exe install HappySynthesizer "上面查到的python路径" "C:\HappySynthesizer\server.py"
C:\HappySynthesizer\nssm.exe set HappySynthesizer AppDirectory C:\HappySynthesizer
C:\HappySynthesizer\nssm.exe start HappySynthesizer
```

## 针对“无浏览器”环境的补充说明

1. **认证窗口就是 App**。学生的全部使用路径：连 WiFi → 弹窗 → 登录 → 问答。页面里已内置提示：关掉窗口后**关开 WLAN 即可重弹**。这是无浏览器环境下最可靠的恢复方式，建议第一次上课时演示一遍。
2. **不要点窗口右上角的“使用此网络”以外的按钮乱试**；有的系统版本该按钮会直接关窗，同样用关开 WLAN 恢复。
3. **免登录的原理**：登录 Cookie 带 180 天有效期，认证窗口的 WebView 会把它持久化到磁盘（和学校认证页“记住密码”同一机制）。极端情况下如果 Cookie 域名对不上导致免登录失效，学生重新登录一次即可，设备绑定基于 MAC 不受影响。
4. **设备绑定基于 MAC**：Android 9 默认使用真实 MAC（不随机化），认证窗口和系统其他部分看到的 MAC 一致，绑定稳定。若个别平板开了“随机 MAC”（开发者选项），随机也是**每个 WiFi 固定**的，绑定依然成立。
5. **平板连回学校 WiFi 上网**不受影响，两边互不干扰。
6. 数据文件统一在 `C:\HappySynthesizer` 下：`happysynthesizer.db`（账号）、`happysynthesizer_qa.csv`（全班问答记录，Excel 可直接打开）、`happysynthesizer.log`（日志）。
   跑通单台后如果遇到学海平板特有的弹窗异常（教育 ROM 有时会裁剪认证窗口），把现象和 `happysynthesizer.log` 对应时间段的日志发我。
