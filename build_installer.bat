@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "RELEASE_VERSION=%~1"
if not defined RELEASE_VERSION (
    for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy.MM.dd-HHmmss"') do set "RELEASE_VERSION=4.0.%%I"
)
set "RELEASE_NAME=SpotDL_GUI_Pro_v4_Setup_%RELEASE_VERSION%"
set "SOURCE_DIR=%~3"
if not defined SOURCE_DIR set "SOURCE_DIR=dist\SpotDL GUI Pro v4"

if not exist "%SOURCE_DIR%\SpotDL GUI Pro v4.exe" (
    echo EXE not found.
    echo Run build_exe.bat first.
    if /I not "%~2"=="--no-pause" pause
    exit /b 1
)

set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo Inno Setup 6 was not found.
    echo Install Inno Setup 6 and run this file again.
    if /I not "%~2"=="--no-pause" pause
    exit /b 1
)

if not exist installer mkdir installer
if not exist releases mkdir releases
"%ISCC%" /DMyAppVersion=%RELEASE_VERSION% /DMyOutputDir=releases /DMyOutputBaseFilename=%RELEASE_NAME% "/DMySourceDir=%SOURCE_DIR%" installer.iss
if errorlevel 1 (
    echo.
    echo Installer build failed.
    if /I not "%~2"=="--no-pause" pause
    exit /b 1
)

copy /y "releases\%RELEASE_NAME%.exe" "installer\SpotDL_GUI_Pro_v4_Setup.exe" >nul

echo.
echo Versioned installer:
echo %~dp0releases\%RELEASE_NAME%.exe
echo.
echo Latest installer:
echo %~dp0installer\SpotDL_GUI_Pro_v4_Setup.exe
if /I not "%~2"=="--no-pause" pause
