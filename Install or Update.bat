@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Εγκατάσταση ή ενημέρωση απαιτούμενων πακέτων...
python -m pip install --upgrade spotdl mutagen pillow
echo.
echo Ολοκληρώθηκε.
pause
