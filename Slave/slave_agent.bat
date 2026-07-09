@echo off
REM ============================================================
REM  SLAVE (agent flow) — one double-click per drone-laptop.
REM   1) publishes port 8080 via Tailscale Funnel (player link)
REM   2) runs the agent, which the master dashboard controls
REM      (the dashboard does Start/Stop of the drone code + Wi-Fi)
REM  Do NOT also run slave_launcher.bat — they'd clash on :8080.
REM ============================================================
setlocal

REM Always work from THIS script's own folder — no hardcoded paths.
cd /d "%~dp0"
set "PORT=8080"

REM pyhula lives in the venv, not the system Python. Prefer the venv interpreter.
REM (agent.py launches the controller with this same Python, so it must be right.)
REM Scans for ANY venv folder here (.venv, venv, .venv12, ...); .venv/venv win if present.
set "PY=python"
for /d %%D in (*) do if exist "%%D\Scripts\python.exe" set "PY=%%D\Scripts\python.exe"
if exist "venv\Scripts\python.exe"  set "PY=venv\Scripts\python.exe"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

echo [SETUP] Folder : %CD%
echo [SETUP] Python : %PY%
echo [SETUP] Publishing port %PORT% via Tailscale Funnel...
tailscale funnel --bg %PORT%
tailscale funnel status

:run
echo.
echo [RUN] Starting agent.py ...
"%PY%" agent.py
echo [RUN] agent exited. Restarting in 3s... (close this window to stop)
timeout /t 3 /nobreak >nul
goto run
