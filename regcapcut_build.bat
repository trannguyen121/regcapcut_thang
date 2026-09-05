@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "APP_NAME=regcapcut"
set "DIST_DIR=dist"

if not exist "%DIST_DIR%" mkdir "%DIST_DIR%"
if not exist "export" mkdir "export"
echo Building %DIST_DIR%\%APP_NAME%\%APP_NAME%.exe with PyInstaller...
python -m PyInstaller --noconfirm --clean regcapcut.spec
if errorlevel 1 goto :fail

echo Build complete: %DIST_DIR%\%APP_NAME%\%APP_NAME%.exe
echo.
echo Runtime inputs:
echo   account.txt format: email^|passmail^|refresh_token^|client_id
echo   Run %DIST_DIR%\%APP_NAME%\%APP_NAME%.exe to open the Reg CapCut GUI.
exit /b 0

:fail
echo Build failed.
exit /b 1
