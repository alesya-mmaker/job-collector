@echo off
chcp 65001 >nul
rem Проверка доступа к hh.ru и t.me. Запуск двойным кликом.
call "%~dp0start.bat" check
echo.
pause
