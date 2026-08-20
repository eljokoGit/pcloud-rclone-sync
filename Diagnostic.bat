@echo off
rem ===========================================================================
rem  Starts pCloud Sync with a visible console.
rem  Use this when double-clicking the normal launcher does nothing.
rem ===========================================================================
setlocal
cd /d "%~dp0"

echo.
echo   pCloud Sync diagnostics
echo   =======================
echo.

if not exist "%~dp0runtime\Scripts\python.exe" (
  echo   The environment is not installed.
  echo   Run "pCloud Sync.bat" first.
  echo.
  pause
  exit /b 1
)

echo   Application Python:
"%~dp0runtime\Scripts\python.exe" --version
echo.

echo   rclone:
where rclone >nul 2>&1 && (rclone version ^| findstr /B rclone) || echo     NOT FOUND
echo.

echo   Configured remotes:
where rclone >nul 2>&1 && (rclone listremotes) || echo     (rclone missing)
echo.

echo   Python components:
"%~dp0runtime\Scripts\python.exe" -c "import fastapi,uvicorn,httpx,yaml;print('    fastapi, uvicorn, httpx, yaml: OK')" 2>&1
"%~dp0runtime\Scripts\python.exe" -c "import webview;print('    pywebview: OK')" 2>&1
"%~dp0runtime\Scripts\python.exe" -c "import pystray;print('    pystray: OK')" 2>&1
echo.

echo   ---------------------------------------------------------------
echo   Starting the application. Errors will show here.
echo   Ctrl+C to stop.
echo   ---------------------------------------------------------------
echo.

"%~dp0runtime\Scripts\python.exe" "%~dp0desktop.py"

echo.
echo   The application stopped.
pause
