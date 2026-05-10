' Launch start.bat with a real console window, but hide it (WindowStyle=0).
' This is what the Scheduled Task points at — pywinpty needs a real console
' to attach a PTY for the spawned claude.cmd child.
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """C:\Users\User\claude-monitor\start.bat""", 0, False
Set WshShell = Nothing
