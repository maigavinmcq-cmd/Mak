@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "PROJECT_DIR=%SCRIPT_DIR%.."
set "PYTHON_EXE=%PROJECT_DIR%\.venv\Scripts\python.exe"
set "APP_FILE=%SCRIPT_DIR%Veo3Generated.py"

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Python virtual environment not found:
    echo %PYTHON_EXE%
    echo.
    echo Expected project venv at .venv\Scripts\python.exe
    pause
    exit /b 1
)

if not exist "%APP_FILE%" (
    echo [ERROR] App file not found:
    echo %APP_FILE%
    pause
    exit /b 1
)

cd /d "%PROJECT_DIR%"
start "" http://localhost:8501
"%PYTHON_EXE%" -m streamlit run "%APP_FILE%"

if errorlevel 1 (
    echo.
    echo [ERROR] Streamlit exited with an error.
    pause
)

endlocal
