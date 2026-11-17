# Installs the Avito bot + manager app on this Windows PC (no WSL). Safe to run again: existing venvs are kept and only
# topped up, the AvitoBot task is re-registered. Run via setup.cmd (double-click) under the Windows account that should
# run the bot.
#
# Python (3.12+ needed), checked in this order:
#   1. a Python this script installed before, in avito-bot\python\
#   2. tools\uv\uv.exe next to the project folder (taylormomsen: uv + Python 3.13 already live there)
#   3. a Python already on this PC: the "py" launcher or python on PATH (not the Microsoft Store stub)
#   4. none found: downloads uv (official GitHub release, checksum verified) into avito-bot\python\uv\ and installs
#      Python 3.13 with it into avito-bot\python\ - nothing outside the bot folder, nothing added to PATH.
#
# Test the Python step alone (no venvs, no task):  setup.ps1 -PythonOnly [-ForceInstall]
param([switch]$PythonOnly, [switch]$ForceInstall)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is very slow with the progress bar on
$Root = $PSScriptRoot
$App = Join-Path $Root "manager-app"
$LocalPy = Join-Path $Root "python"                          # where Python goes if the PC has none
$LocalUv = Join-Path $LocalPy "uv\uv.exe"
$ToolsUv = Join-Path $Root "..\tools\uv\uv.exe"              # taylormomsen layout
$UvVersion = "0.13.8"
$PyVersion = "3.13"

function Use-Uv($uv, $pythonDir) {
    # uv keeps its Pythons and cache in the given folder (never in the user profile)
    $env:UV_PYTHON_INSTALL_DIR = $pythonDir
    $env:UV_PYTHON_BIN_DIR = Join-Path $pythonDir "bin"   # else uv puts python3.13.exe into %USERPROFILE%\.local\bin
    $env:UV_CACHE_DIR = Join-Path (Split-Path $uv) "cache"
    $env:UV_PYTHON_PREFERENCE = "only-managed"
    return @{ Kind = "uv"; Exe = $uv }
}

function Find-SystemPython {
    # -> full path of a Python 3.12+ already installed on this PC, or $null
    $paths = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $p = & py -3 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $p) { $paths += $p.Trim() }
    }
    foreach ($c in @(Get-Command python, python3 -All -ErrorAction SilentlyContinue)) {
        if ($c.Source -and $c.Source -notmatch "WindowsApps") { $paths += $c.Source }   # WindowsApps = Store stub
    }
    foreach ($p in ($paths | Select-Object -Unique)) {
        $v = & $p -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $v -and ([version]$v.Trim() -ge [version]"3.12")) { return $p }
    }
    return $null
}

function Install-LocalPython {
    Write-Host "No Python 3.12+ on this PC: installing Python $PyVersion into $LocalPy"
    $dir = Split-Path $LocalUv
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $name = "uv-x86_64-pc-windows-msvc.zip"
    $url = "https://github.com/astral-sh/uv/releases/download/$UvVersion/$name"
    $zip = Join-Path $dir $name
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    $shaFile = "$zip.sha256"   # saved as a file: GitHub serves it as binary, so .Content would be bytes, not text
    Invoke-WebRequest -Uri "$url.sha256" -OutFile $shaFile -UseBasicParsing
    $want = ((Get-Content $shaFile -Raw).Trim() -split "\s+")[0].ToLower()
    Remove-Item $shaFile -Force
    $got = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
    if ($want -ne $got) { Remove-Item $zip -Force; throw "uv download is damaged (checksum mismatch), run setup.cmd again" }
    Expand-Archive $zip -DestinationPath $dir -Force
    Remove-Item $zip -Force
    if (-not (Test-Path $LocalUv)) { throw "uv.exe not found after unpacking $name" }
    $uv = Use-Uv $LocalUv $LocalPy
    & $LocalUv python install $PyVersion
    if ($LASTEXITCODE -ne 0) { throw "uv could not install Python $PyVersion" }
    Write-Host "Python $PyVersion installed in $LocalPy"
    return $uv
}

function Get-Python {
    if (-not $ForceInstall) {
        if (Test-Path $LocalUv) { Write-Host "Python: installed earlier in $LocalPy"; return Use-Uv $LocalUv $LocalPy }
        if (Test-Path $ToolsUv) {
            Write-Host "Python: uv in $(Split-Path $ToolsUv)"
            return Use-Uv (Resolve-Path $ToolsUv).Path (Join-Path (Split-Path (Resolve-Path $ToolsUv).Path) "python")
        }
        $sys = Find-SystemPython
        if ($sys) { Write-Host "Python: found $sys"; return @{ Kind = "system"; Exe = $sys } }
    }
    return Install-LocalPython
}

$Python = Get-Python
if ($PythonOnly) {
    if ($Python.Kind -eq "uv") { & $Python.Exe python find $PyVersion } else { & $Python.Exe --version }
    return
}

function New-Venv($dir) {
    $py = Join-Path $dir "Scripts\python.exe"
    if (Test-Path $py) { return $py }
    Write-Host "Creating $dir"
    if ($Python.Kind -eq "uv") { & $Python.Exe venv $dir --python $PyVersion --seed } else { & $Python.Exe -m venv $dir }
    if (-not (Test-Path $py)) { throw "Could not create $dir" }
    return $py
}

function Install-Reqs($py, $reqs) {
    & $py -m pip --version *> $null
    if ($LASTEXITCODE -ne 0) { & $py -m ensurepip --upgrade | Out-Null }
    & $py -m pip install --disable-pip-version-check -q -r $reqs
    if ($LASTEXITCODE -ne 0) { throw "pip install failed for $reqs" }
}

# 1. Bot
$botPy = New-Venv (Join-Path $Root ".venv")
Install-Reqs $botPy (Join-Path $Root "requirements.txt")
Write-Host "Bot venv ready"

# 2. Manager app
$appPy = New-Venv (Join-Path $App ".venv")
Install-Reqs $appPy (Join-Path $App "requirements.txt")
Write-Host "Manager app venv ready"

# Shortcut with the BxB icon (a .cmd file can't have its own icon); paths are absolute, so it's made here on each PC
$lnk = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $App "BRO x BRO Manager.lnk"))
$lnk.TargetPath = Join-Path $App "start_manager.cmd"
$lnk.WorkingDirectory = $App
$lnk.IconLocation = (Join-Path $App "assets\icon.ico") + ",0"
$lnk.WindowStyle = 7  # minimized: no console flash
$lnk.Description = "BRO x BRO SHOP manager app"
$lnk.Save()
Write-Host "Shortcut 'BRO x BRO Manager' created in manager-app (copy it to the desktop if you like)"

# 3. Scheduled task AvitoBot: at logon of this user, no time limit, also on battery, one instance. It starts the tray icon
#    (tray.py, in the manager app's venv: pystray + Pillow); the icon starts the bot (run_manager.cmd) and shows its state.
#    The icon menu item "turn off the bot and close" stops the bot; if the icon dies, Windows kills the bot with it (job object).
$appPyw = Join-Path $App ".venv\Scripts\pythonw.exe"
$action = New-ScheduledTaskAction -Execute $appPyw -Argument "`"$(Join-Path $Root 'tray.py')`"" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive
Register-ScheduledTask -TaskName "AvitoBot" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "Scheduled task AvitoBot registered (starts the tray icon at logon; the icon starts the bot)"

Write-Host ""
Write-Host "Next:"
Write-Host "  1. Tokens: run  manager-app\start_manager.cmd --setup  (admin PIN + keys), or create avito-bot\.env with"
Write-Host "     AVITO_CLIENT_ID=..., AVITO_CLIENT_SECRET=..., TG_BOT_TOKEN=..., OPENAI_API_KEY=..."
Write-Host "  2. Start the bot:  schtasks /Run /TN AvitoBot  (a coloured B icon appears in the tray; pin it via the ^ arrow),"
Write-Host "     then press Start in Telegram, the app or the icon's menu."
