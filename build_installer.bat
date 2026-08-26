@echo off
REM ──────────────────────────────────────────────────────────────────
REM build_installer.bat
REM
REM Compiles the DarkSync Inno Setup installer.
REM Requires Inno Setup 6+ to be installed.
REM
REM Usage:
REM Double-click this file, or run from a Developer Command Prompt.
REM ──────────────────────────────────────────────────────────────────
setlocal
set "SCRIPT_DIR=%~dp0"
set "ISS_FILE=%SCRIPT_DIR%installer\DarkSync.iss"

REM ── Find iscc.exe (Inno Setup Compiler) ──
set "ISCC="
REM Check common install paths
for %%P in (
    "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
    "%ProgramFiles%\Inno Setup 6\ISCC.exe"
    "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
) do (
    if exist %%P (
        set "ISCC=%%~P"
        goto :found
    )
)

REM Check PATH
where iscc >nul 2>&1
if %errorlevel%==0 (
    set "ISCC=iscc"
    goto :found
)

REM ── Not found ──
echo.
echo Inno Setup 6 was not found on this system.
echo Download it from: https://jrsoftware.org/isinfo.php
echo (Free, open-source, no registration required)
echo.
pause
exit /b 1

:found
echo Using: %ISCC%
echo Script: %ISS_FILE%
echo.
"%ISCC%" "%ISS_FILE%"
if %errorlevel%==0 (
    echo.
    echo ========================================
    echo Installer built successfully!
    echo Output is in the dist\ subfolder.
    echo ========================================
) else (
    echo.
    echo Build failed with exit code %errorlevel%.
)
echo.
pause
