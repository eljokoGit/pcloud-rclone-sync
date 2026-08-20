@echo off
rem ===========================================================================
rem  pCloud Sync - launcher
rem
rem  First start: installs what is needed into a local folder.
rem  After that: starts directly, without a window.
rem ===========================================================================
setlocal
cd /d "%~dp0"

set "VENV=%~dp0runtime"
set "PYW=%VENV%\Scripts\pythonw.exe"
set "PY=%VENV%\Scripts\python.exe"

if exist "%PYW%" goto launch

rem --- first installation -----------------------------------------------------
echo.
echo   pCloud Sync - first installation
echo   ================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo   Python was not found.
  echo.
  echo   Install it with this command in PowerShell:
  echo       winget install Python.Python.3.12
  echo.
  echo   Then run this file again.
  echo.
  pause
  exit /b 1
)

echo   Preparing the environment...
python -m venv "%VENV%"
if errorlevel 1 (
  echo   Failed to create the environment.
  pause
  exit /b 1
)

echo   Installing components ^(a few minutes^)...
"%PY%" -m pip install --quiet --disable-pip-version-check --upgrade pip
"%PY%" -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo   Failed to install the components.
  pause
  exit /b 1
)

where rclone >nul 2>&1
if errorlevel 1 (
  echo.
  echo   rclone was not found. It does the transfer work.
  echo.
  echo   Install it with:
  echo       winget install Rclone.Rclone
  echo.
  echo   Then connect pCloud with:
  echo       rclone config
  echo.
  pause
)

echo.
echo   Installation finished.
echo.

rem --- launch ------------------------------------------------------------------
:launch
start "" "%PYW%" "%~dp0desktop.py"
exit /b 0
