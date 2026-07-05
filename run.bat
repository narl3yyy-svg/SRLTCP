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

python -m srltcp %*