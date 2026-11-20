# Agent Memory KB Periodic Sync Script for Windows PowerShell
# Usage (manual):
#   powershell -ExecutionPolicy Bypass -File .\windows-sync.ps1
#
# Register as a 30-minute Windows Scheduled Task (run once in PowerShell):
#   $Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$HOME\personal-kb\_同步\windows-sync.ps1`""
#   $Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 30)
#   Register-ScheduledTask -TaskName "AgentMemoryKBSync" -Action $Action -Trigger $Trigger -Description "Periodic local Agent Memory KB sync"

param(
    [string]$KbRoot = $(if ($env:AGENT_MEMORY_KB_ROOT) { $env:AGENT_MEMORY_KB_ROOT } else { Join-Path $HOME "personal-kb" })
)

$ErrorActionPreference = "Stop"
$ScriptPath = Join-Path $KbRoot "_同步\build_kb_index.py"

if (-not (Test-Path $ScriptPath)) {
    Write-Error "build_kb_index.py not found at $ScriptPath. Run init_kb.py first."
    exit 1
}

python $ScriptPath --output-root $KbRoot --skip-icloud --no-notify
exit $LASTEXITCODE
