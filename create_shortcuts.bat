@echo off
echo.
echo ========================================
echo   Creating Desktop Shortcuts
echo ========================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_shortcuts.ps1"
