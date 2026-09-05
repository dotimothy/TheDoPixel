@echo off
setlocal

where powershell.exe >nul 2>&1
if errorlevel 1 (
  echo ERROR: Windows PowerShell could not be found.
  exit /b 1
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-windows.ps1" %*
set "INSTALL_EXIT_CODE=%ERRORLEVEL%"

if not "%INSTALL_EXIT_CODE%"=="0" (
  echo.
  echo TheDoPixel installation failed with exit code %INSTALL_EXIT_CODE%.
)

exit /b %INSTALL_EXIT_CODE%
