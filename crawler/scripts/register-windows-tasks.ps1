param(
    [string]$TaskPrefix = "PositiveNewsCrawler",
    [string]$Listen = "127.0.0.1:8000"
)
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { throw "Run scripts/install.ps1 first" }
$WebArgs = "-m waitress --listen=$Listen newscrawler.wsgi:application"
$WorkerArgs = "manage.py runworker"
$WebAction = New-ScheduledTaskAction -Execute $Python -Argument $WebArgs -WorkingDirectory $Root
$WorkerAction = New-ScheduledTaskAction -Execute $Python -Argument $WorkerArgs -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName "$TaskPrefix-Web" -Action $WebAction -Trigger $Trigger -Settings $Settings -Description "Positive News Crawler web UI" -Force
Register-ScheduledTask -TaskName "$TaskPrefix-Worker" -Action $WorkerAction -Trigger $Trigger -Settings $Settings -Description "Positive News Crawler worker" -Force
Write-Host "Tasks registered. Configure NEWSCRAWLER_* as system environment variables before starting them."
