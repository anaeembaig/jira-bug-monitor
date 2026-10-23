@echo off
REM One-time setup on Windows. Run from this folder.
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% -c "import sys; assert sys.version_info >= (3,11), 'Python 3.11+ required'" || exit /b 1
if not exist .venv %PY% -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip -q
.venv\Scripts\python -m pip install -r requirements.txt -q
if not exist config.toml copy bugmonitor\config.example.toml config.toml >nul
.venv\Scripts\python -m bugmonitor install-claude
echo.
echo Setup done. Next: open config.toml, add your Jira site, email, and API token.
echo Then run start.bat
pause
