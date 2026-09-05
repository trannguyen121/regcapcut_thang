@echo off
setlocal
cd /d "%~dp0"

pyinstaller --noconfirm --clean regcapcut_v7.7.spec
if errorlevel 1 goto :fail

echo Build complete: dist\regcapcut_v7.7\regcapcut.exe
exit /b 0

:fail
echo Build failed.
exit /b 1
