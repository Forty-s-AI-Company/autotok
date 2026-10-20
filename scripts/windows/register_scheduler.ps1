<#
.SYNOPSIS
    在 Windows 工作排程器註冊「AutoTok 排程」：每 10 分鐘執行一次 scripts\windows\run_tick.ps1。
.DESCRIPTION
    預設以目前使用者、登入時執行（可使用 keyring 憑證與 Codex 登入狀態）。
    -RunWhenLoggedOff：不論是否登入都執行，需要輸入 Windows 密碼；此模式下 Codex 與金鑰圈
    是否可用取決於該使用者的設定，請先在登入狀態測試過再切換。
    -Unregister：移除工作。
    同一時間只會有一個 tick 在跑（排程器設定不重複啟動，程式本身也有檔案鎖）。
.EXAMPLE
    .\scripts\windows\register_scheduler.ps1
    .\scripts\windows\register_scheduler.ps1 -Unregister
#>
param(
    [switch]$Unregister,
    [switch]$RunWhenLoggedOff,
    [int]$IntervalMinutes = 10
)

$ErrorActionPreference = "Stop"
$taskName = "AutoTok 排程"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "已移除工作：$taskName" -ForegroundColor Green
    exit 0
}

$script = Join-Path $PSScriptRoot "run_tick.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`"" `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

if ($RunWhenLoggedOff) {
    $cred = Get-Credential -UserName $env:USERNAME -Message "不論是否登入都執行，需要 Windows 密碼"
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
        -User $cred.UserName -Password $cred.GetNetworkCredential().Password -RunLevel Limited -Force | Out-Null
} else {
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Force | Out-Null
}
Write-Host "已註冊工作：$taskName（每 $IntervalMinutes 分鐘）。紀錄：$root\logs\scheduler.log" -ForegroundColor Green
