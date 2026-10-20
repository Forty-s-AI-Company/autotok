<#
.SYNOPSIS
    執行一次排程 tick，輸出寫入 logs\scheduler.log（超過 1 MB 輪替，保留 3 份）。
    由 register_scheduler.ps1 註冊的工作呼叫；也可手動執行。不會輸出任何 Token。
#>
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root
$env:Path = "$HOME\.local\bin;$HOME\.cargo\bin;$env:Path"

$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "scheduler.log"
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 1MB)) {
    foreach ($i in 2, 1) {
        $src = if ($i -eq 1) { $log } else { "$log.$($i - 1)" }
        if (Test-Path $src) { Move-Item -Force $src "$log.$i" }
    }
}

"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] tick 開始" | Out-File -Append -Encoding utf8 $log
uv run python -m app scheduler tick 2>&1 | Out-File -Append -Encoding utf8 $log
"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] tick 結束 (exit=$LASTEXITCODE)" | Out-File -Append -Encoding utf8 $log
exit $LASTEXITCODE
