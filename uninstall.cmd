@echo off
rem Remove BOT BRO and HUB BRO from this PC: double click. Asks for administrator rights, then for confirmation
rem (type DELETE). Details: uninstall.ps1. The script runs from a copy in %TEMP%: it can't delete the folder it runs from.
rem Keep a copy of the shop data on the desktop:  uninstall.cmd -KeepData
set "PS1=%TEMP%\avito-uninstall.ps1"
copy /Y "%~dp0uninstall.ps1" "%PS1%" >nul || (echo Can't copy uninstall.ps1 to %TEMP% & pause & exit /b 1)
set "DIR=%~dp0"
set "DIR=%DIR:~0,-1%"
cd /d "%TEMP%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -NoExit -File \"%PS1%\" -AvitoDir \"%DIR%\" %*'"
