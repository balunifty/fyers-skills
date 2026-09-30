@echo off
setlocal EnableExtensions

REM =============================================================================
REM Equity 15-minute EMA crossover scanner
REM
REM Default: dry-run (no real order and no account funds required)
REM Usage:
REM   run_equity_ema_crossover_15min.bat
REM   run_equity_ema_crossover_15min.bat once
REM   run_equity_ema_crossover_15min.bat live
REM   run_equity_ema_crossover_15min.bat live once
REM =============================================================================

REM Override PYTHON if Python is not on PATH, for example:
REM set PYTHON=C:\Users\Admin\AppData\Local\Programs\Python\Python311\pythonw.exe
if not defined PYTHON set "PYTHON=pythonw"

for %%I in ("%~dp0..\..") do set "WORKDIR=%%~fI"
set "SCRIPT=%WORKDIR%\strategies\scripts\EquityEma15_10_20_50Crossover15min.py"
set "MODE=--dry-run"
set "ONCE="

if /I "%~1"=="help" goto usage
if /I "%~1"=="live" set "MODE=--live"
if /I "%~1"=="once" set "ONCE=--once"
if /I "%~2"=="once" set "ONCE=--once"

:usage
if /I "%~1"=="help" (
    echo Usage:
    echo   %~nx0                 Run continuous dry-run scanner
    echo   %~nx0 once            Run one dry-run scan and exit
    echo   %~nx0 live            Run continuous live scanner
    echo   %~nx0 live once       Run one live scan and exit
    echo.
    echo Dry-run is the default and does not submit orders.
    pause
    exit /b 0
)

if not exist "%SCRIPT%" (
    echo ERROR: Script not found:
    echo %SCRIPT%
    pause
    exit /b 1
)

echo.
echo ============================================================
echo Equity 15-minute EMA crossover scanner
echo Mode: %MODE% %ONCE%
echo Working directory: %WORKDIR%
echo Excel: %WORKDIR%\strategies\logs\EquityEma15_10_20_50Crossover15min.xlsx
echo ============================================================
echo.

pushd "%WORKDIR%"
"%PYTHON%" "%SCRIPT%" %MODE% %ONCE%
set "EXIT_CODE=%ERRORLEVEL%"
popd

echo.
if not "%EXIT_CODE%"=="0" (
    echo Scanner exited with code %EXIT_CODE%.
) else (
    echo Scanner finished.
)
echo.

pause
exit /b %EXIT_CODE%
