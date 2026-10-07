@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "NO_PAUSE="
if /I "%~1"=="--no-pause" set "NO_PAUSE=1"

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy.MM.dd-HHmmss"') do set "RELEASE_VERSION=4.0.%%I"

echo Building release %RELEASE_VERSION%...
call build_exe.bat --no-pause
if errorlevel 1 (
    echo.
    echo Release build failed while creating the application.
    if not defined NO_PAUSE pause
    exit /b 1
)

call build_installer.bat "%RELEASE_VERSION%" --no-pause
if errorlevel 1 (
    echo.
    echo Release build failed while creating the installer.
    if not defined NO_PAUSE pause
    exit /b 1
)

echo.
echo Release %RELEASE_VERSION% completed successfully.
echo Previous installers remain in:
echo %~dp0releases
if not defined NO_PAUSE pause
