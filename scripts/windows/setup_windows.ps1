<#
.SYNOPSIS
    AutoTok — Windows 10/11 環境安裝腳本（Web 操作台與排程器）
.DESCRIPTION
    自動檢查並安裝 uv (若尚未安裝)，配置 Python 3.12 與 .venv 虛擬環境，並執行單元測試確認環境正常。
    不會將任何敏感憑證寫入 Git 或排程日誌。
#>

$ErrorActionPreference = "Stop"

Write-Host "=== [1/4] 檢查 uv 套件管理工具 ===" -ForegroundColor Cyan
if (-not (Get-Command "uv" -ErrorAction SilentlyContinue)) {
    Write-Host "未偵測到 uv，正在透過官方腳本安裝 uv..." -ForegroundColor Yellow
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$HOME\.local\bin;$env:Path"
}
uv --version

Write-Host "=== [2/4] 安裝 Python 3.12 並建立 .venv ===" -ForegroundColor Cyan
uv python install 3.12
uv sync

Write-Host "=== [3/4] 檢查 .env 設定檔 ===" -ForegroundColor Cyan
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "已從 .env.example 建立 .env（預設 ALLOW_LIVE_PUBLISH=false）。" -ForegroundColor Green
}

Write-Host "=== [4/4] 執行 Ruff 與 Pytest 驗證 ===" -ForegroundColor Cyan
uv run ruff check .
uv run pytest -v

Write-Host "=== 完成。啟動操作台：雙擊 啟動控制台.bat；註冊自動排程：scripts\windows\register_scheduler.ps1（說明見 docs\SETUP.md）===" -ForegroundColor Green
