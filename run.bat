@echo off
setlocal EnableDelayedExpansion

cd /d "%~dp0"

if not exist .venv\Scripts\activate.bat (
    echo [srltcp] Creating virtual environment...
    if exist .venv rmdir /s /q .venv
    python -m venv .venv
)

call .venv\Scripts\activate.bat
pip install -q -e . 2>nul

rem Tor (to find contacts on other networks): offer to download it once if missing.
if /i "%~1"=="tor-setup" (
    python -m srltcp.utils.tor_setup --yes
    exit /b !errorlevel!
)
if /i "%~1"=="web" python -m srltcp.utils.tor_setup
if "%~1"=="" python -m srltcp.utils.tor_setup

python -m srltcp %*