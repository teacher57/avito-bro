# Запуск хаба и/или сервера данных в сеансе пользователя Windows — его делает задача «AvitoStart» (deploy.sh после
# обновления: запущенное по SSH умерло бы вместе с сеансом). Что запускать — в файле .start-request рядом
# (слова через запятую): hub — хаб, как ярлык «HUB BRO», только без браузера (он сам поднимает и сервер данных);
# data — только сервер данных. Файл после запуска удаляется. Лог — avito-start.log.
$ErrorActionPreference = "Continue"
$A = $PSScriptRoot
$Req = Join-Path $A ".start-request"
$Log = Join-Path $A "avito-start.log"
function Log($t) { try { Add-Content -Path $Log -Value ("{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $t) -Encoding UTF8 } catch {} }
function Listening($port) { [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) }

$want = if (Test-Path $Req) { ((Get-Content $Req -Raw) -split "[,\s]+") | Where-Object { $_ } } else { @() }
Remove-Item $Req -Force -ErrorAction SilentlyContinue
Log "request: $($want -join ', ')"
if ($want -contains "hub" -and (Test-Path (Join-Path $A "open_hub.ps1"))) {
    & powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File (Join-Path $A "open_hub.ps1") -NoBrowser
    Log "hub: $(if (Listening 8790) { 'up' } else { 'DOWN' })"
}
if ($want -contains "data" -and -not (Listening 8791)) {
    Start-Process -WindowStyle Hidden -FilePath (Join-Path $A "data\server\run-data.cmd")
    for ($i = 0; $i -lt 40 -and -not (Listening 8791); $i++) { Start-Sleep -Milliseconds 250 }
    Log "data server: $(if (Listening 8791) { 'up' } else { 'DOWN' })"
}
