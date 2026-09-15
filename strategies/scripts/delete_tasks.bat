@echo off
REM =============================================================================
REM Delete F&O Trading Scheduled Tasks
REM =============================================================================

echo Deleting scheduled tasks...

schtasks /delete /tn "FO_WebSocket" /f 2>nul
schtasks /delete /tn "FO_SharedDataFetcher" /f 2>nul
schtasks /delete /tn "FO_OrbStrategy" /f 2>nul
schtasks /delete /tn "FO_BuyCallEMA_10_20_30" /f 2>nul
schtasks /delete /tn "FO_BuyCallEMA_10_50" /f 2>nul

echo.
echo All FO trading tasks deleted!
