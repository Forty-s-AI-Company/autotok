# TikTok 越南市場 AI 成長與自動化發布系統（`@carry.vietnam.tw`）

> **定位**：個人自用、非 SaaS｜品牌標示：`Carry | Đài Loan × Việt Nam`  
> **目前進度**：Phase 1 — 發布可行性驗證與工程骨架完成（結論：**CONDITIONAL**）

本專案依據 [`MASTER_SPEC.md`](MASTER_SPEC.md) 與 [`PHASE1_ANTIGRAVITY_PROMPT.md`](PHASE1_ANTIGRAVITY_PROMPT.md) 實作，目標是為 TikTok `@carry.vietnam.tw` 建立不依賴 Metricool 的內容研究、越南文六頁 4:5 (1080×1350) 獨立 JPEG 照片輪播製作、品質檢查（Quality Gate）、Notion 建檔與排程發布系統。

---

## 1. 第一階段核心結論摘要（詳見 [`PHASE1_REPORT.md`](PHASE1_REPORT.md)）

1. **TikTok 官方 Content Posting API (`POST /v2/post/publish/content/init/`)**：
   - **支援原生多張照片輪播**：設定 `media_type="PHOTO"`, `source_info.source="PULL_FROM_URL"`, `photo_images=[...]`（支援 1–35 張 JPEG/WebP，涵蓋本專案需求的 6 張獨立 1080×1350 JPEG）。
   - **公開發布與審核限制**：未通過 TikTok 官方 App Audit 前，應用程式處於未審核（Unaudited）狀態，`DIRECT_POST` 強制只能設為 `privacy_level="SELF_ONLY"`（僅自己可見）；或者使用 `post_mode="MEDIA_UPLOAD"` 將 6 張圖送入創作者 TikTok 手機 App 收件匣／草稿完成公開發布。此外，`PULL_FROM_URL` 圖片必須託管於已在 TikTok Developer Portal 通過網域驗證（Domain Verification）的 HTTPS 網址。
   - **排程能力**：官方 API 無內建未來時間排程參數，必須由本系統外部排程器（GitHub Actions / Windows Task Scheduler）在指定時間（`Asia/Taipei` 07:00、11:30、18:00）觸發。
2. **`makiisthenes/TiktokAutoUploader` (`autotok` v2.1.0, commit `0f76155`)**：
   - **確認不支援六張照片輪播**：審閱最新原始碼 [`autotok/cli.py`](https://github.com/makiisthenes/TiktokAutoUploader/blob/0f7615589ca30677901ffdd8940c40697aac2e32/autotok/cli.py) 與 [`autotok/uploader.py`](https://github.com/makiisthenes/TiktokAutoUploader/blob/0f7615589ca30677901ffdd8940c40697aac2e32/autotok/uploader.py)，其上傳流程硬編碼 `FileType=video`、`/api/v1/video/upload/auth/` 與 `video_id`；且作者於 [GitHub Issue #70](https://github.com/makiisthenes/TiktokAutoUploader/issues/70) 明確回覆「*I have not supported photos in this repository, it only supports videos mp4*」。此外該專案為 `AGPL-3.0` 授權並使用非官方網頁端 `X-Bogus` 簽章，具帳號風控風險。
3. **Go / No-Go 判定：`CONDITIONAL`**：
   - 系統已具備 `official_api`（官方照片輪播 API 支援 `MEDIA_UPLOAD` 半自動與審核後 `DIRECT_POST` 全自動）與 `manual_export`（一鍵匯出合規 6 張 1080×1350 JPEG + 越南文文案與繁中對照包）雙重安全通道，可在不冒風控風險的前提下進入 Phase 2 與 Phase 3。

---

## 2. 快速開始（macOS 開發環境）

本專案強制使用 `uv` 與 `.venv` 管理 Python 3.12+ 虛擬環境，絕不污染系統全域 Python：

```bash
# 1. 安裝依賴並建立 .venv
uv sync

# 2. 執行程式碼靜態檢查與單元測試
uv run ruff check .
uv run pytest -v

# 3. 產生 6 張 1080x1350 獨立測試 JPEG（標記 TEST/MOCK，順序 01-06）
uv run python -m app generate-mock-media artifacts/mock_media/post_001

# 4. 執行六張照片輪播品質驗證器（檢查數量、真實 JPEG 標頭、尺寸、不重複、順序）
uv run python -m app verify-media artifacts/mock_media/post_001

# 5. 執行安全 Dry-Run 發布預覽（無任何外部網路副作用）
uv run python -m app publish --media-dir artifacts/mock_media/post_001 --adapter mock --dry-run
uv run python -m app publish --media-dir artifacts/mock_media/post_001 --adapter official_api --dry-run
uv run python -m app publish --media-dir artifacts/mock_media/post_001 --adapter manual_export --dry-run
```

> **安全護欄說明**：若執行 `uv run python -m app publish` 未加 `--dry-run`，或未明確設定環境變數 `ALLOW_LIVE_PUBLISH=true` 與授權憑證，程式會立即以 `BLOCKED_LIVE_PUBLISH_DISABLED` / `BLOCKED_NEEDS_AUTH` 終止，確保不會在未授權下於 `@carry.vietnam.tw` 公開發文。

---

## 3. 專案文件導覽

- [`MASTER_SPEC.md`](MASTER_SPEC.md)：完整產品與工程規格（Single Source of Truth）
- [`PHASE1_REPORT.md`](PHASE1_REPORT.md)：第一階段可行性驗證報告、證據連結、測試結果與 Go/No-Go 結論
- [`ARCHITECTURE.md`](ARCHITECTURE.md)：系統架構、多 Agent 介面、狀態機與防重發設計
- [`PROJECT_PLAN.md`](PROJECT_PLAN.md)：五階段開發藍圖與交付里程碑
- [`TASKS.md`](TASKS.md)：任務追蹤清單與完成狀態
- [`RISKS.md`](RISKS.md)：技術、帳號合規、排程延遲與成本風險登錄表
- [`DECISIONS.md`](DECISIONS.md)：架構決策紀錄（ADR）
- [`SETUP_MAC.md`](SETUP_MAC.md)：macOS 開發環境建置與操作指南
- [`SETUP_WINDOWS.md`](SETUP_WINDOWS.md)：Windows 常開備援機環境建置與 Task Scheduler 指南
