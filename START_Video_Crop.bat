@echo off
cd /d "%~dp0"
pythonw video_crop.py
if errorlevel 1 (
    python video_crop.py
    pause
)
