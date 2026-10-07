@echo off
chcp 65001 >nul
title Relay 一键初始化
REM ============================================================
REM Relay 一键初始化脚本
REM 用法：右键"以管理员身份运行"（或直接双击，脚本会自动请求提权）
REM 前提：server.py launcher.py autoupdate.py gate.py 已在本目录
REM 可选：nssm.exe 放入本目录（用于安装系统服务）
REM 本脚本幂等，可重复运行
REM ============================================================

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo 需要管理员权限，正在请求提升...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"
echo.
echo ============ Relay 一键初始化 ============
echo 工作目录：%cd%
echo.

REM ---------- 0 探测 Python ----------
set PY=
for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set PY=%%i
if not defined PY (
    echo [错误] 未找到 python，请先安装并加入 PATH
    pause
    exit /b 1
)
echo [步骤 0] 使用解释器：%PY%

REM ---------- 1 迁移旧版 HappySynthesizer ----------
if exist "C:\HappySynthesizer\happysynthesizer.db" (
    echo.
    echo [步骤 1] 检测到旧版目录 C:\HappySynthesizer
    set /p MIG=是否迁移旧数据到本目录并停用旧服务？回车默认 N：
    if /i "%MIG%"=="Y" call :migrate
)

REM ---------- 2 安装依赖 ----------
echo.
echo [步骤 2] 安装 Python 依赖...
"%PY%" -m pip install --disable-pip-version-check -q flask waitress dnslib requests markdown
if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络后重试
    pause
    exit /b 1
)
echo 依赖安装完成

REM ---------- 3 系统设置 ----------
echo.
echo [步骤 3] 防火墙规则 / 热点设备上限 / 防休眠...
netsh advfirewall firewall delete rule name="Relay_HTTP" >nul 2>&1
netsh advfirewall firewall delete rule name="Relay_DNS_U" >nul 2>&1
netsh advfirewall firewall delete rule name="Relay_DNS_T" >nul 2>&1
netsh advfirewall firewall add rule name="Relay_HTTP" dir=in action=allow protocol=TCP localport=80 remoteip=192.168.137.0/24 >nul
netsh advfirewall firewall add rule name="Relay_DNS_U" dir=in action=allow protocol=UDP localport=53 remoteip=192.168.137.0/24 >nul
netsh advfirewall firewall add rule name="Relay_DNS_T" dir=in action=allow protocol=TCP localport=53 remoteip=192.168.137.0/24 >nul
reg add "HKLM\SYSTEM\CurrentControlSet\Services\icssvc\Settings" /v WifiMaxPeers /t REG_DWORD /d 64 /f >nul
net stop icssvc >nul 2>&1
net start icssvc >nul 2>&1
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
echo 系统设置完成（icssvc 已重启，热点开关如已开启会短暂断开）

REM ---------- 5 DNS 接管 ----------
echo.
echo [步骤 5] DNS 接管
netsh interface show interface
echo 下方输入要接管 DNS 的有线网卡名称（ipconfig 里"以太网"那块，不是"本地连接* x"）
set /p IFNAME=网卡名称，回车默认"以太网"：
if "%IFNAME%"=="" set IFNAME=以太网
netsh interface ip set dns name="%IFNAME%" static 127.0.0.1
if errorlevel 1 (
    echo [警告] DNS 设置未成功，请核对网卡名称后手动执行：
    echo     netsh interface ip set dns name="%IFNAME%" static 127.0.0.1
) else (
    echo DNS 接管完成：%IFNAME% 现指向 127.0.0.1
)

REM ---------- 6 key.txt ----------
echo.
echo [步骤 6] key.txt 配置
if not exist key.txt (
    > key.txt echo # 第1行：API Key（必填）
    >> key.txt echo # 第2行：模型名（可选，默认 glm-4.7-flash）
    >> key.txt echo # 第3行：API 地址（可选，默认智谱）
    echo 已生成模板 key.txt
) else (
    echo key.txt 已存在，跳过模板生成
)
set /p FILLKEY=是否现在录入 API Key？明文回显，回车默认 N：
if /i "%FILLKEY%"=="Y" call :fillkey

REM ---------- 8 管理密码（可选，须在归档基线前做） ----------
echo.
echo [步骤 8] 管理密码
echo 当前 server.py 中 ADMIN_PASSWORD = "admin123"，部署后必须修改
set /p CHGPW=是否现在设置新管理密码？明文回显，回车默认 N：
if /i "%CHGPW%"=="Y" call :setpw

REM ---------- 7a 归档基线版本 ----------
echo.
echo [步骤 7] 归档当前版本并建立 LASTGOOD 基线...
call :write_helper
"%PY%" _setup_helper.py bootstrap
if errorlevel 1 (
    echo [错误] 版本归档失败
    pause
    exit /b 1
)

REM ---------- 7b 语法编译检查 ----------
echo.
echo [步骤 7] 语法编译检查...
"%PY%" -m py_compile server.py launcher.py autoupdate.py gate.py
if errorlevel 1 (
    echo [错误] 语法检查未通过，请勿继续部署
    pause
    exit /b 1
)
echo 语法检查通过

REM ---------- 7c 冒烟测试 ----------
echo.
echo [步骤 7] 冒烟测试：临时启动主程序约 12 秒...
start "RelaySmoke" /min "%PY%" server.py
timeout /t 12 /nobreak >nul
set HEALTH=
for /f "delims=" %%a in ('curl -s -m 3 http://127.0.0.1/health 2^>nul') do set HEALTH=%%a
if "%HEALTH%"=="ok" (echo   health 检查：OK) else (echo   health 检查：未通过，80 端口可能被占用或启动异常，见 relay.log)
curl -s -m 3 http://127.0.0.1/ 2>nul | findstr /c:"Relay" >nul
if not errorlevel 1 (echo   门户页面：OK) else (echo   门户页面：未通过)
nslookup captive.apple.com 127.0.0.1 2>nul | findstr /c:"192.168.137.1" >nul
if not errorlevel 1 (echo   DNS 劫持：OK) else (echo   DNS 劫持：未通过，检查 53 端口占用)
taskkill /f /fi "WINDOWTITLE eq RelaySmoke*" >nul 2>&1
timeout /t 2 /nobreak >nul

REM ---------- 7d 门禁自检 ----------
echo.
echo [步骤 7] 对归档基线运行完整门禁（约 30 秒）...
set /p CAND=<LASTGOOD
"%PY%" gate.py "versions\%CAND%"
if errorlevel 1 (
    echo [警告] 门禁未通过，请查看上方 REJECT 原因后再继续
) else (
    echo 门禁自检通过
)

REM ---------- 12 NSSM 服务 ----------
echo.
echo [步骤 12] 安装系统服务 Relay（开机自启 + 崩溃自动拉起）
set NSSM=
if exist "%~dp0nssm.exe" set "NSSM=%~dp0nssm.exe"
if not defined NSSM (
    where nssm >nul 2>&1
    if not errorlevel 1 set NSSM=nssm
)
if not defined NSSM (
    echo [跳过] 未找到 nssm.exe。请将其放入本目录后重新运行本脚本，
    echo        或手动执行 install.bat 安装服务
    goto :summary
)
"%NSSM%" stop Relay >nul 2>&1
"%NSSM%" remove Relay confirm >nul 2>&1
"%NSSM%" install Relay "%PY%" "%~dp0launcher.py" >nul
"%NSSM%" set Relay AppDirectory "%~dp0" >nul
"%NSSM%" set Relay AppStdout "%~dp0launcher_service.log" >nul
"%NSSM%" set Relay AppStderr "%~dp0launcher_service.log" >nul
"%NSSM%" start Relay >nul
timeout /t 5 /nobreak >nul
curl -s -m 3 http://127.0.0.1/health 2>nul | findstr /c:"ok" >nul
if not errorlevel 1 (
    echo 服务已启动且健康检查通过
    start http://127.0.0.1/admin
) else (
    echo 服务已启动但健康检查未通过，请查看 launcher.log 与 launcher_service.log
)

:summary
echo.
echo ============ 自动配置完成 ============
echo 已自动完成：依赖安装 / 防火墙 / 热点设备上限 / 防休眠
if defined IFNAME echo             DNS 接管（%IFNAME%）/ 版本基线 / 门禁自检
echo.
echo 剩余人工步骤：
echo   1. 开热点：下面将自动打开设置页，共享来源选"以太网"，
echo      SSID 建议 Relay，密码至少 8 位，频带优先 5 GHz
echo   2. key.txt：若刚才未录入，用记事本填入第 1 行 API Key
echo   3. 管理密码：若刚才未设置，编辑 server.py 配置区后重启服务
echo      （nssm restart Relay）
echo   4. 浏览器 http://127.0.0.1/admin 批量导入账号
echo   5. 先用 1 台平板实测：连热点、弹窗登录、问答、重弹、换机绑定
echo   6. 通过后再全班铺开
echo.
echo 夜间自动修复窗口 22:30 至 05:30，日志见 relay.log / launcher.log / autoupdate.log
start ms-settings:network-mobilehotspot
echo.
pause
exit /b 0

REM ================= 子过程 =================

:migrate
set OLD=C:\HappySynthesizer
if exist "%~dp0nssm.exe" (
    "%~dp0nssm.exe" stop HappySynthesizer >nul 2>&1
    "%~dp0nssm.exe" remove HappySynthesizer confirm >nul 2>&1
)
if not exist relay.db copy "%OLD%\happysynthesizer.db" relay.db >nul
if not exist relay_qa.csv copy "%OLD%\happysynthesizer_qa.csv" relay_qa.csv >nul
if not exist relay.log copy "%OLD%\happysynthesizer.log" relay.log >nul
if not exist key.txt if exist "%OLD%\key.txt" copy "%OLD%\key.txt" key.txt >nul
echo 旧数据已迁移（relay.db / relay_qa.csv / relay.log），旧服务已移除
goto :eof

:fillkey
set "RELAY_KEY="
set "RELAY_MODEL="
set "RELAY_URL="
set /p RELAY_KEY=请粘贴智谱 API Key：
if "%RELAY_KEY%"=="" (
    echo 未输入，跳过
    goto :eof
)
set /p RELAY_MODEL=模型名，回车默认 glm-4.7-flash：
set /p RELAY_URL=API 地址，回车默认智谱：
if "%RELAY_MODEL%"=="" set "RELAY_MODEL=glm-4.7-flash"
if "%RELAY_URL%"=="" set "RELAY_URL=https://open.bigmodel.cn/api/paas/v4/chat/completions"
call :write_helper
"%PY%" _setup_helper.py setkey
goto :eof

:setpw
set "RELAY_NEWPW="
set /p RELAY_NEWPW=请输入新管理密码，6 至 64 位，仅限字母数字与 _ @ # $ . -：
call :write_helper
"%PY%" _setup_helper.py setpw
if errorlevel 1 (
    echo [错误] 密码格式不合法或改写失败，保持原密码
) else (
    echo 管理密码已写入 server.py
)
goto :eof

:write_helper
if exist _setup_helper.py goto :eof
> _setup_helper.py echo import os,sys,shutil,re
>> _setup_helper.py echo def bootstrap():
>> _setup_helper.py echo     src=open('server.py',encoding='utf-8').read()
>> _setup_helper.py echo     m='__version__ = "'
>> _setup_helper.py echo     i=src.find(m)
>> _setup_helper.py echo     if i<0: print('no version'); sys.exit(1)
>> _setup_helper.py echo     j=src.find('"',i+len(m))
>> _setup_helper.py echo     ver=src[i+len(m):j]
>> _setup_helper.py echo     os.makedirs('versions',exist_ok=True)
>> _setup_helper.py echo     name='server_v'+ver+'.py'
>> _setup_helper.py echo     shutil.copyfile('server.py',os.path.join('versions',name))
>> _setup_helper.py echo     open('LASTGOOD','w').write(name)
>> _setup_helper.py echo     print('bootstrap:',name)
>> _setup_helper.py echo def setpw():
>> _setup_helper.py echo     pw=os.environ.get('RELAY_NEWPW','')
>> _setup_helper.py echo     if not re.match(r'[A-Za-z0-9_@#$.-]{6,64}$',pw): print('bad pw'); sys.exit(1)
>> _setup_helper.py echo     src=open('server.py',encoding='utf-8').read()
>> _setup_helper.py echo     m='ADMIN_PASSWORD = "'
>> _setup_helper.py echo     i=src.find(m)
>> _setup_helper.py echo     if i<0: print('marker missing'); sys.exit(1)
>> _setup_helper.py echo     j=src.find('"',i+len(m))
>> _setup_helper.py echo     src=src[:i]+m+pw+'"'+src[j+1:]
>> _setup_helper.py echo     open('server.py','w',encoding='utf-8',newline='').write(src)
>> _setup_helper.py echo     print('password updated')
>> _setup_helper.py echo def setkey():
>> _setup_helper.py echo     k=os.environ.get('RELAY_KEY','')
>> _setup_helper.py echo     mo=os.environ.get('RELAY_MODEL','glm-4.7-flash')
>> _setup_helper.py echo     u=os.environ.get('RELAY_URL','https://open.bigmodel.cn/api/paas/v4/chat/completions')
>> _setup_helper.py echo     if not k: print('empty key'); sys.exit(1)
>> _setup_helper.py echo     open('key.txt','w',encoding='utf-8').write(k+'\n'+mo+'\n'+u+'\n')
>> _setup_helper.py echo     print('key.txt written')
>> _setup_helper.py echo cmd=sys.argv[1] if len(sys.argv)>1 else ''
>> _setup_helper.py echo if cmd=='bootstrap': bootstrap()
>> _setup_helper.py echo elif cmd=='setpw': setpw()
>> _setup_helper.py echo elif cmd=='setkey': setkey()
>> _setup_helper.py echo else: print('unknown cmd'); sys.exit(2)
goto :eof
