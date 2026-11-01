# Registers the collector (every 5 minutes) and the daily health check in Windows
# Task Scheduler. Run once in PowerShell from the repo root:
#   powershell -ExecutionPolicy Bypass -File ingest\scheduling\windows_task_scheduler.ps1
# Remove later with:
#   Unregister-ScheduledTask -TaskName "BikeshareGBFSCollector","BikeshareGBFSHealthCheck"

$Repo = (Resolve-Path "$PSScriptRoot\..\..").Path
# pythonw.exe runs without opening a console window every 5 minutes.
$Python = Join-Path $Repo ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $Python)) { throw "Create the virtualenv first: python -m venv .venv; .venv\Scripts\pip install -r requirements.txt" }

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 4)

# Collector: starts now, repeats every 5 minutes for 30 days (covers the 3-week run).
$PollAction = New-ScheduledTaskAction -Execute $Python -Argument "ingest\gbfs_collector.py --once" -WorkingDirectory $Repo
$PollTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 30)
Register-ScheduledTask -TaskName "BikeshareGBFSCollector" -Action $PollAction -Trigger $PollTrigger `
    -Settings $Settings -Description "Poll GBFS station status every 5 minutes" -Force

# Health check: daily at 08:00 local time. python.exe (not pythonw) so the balloon
# notification has a process to attach to.
$HealthAction = New-ScheduledTaskAction -Execute (Join-Path $Repo ".venv\Scripts\python.exe") `
    -Argument "ingest\gbfs_health_check.py" -WorkingDirectory $Repo
$HealthTrigger = New-ScheduledTaskTrigger -Daily -At "08:00"
Register-ScheduledTask -TaskName "BikeshareGBFSHealthCheck" -Action $HealthAction -Trigger $HealthTrigger `
    -Settings $Settings -Description "Daily check for missing GBFS snapshots" -Force

Write-Host "Registered. Check with: Get-ScheduledTask -TaskName Bikeshare*"
