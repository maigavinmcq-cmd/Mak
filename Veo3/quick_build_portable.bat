@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "PROJECT_DIR=%SCRIPT_DIR%.."
set "PACK_ENV_DIR=%PROJECT_DIR%\.venv_pack"
set "PYINSTALLER_EXE=%PACK_ENV_DIR%\Scripts\pyinstaller.exe"
set "SPEC_FILE=%PROJECT_DIR%\Veo3Portable.spec"
set "DIST_DIR=%PROJECT_DIR%\dist_pack"
set "BUILD_DIR=%PROJECT_DIR%\build_pack"

if not exist "%PACK_ENV_DIR%\Scripts\python.exe" (
    echo [ERROR] Packaging environment not found. Run this first:
    echo %SCRIPT_DIR%init_pack_env.bat
    pause
    exit /b 1
)

if not exist "%PYINSTALLER_EXE%" (
    echo [ERROR] PyInstaller not found:
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
echo [INFO] Starting fast build...
"%PYINSTALLER_EXE%" --noconfirm --distpath "%DIST_DIR%" --workpath "%BUILD_DIR%" "%SPEC_FILE%"

if errorlevel 1 (
    echo.
    echo [ERROR] Build failed.
    pause
    exit /b 1
)

echo.
echo [OK] Build finished. Output folder:
echo %DIST_DIR%\Veo3Portable
pause
endlocal
