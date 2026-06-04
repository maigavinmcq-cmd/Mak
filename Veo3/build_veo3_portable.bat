@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "PROJECT_DIR=%SCRIPT_DIR%.."
set "PYTHON_EXE=%PROJECT_DIR%\.venv\Scripts\python.exe"
set "PYINSTALLER_EXE=%PROJECT_DIR%\.venv\Scripts\pyinstaller.exe"
set "SPEC_FILE=%PROJECT_DIR%\Veo3Portable.spec"

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Python virtual environment not found:
    echo %PYTHON_EXE%
    pause
    exit /b 1
)

if not exist "%PYINSTALLER_EXE%" (
    echo [ERROR] PyInstaller executable not found:
    echo %PYINSTALLER_EXE%
    pause
    exit /b 1
)

if not exist "%SPEC_FILE%" (
    echo [ERROR] Spec file not found:
    echo %SPEC_FILE%
    pause
    exit /b 1
)

cd /d "%PROJECT_DIR%"
"%PYINSTALLER_EXE%" --noconfirm "%SPEC_FILE%"

if errorlevel 1 (
    echo.
    echo [ERROR] Build failed.
    pause
    exit /b 1
)

echo.
echo [OK] Build finished.
echo Output folder:
echo %PROJECT_DIR%\dist\Veo3Portable
pause
endlocal
