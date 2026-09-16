@echo off
taskkill /F /IM pythonw.exe /FI "WINDOWTITLE eq Gemini Dictate" >nul 2>&1
taskkill /F /IM pythonw.exe >nul 2>&1
echo Gemini Dictate stopped.
timeout /t 2 >nul
