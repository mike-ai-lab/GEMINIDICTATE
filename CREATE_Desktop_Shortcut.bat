@echo off
set SCRIPT_DIR=%~dp0
set SHORTCUT=%USERPROFILE%\Desktop\Video Crop Tool.lnk
set TARGET=%SCRIPT_DIR%START_Video_Crop.bat

powershell -NoProfile -Command ^
  "$ws = New-Object -ComObject WScript.Shell; ^
   $s = $ws.CreateShortcut('%SHORTCUT%'); ^
   $s.TargetPath = '%TARGET%'; ^
   $s.WorkingDirectory = '%SCRIPT_DIR%'; ^
   $s.Description = 'Video Crop Tool'; ^
   $s.Save()"

echo Desktop shortcut created: %SHORTCUT%
pause
