@echo off
rem Installs the bot + manager app on this PC (see setup.ps1). Double-click it, or run from cmd.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
pause
