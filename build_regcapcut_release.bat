@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not exist "BUILD_VERSION.txt" (
    echo ERROR: Khong tim thay BUILD_VERSION.txt
    pause
    exit /b 1
)

set /p REGCAPCUT_VERSION=<BUILD_VERSION.txt
if "%REGCAPCUT_VERSION%"=="" (
    echo ERROR: BUILD_VERSION.txt dang de trong
    pause
    exit /b 1
)

title Building Reg CapCut v%REGCAPCUT_VERSION%
echo ========================================
echo   Building Reg CapCut v%REGCAPCUT_VERSION%
echo ========================================
echo.

python -m PyInstaller --noconfirm --clean regcapcut_release.spec

if errorlevel 1 (
    echo.
    echo ========================================
    echo   Build failed
    echo ========================================
    pause
    exit /b 1
)

if not exist "dist\chrome-152\chrome.exe" (
    echo.
    echo ERROR: Khong tim thay dist\chrome-152\chrome.exe
    echo Ban build da tao xong nhung chua co Chrome 152 de dong goi.
    pause
    exit /b 1
)

echo Copying Chrome 152 into release folder...
robocopy "dist\chrome-152" "dist\regcapcut_v%REGCAPCUT_VERSION%\chrome-152" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (
    echo ERROR: Khong the sao chep Chrome 152 vao ban build
    pause
    exit /b 1
)

echo.
echo ========================================
echo   Build completed successfully
echo   EXE: dist\regcapcut_v%REGCAPCUT_VERSION%\regcapcut_v%REGCAPCUT_VERSION%.exe
echo ========================================
pause
