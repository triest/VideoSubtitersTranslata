@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python не найден. Установите Python 3.10–3.12 с python.org и включите Add Python to PATH.
  pause
  exit /b 1
)
py -3 -m venv .venv
if errorlevel 1 goto :error
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
if errorlevel 1 goto :error
pip install -r requirements.txt
if errorlevel 1 goto :error
echo.
echo Установка завершена. Теперь запустите start.bat
pause
exit /b 0
:error
echo.
echo Не удалось установить зависимости. Проверьте интернет-соединение и версию Python.
pause
exit /b 1
