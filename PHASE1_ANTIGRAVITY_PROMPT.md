# Antigravity｜第一階段執行提示詞：先驗證 TikTok 六張照片輪播

你是本專案的技術負責人與實作工程師。請先完整閱讀同目錄 `MASTER_SPEC.md`，以該文件為唯一需求基準，**目前只執行 Phase 1，不可擅自提前開發完整研究 Agent、AI 製圖或正式 Notion 同步**。

## 此階段唯一核心決策

**能否在 Mac 開發、GitHub Actions 或 Windows 執行環境下，安全、可驗證地發布 TikTok 六張獨立 JPEG 照片輪播（不是六張拼圖、不是轉成影片）？**

優先研究官方 TikTok Content Posting API 的照片 Direct Post、授權與正式公開發布的審核限制。其次深入研究 `https://github.com/makiisthenes/TiktokAutoUploader`：讀取最新 README、原始碼、Issues、近期 Commits、授權；確認它的 `autotok` 是否提供 photo/carousel 介面。不要將影片上傳能力當成圖片功能。

## 執行順序

1. 檢查目前工作目錄、Git、Python、uv、測試工具、macOS 版本與權限；不得修改無關既有專案。建立新的獨立 private-repo-ready 專案結構。
2. 建立 `PROJECT_PLAN.md`、`ARCHITECTURE.md`、`TASKS.md`、`RISKS.md`、`DECISIONS.md`、`README.md` 和 `PHASE1_REPORT.md`。
3. 研究官方 API（包含照片輪播張數、直接公開、OAuth、審核、圖片 URL、排程是否需自行實作）及 autotok（CLI、MCP、登入 session、Python SDK、Docker、Chromium、照片功能和授權）。所有關鍵結論附連結或程式碼路徑與版本；不可編造測試。
4. 檢查 TikTok 網頁端／現成上傳器能否支援**原生多照片輪播**。如實作需操作正式帳號或登入，先停在授權邊界提出一組最少必要問題。不得使用 CAPTCHA 繞過、反偵測偽裝、風控規避手段。不得自行在真實帳號公開發文。
5. 設計 `UploaderAdapter` 與 `mock`, `official_api`, `autotok_candidate`, `manual_export` 可替換實作。若某 adapter 尚未可用，需 raise 清楚的 UnsupportedFeature／Blocked，不得假成功。
6. 建立一個可重現的**六張測試 JPEG**產生器（明顯標記 TEST/MOCK；1080×1350、順序 01–06）。建立 `python -m app verify-media <dir>` 檢查六張、JPG 真實格式、尺寸、排序；建立 `python -m app publish --dry-run` 輸出安全的發文計畫。正式 `publish` 默認禁用，直到明確設定 `ALLOW_LIVE_PUBLISH=true` 及使用者授權。
7. 配置 Python 3.12+、uv、pytest、ruff、Pydantic、跨平台路徑；本機 SQLite 可作原型，但明確指出 GitHub Actions 需要共享持久化狀態才能防重發。
8. 建立 GitHub Actions `CI`（只跑測試）和可由 `workflow_dispatch` 手動觸發的 dry-run 工作流程。第一階段**不建立會自動公開發文的排程**。避免把登入 Cookie 放在 logs/artifacts/git。
9. 建立 Windows PowerShell 安裝／dry-run 腳本骨架，測試或靜態檢查跨平台相容。
10. 安裝必要且信任來源的 Skills、套件／工具；需先查來源與安全性。涉及高權限、付款、外部帳號、持久敏感憑證需經我確認。不要因缺少 TikTok/Notion token 停下其他工作。
11. 執行測試與 Ruff、修正錯誤，輸出可重現的 Mac 命令。做好 git commits；若無遠端儲存庫授權，保留本地 commit 並提供設定方式。
12. 寫入 `PHASE1_REPORT.md`：實際環境、研究證據、已有實作、實測項目、未驗證項目、正式 API 核准限制、照片輪播目前狀態、安全與帳號風險、下階段建議。標示 GO／CONDITIONAL／BLOCKED，說明判定依據。

## 驗收檢查

- [ ] `uv sync` 成功；`uv run pytest` 與 Ruff 通過。
- [ ] 生成六張獨立、順序正確的測試 JPG；媒體驗證器會拒絕 PNG、尺寸錯誤、漏圖、重複圖。
- [ ] 在 Mac 執行 CLI dry-run 無外部副作用。
- [ ] GitHub Actions 能執行測試工作流（如未有遠端權限，標示尚未實測）。
- [ ] Windows 兼容性文件／腳本已建立（若沒實際 Windows，標示未實機測試）。
- [ ] TikTok 官方 API 與 autotok 的照片輪播能力都有來源可追查的實際結論。
- [ ] 正式照片輪播的成功與否只依真實可驗證測試；無帳號授權時標記 `BLOCKED_NEEDS_AUTH`，不能宣稱公開自動發布成功。
- [ ] 沒有 Session/Cookie/token 洩露，也沒有真正發出未授權貼文。

## 工作方式

請開始實作，不要只輸出設計建議。自主完成所有無須外部帳號授權的工作；每完成主要里程碑更新 `TASKS.md` 並執行測試。遇到阻礙時，先把可完成工作做完，再整理最少必要的使用者操作。**此輪只交付 Phase 1，且不得以假資料宣稱已通過 TikTok 的真實公開發布。**
