@echo off
rem Native Messaging host launcher (spawned by the browser, no window).
rem stdout must carry ONLY the protocol bytes: keep this file ASCII-only,
rem no chcp and no extra output lines (Chinese comments here would be
rem mis-parsed under the ANSI codepage and could leak into stdout).
set "ROOT=%~dp0..\.."
set PYTHONUTF8=1
"%ROOT%\.venv\Scripts\python.exe" "%~dp0pxb7_gateway_host.py"
