@echo off
setlocal

REM Read-only dashboard: it scans and ranks signals but never places orders.
if not defined PYTHON set "PYTHON=python"

"%PYTHON%" "%~dp0equity_strategy_dashboard.py" --open-browser %*
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%EXIT_CODE%"=="0" (
    echo Dashboard exited with code %EXIT_CODE%.
) else (
    echo Dashboard stopped.
)
echo.
pause
exit /b %EXIT_CODE%
