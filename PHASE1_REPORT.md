# 第一階段可行性驗證與工程骨架交付報告（PHASE1_REPORT.md）

> **專案名稱**：TikTok 越南市場 AI 成長與自動化發布系統（`@carry.vietnam.tw`）  
> **報告日期**：2026-10-20（`Asia/Taipei`）  
> **階段判定結論**：**`CONDITIONAL`（條件式通過，建議進入第二階段）**

---

## 1. 第一階段唯一核心問題與最終結論

> **核心問題**：能否在 Mac 開發、GitHub Actions 或 Windows 執行環境下，安全、可驗證地發布 TikTok 六張獨立 JPEG 照片輪播（不是六張拼圖、不是轉成影片）？

### 最終判定：**`CONDITIONAL`**（依據 [`MASTER_SPEC.md`](MASTER_SPEC.md) 第 13 節標準）

1. **為什麼不是 `NO-GO`？**
   - TikTok 官方 **Content Posting API**（`POST https://open.tiktokapis.com/v2/post/publish/content/init/`）**原生支援多張照片輪播（`media_type: "PHOTO"`，1–35 張 JPEG/WebP）**，無須將六張圖片拼成單張或轉為 MP4 影片。
2. **為什麼是 `CONDITIONAL` 而非直接標示為已全自動公開 `GO`？**
   - **官方 API 審核門檻**：在開發者應用程式通過 TikTok 官方 **App Audit** 之前，未審核（Unaudited）應用程式呼叫 `DIRECT_POST` 時，TikTok 強制限制 `privacy_level` 必須為 `"SELF_ONLY"`（私人可見），若傳入 `"PUBLIC_TO_EVERYONE"` 會被 API 拒絕（HTTP 403 `unaudited_client_can_only_post_to_private_accounts`）。
   - **網域驗證門檻**：官方照片 API 僅接受 `source: "PULL_FROM_URL"`，且圖片所在的 HTTPS 網域必須先在 TikTok Developer Portal 完成網域所有權驗證（Domain Verification）。
   - **立即可用的合規替代方案（半自動流程）**：在等待官方 App Audit 通過期間，系統支援：
     1. 官方 API 的 `post_mode: "MEDIA_UPLOAD"`（將 6 張獨立 JPEG 傳送至 `@carry.vietnam.tw` 手機 App 草稿／收件匣，點擊通知即可公開發布）。
     2. 本機／雲端 `manual_export` Adapter（自動驗證並打包 6 張 1080×1350 JPEG、越南文標題/內文、5 個 Hashtag 與繁中逐頁對照表）。
   - **真實帳號實測狀態**：因目前環境尚未配置使用者授權之 TikTok Developer OAuth Token（`TIKTOK_ACCESS_TOKEN`）與已驗證圖片網域，且嚴格遵守「未經同意不得在正式帳號公開發文」之規定，真實外部 API 寫入標記為 **`BLOCKED_NEEDS_AUTH`**，絕不以 Mock 數據宣稱已完成真實公開發文。

---

## 2. 深度研究證據與來源查核

### 2.1 TikTok 官方 Content Posting API（照片輪播能力查核）

| 查核項目 | 實證結論 | 官方規範細節與限制 | 來源與參考文件 |
|---|---|---|---|
| **照片輪播端點** | **支援** | `POST https://open.tiktokapis.com/v2/post/publish/content/init/`，設定 `media_type: "PHOTO"`。 | [TikTok Content Posting API - Photo Posts](https://developers.tiktok.com/doc/content-posting-api-get-started-upload-photo) |
| **單篇照片張數** | **支援 6 張** | `source_info.photo_images` 陣列支援 **1 至 35 張**圖片 URL，完全涵蓋本系統每篇 6 張獨立 1080×1350 JPEG 之需求；`photo_cover_index` 可指定封面（`0` 代表第一張）。 | [TikTok Content Posting API Reference](https://developers.tiktok.com/doc/content-posting-api-reference-upload-photo) |
| **圖片格式與來源** | **支援 JPEG，需公開已驗證 URL** | 支援 **JPEG** 與 WebP（不支援 PNG）；`source` 必須為 `"PULL_FROM_URL"`，且 URL 網域須先於 Developer Portal 完成 Domain Verification。 | [TikTok Content Posting API Media Transfer Guide](https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide) |
| **標題與內文限制** | **支援越文與 Hashtags** | `post_info.title` 上限 **90 UTF-16 runes**；`post_info.description` 上限 **4,000 UTF-16 runes**（可包含本系統規定的 5 個 Hashtag）。 | [TikTok Content Posting API Reference](https://developers.tiktok.com/doc/content-posting-api-reference-upload-photo) |
| **直接公開 (`DIRECT_POST`) 與審核** | **需通過 App Audit 才能公開** | OAuth 需 `video.publish`（Direct Post）或 `video.upload`（Media Upload）scope。**未通過 App Audit 前，`DIRECT_POST` 僅允許 `privacy_level: "SELF_ONLY"`**；通過 Audit 後方可設為 `"PUBLIC_TO_EVERYONE"`（發送前應先呼叫 `/v2/post/creator_info/query/` 確認創作者可用隱私選項）。 | [TikTok App Audit Guidelines](https://developers.tiktok.com/doc/content-posting-api-app-audit-guidelines) |
| **狀態驗證與排程** | **非同步輪詢；排程需自行實作** | 初始化後回傳 `publish_id`，需呼叫 `POST /v2/post/publish/status/fetch/` 驗證實際發布狀態。官方 API **無內建未來時間排程參數**，必須由本系統外部排程器（GitHub Actions / Windows Task Scheduler）準時觸發。 | [TikTok Fetch Publish Status](https://developers.tiktok.com/doc/content-posting-api-reference-get-video-status) |

### 2.2 `makiisthenes/TiktokAutoUploader` (`autotok`) 原始碼與可行性查核

我們於 2026-10-20 直接透過 GitHub API 檢視 `https://github.com/makiisthenes/TiktokAutoUploader` 最新 `main` 分支（Release `v2.1.0`，最新 commit `0f7615589ca30677901ffdd8940c40697aac2e32`，日期 `2026-10-16`）：

1. **是否支援六張照片輪播（Photo / Carousel）？**
   - **結論：完全不支援（僅支援單一 MP4 影片上傳）。**
   - **證據 A（作者官方 Issue 回覆）**：在 [GitHub Issue #70: "Can I use this tool to upload photos to TikTok?"](https://github.com/makiisthenes/TiktokAutoUploader/issues/70) 中，儲存庫作者 `makiisthenes` 明確回覆：
     > *"I have not supported photos in this repository, it only supports videos mp4"*
   - **證據 B（CLI 介面原始碼）**：檢視 [`autotok/cli.py`](https://github.com/makiisthenes/TiktokAutoUploader/blob/0f7615589ca30677901ffdd8940c40697aac2e32/autotok/cli.py) 第 41–55 行，`upload` 子命令的來源群組僅有 `-v / --video` 與 `-yt / --youtube`，無任何照片或多圖參數。
   - **證據 C（上傳核心原始碼）**：檢視 [`autotok/uploader.py`](https://github.com/makiisthenes/TiktokAutoUploader/blob/0f7615589ca30677901ffdd8940c40697aac2e32/autotok/uploader.py) 第 152–270 行：
     - `_transfer()` 呼叫 `https://www.tiktok.com/api/v1/video/upload/auth/?aid=1988` 取得 `video_token_v5`，並向 `ApplyUploadInner` 固定傳入 `FileType=video`（第 173 行）。
     - `_payload()` 固定建立 `vedit_common_info: {"draft": "", "video_id": video_id}` 與 `single_post_req_list`（第 245–269 行）。
   - **證據 D（MCP Server 與 Go/JS SDK）**：檢視 [`autotok/mcp_server.py`](https://github.com/makiisthenes/TiktokAutoUploader/blob/0f7615589ca30677901ffdd8940c40697aac2e32/autotok/mcp_server.py)、`js/src/uploader.ts` 與 `go/uploader.go`，所有介面皆僅封裝 `upload_video`。
2. **安全、風控與開源授權風險**：
   - **開源授權**：該專案採用 **`AGPL-3.0`**（[`LICENSE`](https://github.com/makiisthenes/TiktokAutoUploader/blob/0f7615589ca30677901ffdd8940c40697aac2e32/LICENSE)），具強烈傳染性。
   - **非官方簽章與風控風險**：`autotok` 透過無頭瀏覽器（Playwright Chromium / Steel / Browserbase）載入 `autotok/signer_js/webmssdk.js` 與 `xbogus.js` 計算非官方網頁端 `X-Bogus` 與 `_signature`，並直接使用使用者 `sessionid` cookie。社群 Issues 中已有「帳號被限流 / Shadowban」（[Issue #78](https://github.com/makiisthenes/TiktokAutoUploader/issues/78)）、「Session 失效」（[Issue #81](https://github.com/makiisthenes/TiktokAutoUploader/issues/81)）與「Datacenter 錯誤」（[Issue #52](https://github.com/makiisthenes/TiktokAutoUploader/issues/52)）等案例。
   - **工程處置**：遵循 [`MASTER_SPEC.md`](MASTER_SPEC.md) 第 9 節與 [`PHASE1_ANTIGRAVITY_PROMPT.md`](PHASE1_ANTIGRAVITY_PROMPT.md) 第 4–5 點，我們**不依賴、不複製 `autotok` 程式碼**，並在 [`app/adapters.py`](app/adapters.py) 的 `AutotokCandidateAdapter` 中明確拋出 `UnsupportedFeatureError`。

---

## 3. 實際環境與已完成之工程交付物

### 3.1 開發環境實測資訊
- **作業系統**：macOS 26.7 (Build 25G229, arm64)
- **套件管理與虛擬環境**：安裝 `uv 0.13.3`（位於 `/Users/eden/.local/bin/uv`），並透過 `uv python install 3.12` 安裝獨立 `CPython 3.12.15`，建立專案專屬 `.venv`（完全未動用系統內建 Python 3.8.2 或全域 `pip`）。
- **Git 儲存庫**：`/Users/eden/Documents/AI/autotok`（分支 `main`，遠端 `git@github.com:Forty-s-AI-Company/autotok.git`）。

### 3.2 程式碼位置與模組清單
- [`pyproject.toml`](pyproject.toml)：Python 3.12+、Pydantic v2、Pillow、HTTPX、Pytest、Ruff 設定。
- [`app/config.py`](app/config.py)：環境變數管理、預設 `ALLOW_LIVE_PUBLISH=false` 安全鎖、敏感憑證遮罩函式 `redact_sensitive()`。
- [`app/errors.py`](app/errors.py)：定義 `UnsupportedFeatureError`、`BlockedNeedsAuthError`、`BlockedNeedsAuditError`、`MediaValidationError`。
- [`app/models.py`](app/models.py)：定義貼文狀態機 `PostStatus`、時段 `ContentSlot`、6 頁圖文結構 `SlideItem`、強制 5 個 Hashtag 與 6 頁順序驗證之 `CarouselPostPackage`。
- [`app/media.py`](app/media.py)：
  - `generate_mock_carousel_media()`：生成 6 張 1080×1350 (4:5)、明顯標記 `TEST / MOCK ONLY`、含越南文與繁體中文對照之獨立 JPEG（`slide_01.jpg` 至 `slide_06.jpg`）。
  - `verify_carousel_directory()`：嚴格檢查數量（6 張）、檔名頁碼順序（01–06）、真實二進位 JPEG Magic Bytes（`FF D8 FF`，拒絕將 PNG 改副檔名為 `.jpg`）、尺寸（1080×1350）與 SHA-256 去重（拒絕重複圖片）。
- [`app/adapters.py`](app/adapters.py)：實作 `UploaderAdapter` 介面與 `mock`、`official_api`、`autotok_candidate`、`manual_export` 四種可替換 Adapter。
- [`app/state.py`](app/state.py)：本機 SQLite `PostStateRepository` 與防重複發文鎖 `acquire_publish_lock()`。
- [`app/cli.py`](app/cli.py) & [`app/__main__.py`](app/__main__.py)：提供 `python -m app generate-mock-media`、`verify-media`、`publish` CLI。
- [`tests/test_phase1.py`](tests/test_phase1.py)：11 項完整單元與整合測試。
- [`.github/workflows/ci.yml`](.github/workflows/ci.yml) & [`.github/workflows/dry_run.yml`](.github/workflows/dry_run.yml)：GitHub Actions 自動測試與 `workflow_dispatch` 手動安全 Dry-Run 工作流（強制 `ALLOW_LIVE_PUBLISH="false"`，不設定任何自動公開發文排程）。
- [`scripts/windows/setup_windows.ps1`](scripts/windows/setup_windows.ps1) & [`scripts/windows/run_dry_run.ps1`](scripts/windows/run_dry_run.ps1)：Windows PowerShell 安裝與安全 Dry-Run 腳本骨架。

---

## 4. 真實測試結果與可重現命令

### 4.1 靜態檢查與自動化測試執行結果（macOS 實測通過）

```bash
export PATH="$HOME/.local/bin:$PATH"
uv sync
uv run ruff check .
uv run pytest -v
```

**實測輸出摘要**：
- `uv run ruff check .`：`All checks passed!`（0 errors）
- `uv run pytest -v`：**11 passed in 0.78s**
  - `test_generate_and_verify_six_valid_jpegs`：PASSED（驗證 6 張 1080×1350 獨立 JPEG 生成與檢核）
  - `test_verifier_rejects_png_and_disguised_png`：PASSED（驗證拒絕 `.png` 與偽裝為 `.jpg` 的 PNG）
  - `test_verifier_rejects_wrong_dimensions`：PASSED（驗證拒絕 1080×1080 等錯誤尺寸）
  - `test_verifier_rejects_missing_slide`：PASSED（驗證拒絕少於 6 張或漏頁）
  - `test_verifier_rejects_duplicate_images`：PASSED（驗證拒絕 SHA-256 重複圖片）
  - `test_autotok_candidate_adapter_raises_unsupported`：PASSED（驗證 `autotok_candidate` 拋出 `UnsupportedFeatureError`）
  - `test_official_api_adapter_dry_run_and_live_blocks`：PASSED（驗證 `official_api` dry-run 輸出與未授權/未審核攔截）
  - `test_manual_export_adapter_writes_bundle`：PASSED（驗證半自動匯出完整 6 張圖與越中文案包）
  - `test_cli_commands_and_safety_gate`：PASSED（驗證 CLI 正常執行且未加 `--dry-run` 時強制阻擋）
  - `test_state_repository_and_publish_lock`：PASSED（驗證 SQLite 狀態更新、防重發鎖與 `UNKNOWN` 狀態保護）
  - `test_log_redaction`：PASSED（驗證 Token / Session ID 自動遮罩為 `[REDACTED]`）

### 4.2 CLI 實測命令

```bash
# 1. 生成 6 張測試 JPEG
uv run python -m app generate-mock-media artifacts/mock_media/post_001

# 2. 驗證媒體目錄
uv run python -m app verify-media artifacts/mock_media/post_001

# 3. 預覽官方 API 發布計畫 (無外部副作用)
uv run python -m app publish --media-dir artifacts/mock_media/post_001 --adapter official_api --dry-run
```

---

## 5. 已驗證項目 vs. 未驗證／待授權項目（誠實揭露）

| 項目 | 狀態 | 說明 |
|---|---|---|
| 六張獨立 1080×1350 JPEG 產生器與品質 Gate 驗證器 | **已實測通過** | 於 macOS 本機透過 Pillow 與 Pytest 完整驗證。 |
| CLI `verify-media` 與 `publish --dry-run` 安全閘門 | **已實測通過** | 於 macOS 本機實測無外部網路副作用，且未加 `--dry-run` 會安全攔截（Exit Code 2）。 |
| `official_api` 與 `autotok` 照片輪播支援度調查 | **已查證完成** | 附官方文件與 `autotok` 原始碼 / Issue #70 實證。 |
| GitHub Actions 工作流（`ci.yml`, `dry_run.yml`） | **已建立，尚未推上遠端實測** | 工作流語法與本地執行步驟一致；待推送至 GitHub 遠端儲存庫後觸發驗證。 |
| Windows PowerShell 腳本（`setup_windows.ps1`, `run_dry_run.ps1`） | **已建立，尚未於 Windows 實機測試** | 於 macOS 完成跨平台路徑設計與靜態檢視，待於 Windows 常開備援機實機執行。 |
| TikTok 真實帳號 `@carry.vietnam.tw` 六張圖公開發布 | **`BLOCKED_NEEDS_AUTH`** | 需使用者授權提供 TikTok Developer App 憑證、驗證圖片託管網域並通過 App Audit（或授權以 `MEDIA_UPLOAD` 測試草稿上傳）。 |
| Notion 既有資料庫真實 Schema 讀取與寫入 | **待 Phase 2 執行** | 遵守 Phase 1 邊界，未提前修改或讀取 Notion 資料庫。 |

---

## 6. 下一步建議：是否適合進入第二階段（Phase 2）？

### 結論：**適合立即進入 Phase 2（研究 Agent 與內容庫）與 Phase 3（越南文六頁製圖與品質 Gate）**

**理由**：
1. Phase 1 已經排除 `autotok` 不支援照片輪播的假設，避免了在錯誤的非官方影片工具上浪費開發資源或導致 `@carry.vietnam.tw` 帳號遭風控。
2. 系統已建立穩固的 `UploaderAdapter` 抽象層與嚴格的 6 張 1080×1350 JPEG 品質驗證器：
   - 即使 TikTok 官方 Content Posting API 的 App Audit 仍在申請或審核中，Phase 2 與 Phase 3 產出的每一篇越南文六頁輪播，都能立即透過 **`manual_export`**（匯出標準 6 張圖＋越中文案包）或官方 API 的 **`MEDIA_UPLOAD`**（送入 TikTok 手機 App 收件匣一鍵發布）穩定上線，完全不影響內容產線推進。
3. **需要使用者後續協助的最小必要事項（可與 Phase 2 並行準備）**：
   - 若希望啟用 `official_api` 自動傳送草稿（`MEDIA_UPLOAD`）或申請直接公開（`DIRECT_POST`），請於 TikTok Developer Portal 建立應用程式、申請 Content Posting API 權限，並準備一個可驗證所有權的 HTTPS 圖片託管網域。
   - 若要進入 Phase 2 連接 Notion 資料庫（`f1bc34396f154e62bca90c9deea19d7a`），請準備具備該資料庫讀寫權限的 `NOTION_TOKEN`。
