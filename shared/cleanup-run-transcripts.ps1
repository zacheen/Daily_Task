<#
Deletes transcript files left behind by scheduled runs of this repo's tasks.

Every task in this repo shares one transcript directory, keyed off the repo
path, so one sweep covers all of them. There is no per-task filter.

Only touches files that satisfy ALL of these conditions, so a real conversation
can never be caught by accident:
  1. Located in the repo's transcript directory
  2. First 3 lines contain the "<scheduled-task name=" marker, which only an
     automated run's opening message has. Grepping for the task name instead
     would also match hand-typed sessions that merely discuss the task.
  3. Older than -DaysToKeep
  4. Not modified within -SkipRecentHours, which protects a run still in flight

Dry run by default. Pass -Execute to actually delete.
#>
param(
    [int]$DaysToKeep = 14,
    [int]$SkipRecentHours = 2,
    [switch]$Execute
)

$dir = Join-Path $env:USERPROFILE ".claude\projects\D--dont-move-git-save-Daily-Task"

if (-not (Test-Path $dir)) {
    Write-Output "Transcript directory not found, nothing to do. $dir"
    exit 0
}

$ageCutoff    = (Get-Date).AddDays(-$DaysToKeep)
$recentCutoff = (Get-Date).AddHours(-$SkipRecentHours)

$candidates = @()
foreach ($f in Get-ChildItem -Path $dir -Filter *.jsonl -File) {
    $head = Get-Content -Path $f.FullName -TotalCount 3 -ErrorAction SilentlyContinue
    if (-not ($head -match '<scheduled-task name=')) { continue }
    if ($f.LastWriteTime -gt $recentCutoff) {
        Write-Output ("SKIP (in flight)  {0}  {1:N0} KB  {2}" -f $f.Name, ($f.Length / 1KB), $f.LastWriteTime)
        continue
    }
    if ($f.LastWriteTime -gt $ageCutoff) {
        Write-Output ("KEEP (recent)     {0}  {1:N0} KB  {2}" -f $f.Name, ($f.Length / 1KB), $f.LastWriteTime)
        continue
    }
    $candidates += $f
}

if ($candidates.Count -eq 0) {
    Write-Output "No scheduled-run transcripts older than $DaysToKeep days."
    exit 0
}

$totalKb = ($candidates | Measure-Object -Property Length -Sum).Sum / 1KB
foreach ($f in $candidates) {
    Write-Output ("DELETE            {0}  {1:N0} KB  {2}" -f $f.Name, ($f.Length / 1KB), $f.LastWriteTime)
}
Write-Output ("{0} file(s), {1:N0} KB total." -f $candidates.Count, $totalKb)

if (-not $Execute) {
    Write-Output "Dry run. Re-run with -Execute to delete."
    exit 0
}

foreach ($f in $candidates) {
    Remove-Item -Path $f.FullName -Confirm:$false
    Write-Output ("Deleted {0}" -f $f.Name)
}
