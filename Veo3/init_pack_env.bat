@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "PROJECT_DIR=%SCRIPT_DIR%.."
set "PACK_ENV_DIR=%PROJECT_DIR%\.venv_pack"
set "PYTHON_BASE=python"
set "REQ_FILE=%SCRIPT_DIR%requirements-pack.txt"

if not exist "%REQ_FILE%" (
    echo [ERROR] Missing requirements file:
    echo %REQ_FILE%
    pause
    exit /b 1
)

if not exist "%PACK_ENV_DIR%\Scripts\python.exe" (
    echo [INFO] Creating packaging virtual environment...
    %PYTHON_BASE% -m venv "%PACK_ENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

echo [INFO] Upgrading pip...
"%PACK_ENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
    echo [ERROR] Failed to upgrade pip.
    pause
    exit /b 1
)

echo [INFO] Installing packaging dependencies...
"%PACK_ENV_DIR%\Scripts\python.exe" -m pip install -r "%REQ_FILE%"
if errorlevel 1 (
    echo [ERROR] Failed to install packaging dependencies.
    pause
    exit /b 1
)

echo.
echo [OK] Packaging environment is ready:
echo %PACK_ENV_DIR%
pause
endlocal
