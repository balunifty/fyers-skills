<#
.SYNOPSIS
    Run the consolidated 15-minute scanner every 15 minutes, 09:15-15:15 IST.

.DESCRIPTION
    Replaces the per-script scheduled tasks, one per strategy. Dry-run is the
    default; -Live is the only way to send orders, and it also requires
    place_order=YES in strategies/config/equity/config.json.

    One task now: the scanner holds every 15-minute rule in a single process,
    so a single 15-minute tick covers all of them and there is no chance of two
    tasks racing on the same candle.

    Each tick uses the scanner's --once mode, so no overlapping scanner
    processes are created. A tick that overruns its slot is skipped rather than
    piled up behind the next one.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\run_all_15min_strategies.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\run_all_15min_strategies.ps1 -Live

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\run_all_15min_strategies.ps1 -Once
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

$scriptPath = Join-Path $WorkspaceRoot "strategies\scripts\EquityAllStrategies15min.py"
$logDirectory = Join-Path $WorkspaceRoot "strategies\logs"
$schedulerLog = Join-Path $logDirectory "EquityAllStrategies15min_scheduler.log"
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

    if ($Now -lt $Start) { return $Start }
    if ($Now -gt $End) { return $End.AddMinutes(15) }

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
    Write-SchedulerLog "WARNING: -Live was requested; buys will be sent if the equity config allows it."
}
else {
    Write-SchedulerLog "Dry-run selected; no FYERS order will be transmitted."
}

# Report the registry once, so the log records which rules this run covers.
try {
    & $Python $scriptPath --list 2>&1 | ForEach-Object {
        Add-Content -LiteralPath $schedulerLog -Value $_.ToString() -Encoding UTF8
    }
}
catch {
    Write-SchedulerLog "WARNING: could not read the strategy registry: $($_.Exception.Message)"
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
    if ($Live.IsPresent) { $arguments += "--live" } else { $arguments += "--dry-run" }

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

    # Skip missed slots if a scan took longer than the polling interval, so a
    # slow tick cannot queue up behind the next one.
    $slot = $slot.AddMinutes(15)
    while ($slot -le (Get-IstNow)) {
        $slot = $slot.AddMinutes(15)
    }
}

Write-SchedulerLog "STOPPED: scheduled window complete."
exit 0
