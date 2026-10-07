@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo Installing build tools...
python -m pip install --upgrade pyinstaller spotdl mutagen pillow ttkbootstrap
if errorlevel 1 (
    echo.
    echo Build tools install failed.
    if /I not "%~1"=="--no-pause" pause
    exit /b 1
)

echo.
echo Cleaning old build folders...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo.
echo Building GUI app...
python -m PyInstaller --noconfirm --clean --windowed --onedir --name "SpotDL GUI Pro v4" --add-data "spotdl_cli_entry.py;." --add-data "yt_dlp_no_window.py;." --collect-all spotdl --collect-all yt_dlp --collect-all ytmusicapi --collect-all ttkbootstrap --collect-all pykakasi spotdl_gui_v4.py
if errorlevel 1 (
    echo.
    echo GUI build failed.
    if /I not "%~1"=="--no-pause" pause
    exit /b 1
)

echo.
echo Build spotdl-cli.exe...
python -m PyInstaller --noconfirm --clean --console --onefile --name spotdl-cli --collect-all spotdl --collect-all yt_dlp --collect-all ytmusicapi --collect-all pykakasi spotdl_cli_entry.py
if errorlevel 1 (
    echo.
    echo spotdl-cli build failed.
    if /I not "%~1"=="--no-pause" pause
    exit /b 1
)

if exist "dist\spotdl-cli.exe" move /y "dist\spotdl-cli.exe" "dist\SpotDL GUI Pro v4\spotdl-cli.exe" >nul

echo.
echo Build completed:
echo %~dp0dist\SpotDL GUI Pro v4\SpotDL GUI Pro v4.exe
if /I not "%~1"=="--no-pause" pause
