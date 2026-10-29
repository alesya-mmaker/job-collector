@echo off
chcp 65001 >nul
rem Разовая проба страниц hh.ru.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" call "%~dp0start.bat" check >nul
".venv\Scripts\python.exe" scripts\probe_hh.py
echo.
pause
