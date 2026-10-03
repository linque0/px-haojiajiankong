@echo off
rem pxb7 collection gateway - stop background service
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
".venv\Scripts\python.exe" run.py stop-gateway
pause
