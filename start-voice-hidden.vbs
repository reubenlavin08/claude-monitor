' Launch start-voice.bat hidden. Uses a real console so child process stays alive.
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """C:\Users\User\claude-monitor\start-voice.bat""", 0, False
Set WshShell = Nothing
