@echo off
REM =============================================================================
REM Schedule F&O Trading Scripts - Market Hours (9:15 AM to 3:00 PM)
REM Run as Administrator: Right-click > Run as administrator
REM =============================================================================

set PYTHON=C:\Users\Admin\AppData\Local\Programs\Python\Python311\pythonw.exe
set WORKDIR=C:\Users\Admin\fyersAutomation\fyers-skills

echo.
echo Deleting existing tasks...
schtasks /delete /tn "FO_WebSocket" /f 2>nul
schtasks /delete /tn "FO_SharedDataFetcher" /f 2>nul
schtasks /delete /tn "FO_OrbStrategy" /f 2>nul
schtasks /delete /tn "FO_BuyCallEMA_10_20_30" /f 2>nul
schtasks /delete /tn "FO_BuyCallEMA_10_50" /f 2>nul

echo.
echo Creating scheduled tasks (running as current user)...

REM Task 1: WebSocket - Long-running process during market hours
schtasks /create /tn "FO_WebSocket" /tr "\"%PYTHON%\" \"%WORKDIR%\strategies\utils\websocketNiftyfno100.py\"" /sc daily /st 09:15 /et 15:00 /f
if %errorLevel% neq 0 echo ERROR: Failed to create FO_WebSocket

REM Task 2: Shared Data Fetcher - Runs every 15 minutes
schtasks /create /tn "FO_SharedDataFetcher" /tr "\"%PYTHON%\" \"%WORKDIR%\strategies\utils\shared_data_fetcher.py\"" /sc minute /mo 15 /st 09:15 /et 15:00 /f
if %errorLevel% neq 0 echo ERROR: Failed to create FO_SharedDataFetcher

REM Task 3: ORB Strategy - Runs every 5 minutes
schtasks /create /tn "FO_OrbStrategy" /tr "\"%PYTHON%\" \"%WORKDIR%\strategies\scripts\OrbStrategyCallPut.py\" --live" /sc minute /mo 5 /st 09:15 /et 15:00 /f
if %errorLevel% neq 0 echo ERROR: Failed to create FO_OrbStrategy

REM Task 4: BuyCallOption EMA 10/20/30 - Runs every 5 minutes
schtasks /create /tn "FO_BuyCallEMA_10_20_30" /tr "\"%PYTHON%\" \"%WORKDIR%\strategies\scripts\BuyCallOption102030EmaCrossover5min.py\" --live" /sc minute /mo 5 /st 09:15 /et 15:00 /f
if %errorLevel% neq 0 echo ERROR: Failed to create FO_BuyCallEMA_10_20_30

REM Task 5: BuyCallOption EMA 10/50 - Runs every 5 minutes
schtasks /create /tn "FO_BuyCallEMA_10_50" /tr "\"%PYTHON%\" \"%WORKDIR%\strategies\scripts\BuyCallOptionEma10_50Crossover.py\" --live" /sc minute /mo 5 /st 09:15 /et 15:00 /f
if %errorLevel% neq 0 echo ERROR: Failed to create FO_BuyCallEMA_10_50

echo.
echo ============================================================
echo Verifying tasks:
echo ============================================================
schtasks /query /tn "FO_*" /fo table
echo.
echo NOTE: Tasks run only when you are logged in.
echo.
pause
