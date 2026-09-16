@echo off
:: INSTALL_GEMINIDICTATE.bat
:: Creates Desktop and Start Menu shortcuts for GEMINIDICTATE.
:: Safe to run more than once. Does NOT start the application.
:: Run this once after cloning/moving the project to a new location.

setlocal EnableDelayedExpansion

:: Resolve project directory from this script's own location — portable, no hardcoded paths
set "PROJ_DIR=%~dp0"
:: Strip trailing backslash
if "!PROJ_DIR:~-1!"=="\" set "PROJ_DIR=!PROJ_DIR:~0,-1!"

set "VBS=!PROJ_DIR!\START_Gemini_Dictate_HIDDEN.vbs"
set "ICO=!PROJ_DIR!\geminidictate.ico"

:: Validate required files exist
if not exist "!VBS!" (
    echo ERROR: START_Gemini_Dictate_HIDDEN.vbs not found in !PROJ_DIR!
    pause
    exit /b 1
)
if not exist "!ICO!" (
    echo WARNING: geminidictate.ico not found. Shortcuts will use default icon.
    set "ICO="
)

:: Resolve shortcut destinations using shell folders — no hardcoded usernames
for /f "delims=" %%D in ('powershell -NoProfile -Command "[System.Environment]::GetFolderPath(\"Desktop\")"') do set "DESKTOP=%%D"
for /f "delims=" %%P in ('powershell -NoProfile -Command "[System.Environment]::GetFolderPath(\"Programs\")"') do set "PROGRAMS=%%P"

set "LNK_DESKTOP=!DESKTOP!\GEMINIDICTATE.lnk"
set "LNK_STARTMENU=!PROGRAMS!\GEMINIDICTATE.lnk"

:: Build PowerShell script to create both shortcuts
:: WshShell.CreateShortcut handles .lnk natively — no extra tools needed
powershell -NoProfile -Command ^
    "$ws = New-Object -ComObject WScript.Shell; " ^
    "$lnk = $ws.CreateShortcut('!LNK_DESKTOP:\=\\!'); " ^
    "$lnk.TargetPath = 'wscript.exe'; " ^
    "$lnk.Arguments = '\"!VBS:\=\\!\"'; " ^
    "$lnk.WorkingDirectory = '!PROJ_DIR:\=\\!'; " ^
    "$lnk.Description = 'GEMINIDICTATE — AI speech-to-text dictation overlay'; " ^
    "if ('!ICO!' -ne '') { $lnk.IconLocation = '!ICO:\=\\!,0' }; " ^
    "$lnk.WindowStyle = 7; " ^
    "$lnk.Save(); " ^
    "Write-Host 'Desktop shortcut created: !LNK_DESKTOP:\=\\!'"

powershell -NoProfile -Command ^
    "$ws = New-Object -ComObject WScript.Shell; " ^
    "$lnk = $ws.CreateShortcut('!LNK_STARTMENU:\=\\!'); " ^
    "$lnk.TargetPath = 'wscript.exe'; " ^
    "$lnk.Arguments = '\"!VBS:\=\\!\"'; " ^
    "$lnk.WorkingDirectory = '!PROJ_DIR:\=\\!'; " ^
    "$lnk.Description = 'GEMINIDICTATE — AI speech-to-text dictation overlay'; " ^
    "if ('!ICO!' -ne '') { $lnk.IconLocation = '!ICO:\=\\!,0' }; " ^
    "$lnk.WindowStyle = 7; " ^
    "$lnk.Save(); " ^
    "Write-Host 'Start Menu shortcut created: !LNK_STARTMENU:\=\\!'"

echo.
echo INSTALL COMPLETE.
echo Desktop shortcut : !LNK_DESKTOP!
echo Start Menu entry : !LNK_STARTMENU!
echo.
echo NOTE: If you move the project folder, re-run this script from the new location.
echo.
endlocal
