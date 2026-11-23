@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist logs mkdir logs
if not exist .venv\Scripts\python.exe (
  echo Первый запуск: устанавливаю зависимости. Это может занять несколько минут.
  call install.bat --quiet >> logs\startup.log 2>&1
  if errorlevel 1 (
    echo Установка не завершилась. Подробности: logs\startup.log
    pause
    exit /b 1
  )
)
.venv\Scripts\python.exe app.py >> logs\startup.log 2>&1
if errorlevel 1 (
  echo Ошибка запуска. Подробности: logs\startup.log
  pause
)
