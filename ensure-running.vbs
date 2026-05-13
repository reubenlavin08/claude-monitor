' Wrapper for ensure-running.ps1 so the scheduled task does not flash a
' console window every 5 minutes. wscript.exe is windowless, so launching
' powershell.exe with WindowStyle=0 from here avoids the brief cmd flash
' that happens when Task Scheduler starts powershell.exe directly.
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""C:\Users\User\claude-monitor\ensure-running.ps1""", 0, False
Set WshShell = Nothing
