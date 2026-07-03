@echo off
setlocal EnableDelayedExpansion

cd /d "%~dp0"

if not exist .venv (
    echo [srltcp] Creating virtual environment...
    python -m venv .venv
)

call .venv\Scripts\activate.bat
pip install -q -e . 2>nul

python -m srltcp %*