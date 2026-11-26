# Ярлык «HUB BRO» (рабочий стол и папка avito; запускается через open_hub.vbs, без окна).
# Хаб работает от имени пользователя, без прав администратора, и сам при включении компьютера не стартует — его
# поднимает этот ярлык:
#   - хаб не отвечает (порт 8790) -> запустить hub-bro\server\run-server.cmd (он сам поднимет сервер данных, порт 8791)
#     и подождать до 20 секунд;
#   - значка HUB BRO в трее нет (закрыли) -> запустить hub-bro\server\hub-tray.vbs;
#   - открыть кабинет: адрес компьютера в Tailscale (https://....ts.net), без Tailscale — http://127.0.0.1:8790.
# Если хаб уже работает — сразу открывает кабинет. Лог ярлыка — hub-bro\server\open-hub.log.
# -NoBrowser — всё то же, но кабинет не открывать (так хаб поднимает avito-start.ps1 после обновления).
# Файл сохранён в UTF-8 с BOM: иначе Windows PowerShell 5.1 прочитает русский текст как «кракозябры».
param([switch]$NoBrowser)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms

$Server = Join-Path $PSScriptRoot "hub-bro\server"
$Log = Join-Path $Server "open-hub.log"
$Ts = "$env:ProgramFiles\Tailscale\tailscale.exe"
function Log($t) { try { Add-Content -Path $Log -Value ("{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $t) -Encoding UTF8 } catch {} }
function Listening($port) { [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) }

# 1. хаб
if (-not (Listening 8790)) {
    Log "hub is down, starting"
    Start-Process -WindowStyle Hidden -FilePath (Join-Path $Server "run-server.cmd")
    for ($i = 0; $i -lt 80 -and -not (Listening 8790); $i++) { Start-Sleep -Milliseconds 250 }
    if (-not (Listening 8790)) {
        Log "hub did not start in 20 s"
        [void][System.Windows.Forms.MessageBox]::Show(
            "HUB BRO не запустился за 20 секунд.`n`nЧто случилось — в логе хаба:`n$Server\server.log",
            "HUB BRO", "OK", "Error")
        exit 1
    }
    Log "hub started"
}

# 2. значок в трее (hub-tray.ps1 держит мьютекс Global\avito-hub-bro-tray, пока работает)
$m = $null
if ([System.Threading.Mutex]::TryOpenExisting("Global\avito-hub-bro-tray", [ref]$m)) { $m.Dispose() }
else {
    Log "tray icon is closed, starting"
    Start-Process -FilePath "$env:WINDIR\System32\wscript.exe" -ArgumentList "`"$Server\hub-tray.vbs`""
}

# 3. кабинет
$url = "http://127.0.0.1:8790"
if (Test-Path $Ts) {
    try { $d = ((& $Ts status --json 2>$null | ConvertFrom-Json).Self.DNSName).TrimEnd("."); if ($d) { $url = "https://$d" } } catch {}
}
if ($NoBrowser) { Log "hub up, cabinet not opened (-NoBrowser)"; exit 0 }
Log "open $url"
Start-Process $url
