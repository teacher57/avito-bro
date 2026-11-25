@echo off
rem Starts the Avito bot on Windows (no WSL): avito-bot\.venv (setup.cmd creates it). Tokens: the manager app's DPAPI store
rem (manager-app\bot_tokens.py) if it is installed and set up on this PC; anything missing there comes from avito-bot\.env.
cd /d "%~dp0"
set "PY=%~dp0manager-app\.venv\Scripts\python.exe"
if exist "%PY%" for /f "usebackq tokens=1,* delims==" %%A in (`""%PY%" "%~dp0manager-app\bot_tokens.py""`) do set "%%A=%%B"
rem Data server key (avito\data\server\access-key.txt, same as HUB BRO) unless set in .env / the environment.
if not defined AVITO_DATA_KEY if exist "%~dp0..\data\server\access-key.txt" set /p AVITO_DATA_KEY=<"%~dp0..\data\server\access-key.txt"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
rem CA bundle from certifi: the Windows store may lack the root of api.avito.ru (CERTIFICATE_VERIFY_FAILED without it)
if exist "%~dp0.venv\Lib\site-packages\certifi\cacert.pem" set "SSL_CERT_FILE=%~dp0.venv\Lib\site-packages\certifi\cacert.pem"
if not exist live mkdir live
"%~dp0.venv\Scripts\python.exe" -u manager.py >> live\stdout.log 2>&1
