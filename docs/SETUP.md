# 設定

## macOS

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # 尚未安裝 uv 時
uv sync
cp -n .env.example .env
uv run python run.py
```

`.env` 內的 Token 只供舊版全域設定使用；每個帳號的 Notion Token 請在操作台 設定 → Notion 輸入，會存進作業系統金鑰圈。

生圖需要 Codex CLI。操作台會自動找 `CODEX_BIN`、PATH 中的 `codex`、macOS 的 ChatGPT App；找不到時在 設定 → 生圖與文案引擎 顯示原因。

生圖時 Codex 預設用 `workspace-write` 沙盒（只能寫輸出資料夾）。如果圖片沒有產出且確認是權限造成，可在 `.env` 設 `CODEX_IMAGE_SANDBOX=danger-full-access`，但這會讓 Codex 擁有完整檔案與網路權限，提示詞中的外部文字也可能影響它，請只在必要時使用。

## Windows

> 尚未在實體 Windows 機器上實測。

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\windows\setup_windows.ps1
```

之後雙擊 `啟動控制台.bat`。備援機與主機不能同時對同一個帳號發布；在狀態搬到共享資料庫之前，備援機只做 dry-run（`scripts/windows/run_dry_run.ps1`）。

## 驗證

```bash
uv run pytest -q
uv run ruff check .
uv run python -m app generate-mock-media artifacts/mock_media/post_001
uv run python -m app verify-media artifacts/mock_media/post_001
uv run python -m app publish --media-dir artifacts/mock_media/post_001 --adapter manual_export --dry-run
```

未加 `--dry-run`，或未設定 `ALLOW_LIVE_PUBLISH=true` 與授權時，發布指令會直接中止。

## 自動排程（Windows）

1. 在操作台 設定 → 自動排程 打開要自動處理的帳號。
2. 註冊工作排程器（每 10 分鐘一次）：

```powershell
.\scripts\windows\register_scheduler.ps1
```

移除：`.\scripts\windows\register_scheduler.ps1 -Unregister`。紀錄在 `logs\scheduler.log`。預設以目前使用者、登入時執行；`-RunWhenLoggedOff` 可在未登入時執行，但 Codex 與金鑰圈是否可用要先實測。

手動檢查：`uv run python -m app scheduler tick --dry-run` 只列出規劃，不執行。macOS 可用 `scripts/macos/com.autotok.tick.plist`。
