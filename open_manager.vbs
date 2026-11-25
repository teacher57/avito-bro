' Desktop shortcuts "BOT BRO": runs open_manager.cmd with no console window (0 = hidden).
' Arguments (e.g. --setup) are passed on.
Set sh = CreateObject("WScript.Shell")
here = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
args = ""
For Each a In WScript.Arguments
  args = args & " " & a
Next
sh.Run "cmd.exe /c """ & here & "\open_manager.cmd""" & args, 0, False
