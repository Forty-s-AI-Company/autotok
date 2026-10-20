# Windows 常開備援機設定與排程指南（SETUP_WINDOWS.md）

> 適用於 Windows 10 / Windows 11 常開本機備援環境。
> **測試狀態揭露**：Phase 1 於 macOS 開發機完成跨平台路徑（`pathlib.Path`）與 PowerShell 腳本設計，**尚未在實體 Windows 機器上實機執行**（標記為待 Windows 實機驗證）。

---

## 1. 設計定位與防重發原則

依據 [`MASTER_SPEC.md`](MASTER_SPEC.md) 第 8 節：
1. **雲端優先、本機備援**：日常以 GitHub Actions 為主，Windows 常開機透過 Windows Task Scheduler（工作排程器）作為備援。
2. **禁止雙重上傳**：GitHub Actions 的臨時環境（ephemeral runner）無法與 Windows 本機共用本地 SQLite 檔案。因此：
   - **Phase 1 階段**：Windows 腳本 [`scripts/windows/run_dry_run.ps1`](scripts/windows/run_dry_run.ps1) 強制設定 `$env:ALLOW_LIVE_PUBLISH = "false"`，僅執行本地六張圖品質檢查與 `--dry-run` 預覽，絕不對外發文。
   - **Phase 4 階段**：在啟用正式排程前，必須先配置雲端共享狀態庫（或 Notion 狀態鎖），當 Windows 備援機在預定時間（如 07:10、11:40、18:10 `Asia/Taipei`）醒來時，先檢查該時段 `post_id` 是否已由 GitHub Actions 標記為 `PUBLISHED` 或鎖定，僅在雲端未執行時才接手。

---

## 2. Windows PowerShell 環境初始化步驟

以一般使用者權限開啟 PowerShell，進入專案目錄後執行：

```powershell
# 允許目前工作階段執行本機腳本
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

# 執行自動安裝與測試腳本（自動安裝 uv、Python 3.12、建立 .venv 並跑完 ruff 與 pytest）
.\scripts\windows\setup_windows.ps1
```

---

## 3. 在 Windows 執行安全 Dry-Run

```powershell
# 使用預設 official_api adapter 進行 dry-run 預覽
.\scripts\windows\run_dry_run.ps1 -Adapter official_api -PostId win-test-001

# 使用 manual_export adapter 進行 dry-run 預覽
.\scripts\windows\run_dry_run.ps1 -Adapter manual_export -PostId win-test-001
```

---

## 4. Windows Task Scheduler（工作排程器）備援註冊範本（供 Phase 4 啟用參考）

在 Phase 1 驗證階段，若欲測試工作排程器呼叫 Dry-Run，可於 PowerShell 執行以下命令註冊單次或每日測試任務（僅跑 `--dry-run`）：

```powershell
$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PWD\scripts\windows\run_dry_run.ps1`" -Adapter official_api" `
    -WorkingDirectory $PWD.Path

$Trigger = New-ScheduledTaskTrigger -Daily -At "20:05"

Register-ScheduledTask `
    -TaskName "AutotokVN_Phase1_DryRun" `
    -Action $Action `
    -Trigger $Trigger `
    -Description "TikTok 越南市場六張輪播系統 Phase 1 安全 Dry-Run 測試任務（無外部發布副作用）"
```
