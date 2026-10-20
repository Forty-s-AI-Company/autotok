# 任務追蹤清單（TASKS.md）

> 記錄各階段任務執行狀態。未驗證或需外部帳號授權之項目皆如實標記，絕不偽裝完成。

---

## Phase 1｜發布可行性驗證與工程骨架（當前執行階段）

- [x] **1.1 環境檢查與初始化**
  - [x] 檢查 macOS 版本（macOS 26.7）、Git（2.34.1）與工作目錄狀態
  - [x] 安裝 `uv`（0.13.3）與受管 CPython 3.12.15，建立 `.venv` 虛擬環境（遵循全域規則：不污染系統 base Python）
  - [x] 建立 [`pyproject.toml`](pyproject.toml)、[`.gitignore`](.gitignore)、[`.env.example`](.env.example)
- [x] **1.2 核心可行性研究（附來源與原始碼證據）**
  - [x] 查證 TikTok 官方 Content Posting API 照片輪播（`POST /v2/post/publish/content/init/`，`media_type="PHOTO"`, `PULL_FROM_URL`）之張數、格式、網域驗證與未審核 `SELF_ONLY` 限制
  - [x] 審閱 `makiisthenes/TiktokAutoUploader`（`autotok` v2.1.0, commit `0f76155`）原始碼（`autotok/cli.py`, `autotok/uploader.py`）、`AGPL-3.0` 授權與 GitHub Issue #70，確認其**不支援照片輪播**（僅支援 MP4 影片）
- [x] **1.3 核心程式與 `UploaderAdapter` 實作**
  - [x] 實作 [`app/config.py`](app/config.py)（含 `ALLOW_LIVE_PUBLISH=false` 預設保護與敏感 Token 遮罩）
  - [x] 實作 [`app/models.py`](app/models.py) 與 [`app/errors.py`](app/errors.py)（6 頁輪播結構、恰好 5 個 Hashtag 驗證、狀態機與阻礙例外）
  - [x] 實作 [`app/media.py`](app/media.py)：產生 6 張獨立 1080×1350 測試 JPEG（標記 `TEST/MOCK`、順序 `01–06`）與嚴格驗證器（拒絕 PNG、偽裝副檔名、尺寸錯誤、漏圖、重複圖）
  - [x] 實作 [`app/adapters.py`](app/adapters.py)：`mock`、`official_api`、`autotok_candidate`（明確 raise `UnsupportedFeatureError`）、`manual_export`
  - [x] 實作 [`app/state.py`](app/state.py)：本機 SQLite 狀態儲存與 `acquire_publish_lock` 防重發鎖原型
  - [x] 實作 [`app/cli.py`](app/cli.py)：支援 `python -m app generate-mock-media`、`verify-media`、`publish --dry-run`
- [x] **1.4 測試、CI 與跨平台腳本**
  - [x] 撰寫並通過 [`tests/test_phase1.py`](tests/test_phase1.py) 11 項單元與整合測試（`uv run pytest` 與 `uv run ruff check` 全部通過）
  - [x] 建立 GitHub Actions CI（[`.github/workflows/ci.yml`](.github/workflows/ci.yml)）與手動 Dry-Run 工作流（[`.github/workflows/dry_run.yml`](.github/workflows/dry_run.yml)）
  - [x] 建立 Windows PowerShell 安裝與 Dry-Run 腳本（[`scripts/windows/setup_windows.ps1`](scripts/windows/setup_windows.ps1)、[`scripts/windows/run_dry_run.ps1`](scripts/windows/run_dry_run.ps1)）
- [x] **1.5 專案文件交付**
  - [x] 建立 [`README.md`](README.md)、[`ARCHITECTURE.md`](ARCHITECTURE.md)、[`PROJECT_PLAN.md`](PROJECT_PLAN.md)、[`TASKS.md`](TASKS.md)、[`RISKS.md`](RISKS.md)、[`DECISIONS.md`](DECISIONS.md)、[`SETUP_MAC.md`](SETUP_MAC.md)、[`SETUP_WINDOWS.md`](SETUP_WINDOWS.md)、[`PHASE1_REPORT.md`](PHASE1_REPORT.md)
- [ ] **1.6 真實 TikTok 帳號端到端六張照片發布實測（狀態：`BLOCKED_NEEDS_AUTH`）**
  - **阻礙原因**：尚未設定 TikTok Developer App OAuth Token（`TIKTOK_ACCESS_TOKEN`）與已驗證圖片託管網域（`TIKTOK_VERIFIED_IMAGE_URL_PREFIX`），且依據規範不得不經使用者授權在正式帳號 `@carry.vietnam.tw` 發文。

---

## Phase 2–5 待辦預覽與進度

- [x] **Phase 2｜研究 Agent 與內容庫（已完成核心開發與測試）**
  - [x] 建立 12 組越南市場原始種子關鍵字與擴充機制（[`app/research.py`](app/research.py)、`seed-keywords` CLI）
  - [x] 實作 `ResearchProvider`（`JsonFileResearchProvider` 真實案例載入與不足 30 篇誠實揭露、`MockResearchProvider` 35 篇批次樣本，且分離 `organic` 與 `ad`）
  - [x] 實作完整互動率 vs. 部分互動率口徑計算（缺失欄位保留 `None`，絕不預設填 `0`）
  - [x] 實作 35/25/20/10/10 選題研究性評分與 90 天內主題／URL 去重（`research-batch` CLI）
  - [x] 實作 Notion 真實 Schema 唯讀探測與映射器（[`app/notion_schema.py`](app/notion_schema.py)、`inspect-notion` CLI）
  - [x] 撰寫並通過 [`tests/test_phase2.py`](tests/test_phase2.py) 全部測試與交付 [`PHASE2_REPORT.md`](PHASE2_REPORT.md)、[`docs/RESEARCH_SCHEMA.md`](docs/RESEARCH_SCHEMA.md)
  - [ ] 使用真實 `NOTION_TOKEN` 讀取線上 Notion 資料庫 `f1bc34396f154e62bca90c9deea19d7a`（狀態：`BLOCKED_NEEDS_AUTH`）
- [ ] **Phase 3**：實作 AI 場景圖生成與分離式越南文字體排版渲染器、完成每日三時段六頁圖文產線
- [ ] **Phase 4**：整合 Notion 頁面六張圖同步、雲端持久化儲存與共享防重發資料庫、啟用排程工作流
- [ ] **Phase 5**：實作 `AnalyticsAgent` 成效回收與每週權重調整報告
