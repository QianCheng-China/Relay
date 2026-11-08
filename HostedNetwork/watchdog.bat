@echo off
REM ================================================================
REM RelayHostedWatchdog - hosted network watchdog (English edition)
REM Invoked by scheduled tasks:
REM   RelayHostedWatchdog - every 15 minutes
REM   RelayHostedNetwork  - at boot (+30s delay, runs this same file)
REM Self-checks and self-heals, in order:
REM   1 hosted network running (locale-independent adapter check;
REM     restarts it when down)
REM   2 gateway 192.168.137.1 present (re-runs ICS config when
REM     missing; hostednet_ics.ps1 v2.3 brings the services up first)
REM   3 Relay service running (starts it when down)
REM   4 uplink DNS takeover kept (idempotent refresh while the
REM     gateway is healthy, so a manual DNS change or a system reset
REM     can never silently break the hijack chain)
REM   5 portal health probe (log-only, never restarts Relay
REM     automatically - queued classroom requests must not be killed)
REM Log: hostednet_watchdog.log next to this file (truncated at 1MB)
REM Encoding: this file is pure ASCII. Save as ANSI or UTF-8 without
REM BOM. Do NOT save as UTF-16. No chcp needed.
REM ================================================================
set LOG=%~dp0hostednet_watchdog.log

for %%F in ("%LOG%") do set LOGSIZE=%%~zF
if defined LOGSIZE if %LOGSIZE% GTR 1048576 type nul > "%LOG%"

REM Give the system a few seconds to settle in the boot scenario
REM (adds only ~8s in the regular 15-minute rounds - negligible).
ping -n 9 127.0.0.1 >nul

REM --- 1 hosted network ---
REM Locale-independent: hosted network started <=> the Microsoft
REM Hosted Network Virtual Adapter exists in Up state. The zh-CN
REM device name fragment (U+627F U+8F7D) is built from code points,
REM so this file stays pure ASCII. No localized netsh text matching.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=[string][char]0x627F+[string][char]0x8F7D; $a=Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { ($_.InterfaceDescription -match 'Hosted|Host Network' -or $_.InterfaceDescription -like ('*'+$d+'*')) -and $_.Status -eq 'Up' }; if ($a) { exit 0 } else { exit 1 }"
if errorlevel 1 (
    netsh wlan start hostednetwork >nul 2>&1
    ping -n 6 127.0.0.1 >nul
    >> "%LOG%" echo %date% %time% hosted network not running, restart attempted
)

REM --- 2 gateway address ---
ipconfig | findstr /c:"192.168.137.1" >nul
if errorlevel 1 (
    >> "%LOG%" echo %date% %time% gateway 192.168.137.1 missing, re-running ICS config
    >> "%LOG%" 2>&1 powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0hostednet_ics.ps1"
    set GWMISS=1
) else (
    set GWMISS=
)

REM --- 3 Relay service ---
sc query Relay | findstr /i "RUNNING" >nul
if errorlevel 1 (
    net start Relay >nul 2>&1
    ping -n 7 127.0.0.1 >nul
    >> "%LOG%" echo %date% %time% Relay service not running, start attempted
)

REM --- 4 uplink DNS takeover keep-alive (only while the gateway is
REM     healthy - otherwise the server would cut its own resolution) ---
if not defined GWMISS (
    >> "%LOG%" 2>&1 powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0hostednet_ics.ps1" -DnsTakeover
)

REM --- 5 portal health probe (log-only) ---
curl -s -m 3 http://127.0.0.1/health | findstr /c:"ok" >nul
if errorlevel 1 (
    >> "%LOG%" echo %date% %time% WARNING: /health probe failed (service RUNNING but portal silent), check port 80 occupancy
)
