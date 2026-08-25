@echo off
setlocal enabledelayedexpansion
::
:: DarkSync Updater — Pull latest code from the shared git remote.
:: Usage:
::   update.bat            Pull latest changes
::   update.bat --check    Only check for updates (no pull)
::

:: Navigate to this script's directory
cd /d "%~dp0"

:: Make sure this is a git repo
if not exist ".git" (
    echo [X] Not a git repository. Run 'git init' and add your remote first.
    exit /b 1
)

:: Check for origin remote
for /f "tokens=*" %%i in ('git remote get-url origin 2^>nul') do set "REMOTE=%%i"
if not defined REMOTE (
    echo [X] No 'origin' remote configured.
    echo     Set one with: git remote add origin ^<your-repo-url^>
    exit /b 1
)

:: Get current branch
for /f "tokens=*" %%i in ('git rev-parse --abbrev-ref HEAD') do set "BRANCH=%%i"

echo [i] Repo:     %CD%
echo [i] Branch:   %BRANCH%
echo [i] Remote:   %REMOTE%
echo.

:: Fetch latest
echo [v] Fetching...
git fetch origin
if errorlevel 1 (
    echo [X] Fetch failed. Check your network connection.
    exit /b 1
)

:: Compare local vs remote
for /f "tokens=*" %%i in ('git rev-parse HEAD') do set "LOCAL_SHA=%%i"
for /f "tokens=*" %%i in ('git rev-parse origin/%BRANCH% 2^>nul') do set "REMOTE_SHA=%%i"

if not defined REMOTE_SHA set "REMOTE_SHA=%LOCAL_SHA%"

if "%LOCAL_SHA%"=="%REMOTE_SHA%" (
    echo [i] Already up to date.
    exit /b 0
)

:: Count commits behind
for /f "tokens=*" %%i in ('git rev-list HEAD..origin/%BRANCH% --count') do set "BEHIND=%%i"
echo [v] %BEHIND% commit(s) behind.

:: If --check only, stop here
if "%~1"=="--check" (
    echo     (Use 'update.bat' without --check to pull.)
    exit /b 0
)

echo.
echo Pulling...
git pull origin "%BRANCH%"
if errorlevel 1 (
    echo [X] Pull failed. You may have local conflicts.
    exit /b 1
)

:: Show updated commit
for /f "tokens=*" %%i in ('git rev-parse --short HEAD') do set "SHORT_SHA=%%i"
echo.
echo [i] Updated to %SHORT_SHA%.
echo.
echo [i] If DarkSync is running, restart it to load the new code.

endlocal
