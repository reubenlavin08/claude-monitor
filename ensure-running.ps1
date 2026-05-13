# Idempotent dashboard launcher.
# Called by the ClaudeMonitor scheduled task on logon AND every 5 min.
# If server.py is already serving on :8765, exits silently. Otherwise
# launches start-hidden.vbs (which gives pywinpty a real-but-hidden console).
$ErrorActionPreference = 'SilentlyContinue'
$conn = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($conn) { exit 0 }
$vbs = Join-Path $PSScriptRoot 'start-hidden.vbs'
Start-Process wscript.exe -ArgumentList "`"$vbs`"" -WorkingDirectory $PSScriptRoot -WindowStyle Hidden | Out-Null
