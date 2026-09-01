# setup_scheduled_tasks.ps1 — one-time setup: registers two Windows
# Scheduled Tasks that run run_daily_refresh.py shortly after each market's
# own close, in LOCAL (Eastern) time, using the same safety margins the
# GitHub Actions workflows already use in UTC:
#
#   US refresh    7:35 PM ET, Mon-Fri   (23:00 UTC in EDT summer / 00:00 UTC
#                                        in EST winter — clears the 20:00/
#                                        21:00 UTC close either way)
#   India refresh 7:45 AM ET, Mon-Fri   (matches the existing 11:30 UTC
#                                        Action's own 1.5h margin over
#                                        India's 10:00 UTC close)
#
# Windows Task Scheduler triggers set with -Daily/-At store a LOCAL wall-
# clock time and re-resolve it against the machine's current time zone on
# every run, so these stay correct across DST changes without any extra
# handling here.
#
# This does not remove or disable the GitHub Actions workflows — they stay
# as the fallback for the one day this machine is off. Run this script once,
# from an elevated (Run as Administrator) PowerShell, to set both tasks up.

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = (Get-Command python).Source

function Register-Refresh($name, $country, $time) {
    $action = New-ScheduledTaskAction -Execute $pythonExe `
        -Argument "scripts\run_daily_refresh.py $country" `
        -WorkingDirectory $repoRoot
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

    # -Daily fires every day; restrict to weekdays the same way the GitHub
    # Actions cron does ("* * 1-5") by checking inside the script would need
    # a wrapper, so instead this uses a weekly trigger per weekday — five
    # triggers, one task, all pointed at the same 7:35pm/7:45am slot.
    $weekdayTriggers = 1..5 | ForEach-Object {
        New-ScheduledTaskTrigger -Weekly -DaysOfWeek $_ -At $time
    }

    Register-ScheduledTask -TaskName $name -Action $action `
        -Trigger $weekdayTriggers -Settings $settings -Force `
        -Description "Runs the $country market breadth pipeline and pushes the result - the reliable local counterpart to .github/workflows/daily-$($country.ToLower()).yml, which depends on GitHub's own scheduler and can run hours late."
}

Register-Refresh "MBT Daily US Refresh"    "US" "19:35"
Register-Refresh "MBT Daily India Refresh" "IN" "07:45"

$logPath = Join-Path (Split-Path -Parent $repoRoot) "Portfolio Local\daily-refresh.log"
Write-Host "Registered: MBT Daily US Refresh (7:35 PM ET, Mon-Fri)"
Write-Host "Registered: MBT Daily India Refresh (7:45 AM ET, Mon-Fri)"
Write-Host ""
Write-Host "Logs: $logPath"
Write-Host "To test one immediately, run: Start-ScheduledTask -TaskName MBT Daily US Refresh"
Write-Host "To remove either later, run: Unregister-ScheduledTask -TaskName MBT Daily US Refresh"
