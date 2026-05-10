@echo off
REM Launch the dashboard server. Output is redirected to server.log so this
REM is safe to call from Task Scheduler with the cmd window hidden — there's
REM no risk of the process exiting because stdout has nowhere to go.
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe server.py >> server.log 2>&1
) else (
    python server.py >> server.log 2>&1
)
