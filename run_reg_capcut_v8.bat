@echo off
chcp 65001 >nul
title Reg CapCut v8

echo Starting Reg CapCut v8...
echo.

cd /d "%~dp0dist\regcapcut_v8"
start "" "regcapcut.exe"
