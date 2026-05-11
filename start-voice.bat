@echo off
REM Launch the voice-alerts daemon. Output -> voice-alerts.log
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe voice-alerts.py >> voice-alerts.log 2>&1
) else (
    python voice-alerts.py >> voice-alerts.log 2>&1
)
