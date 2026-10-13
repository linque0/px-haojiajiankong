@echo off
rem Unified administrator dashboard; legacy collector remains a fallback.
chcp 65001 >nul
if exist "%~dp0data\analysis\valuation\wuwa-dashboard\bootstrap.ps1" (
  call "%~dp0监控采集看板.bat"
  exit /b
)
cd /d "%~dp0"
set PYTHONUTF8=1
".venv\Scripts\python.exe" run.py dashboard
if errorlevel 1 pause
