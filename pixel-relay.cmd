@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0pixel-relay.ps1" %*
exit /b %ERRORLEVEL%
