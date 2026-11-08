@echo off
setlocal
title Relay Hosted Network Setup v2.3
REM ============================================================
REM Relay hosted network one-click setup v2.3 (English edition)
REM v2.3: Step 4 shows the ICS service status instead of silencing
REM it (0x80040201 guard); Step 5 failure no longer aborts the run;
REM DNS takeover is skipped while the gateway is missing (it would
REM otherwise cut the server's own DNS); timeout replaced by ping
REM (reliable under redirection); fixed an echo pipe that swallowed
REM the port-80 warning; boot task now runs watchdog.bat so gateway
REM and Relay are checked at boot instead of after 15 minutes.
REM Usage: double-click (auto-elevates). Idempotent, safe to re-run;
REM a correct existing ICS binding is kept, never cleared.
REM Required in the same folder: hostednet_ics.ps1, watchdog.bat
REM Encoding: this file is pure ASCII. Save as ANSI or UTF-8 without
REM BOM. Do NOT save as UTF-16. No chcp needed.
REM ============================================================
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Administrator privileges required. Requesting elevation...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

REM ====== Adjust these parameters as needed ======
set SSID=Relay
set WIFIPASS=Relay12345678
REM Uplink adapter name as shown in ncpa.cpl. Keep AUTO to let
REM hostednet_ics.ps1 auto-detect the uplink. AUTO is deterministic
REM here because Step 2 disconnects the WLAN station link, leaving
REM only the wired uplink with a default route.
set UPLINK=AUTO
REM Set DISCONNECT_WLAN=0 ONLY if your uplink is the Wi-Fi adapter
REM itself (then Step 2 must not disconnect WLAN). Default 1 is safe
REM and recommended for a wired (Ethernet) uplink.
set DISCONNECT_WLAN=1
REM ===============================================

set RELAY_UPLINK=%UPLINK%
echo.
echo [Step 1] WLAN driver hosted-network capability (display only)
netsh wlan show drivers
echo NOTE: look for the hosted network capability line - "Hosted network
echo supported : Yes" on English systems, or its localized equivalent.
echo If it says No, this NIC cannot host; the real gate is Step 3.
echo.
echo [Step 2] Stop Mobile Hotspot (WinRT cleanup + service stop, double safety)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0hostednet_ics.ps1" -StopHotspot
net stop icssvc >nul 2>&1
if "%DISCONNECT_WLAN%"=="1" netsh wlan disconnect >nul 2>&1
echo If auto-stop failed above, turn off Mobile Hotspot manually in Settings.
echo.
echo [Step 3] Create and start the hosted network
netsh wlan set hostednetwork mode=allow ssid="%SSID%" key="%WIFIPASS%" keyUsage=persistent
netsh wlan start hostednetwork
if errorlevel 1 (
    echo [ERROR] Failed to start the hosted network. Common causes:
    echo - the driver does not support hosted network
    echo - the wireless radio is disabled by a hardware or software switch
    echo - Windows 11 24H2 or newer: hosted network was removed by Microsoft
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0hostednet_ics.ps1" -CheckClients
netsh wlan show hostednetwork
echo NOTE: status must be Started. The client limit is the driver-reported
echo value and does not guarantee stability at full load.
echo.
echo [Step 4] ICS service assurance (explicit start + visible status)
sc config SharedAccess start= auto >nul
net start SharedAccess >nul 2>&1
sc query SharedAccess | findstr /i "RUNNING" >nul
if errorlevel 1 (
    echo [WARN] SharedAccess ^(ICS^) is NOT running. Step 5 would fail with
    echo        error 0x80040201. Check "sc qc SharedAccess" - it may be
    echo        disabled by group policy or a system cleaner tool.
) else (
    echo SharedAccess ^(ICS^) service is running.
)
echo.
echo [Step 5] Configure Internet Connection Sharing (uplink=Public, hosted=Private)
if not exist "%~dp0hostednet_ics.ps1" (
    echo [ERROR] Missing hostednet_ics.ps1. Put it next to this script.
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0hostednet_ics.ps1"
if errorlevel 1 (
    echo [INFO] Auto config failed. Use the GUI fallback:
    echo 1. Run ncpa.cpl
    echo 2. Right-click the UPLINK adapter -^> Properties -^> Sharing
    echo 3. Check "Allow other network users to connect through this
    echo    computer's Internet connection"
    echo 4. In the dropdown, select the connection of the Microsoft
    echo    Hosted Network Virtual Adapter
    echo 5. Verify READ-ONLY with:
    echo    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0hostednet_ics.ps1" -VerifyIcs
    echo    Re-running the FULL script is now also safe: v2.3 keeps a
    echo    correct existing binding instead of clearing it.
    echo The setup continues with the remaining steps below.
    pause
)
echo.
echo [Step 6] Verify the gateway address
ipconfig | findstr /c:"192.168.137.1" >nul
if errorlevel 1 (
    echo [WARN] 192.168.137.1 not found. Restarting SharedAccess and retrying ICS...
    net stop SharedAccess >nul 2>&1
    ping -n 4 127.0.0.1 >nul
    net start SharedAccess >nul 2>&1
    ping -n 5 127.0.0.1 >nul
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0hostednet_ics.ps1"
    ipconfig | findstr /c:"192.168.137.1" >nul
    if errorlevel 1 echo [WARN] Still no gateway address. Check hostednet_watchdog.log or use the GUI method.
) else (
    echo Gateway address 192.168.137.1 is ready.
)
echo.
echo [Step 7] Relay service self-check and start
sc query Relay | findstr /i "RUNNING" >nul
if errorlevel 1 (
    echo Relay service is not running. Trying to start...
    net start Relay >nul 2>&1
    ping -n 7 127.0.0.1 >nul
)
sc query Relay | findstr /i "RUNNING" >nul
if errorlevel 1 (
    echo [WARN] Relay service failed to start. Run setup.bat first to finish deployment.
) else (
    curl -s -m 3 http://127.0.0.1/health | findstr /c:"ok" >nul
    if errorlevel 1 (
        echo [WARN] Health probe failed. Check port 80 occupancy:
        echo        netstat -ano ^| findstr ":80 "
    ) else (
        echo Relay portal health check passed.
    )
)
echo.
ipconfig | findstr /c:"192.168.137.1" >nul
if errorlevel 1 (
    echo [SKIP] Uplink DNS takeover skipped: gateway 192.168.137.1 is still
    echo        missing. Taking over DNS now would cut the server's own
    echo        name resolution. Fix ICS via the Step 5 GUI fallback, then
    echo        re-run this script ^(it is idempotent^).
) else (
    echo [Step 8] Uplink DNS takeover ^(key link of the client DNS chain^)
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0hostednet_ics.ps1" -DnsTakeover
)
echo.
echo [Step 9] Register boot task and watchdog scheduled task
if not exist "%~dp0watchdog.bat" (
    echo [WARN] watchdog.bat is missing next to this script - tasks not created.
) else (
    schtasks /create /tn "RelayHostedNetwork" /tr "\"%~dp0watchdog.bat\"" /sc onstart /delay 0000:30 /ru SYSTEM /rl HIGHEST /f >nul
    if errorlevel 1 (
        echo [WARN] Boot task creation failed. Run "netsh wlan start hostednetwork" manually after each boot.
    ) else (
        echo Boot task RelayHostedNetwork created ^(runs watchdog.bat at boot^).
    )
    schtasks /create /tn "RelayHostedWatchdog" /tr "\"%~dp0watchdog.bat\"" /sc minute /mo 15 /ru SYSTEM /rl HIGHEST /f >nul
    if errorlevel 1 (
        echo [WARN] Watchdog task creation failed.
    ) else (
        echo Watchdog task RelayHostedWatchdog created. Self-healing every 15 minutes.
    )
)
echo.
echo ============ Hosted network setup complete ============
echo Acceptance checklist:
echo 1. Tablet joins SSID "%SSID%".
echo 2. On the server, netsh wlan show hostednetwork client count +1,
echo    and arp -a shows 192.168.137.x entries.
echo 3. Tablet browser opens http://192.168.137.1/ and gets the login page;
echo    after WLAN off/on, the auth page pops up automatically.
echo Manual checks:
echo - Field-test the driver client limit at full class size.
echo - In the NIC advanced properties, disable "AP isolation" if offered.
echo - Read-only health check any time:
echo   powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0hostednet_ics.ps1" -VerifyIcs
pause
