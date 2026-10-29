@echo off
chcp 65001 >nul
rem Запуск: start.bat [serve^|run^|check]
rem   serve - панель (по умолчанию), run - сбор из командной строки, check - проверка доступа
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Первый запуск: создаю окружение и ставлю зависимости...
    python -m venv .venv
    if errorlevel 1 (
        echo Не найден Python. Установи его с https://www.python.org/downloads/
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
    ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
    if errorlevel 1 (
        echo Не удалось поставить зависимости
        pause
        exit /b 1
    )
)

set "PYTHONPATH=%~dp0src"
".venv\Scripts\python.exe" -m jobauto %*
set "CODE=%errorlevel%"
if "%~1"=="" pause
exit /b %CODE%
