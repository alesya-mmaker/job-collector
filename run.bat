@echo off
chcp 65001 >nul
rem Сбор вакансий без панели. Запуск двойным кликом.
call "%~dp0start.bat" run
echo.
pause
