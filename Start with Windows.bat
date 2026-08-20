@echo off
rem Adds or removes pCloud Sync from the Windows automatic startup.
setlocal
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LINK=%STARTUP%\pCloud Sync.lnk"

if exist "%LINK%" (
  del "%LINK%"
  echo   pCloud Sync will no longer start with Windows.
) else (
  powershell -NoProfile -Command ^
    "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%LINK%');" ^
    "$s.TargetPath='%~dp0pCloud Sync.bat';" ^
    "$s.WorkingDirectory='%~dp0';" ^
    "$s.IconLocation='%~dp0app\static\icon.ico';" ^
    "$s.WindowStyle=7;" ^
    "$s.Save()"
  echo   pCloud Sync will start with Windows.
)
timeout /t 3 >nul
