<#
UserPromptSubmit hook for the Gmail check routines. Registered only in the
project's .claude/settings.json.

When statemachine.py gate answers SKIP, it blocks a scheduled Gmail run before
the model starts, so a skipped run costs no tokens. Deciding inside the session
instead cost about 60k cache-write tokens, nearly a whole round, because the
session prefix and INSTRUCTIONS.md were already loaded by then.

Every other outcome lets the prompt through, including any error, a timeout, or
a gate that cannot be reached. A wrong block silently loses a check, while a
wrong pass only costs tokens.

The hook fires on every prompt submitted in this project, interactive ones
included, so it returns before starting Python unless the prompt opens with the
scheduler's own wrapper.

Keep this file ASCII. Windows PowerShell 5.1 decodes a BOM-less .ps1 as the
system ANSI codepage, so non-ASCII text needs a BOM, as notify.ps1 explains.
#>
$ErrorActionPreference = 'Stop'
try {
    [Console]::InputEncoding = [Text.Encoding]::UTF8
    $payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
    if (-not ([string]$payload.prompt).StartsWith('<scheduled-task name="gmail-check-')) { exit 0 }

    $out = & conda run -n ML python 'D:\dont_move\git_save\Daily_Task\Email_Check\statemachine.py' gate
    $line = $out | Where-Object { $_ -match '^\s*\{' } | Select-Object -Last 1
    $decision = $line | ConvertFrom-Json
    if ($decision.status -eq 'SKIP') {
        $minutes = [int][Math]::Floor($decision.lastCommitAgeSeconds / 60)
        $reason = "Skipped by the Gmail gate. A check committed $minutes minutes ago and nothing is owed."
        @{ decision = 'block'; reason = $reason } | ConvertTo-Json -Compress
    }
} catch {
}
exit 0
