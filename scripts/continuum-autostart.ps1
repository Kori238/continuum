# Registers Continuum to start when you log in, via a Scheduled Task.
#
# Run once:  powershell -ExecutionPolicy Bypass -File scripts\continuum-autostart.ps1
# Remove:    Unregister-ScheduledTask -TaskName "Continuum" -Confirm:$false
#
# Kept ASCII-only on purpose: Windows PowerShell 5.1 reads an unsigned .ps1 as
# ANSI, so a stray em dash or curly quote is silently mangled, and one landing
# inside a string terminates it early. This file failed to parse for exactly
# that reason once.
#
# You probably do not need this. A Startup-folder shortcut already boots this
# machine and has worked since the migration; see below. A Scheduled Task buys
# -StartWhenAvailable (a missed logon is caught up rather than skipped) and the
# battery settings. If the shortcut is doing its job, leave it alone.
#
# What this used to do, and why it never worked:
#
#   * It launched `(Get-Command python).Source`, the interpreter on PATH, which
#     here is a bare 3.10 with neither `websockets` nor `fastapi`. The task would
#     have started and died on the first import. It was never registered, so the
#     failure stayed theoretical.
#   * It started `-m continuum app` only. That was defensible when the server ran
#     on this machine, but after the migration `app` is just a viewer onto the
#     hub. The process that matters is `-m continuum desktop`: it carries every
#     desk capability the hub can reach, plus notifications, activity tracking
#     and budget enforcement. Registering the old task would have autostarted the
#     window and left the actual work unstarted.
#
# Rather than respell the boot sequence here and let the two drift apart again,
# this runs `_autostart.vbs`, the launcher the Startup shortcut already uses. It
# owns the hub URL, the venv interpreter, the stdout redirection that keeps
# pythonw alive, and the ordering of the two processes. One boot path, one place
# to change it.

param(
    # Both boot paths at once means two desktop clients reporting the same
    # machine to the hub. Off by default: say so explicitly.
    [switch]$ReplaceStartupShortcut
)

$ErrorActionPreference = "Stop"
$repo    = Split-Path -Parent $PSScriptRoot
$vbs     = Join-Path $repo "_autostart.vbs"
$wscript = Join-Path $env:SystemRoot "System32\wscript.exe"

if (-not (Test-Path $vbs)) {
    throw "Missing $vbs. That file is the boot path; without it there is nothing to schedule."
}

# The Startup shortcut and this task would both fire at logon. The client does
# not deduplicate: two of them connect, both report, and the hub sees one
# machine talking over itself.
$startup  = [Environment]::GetFolderPath('Startup')
$shortcut = Join-Path $startup 'Continuum.lnk'
if (Test-Path $shortcut) {
    if (-not $ReplaceStartupShortcut) {
        Write-Host "Continuum already autostarts from $shortcut."
        Write-Host "Registering this task as well would start two desktop clients at every logon."
        Write-Host ""
        Write-Host "Either leave things as they are, or re-run with -ReplaceStartupShortcut"
        Write-Host "to disable the shortcut and let the task own startup instead."
        exit 1
    }
    # Renamed, not deleted: if the task turns out to be worse, this is one
    # rename away from the arrangement that was working ten seconds ago.
    $disabled = "$shortcut.disabled"
    if (Test-Path $disabled) { Remove-Item $disabled -Force }
    Rename-Item $shortcut -NewName (Split-Path $disabled -Leaf)
    Write-Host "Disabled $shortcut (renamed to Continuum.lnk.disabled)."
}

$action  = New-ScheduledTaskAction -Execute $wscript -Argument "`"$vbs`"" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtLogOn
# ExecutionTimeLimit 0: no cap. The launcher exits in seconds, but Task
# Scheduler's three-day default would be aimed at the wrong thing if that ever
# changed, and a budget tracker killed on day three is the failure this whole
# arrangement exists to prevent.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName "Continuum" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Continuum personal OS" -Force | Out-Null

Write-Host "Registered. Continuum starts at next logon."
Write-Host "Start now without logging out:  wscript `"$vbs`""
