@echo off
rem Starts the manager app. Add --setup for the first run on a new PC (admin).
rem If C:\...\claude_trusted\tools exists (taylormomsen), the Flet client and temp files stay inside it (not in the user
rem profile); on any other PC Flet uses its own defaults.
set "TOOLS=%~dp0..\..\tools"
if exist "%TOOLS%\flet-client\flet" set "FLET_VIEW_PATH=%TOOLS%\flet-client\flet"
if exist "%TOOLS%\tmp" set "TEMP=%TOOLS%\tmp"
if exist "%TOOLS%\tmp" set "TMP=%TOOLS%\tmp"
cd /d "%~dp0"
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0app.py" %*
