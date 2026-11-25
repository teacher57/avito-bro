# avito: remove BOT BRO and HUB BRO from this PC (everything deploy.sh / setup.ps1 / the app put here).
# Start it with uninstall.cmd (double click): it asks for administrator rights and runs a copy of this file from %TEMP%,
# because a script can't delete the folder it runs from.
#
#   uninstall.ps1 -AvitoDir <folder>   the avito folder (default: the folder of this script)
#                 -KeepData            first copy the shop data (data\store) to the desktop
#                 -Yes                 don't ask for confirmation
#
# Removes: tasks HubBroServer and AvitoBot, every process running from the avito folder (hub, data server, bot, app,
# tray, Flet window), the Tailscale https address pointing to the hub (only that one), the "BOT BRO" shortcuts, the
# Flet window cache (%USERPROFILE%\.flet), and the avito folder itself (code, Python of the bot, data, tokens, logs).
# Leaves: Tailscale, OpenSSH, Node.js (prepare.ps1 installs them; remove them in Settings -> Apps if needed).
param(
    [string]$AvitoDir = "",
    [switch]$KeepData,
    [switch]$Yes
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
function Say($t, $c = "Gray") { Write-Host $t -ForegroundColor $c }
function Ok($t) { Say "   ok: $t" Green }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Run as Administrator (uninstall.cmd asks for it by itself)." }

if (-not $AvitoDir) { $AvitoDir = $PSScriptRoot }
$A = (Resolve-Path -LiteralPath $AvitoDir).Path.TrimEnd("\")
if (-not ((Test-Path "$A\data") -or (Test-Path "$A\bot") -or (Test-Path "$A\hub-bro"))) {
    throw "$A doesn't look like the avito folder (no data, bot or hub-bro inside) - nothing removed."
}
Set-Location $env:TEMP

Say ""
Say "This removes BOT BRO and HUB BRO from this PC:" Yellow
Say "  - folder $A (code, bot's Python, shop data, tokens, logs)" Yellow
Say "  - tasks HubBroServer, AvitoBot; running hub, data server, bot and app" Yellow
Say "  - Tailscale https address of the hub, 'BOT BRO' shortcuts, Flet cache" Yellow
Say "  Tailscale, OpenSSH and Node.js stay." Yellow
if ($KeepData) { Say "  The shop data is copied to the desktop first." Yellow } else { Say "  The shop data is NOT kept (run with -KeepData to keep a copy)." Yellow }
if (-not $Yes) {
    $answer = Read-Host "Type DELETE to continue"
    if ($answer -ne "DELETE") { Say "Cancelled, nothing changed." Green; return }
}

# 1. copy of the data
if ($KeepData -and (Test-Path "$A\data\store")) {
    $to = Join-Path ([Environment]::GetFolderPath("Desktop")) ("avito-data-" + (Get-Date -Format "yyyy-MM-dd-HHmm"))
    Copy-Item "$A\data\store" $to -Recurse
    Ok "shop data copied to $to"
}

# 2. scheduled tasks (stop first so nothing starts again)
foreach ($t in "HubBroServer", "AvitoBot") {
    $task = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    if ($task) {
        Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $t -Confirm:$false
        Ok "task $t removed"
    }
}

# 3. processes from the folder: by command line, plus whatever listens on the hub/data ports with a parent from the folder
$esc = [regex]::Escape($A)
$procs = @(Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne $PID -and (($_.CommandLine -match $esc) -or ($_.ExecutablePath -and $_.ExecutablePath -match $esc)) })
foreach ($port in 8790, 8791) {
    foreach ($c in @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)"
        $parent = if ($p) { Get-CimInstance Win32_Process -Filter "ProcessId=$($p.ParentProcessId)" } else { $null }
        if ($p -and (($parent -and $parent.CommandLine -match $esc) -or $p.CommandLine -match "server\\server\.mjs")) { $procs += $p }
    }
}
$procs = $procs | Sort-Object ProcessId -Unique
foreach ($p in $procs) {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
if ($procs) { Ok "stopped $($procs.Count) process(es): $((($procs | ForEach-Object { $_.Name }) | Sort-Object -Unique) -join ', ')" }
Start-Sleep -Seconds 2

# 4. Tailscale https address -> only if it points to the hub (127.0.0.1:8790)
$ts = "$env:ProgramFiles\Tailscale\tailscale.exe"
if (Test-Path $ts) {
    $serve = (& $ts serve status 2>$null) -join " "
    if ($serve -match "127\.0\.0\.1:8790") {
        & $ts serve --https=443 off 2>$null | Out-Null
        Ok "Tailscale https address of the hub removed"
    }
}

# 5. shortcuts pointing into the folder
$ws = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("CommonDesktopDirectory"))) {
    foreach ($l in @(Get-ChildItem $dir -Filter "*.lnk" -ErrorAction SilentlyContinue)) {
        $s = $ws.CreateShortcut($l.FullName)
        if ("$($s.TargetPath) $($s.Arguments)" -match $esc) { Remove-Item $l.FullName -Force; Ok "shortcut $($l.Name) removed" }
    }
}

# 6. Flet window cache (downloaded by the app on its first start)
if (Test-Path "$env:USERPROFILE\.flet") { Remove-Item "$env:USERPROFILE\.flet" -Recurse -Force -ErrorAction SilentlyContinue; Ok "Flet cache removed" }

# 7. the folder (a few tries: Windows releases files a moment after the processes end)
for ($i = 1; $i -le 5 -and (Test-Path $A); $i++) {
    try { Remove-Item -LiteralPath $A -Recurse -Force -ErrorAction Stop } catch { Start-Sleep -Seconds 2 }
}
if (Test-Path $A) {
    Say ""
    Say "The folder is still there - something else keeps files open in it:" Red
    Get-ChildItem $A -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 10 | ForEach-Object { Say "   $($_.FullName)" Red }
    Say "Close any window/editor opened in that folder (Explorer, Notepad, VS Code) and run uninstall again." Red
} else {
    Ok "folder $A removed"
    Say ""
    Say "BOT BRO and HUB BRO are removed. Tailscale, OpenSSH and Node.js stay." Green
}
