@echo off
chcp 65001 >nul
title Building Reg CapCut v8

echo ========================================
echo   Building Reg CapCut v8
echo ========================================
echo.

pyinstaller regcapcut_v8.spec

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ========================================
    echo   Build completed successfully!
    echo   Output: dist\regcapcut_v8\
    echo ========================================
    pause
) else (
    echo.
    echo ========================================
    echo   Build failed!
    echo ========================================
    pause
    exit /b 1
)
