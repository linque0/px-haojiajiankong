@echo off
rem pxb7 collector extension - install (load unpacked) guide for Chromium browsers
rem   usage: 安装浏览器扩展.bat  (opens extension folder + per-browser guided steps)
rem NOTE: browsers ignore chrome:// / edge:// / quark:// URLs passed on the command line,
rem       so this script starts the browser and copies the extension page address to the
rem       clipboard; paste it in the address bar, enable Developer mode, load this folder.
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
".venv\Scripts\python.exe" run.py browser-extension --action install
echo.
echo   正在打开扩展目录（「加载已解压的扩展程序」时选择它）...
start "" "%~dp0extension\pxb7-extension"
echo.
echo   ── 逐浏览器引导（扩展页地址会自动复制到剪贴板：地址栏 Ctrl+V 回车）──
echo.
if not exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" goto :skip_chrome1
call :guide "Google Chrome" "%ProgramFiles%\Google\Chrome\Application\chrome.exe" "chrome://extensions/"
:skip_chrome1
if not exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" goto :skip_chrome2
call :guide "Google Chrome (x86)" "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" "chrome://extensions/"
:skip_chrome2
if not exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" goto :skip_edge1
call :guide "Microsoft Edge" "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" "edge://extensions/"
:skip_edge1
if not exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" goto :skip_edge2
call :guide "Microsoft Edge (x86)" "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" "edge://extensions/"
:skip_edge2
if not exist "%ProgramFiles%\Quark\Quark.exe" goto :skip_quark1
call :guide "夸克浏览器" "%ProgramFiles%\Quark\Quark.exe" "quark://extensions/"
:skip_quark1
if not exist "%ProgramFiles(x86)%\Quark\Quark.exe" goto :skip_quark2
call :guide "夸克浏览器 (x86)" "%ProgramFiles(x86)%\Quark\Quark.exe" "quark://extensions/"
:skip_quark2
goto :done

:guide
echo   ▶ %~1：开发者模式 → 加载已解压的扩展程序 → 选择已打开的 pxb7-extension 目录
echo       正在启动浏览器 …
start "" "%~2"
echo %~3|clip
echo       已复制 %~3 到剪贴板 → 地址栏 Ctrl+V 回车 → 加载解压扩展
echo.
pause
exit /b 0

:done
echo.
echo   提示：位于自定义位置（如 F:\夸克）的浏览器不在引导流程内，请手动启动后
echo         在地址栏输入 quark://extensions（若无「开发者模式」开关：
echo         关于夸克→连点版本号 7 次→夸克实验室→开启「扩展支持」）。
pause
