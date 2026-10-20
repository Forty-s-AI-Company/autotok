<#
.SYNOPSIS
    TikTok 越南市場 AI 成長與自動化發布系統 — Windows 安全 Dry-Run 執行腳本 (Phase 1)
.DESCRIPTION
    供 Windows PowerShell 或 Windows Task Scheduler (工作排程器) 呼叫之安全測試腳本。
    強制設定 ALLOW_LIVE_PUBLISH=false，生成 6 張 1080x1350 測試 JPEG、執行品質驗證並輸出 Dry-Run 計畫。
#>

param(
    [ValidateSet("mock", "official_api", "manual_export")]
    [string]$Adapter = "official_api",

    [string]$PostId = "win-dryrun-001"
)

$ErrorActionPreference = "Stop"

# 切換至專案根目錄
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $ProjectRoot

# 強制鎖定安全開關，確保不會在未授權時意外公開發文
$env:ALLOW_LIVE_PUBLISH = "false"
$env:TIMEZONE = "Asia/Taipei"

$MediaDir = Join-Path $ProjectRoot "artifacts\mock_media\$PostId"

Write-Host "[1/3] 產生 6 張 1080x1350 測試 JPEG: $MediaDir" -ForegroundColor Cyan
uv run python -m app generate-mock-media $MediaDir --post-id $PostId

Write-Host "[2/3] 驗證六張照片輪播品質 Gate" -ForegroundColor Cyan
uv run python -m app verify-media $MediaDir

Write-Host "[3/3] 執行發布計畫預覽 (--dry-run, Adapter=$Adapter)" -ForegroundColor Cyan
uv run python -m app publish --media-dir $MediaDir --post-id $PostId --adapter $Adapter --dry-run
