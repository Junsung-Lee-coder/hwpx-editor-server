@echo off
setlocal
cd /d %~dp0\..

title HWPX Converter - setup_venv

set "PYTHON_CMD="
where py >nul 2>nul
if %ERRORLEVEL%==0 set "PYTHON_CMD=py -3"

if not defined PYTHON_CMD (
    where python >nul 2>nul
    if %ERRORLEVEL%==0 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
    where python3 >nul 2>nul
    if %ERRORLEVEL%==0 set "PYTHON_CMD=python3"
)

if not defined PYTHON_CMD (
    echo Python launcher^(`py`^), `python`, or `python3` was not found in PATH.
    echo Please install Python 3 and enable the PATH option, then run this again.
    pause
    exit /b 1
)

echo Using Python command: %PYTHON_CMD%

if not exist .venv (
    %PYTHON_CMD% -m venv .venv
    if %ERRORLEVEL% neq 0 (
        echo Failed to create virtual environment.
        pause
        exit /b 1
    )
)

if not exist .venv\Scripts\activate.bat (
    echo Virtual environment looks incomplete: .venv\Scripts\activate.bat not found.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
if %ERRORLEVEL% neq 0 (
    echo Failed while upgrading pip.
    pause
    exit /b 1
)

pip install -r requirements.txt
if %ERRORLEVEL% neq 0 (
    echo Failed while installing requirements.
    pause
    exit /b 1
)

if not exist .env (
    if not exist config.example (
        echo config.example was not found; create .env manually before starting the runtime.
    ) else (
        copy config.example .env >nul
        echo Created .env from config.example
    )
)

echo Setup complete.
pause
endlocal
