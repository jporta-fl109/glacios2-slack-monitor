<#
Glacios 2 TEM interface log -> Slack monitor
Windows launcher for use with Task Scheduler.

Setup (one time):
  1. Install Python 3.10+ on the instrument PC / server.
  2. From this project's folder, run:
       python -m venv venv
       venv\Scripts\pip install -r requirements.txt
  3. Copy config.example.yaml to config.yaml and edit it.
  4. Test it manually first:
       venv\Scripts\python tem_monitor.py --config config.yaml --once

Running continuously via Task Scheduler:
  1. Open Task Scheduler -> Create Task.
  2. General tab: "Run whether user is logged on or not", check
     "Run with highest privileges" if the log path needs it.
  3. Triggers tab: "At startup" (and optionally also "Daily" as a
     safety-net restart).
  4. Actions tab: "Start a program", point it at:
       powershell.exe -ExecutionPolicy Bypass -File "<path to this file>"
  5. Settings tab: check "If the task fails, restart every: 1 minute",
     up to a high retry count, so the monitor recovers from transient
     issues (log share briefly unavailable, etc.)

This script just loops tem_monitor.py so a single crashed iteration
doesn't require Task Scheduler to notice and restart the whole task.
#>

$ProjectDir = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectDir

while ($true) {
    & "$ProjectDir\venv\Scripts\python.exe" "$ProjectDir\tem_monitor.py" --config "$ProjectDir\config.yaml"
    Write-Output "tem_monitor.py exited unexpectedly; restarting in 10 seconds..."
    Start-Sleep -Seconds 10
}
