@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "QUIET="
if /i "%~1"=="--quiet" set "QUIET=1"
where py >nul 2>nul
if errorlevel 1 goto :python_missing
set "PYVER=3.11"
py -3.11 --version >nul 2>nul
if errorlevel 1 set "PYVER=3.10"
py -%PYVER% --version >nul 2>nul
if errorlevel 1 goto :python_missing
py -%PYVER% -m venv .venv
if errorlevel 1 goto :error
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
if errorlevel 1 goto :error
pip install -r requirements.txt
if errorlevel 1 goto :error
echo.
echo Установка завершена. Теперь запустите start.bat
if not defined QUIET pause
exit /b 0
:python_missing
echo Python 3.11 или 3.10 не найден. Установите его с python.org и включите Add Python to PATH.
if not defined QUIET pause
exit /b 1
:error
echo.
echo Не удалось установить зависимости. Проверьте интернет-соединение и версию Python.
if not defined QUIET pause
exit /b 1
