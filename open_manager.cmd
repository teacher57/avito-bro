@echo off
rem Ярлык «BOT BRO» (рабочий стол и папка avito; запускается через open_manager.vbs, без окна): поднимает сервер данных (если ещё не запущен) и открывает приложение
rem менеджера. Сервер данных остаётся работать в фоне (без окна), лог — data\server\data.log.
rem На новом компьютере приложение само откроет первую настройку (PIN администратора и токены).
cd /d "%~dp0"
rem Ключ сервера данных — тот же, что вводят в HUB BRO.
if exist "%~dp0data\server\access-key.txt" set /p AVITO_DATA_KEY=<"%~dp0data\server\access-key.txt"
netstat -ano | findstr /R /C:"127.0.0.1:8791 .*LISTENING" >nul
if errorlevel 1 (
  powershell -NoProfile -Command "Start-Process -WindowStyle Hidden -FilePath '%~dp0data\server\run-data.cmd'"
  powershell -NoProfile -Command "for ($i=0; $i -lt 20; $i++) { if (Get-NetTCPConnection -LocalPort 8791 -State Listen -ErrorAction SilentlyContinue) { break }; Start-Sleep -Milliseconds 250 }"
)
call "%~dp0bot\manager-app\start_manager.cmd" %*
