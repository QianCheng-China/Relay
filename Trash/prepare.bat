@chcp 65001 >nul
@echo off
setlocal
:: ============================================================
::  HappySynthesizer 一键准备工具
::  Developer  HighspeedG2304
::  用法：右键 → 以管理员身份运行
::  本文件必须以 UTF-8（无 BOM）编码保存
:: ============================================================
title HappySynthesizer 准备工具

:: ---- 管理员权限检查 ----
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 请右键本文件，选择"以管理员身份运行"。
    pause
    exit /b 1
)

:: ---- 探测 Python 命令名 ----
set PY=python
where python >nul 2>&1
if %errorlevel% neq 0 (
    where py >nul 2>&1
    if %errorlevel%==0 (set PY=py) else (
        echo [错误] 找不到 python，请先安装 Python 3.11 并加入 PATH。
        pause
        exit /b 1
    )
)

:MENU
cls
echo ============================================================
echo    HappySynthesizer 一键准备工具
echo    Developer  HighspeedG2304
echo ============================================================
echo   [1] 全部安装（首次部署选这个）
echo   [2] 仅安装 Python 依赖
echo   [3] 仅配置防火墙 + 设备上限64 + 防睡眠
echo   [4] 仅 DNS 接管（有线网卡 DNS 指向本机）
echo   [5] 恢复 DNS 为自动获取（卸载/回退用）
echo   [0] 退出
echo ============================================================
set /p choice=请输入编号后回车:
if "%choice%"=="1" goto ALL
if "%choice%"=="2" goto PIP
if "%choice%"=="3" goto SYS
if "%choice%"=="4" goto DNS_ON
if "%choice%"=="5" goto DNS_OFF
if "%choice%"=="0" exit /b 0
goto MENU

:ALL
call :DO_PIP
call :DO_SYS
call :DO_DNS_ON
goto DONE

:PIP
call :DO_PIP
goto DONE

:SYS
call :DO_SYS
goto DONE

:DNS_ON
call :DO_DNS_ON
goto DONE

:DNS_OFF
call :DO_DNS_OFF
goto DONE

:DO_PIP
echo.
echo ---- [安装] Python 依赖 ----
%PY% -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple flask waitress dnslib requests
if %errorlevel% neq 0 (
    echo [提示] 清华镜像不可用，改用官方源重试...
    %PY% -m pip install flask waitress dnslib requests
)
goto :eof

:DO_SYS
echo.
echo ---- [配置] 防火墙 / 设备上限 / 防睡眠 ----
netsh advfirewall firewall delete rule name="HS_HTTP"  >nul 2>&1
netsh advfirewall firewall delete rule name="HS_DNS_U" >nul 2>&1
netsh advfirewall firewall delete rule name="HS_DNS_T" >nul 2>&1
netsh advfirewall firewall add rule name="HS_HTTP"  dir=in action=allow protocol=TCP localport=80 remoteip=192.168.137.0/24
netsh advfirewall firewall add rule name="HS_DNS_U" dir=in action=allow protocol=UDP localport=53 remoteip=192.168.137.0/24
netsh advfirewall firewall add rule name="HS_DNS_T" dir=in action=allow protocol=TCP localport=53 remoteip=192.168.137.0/24
reg add "HKLM\SYSTEM\CurrentControlSet\Services\icssvc\Settings" /v WifiMaxPeers /t REG_DWORD /d 64 /f
net stop icssvc >nul 2>&1
net start icssvc
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
goto :eof

:DO_DNS_ON
echo.
echo ---- [DNS] 接管 ----
echo 当前网卡列表：
netsh interface show interface
echo.
echo 请输入连校园网的有线网卡名（如"以太网"），
echo 直接回车 = 使用默认名"以太网"
set /p NIC=
if "%NIC%"=="" set NIC=以太网
netsh interface ip set dns name="%NIC%" static 127.0.0.1
if %errorlevel%==0 (
    echo [成功] %NIC% 的 DNS 已指向 127.0.0.1
) else (
    echo [失败] 网卡名可能不对，回主菜单重跑一次并核对名称
)
goto :eof

:DO_DNS_OFF
echo.
echo ---- [DNS] 恢复自动获取 ----
netsh interface show interface
set /p NIC=请输入要恢复的网卡名（直接回车 = "以太网"）:
if "%NIC%"=="" set NIC=以太网
netsh interface ip set dns name="%NIC%" dhcp
goto :eof

:DONE
echo.
echo ============================================================
echo   脚本部分完成。以下两步必须手动做（没有可靠的命令行方式）：
echo     1. 设置 → 网络和 Internet → 移动热点：
echo        共享来源=以太网，SSID=HappySynthesizer，
echo        密码自定，频带=5 GHz，打开开关
echo     2. 命令行 ipconfig 确认"本地连接\* x"的 IP = 192.168.137.1
echo   然后启动服务：
echo     cd C:\HappySynthesizer
echo     python server.py
echo   自检：
echo     nslookup captive.apple.com 127.0.0.1   （应返回 192.168.137.1）
echo     nslookup open.bigmodel.cn 127.0.0.1    （应返回真实公网 IP）
echo ============================================================
pause
