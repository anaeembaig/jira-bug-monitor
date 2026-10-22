@echo off
cd /d "%~dp0"
.venv\Scripts\python -m bugmonitor run %*
