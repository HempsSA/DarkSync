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
    echo [!] Not a git repository — attempting to recover...
    echo.

    set "REMOTE_URL=https://github.com/HempsSA/DarkSync.git"

    echo [v] Initializing git repo...
    git init
    git remote add origin "!REMOTE_URL!"
    git fetch origin
    if errorlevel 1 (
        echo [X] Could not connect to remote. Check your internet connection.
        exit /b 1
    )
    git checkout -b main origin/main
    if errorlevel 1 (
        echo [X] Could not set up branch. Try running setup.bat instead.
        exit /b 1
    )
    echo [i] Repo recovered from !REMOTE_URL!
    echo.
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

:: ── Pull with stash if needed ──────────────────────────────────
echo.
set "STASHED=0"
git diff --quiet
if errorlevel 1 (
    echo [v] Local changes detected — stashing before pull...
    git stash push -m "auto-stash by update.bat"
    if errorlevel 1 (
        echo [X] Could not stash local changes.
        exit /b 1
    )
    set "STASHED=1"
)

echo Pulling...
git pull origin "%BRANCH%"
if errorlevel 1 (
    echo [X] Pull failed.
    if "!STASHED!"=="1" (
        echo [v] Restoring stashed changes...
        git stash pop
    )
    exit /b 1
)

:: Restore stashed changes if we stashed
if "!STASHED!"=="1" (
    echo [v] Restoring stashed changes...
    git stash pop
    if errorlevel 1 (
        echo [!] Stash pop had conflicts. Run 'git stash drop' to discard.
    )
)

:: Show updated commit
for /f "tokens=*" %%i in ('git rev-parse --short HEAD') do set "SHORT_SHA=%%i"
echo.
echo [i] Updated to %SHORT_SHA%.
echo.
echo [i] If DarkSync is running, restart it to load the new code.

endlocal
