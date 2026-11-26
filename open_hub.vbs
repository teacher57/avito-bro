' Shortcut "HUB BRO" (desktop and the avito folder): runs open_hub.ps1 with no window - starts the hub if it is down,
' brings back the tray icon if it was closed, opens the cabinet.
Set sh = CreateObject("WScript.Shell")
here = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.Run "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File """ & here & "\open_hub.ps1""", 0, False
