<#
.SYNOPSIS
    Run the equity 15-minute EMA crossover scanner every 15 minutes from 09:15
    through 15:15 IST.

.DESCRIPTION
    Dry-run is the default. Use -Live only when real FYERS orders are intended.
    Each scheduled invocation uses the Python script's --once mode, so no
    overlapping scanner processes are created. The Python script evaluates the
    latest completed 15-minute candle and upserts matched signals to Excel.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\run_equity_ema_crossover_15min_scheduler.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\run_equity_ema_crossover_15min_scheduler.ps1 -Live

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\run_equity_ema_crossover_15min_scheduler.ps1 -Once
#>
[CmdletBinding()]
param(
    [switch]$Live,
    [switch]$Once,
    [string]$Python = "",
    [string]$WorkspaceRoot = ""
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$defaultWorkspace = Split-Path -Parent (Split-Path -Parent $scriptDir)
if ([string]::IsNullOrWhiteSpace($WorkspaceRoot)) {
    $WorkspaceRoot = $defaultWorkspace
}
$WorkspaceRoot = [System.IO.Path]::GetFullPath($WorkspaceRoot)

$scriptPath = Join-Path $WorkspaceRoot "strategies\scripts\EquityEma15_10_20_50Crossover15min.py"
$logDirectory = Join-Path $WorkspaceRoot "strategies\logs"
$schedulerLog = Join-Path $logDirectory "EquityEma15_10_20_50Crossover15min_scheduler.log"
$mode = if ($Live.IsPresent) { "LIVE" } else { "DRY_RUN" }

function Write-SchedulerLog {
    param([string]$Message)
    $timestamp = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
    $line = "$timestamp $Message"
    Write-Host $line
    Add-Content -LiteralPath $schedulerLog -Value $line -Encoding UTF8
}

function Get-IstNow {
    try {
        $istZone = [TimeZoneInfo]::FindSystemTimeZoneById("India Standard Time")
    }
    catch {
        $istZone = [TimeZoneInfo]::FindSystemTimeZoneById("UTC")
        Write-SchedulerLog "WARNING: India Standard Time was unavailable; using UTC."
    }
    return [TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $istZone)
}

function Get-NextQuarterHourSlot {
    param(
        [DateTime]$Now,
        [DateTime]$Start,
        [DateTime]$End
    )

    if ($Now -lt $Start) {
        return $Start
    }
    if ($Now -gt $End) {
        return $End.AddMinutes(15)
    }

    $elapsedMinutes = ($Now - $Start).TotalMinutes
    $slotNumber = [Math]::Ceiling($elapsedMinutes / 15.0)
    return $Start.AddMinutes($slotNumber * 15)
}

if ([string]::IsNullOrWhiteSpace($Python)) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        throw "Python was not found. Pass -Python with the full python.exe path."
    }
    $Python = $pythonCommand.Source
}

if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    throw "Scanner script not found: $scriptPath"
}

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
Write-SchedulerLog "STARTED mode=$mode once=$($Once.IsPresent) workspace=$WorkspaceRoot"

if ($Live.IsPresent) {
    Write-SchedulerLog "WARNING: -Live was requested; the equity config must allow live orders."
}
else {
    Write-SchedulerLog "Dry-run selected; no FYERS order will be transmitted."
}

$today = (Get-IstNow).Date
$start = $today.AddHours(9).AddMinutes(15)
$end = $today.AddHours(15).AddMinutes(15)
$slot = Get-NextQuarterHourSlot -Now (Get-IstNow) -Start $start -End $end

if ($slot -gt $end) {
    Write-SchedulerLog "STOPPED: current IST time is outside the 09:15-15:15 window."
    exit 0
}

while ($slot -le $end) {
    $now = Get-IstNow
    $waitSeconds = ($slot - $now).TotalSeconds
    if ($waitSeconds -gt 0) {
        Write-SchedulerLog ("WAIT until {0:HH:mm:ss} IST" -f $slot)
        Start-Sleep -Milliseconds ([int][Math]::Ceiling($waitSeconds * 1000))
    }

    $now = Get-IstNow
    if ($now -gt $end) {
        Write-SchedulerLog "STOPPED: 15:15 IST window has passed."
        break
    }

    Write-SchedulerLog ("RUN {0:yyyy-MM-dd HH:mm:ss} IST mode={1}" -f $now, $mode)
    $arguments = @($scriptPath, "--once")
    if ($Live.IsPresent) {
        $arguments += "--live"
    }
    else {
        $arguments += "--dry-run"
    }

    try {
        & $Python @arguments 2>&1 | ForEach-Object {
            $line = $_.ToString()
            Write-Host $line
            Add-Content -LiteralPath $schedulerLog -Value $line -Encoding UTF8
        }
        $exitCode = $LASTEXITCODE
    }
    catch {
        $exitCode = 1
        Write-SchedulerLog "ERROR: $($_.Exception.Message)"
    }

    if ($exitCode -ne 0) {
        Write-SchedulerLog "Scanner exited with code $exitCode; continuing schedule."
    }
    if ($Once.IsPresent) {
        Write-SchedulerLog "STOPPED: -Once requested."
        break
    }

    # Skip missed slots if a scan took longer than the polling interval.
    $slot = $slot.AddMinutes(15)
    while ($slot -le (Get-IstNow)) {
        $slot = $slot.AddMinutes(15)
    }
}

Write-SchedulerLog "STOPPED: scheduled window complete."
exit 0
