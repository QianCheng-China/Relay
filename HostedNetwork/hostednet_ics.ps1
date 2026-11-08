# hostednet_ics.ps1 v2.3 - ICS auto configuration (ASCII-only output)
# ------------------------------------------------------------------
# Modes:
#  (no switch)    Configure ICS: uplink (Public) -> hosted adapter (Private)
#  -StopHotspot   Stop Mobile Hotspot via the WinRT tethering API
#  -CheckClients  Parse driver max client count (locale-independent)
#  -DnsTakeover   Point uplink DNS at 127.0.0.1 and verify the hijack chain
#  -VerifyIcs     Read-only check of sharing bindings + gateway (v2.2+)
#
# v2.3 changes:
#  - FIX COMException 0x80040201 (EVENT_E_NO_SUBSCRIBERS) at EnableSharing:
#    that call publishes the change through COM+ events and the ONLY
#    subscriber is the ICS service (SharedAccess). If the service is
#    stopped or disabled, the event has no subscriber and the call throws.
#    Now EventSystem + SharedAccess are brought up first (un-disabled if
#    needed), EnableSharing retries 3x (sleep, then service restart), and
#    every retry re-resolves the connection on a FRESH COM object so a
#    service restart in between cannot invalidate state.
#  - EnableRebootPersistConnection=1 (per Microsoft KB4054517): the ICS
#    binding now survives reboots; SharedAccess set to Automatic.
#  - IDEMPOTENCY: if the correct binding (uplink=Public, hosted=Private)
#    already exists (e.g. configured manually in ncpa.cpl), the script
#    KEEPS it and only verifies the gateway. A re-run can no longer undo
#    a manual configuration. Stale-binding clearing happens only when
#    the pairing is missing or wrong.
#  - A failing stale-binding cleanup no longer aborts the run.
#  - New -VerifyIcs: read-only state dump, safe at any time.
#
# Encoding: this file contains ONLY ASCII characters. Save as ANSI or
# UTF-8 (identical for pure ASCII). Never save as UTF-16.
# ------------------------------------------------------------------
param(
    [switch]$StopHotspot,
    [switch]$CheckClients,
    [switch]$DnsTakeover,
    [switch]$VerifyIcs
)

$ErrorActionPreference = 'Continue'

function Get-UplinkName {
    # 1) explicit override wins (set RELAY_UPLINK in the .bat)
    $name = $env:RELAY_UPLINK
    if ($name -and $name -ne 'AUTO') { return $name }
    # 2) auto-detect: connected adapter owning the IPv4 default route,
    #    skipping virtual/loopback/bluetooth adapters (incl. the hosted one)
    $skip = 'Hosted|Host Network|Virtual|Bluetooth|Loopback|WAN Miniport|TAP|VMware|Hyper-V|VirtualBox|Wi-Fi Direct'
    $cands = @(Get-NetAdapter -ErrorAction SilentlyContinue |
        Where-Object { $_.Status -eq 'Up' -and $_.InterfaceDescription -notmatch $skip } |
        Sort-Object ifIndex)
    foreach ($a in $cands) {
        $gw = Get-NetRoute -InterfaceIndex $a.ifIndex -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue
        if ($gw) { return $a.Name }
    }
    if ($cands.Count -gt 0) { return $cands[0].Name }
    return $null
}

function Ensure-IcsServices {
    # The COM+ event fired by EnableSharing has exactly one subscriber:
    # the ICS service (SharedAccess). 0x80040201 means it is down/disabled.
    $ok = $true
    foreach ($svcName in @('EventSystem', 'SharedAccess')) {
        $svc = Get-Service -Name $svcName -ErrorAction SilentlyContinue
        if (-not $svc) {
            Write-Host "[ERROR] Required service is missing: $svcName"
            $ok = $false
            continue
        }
        if ($svc.StartType -eq 'Disabled') {
            Set-Service -Name $svcName -StartupType Automatic -ErrorAction SilentlyContinue
            $svc.Refresh()
            Write-Host "Service $svcName was Disabled - switched to Automatic."
        }
        if ($svc.Status -ne 'Running') {
            try {
                $null = Start-Service -Name $svcName -ErrorAction Stop
                Write-Host "Service $svcName started."
            } catch {
                Write-Host "[ERROR] Could not start service $svcName : $($_.Exception.Message)"
                $ok = $false
            }
        }
    }
    if ($ok) { Start-Sleep -Seconds 2 }  # let COM+ event subscribers register
    return $ok
}

function Enable-SharingRetry {
    # Re-resolves the connection on a FRESH COM object for every attempt,
    # so a SharedAccess restart between attempts cannot invalidate state.
    # Mode: 0 = Public (uplink), 1 = Private (hosted adapter).
    param([int]$Mode, [string]$Label)
    for ($try = 1; $try -le 3; $try++) {
        try {
            $s = New-Object -ComObject HNetCfg.HNetShare
            $conn = $null
            foreach ($c in $s.EnumEveryConnection()) {
                $p = $s.NetConnectionProps($c)
                if ($Mode -eq 0) {
                    if ($p.Name -eq $uplinkName) { $conn = $c; break }
                } else {
                    if ($p.DeviceName -match $hostedRe) { $conn = $c; break }
                }
            }
            if (-not $conn) { throw 'connection not found (is the hosted network started?)' }
            $cfg = $s.INetSharingConfigurationForINetConnection($conn)
            if ($cfg.SharingEnabled -and $cfg.SharingConnectionType -eq $Mode) {
                $kind = 'Public'; if ($Mode -eq 1) { $kind = 'Private' }
                Write-Host ("Sharing already active: {0} ({1})." -f $Label, $kind)
                return $true
            }
            if ($cfg.SharingEnabled) { $cfg.DisableSharing() }
            $cfg.EnableSharing($Mode)
            $kind = 'Public'; if ($Mode -eq 1) { $kind = 'Private' }
            Write-Host ("Sharing enabled: {0} ({1})." -f $Label, $kind)
            return $true
        } catch {
            $hr = 0
            try { $hr = $_.Exception.HResult } catch { }
            Write-Host ("[WARN] EnableSharing on {0} failed (attempt {1}/3, HR=0x{2:X8})." -f $Label, $try, $hr)
            if ($try -lt 3) {
                if ($try -eq 1) {
                    Write-Host '       Waiting 5s for COM+ event subscribers to register...'
                    Start-Sleep -Seconds 5
                } else {
                    Write-Host '       Restarting SharedAccess and retrying...'
                    Restart-Service -Name SharedAccess -Force -ErrorAction SilentlyContinue
                    Start-Sleep -Seconds 5
                }
            }
        }
    }
    return $false
}

# ---------------- Mode: -StopHotspot ----------------
if ($StopHotspot) {
    try {
        [void][Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime]
        [void][Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]
        $profiles = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfiles()
        $stopped = 0
        foreach ($p in $profiles) {
            try {
                $tm = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($p)
                if ($tm.TetheringOperationalState -eq 'On') {
                    $null = $tm.StopTetheringAsync()
                    $stopped++
                }
            } catch { }
        }
        Start-Sleep -Seconds 2
        Write-Host ("Mobile Hotspot stop issued; processed {0} connection profile(s)." -f $stopped)
        exit 0
    } catch {
        Write-Host '[INFO] Auto-stop hotspot failed. Turn off Mobile Hotspot manually in Settings.'
        exit 1
    }
}

# ---------------- Mode: -CheckClients ----------------
# netsh output is localized, but its structure is not: the settings block
# comes first, and its only "label : number" line is the max-client count.
if ($CheckClients) {
    $lines = @(netsh wlan show hostednetwork 2>$null)
    $max = $null
    foreach ($ln in $lines) {
        if ($ln -match '"') { continue }  # skip SSID line (may end with a digit)
        if ($ln -match ':\s*(\d+)\s*$') { $max = [int]$Matches[1]; break }
    }
    if ($max) {
        Write-Host ("Driver-reported maximum clients: {0}" -f $max)
        if ($max -lt 64) {
            Write-Host '[WARN] Limit below 64: 64 targets will not fit. Change the NIC or use a router-based design.'
        } else {
            Write-Host '[OK] Limit is sufficient on paper.'
            Write-Host ' The reported value does not guarantee stability at full load - field-test with the whole class.'
        }
    } else {
        Write-Host '[WARN] Could not parse the max client count from netsh output. Verify manually.'
    }
    exit 0
}

# ---------------- Mode: -DnsTakeover ----------------
if ($DnsTakeover) {
    $up = Get-UplinkName
    if (-not $up) {
        Write-Host '[ERROR] Could not resolve the uplink adapter. Set RELAY_UPLINK and retry.'
        exit 1
    }
    Write-Host ("Uplink adapter resolved to: {0}" -f $up)
    netsh interface ip set dns name="$up" static 127.0.0.1 | Out-Null
    Start-Sleep -Seconds 2
    $answer = (nslookup baidu.com 127.0.0.1 2>$null | Out-String)
    if ($answer -match '192\.168\.137\.1') {
        Write-Host 'DNS hijack chain verified: baidu.com answers 192.168.137.1.'
        exit 0
    } else {
        Write-Host '[WARN] DNS hijack probe failed.'
        Write-Host ' Check port 53 occupancy: netstat -ano | findstr ":53"'
        Write-Host ' Confirm relay.log shows: DNS (UDP) 127.0.0.1:53 started.'
        exit 1
    }
}

# ---------------- Mode: -VerifyIcs (read-only, always safe) ----------------
if ($VerifyIcs) {
    $svc = Get-Service -Name SharedAccess -ErrorAction SilentlyContinue
    if (-not $svc -or $svc.Status -ne 'Running') {
        Write-Host '[WARN] SharedAccess (ICS) service is not running - reported state may be empty or stale.'
    }
    try { $share = New-Object -ComObject HNetCfg.HNetShare } catch {
        Write-Host "[ERROR] Cannot create the ICS COM object: $($_.Exception.Message)"
        exit 1
    }
    $uplinkName = Get-UplinkName
    $localizedHosted = [regex]::Escape((-join ([char]0x627F, [char]0x8F7D)))
    $hostedRe = 'Hosted|Host Network|' + $localizedHosted
    $pubOk = $false; $privOk = $false
    Write-Host '--- Connections and sharing state (read-only) ---'
    foreach ($c in $share.EnumEveryConnection()) {
        $p = $share.NetConnectionProps($c)
        $state = 'state unavailable (SharedAccess stopped?)'
        try {
            $cfg = $share.INetSharingConfigurationForINetConnection($c)
            if ($cfg.SharingEnabled) {
                if ($cfg.SharingConnectionType -eq 0) { $state = 'shared PUBLIC' }
                elseif ($cfg.SharingConnectionType -eq 1) { $state = 'shared PRIVATE' }
                else { $state = 'shared (unknown type)' }
            } else { $state = 'not shared' }
        } catch { }
        $mark = ''
        if ($p.Name -eq $uplinkName) { $mark = ' [uplink]' }
        if ($p.DeviceName -match $hostedRe) { $mark = "$mark [hosted]" }
        Write-Host (" {0} | {1} | {2}{3}" -f $p.Name, $p.DeviceName, $state, $mark)
        if ($p.Name -eq $uplinkName -and $state -eq 'shared PUBLIC') { $pubOk = $true }
        if ($p.DeviceName -match $hostedRe -and $state -eq 'shared PRIVATE') { $privOk = $true }
    }
    $ip = Get-NetIPAddress -IPAddress 192.168.137.1 -ErrorAction SilentlyContinue
    Write-Host ''
    Write-Host (" Uplink PUBLIC sharing  : {0}" -f $(if ($pubOk) { 'OK' } else { 'NOT SET' }))
    Write-Host (" Hosted PRIVATE sharing : {0}" -f $(if ($privOk) { 'OK' } else { 'NOT SET' }))
    Write-Host (" Gateway 192.168.137.1  : {0}" -f $(if ($ip) { 'present on ' + $ip.InterfaceAlias } else { 'missing' }))
    if ($pubOk -and $privOk -and $ip) {
        Write-Host ' [OK] ICS binding complete. Tablets should reach http://192.168.137.1/'
        exit 0
    }
    Write-Host ' [FAIL] ICS binding incomplete. Configure in ncpa.cpl (see guide)'
    Write-Host '        or re-run hostednet.bat (full config mode).'
    exit 1
}

# ---------------- Default mode: configure ICS ----------------
Write-Host '[Step A] Bringing up the ICS service stack (0x80040201 fix)...'
if (-not (Ensure-IcsServices)) {
    Write-Host '[ERROR] The ICS service (SharedAccess) cannot be brought up.'
    Write-Host '        Diagnose with:  sc query SharedAccess   /   sc qc SharedAccess'
    Write-Host '        If disabled by group policy or a cleaner tool, re-enable it,'
    Write-Host '        or enable sharing once in ncpa.cpl (the GUI starts the service'
    Write-Host '        itself) and verify with: hostednet_ics.ps1 -VerifyIcs'
    exit 1
}

$uplinkName = Get-UplinkName
if (-not $uplinkName) {
    Write-Host '[ERROR] No connected uplink adapter detected. Connect the uplink or set RELAY_UPLINK.'
    exit 1
}
Write-Host ("Uplink adapter: {0}" -f $uplinkName)

# The hosted virtual adapter device name is English on most systems but can
# be localized (the two CJK chars U+627F U+8F7D on zh-CN builds). Built from
# code points so this file stays pure ASCII.
$localizedHosted = [regex]::Escape((-join ([char]0x627F, [char]0x8F7D)))
$hostedRe = 'Hosted|Host Network|' + $localizedHosted

try { $share = New-Object -ComObject HNetCfg.HNetShare } catch {
    Write-Host "[ERROR] Cannot create the ICS COM object: $($_.Exception.Message)"
    exit 1
}

Write-Host '--- Current network connections ---'
foreach ($c in $share.EnumEveryConnection()) {
    $p = $share.NetConnectionProps($c)
    Write-Host (" Name: {0} | Device: {1}" -f $p.Name, $p.DeviceName)
}

# Sanity: both endpoints must exist before touching anything.
$foundUplink = $false
$hostedAlias = $null
foreach ($c in $share.EnumEveryConnection()) {
    $p = $share.NetConnectionProps($c)
    if ($p.Name -eq $uplinkName) { $foundUplink = $true }
    if (-not $hostedAlias -and $p.DeviceName -match $hostedRe) { $hostedAlias = $p.Name }
}
if (-not $foundUplink) {
    Write-Host ("[ERROR] Uplink adapter not found: {0}" -f $uplinkName)
    Write-Host ' Check the exact adapter name in ncpa.cpl, or set RELAY_UPLINK.'
    exit 1
}
if (-not $hostedAlias) {
    Write-Host '[ERROR] Hosted network virtual adapter not found (start the hosted network first).'
    exit 1
}

# Persist the ICS binding across reboots (KB4054517) + auto service start.
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\SharedAccess" /v EnableRebootPersistConnection /t REG_DWORD /d 1 /f | Out-Null
Set-Service -Name SharedAccess -StartupType Automatic -ErrorAction SilentlyContinue

# IDEMPOTENCY: if the correct pairing already exists (e.g. set manually in
# the GUI), keep it untouched - a re-run must never undo a working setup.
$pubSet = $false
$privSet = $false
foreach ($c in $share.EnumEveryConnection()) {
    try {
        $p = $share.NetConnectionProps($c)
        $cfg = $share.INetSharingConfigurationForINetConnection($c)
        if (-not $cfg.SharingEnabled) { continue }
        if ($p.Name -eq $uplinkName -and $cfg.SharingConnectionType -eq 0) { $pubSet = $true }
        elseif ($p.DeviceName -match $hostedRe -and $cfg.SharingConnectionType -eq 1) { $privSet = $true }
    } catch { }
}
if ($pubSet -and $privSet) {
    Write-Host '[OK] Correct ICS binding already present (uplink=Public, hosted=Private).'
    Write-Host '     Keeping it untouched and verifying the gateway only.'
    Start-Sleep -Seconds 2
    $ip = Get-NetIPAddress -IPAddress 192.168.137.1 -ErrorAction SilentlyContinue
    if ($ip) {
        Write-Host ("Verified: {0} holds the gateway address 192.168.137.1." -f $ip.InterfaceAlias)
        exit 0
    }
    Write-Host '[WARN] Binding present but the gateway address is missing - rebuilding ICS.'
}

# Clear stale sharings (reached only when the pairing is missing/wrong).
# A failing cleanup must not abort the run.
foreach ($c in $share.EnumEveryConnection()) {
    try {
        $cfg = $share.INetSharingConfigurationForINetConnection($c)
        if ($cfg.SharingEnabled) {
            $cfg.DisableSharing()
            Write-Host 'Cleared one stale sharing binding.'
        }
    } catch {
        Write-Host '[WARN] One stale sharing binding could not be cleared (ignored).'
    }
}

Set-NetIPInterface -InterfaceAlias $hostedAlias -AddressFamily IPv4 -Dhcp Enabled -ErrorAction SilentlyContinue
Set-DnsClientServerAddress -InterfaceAlias $hostedAlias -ResetServerAddresses -ErrorAction SilentlyContinue

if (-not (Enable-SharingRetry -Mode 0 -Label $uplinkName)) {
    Write-Host '[ERROR] Could not enable PUBLIC sharing on the uplink.'
    Write-Host ' Likely causes:'
    Write-Host ' - SharedAccess disabled by group policy or cleaner tools (sc qc SharedAccess)'
    Write-Host ' - COM+ Event System (EventSystem) stopped'
    Write-Host ' Fallback: enable sharing once in ncpa.cpl (the GUI starts the service),'
    Write-Host ' then verify with: powershell -File hostednet_ics.ps1 -VerifyIcs'
    exit 1
}
if (-not (Enable-SharingRetry -Mode 1 -Label $hostedAlias)) {
    Write-Host '[ERROR] Could not enable PRIVATE sharing on the hosted adapter.'
    Write-Host ' Make sure the hosted network is started, then run this script again.'
    exit 1
}

Write-Host ("ICS configured: {0} (Public) -> hosted network virtual adapter (Private)." -f $uplinkName)
Start-Sleep -Seconds 4
$ip = Get-NetIPAddress -IPAddress 192.168.137.1 -ErrorAction SilentlyContinue
if ($ip) {
    Write-Host ("Verified: {0} holds the gateway address 192.168.137.1." -f $ip.InterfaceAlias)
    exit 0
}
Write-Host '[WARN] 192.168.137.1 not detected. ICS may not be effective; restart SharedAccess and retry.'
exit 1
