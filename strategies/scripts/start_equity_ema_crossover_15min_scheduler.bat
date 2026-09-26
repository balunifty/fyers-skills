@echo off
setlocal

REM Dry-run scheduler by default. Use "live" or "-Live" only for intentional live orders.
set "PS_ARGS=%*"
if /I "%~1"=="live" (
    set "PS_ARGS=-Live"
    if /I "%~2"=="once" set "PS_ARGS=-Live -Once"
)
if /I "%~1"=="once" set "PS_ARGS=-Once"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_equity_ema_crossover_15min_scheduler.ps1" %PS_ARGS%
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%EXIT_CODE%"=="0" (
    echo Scheduler exited with code %EXIT_CODE%.
) else (
    echo Scheduler finished.
)
echo.
pause
exit /b %EXIT_CODE%
