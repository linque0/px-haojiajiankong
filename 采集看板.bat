@echo off
rem pxb7 collection dashboard - double click to open (auto-starts background service)
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
".venv\Scripts\python.exe" run.py dashboard
if errorlevel 1 pause
