@echo off
setlocal EnableDelayedExpansion

cd /d "%~dp0"

set "YES=0"
:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="-y" set "YES=1"
if /i "%~1"=="--yes" set "YES=1"
if /i "%~1"=="-h" goto show_help
if /i "%~1"=="--help" goto show_help
shift
goto parse_args

:show_help
echo Usage: uninstall.bat [--yes]
echo Removes %%APPDATA%%\SRLTCP and any custom incoming/shared folders from settings.
exit /b 0

:args_done
if not defined APPDATA (
  echo ERROR: APPDATA is not set.
  exit /b 1
)

set "EXTRA_LIST=%TEMP%\srltcp_uninstall_paths.txt"
if exist "%EXTRA_LIST%" del /f /q "%EXTRA_LIST%" >nul 2>&1

for /f "delims=" %%D in ('python "%~dp0uninstall_paths.py" --data-dir 2^>nul') do set "DATA_DIR=%%D"
if not defined DATA_DIR set "DATA_DIR=%APPDATA%\SRLTCP"

echo SRLTCP uninstall will remove:
echo   %DATA_DIR%\
echo     settings, identities, trusted peers, TLS certs,
echo     uploads, transfers\incoming, shared (defaults)

python "%~dp0uninstall_paths.py" > "%EXTRA_LIST%" 2>nul
if exist "%EXTRA_LIST%" (
  for /f "usebackq delims=" %%P in ("%EXTRA_LIST%") do (
    echo   Custom folder from saved settings:
    echo     %%P\
  )
)

if "%YES%"=="0" (
  set /p CONFIRM="Continue? [y/N] "
  if /i not "!CONFIRM!"=="y" if /i not "!CONFIRM!"=="yes" (
    echo Cancelled.
    if exist "%EXTRA_LIST%" del /f /q "%EXTRA_LIST%" >nul 2>&1
    exit /b 0
  )
)

if exist "%DATA_DIR%" (
  rd /s /q "%DATA_DIR%"
  echo Removed %DATA_DIR%
)

if exist "%EXTRA_LIST%" (
  for /f "usebackq delims=" %%P in ("%EXTRA_LIST%") do (
    if exist "%%P" (
      rd /s /q "%%P"
      echo Removed %%P
    )
  )
  del /f /q "%EXTRA_LIST%" >nul 2>&1
)

echo SRLTCP data removed. Re-run run.bat web for a fresh setup.
exit /b 0