@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Первый запуск: устанавливаю зависимости. Это может занять несколько минут.
  call install.bat --quiet
  if errorlevel 1 (
    pause
    exit /b 1
  )
)
.venv\Scripts\python.exe app.py
if errorlevel 1 pause
