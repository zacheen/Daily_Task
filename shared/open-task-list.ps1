<#
Launches the task list GUI detached from the round that calls it.

Exists so the documented command carries no local username. The interpreter
path is resolved from $env:USERPROFILE here rather than spelled out in
INSTRUCTIONS.md, which goes to a public remote.

Callers must use `powershell.exe -NoProfile -File "<this script>"`. That is
the one form proven to match the permission allowlist; the `& "<path>"`
call-operator form hung an unattended round on a permission prompt twice, on
2026-09-09 and 2026-09-10, despite a verbatim rule in both settings layers.

Three things must not change:
  - pythonw.exe, not python.exe, so no console window appears
  - Start-Process, so the server outlives the round that launched it
  - never `conda run`, which waits for the child and would block the round
    until the user closes the page

Deliberately ASCII-only: notify.ps1 must stay UTF-8 with BOM (Windows
PowerShell 5.1 decodes a BOM-less .ps1 as system ANSI, mangling its Chinese
default), so staying pure ASCII here means there is no BOM to lose.

Port 8765 already being in use needs no handling here. task_list_gui.py exits on
its own in that case. It checks occupancy, not identity, so the usual cause is
that the user already has the page open, but any other listener on 8765 stops
the launch just the same.
#>
$ErrorActionPreference = "Stop"

$python = Join-Path $env:USERPROFILE "miniconda3\envs\ML\pythonw.exe"
$script = "D:\dont_move\git_save\Daily_Task\Email_Check\task_list\task_list_gui.py"

# Reported rather than thrown: the caller is told to ignore this script's
# output entirely, so a missing path is only ever read by a human running it
# by hand. Throwing would add a scary stack trace to an unattended round
# without making the failure any more visible.
if (-not (Test-Path $python)) {
    Write-Output "Interpreter not found, nothing launched. $python"
    exit 1
}
if (-not (Test-Path $script)) {
    Write-Output "GUI script not found, nothing launched. $script"
    exit 1
}

Start-Process -FilePath $python -ArgumentList $script -WindowStyle Hidden
Write-Output "Task list GUI launched."
