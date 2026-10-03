@echo off
rem create desktop shortcut "pxb7 collection dashboard"
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
".venv\Scripts\python.exe" run.py install-shortcut
pause
