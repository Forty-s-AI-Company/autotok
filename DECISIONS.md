# 架構決策紀錄（DECISIONS.md）

> 記錄本專案關鍵技術與架構決策（Architecture Decision Records, ADR）。

---

## ADR-001：不採用 `makiisthenes/TiktokAutoUploader` 作為六張照片輪播上傳引擎

- **日期**：2026-10-20
- **狀態**：已採納（Accepted）
- **背景**：[`MASTER_SPEC.md`](MASTER_SPEC.md) 要求於第一階段驗證 `makiisthenes/TiktokAutoUploader`（`autotok`）是否支援六張獨立 JPEG 照片輪播發布。
- **決策依據（事實證據）**：
  1. 審閱該儲存庫最新 `main` 分支（v2.1.0, commit `0f7615589ca30677901ffdd8940c40697aac2e32`）之 [`autotok/cli.py`](https://github.com/makiisthenes/TiktokAutoUploader/blob/0f7615589ca30677901ffdd8940c40697aac2e32/autotok/cli.py) 與 [`autotok/uploader.py`](https://github.com/makiisthenes/TiktokAutoUploader/blob/0f7615589ca30677901ffdd8940c40697aac2e32/autotok/uploader.py)，其上傳參數僅接受 `-v / --video` 或 `-yt / --youtube`，且在 `_transfer` 與 `_payload` 硬編碼 `FileType=video`、`/api/v1/video/upload/auth/` 與 `video_id`。
  2. 在 [GitHub Issue #70 ("Can I use this tool to upload photos to TikTok?")](https://github.com/makiisthenes/TiktokAutoUploader/issues/70) 中，專案作者 `makiisthenes` 明確回覆：「*I have not supported photos in this repository, it only supports videos mp4*」。
  3. 該專案採用 `AGPL-3.0` 授權，且依賴網頁端 `sessionid` cookie 與 `X-Bogus` 簽章，違反本專案「不規避風控、保護 `@carry.vietnam.tw` 帳號安全」原則。
- **決策**：不將 `autotok` 納入依賴；在 [`app/adapters.py`](app/adapters.py) 中將 `AutotokCandidateAdapter` 實作為明確拋出 `UnsupportedFeatureError`，防止誤用。

---

## ADR-002：採用 `UploaderAdapter` 策略模式，結合官方 Content Posting API 與半自動匯出備案

- **日期**：2026-10-20
- **狀態**：已採納（Accepted）
- **背景**：TikTok 官方 Content Posting API (`POST /v2/post/publish/content/init/`) 原生支援 `media_type="PHOTO"`（最多 35 張 JPEG），但要求：
  1. 圖片必須透過已通過網域驗證（Domain Verification）的 HTTPS URL（`PULL_FROM_URL`）提供。
  2. 未通過 TikTok App Audit 前，`DIRECT_POST` 僅能發布為 `SELF_ONLY`（私人可見），或使用 `MEDIA_UPLOAD` 將圖片傳送至創作者手機 TikTok App 草稿／收件匣。
- **決策**：
  - 定義統一抽象介面 [`UploaderAdapter`](app/adapters.py)，內建四個可替換實作：
    1. `official_api`：支援官方 `PHOTO` 結構之 `DIRECT_POST` 與 `MEDIA_UPLOAD`，並具備嚴格的 `ALLOW_LIVE_PUBLISH` 與 Audit 狀態檢查。
    2. `manual_export`：將通過品質驗證的 6 張 1080×1350 JPEG、越南文標題/內文、5 個 Hashtag 與繁體中文對照匯出為標準內容包，確保在官方 Audit 審核期間仍可每日穩定產出與半自動發布。
    3. `mock`：供本機測試與 CI 驗證。
    4. `autotok_candidate`：明確回報 `UNSUPPORTED_FEATURE`。

---

## ADR-003：Python 開發環境強制使用 `uv` + `.venv`（Python 3.12）

- **日期**：2026-10-20
- **狀態**：已採納（Accepted）
- **背景**：macOS 系統內建 `/usr/bin/python3` 為舊版 Python 3.8.2，且使用者全域規則嚴格要求使用 `uv` 與 `.venv` 建立環境，禁止污染 base 環境或單獨使用 `pip`。
- **決策**：統一使用 `uv python install 3.12` 與 `uv sync` 建立專案專屬 `.venv`（CPython 3.12.15），所有指令皆透過 `uv run ...` 執行。
