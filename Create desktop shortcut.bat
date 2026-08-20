@echo off
rem Creates a desktop shortcut with the application icon.
setlocal
set "TARGET=%~dp0pCloud Sync.bat"
set "ICON=%~dp0app\static\icon.ico"
set "LINK=%USERPROFILE%\Desktop\pCloud Sync.lnk"

powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%LINK%');" ^
  "$s.TargetPath='%TARGET%';" ^
  "$s.WorkingDirectory='%~dp0';" ^
  "$s.IconLocation='%ICON%';" ^
  "$s.WindowStyle=7;" ^
  "$s.Description='Backup to pCloud';" ^
  "$s.Save()"

if errorlevel 1 (
  echo   Failed to create the shortcut.
) else (
  echo   Shortcut created on the Desktop.
)
timeout /t 3 >nul
