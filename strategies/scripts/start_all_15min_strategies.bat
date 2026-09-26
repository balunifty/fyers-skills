@echo off
setlocal

REM Dry-run scheduler by default. Use "live" only for intentional live orders.
REM One task now covers every 15-minute strategy, instead of one per script.
set "PS_ARGS=%*"
if /I "%~1"=="live" (
    set "PS_ARGS=-Live"
    if /I "%~2"=="once" set "PS_ARGS=-Live -Once"
)
if /I "%~1"=="once" set "PS_ARGS=-Once"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_all_15min_strategies.ps1" %PS_ARGS%
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
