@echo off
chcp 65001 >nul
cd /d %~dp0
set PY=
for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set PY=%%i
if not defined PY ( echo [错误] 未找到 python，请确认已安装并加入 PATH & pause & exit /b 1 )
echo 使用解释器：%PY%
nssm stop Relay >nul 2>&1
nssm remove Relay confirm >nul 2>&1
nssm install Relay "%PY%" "%~dp0launcher.py"
nssm set Relay AppDirectory "%~dp0"
nssm set Relay AppStdout "%~dp0launcher_service.log"
nssm set Relay AppStderr "%~dp0launcher_service.log"
nssm start Relay
echo.
echo 完成：Relay 服务现在运行 launcher.py（监护进程）
pause
