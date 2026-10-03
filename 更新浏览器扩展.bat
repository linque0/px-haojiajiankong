@echo off
rem pxb7 collector extension - update guide for EVERY installed Chromium browser
rem   usage: 更新浏览器扩展.bat                        (guided: standard install locations)
rem         更新浏览器扩展.bat "exe路径" "扩展页地址"   (custom location, e.g. Quark on F:\)
rem NOTE: Chrome/Edge/Quark ignore chrome:// / edge:// / quark:// URLs passed on the
rem       command line (verified), so this script starts the browser, copies the
rem       extension page address to the clipboard, and you paste it in the address bar.
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
".venv\Scripts\python.exe" run.py browser-extension --action update
echo.
if not "%~1"=="" goto :custom

echo   ── 逐浏览器引导（扩展页地址会自动复制到剪贴板：地址栏 Ctrl+V 回车 → 点「重新加载」）──
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
if not exist "%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe" goto :skip_brave
call :guide "Brave" "%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe" "brave://extensions/"
:skip_brave
if not exist "%ProgramFiles%\Vivaldi\Application\vivaldi.exe" goto :skip_vivaldi
call :guide "Vivaldi" "%ProgramFiles%\Vivaldi\Application\vivaldi.exe" "vivaldi://extensions/"
:skip_vivaldi
if not exist "%ProgramFiles%\Tencent\QQBrowser\QQBrowser.exe" goto :skip_qq1
call :guide "QQ 浏览器" "%ProgramFiles%\Tencent\QQBrowser\QQBrowser.exe" "qqbrowser://extensions/"
:skip_qq1
if not exist "%ProgramFiles(x86)%\Tencent\QQBrowser\QQBrowser.exe" goto :skip_qq2
call :guide "QQ 浏览器 (x86)" "%ProgramFiles(x86)%\Tencent\QQBrowser\QQBrowser.exe" "qqbrowser://extensions/"
:skip_qq2
goto :done

:custom
if "%~2"=="" (
  echo   [custom] 用法: "%~nx0" "浏览器exe路径" "扩展页地址"
  echo   [custom] 例：  "%~nx0" "F:\夸克\Quark\quark.exe" "quark://extensions/"
  goto :done
)
call :guide "%~1" "%~1" "%~2"
goto :done

:guide
echo   ▶ %~1
echo       正在启动浏览器 …
start "" "%~2"
echo %~3|clip
echo       已复制 %~3 到剪贴板 → 地址栏 Ctrl+V 回车 → 点「重新加载」
echo.
pause
exit /b 0

:done
echo.
echo   提示：位于自定义位置（如 F:\夸克）的浏览器不在引导流程内，可用：
echo         更新浏览器扩展.bat "F:\夸克\Quark\quark.exe" "quark://extensions/"
echo   扩展升级到 v0.3.0 后支持自更新，以后无需再手动重载。
echo.
pause
