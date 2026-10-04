@echo off
rem pxb7 gateway start/stop for browser extension - register Native Messaging host (HKCU)
rem   usage: 安装网关启停.bat  (writes host manifest + registry; no admin needed)
rem   after install: reload the extension (or wait for self-update), then the popup
rem   shows the「启动网关」button when the gateway is not running.
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
".venv\Scripts\python.exe" run.py native-host --action install
echo.
echo   若自动推导的扩展 ID 与扩展管理页显示不一致，请复制页面上的 ID 后执行：
echo     .venv\Scripts\python.exe run.py native-host --action install --ext-id ^<扩展ID^>
echo.
pause
