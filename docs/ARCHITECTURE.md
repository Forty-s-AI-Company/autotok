# 架構

## 組成

```
app/
  web/          FastAPI + Jinja + htmx 的本機操作台（routes/、templates/、static/）
  markets.py    MarketProfile：每個帳號的語言、時區、發文時間、受眾、合規提醒
  state.py      SQLite：帳號、市場設定、Notion／TikTok 綁定、貼文、研究案例、sync_log
  credentials.py  每帳號 Token 存放（作業系統金鑰圈，無後端時退回 0600 檔案）
  research*.py  研究案例、互動率口徑、選題評分、90 天去重、資料來源 provider
  editorial.py  依選題與帳號市場產生逐頁文案（LLM）
  production.py 生圖、排版、品質檢查與重試
  media.py      圖片驗證（尺寸、JPEG 標頭、頁碼、重複）
  notion_sync.py, integrations/notion/   Notion 寫入、搜尋、欄位檢查、OAuth
  adapters.py   發布通道：official_api、manual_export、mock
  analytics.py  發布後成效彙整與選題權重建議
  engines.py    Codex CLI 偵測
```

## 資料流

研究案例 → 帳號適配度 → 選題 → 文案（依 MarketProfile）→ 圖片 → 品質檢查 → Notion → 排程／發布 → 成效 → 選題權重。

## 本機優先

SQLite 與圖片都在本機，操作台只綁 `127.0.0.1`。GitHub Actions 的 runner 每次都是全新環境，無法共用這個 SQLite，因此雲端只跑測試與 dry-run，不負責狀態。要做雲端排程前，狀態必須先搬到共享資料庫（見 ADR-006）。

## 網頁層

- 只接受本機 Host；所有寫入請求都要帶每次啟動隨機產生的 token（`X-AutoTok-Token`），並檢查 Origin。
- 背景工作一次只跑一個（`web/jobs.py`），開始、結束、摘要與錯誤都寫入 `sync_log`。畫面上的「更新於」都來自這張表或各來源自己的時間戳。
- 圖片只從 `artifacts/` 提供，並擋掉路徑穿越。
- 寫入外部（Notion、發布）之前需要使用者確認。

## 貼文狀態

`DRAFT → RESEARCHED → GENERATING → READY_TO_REVIEW / QUALITY_FAILED → READY → SCHEDULED → PUBLISHING → PUBLISHED / FAILED / UNKNOWN`

品質檢查未通過不得進入 `READY`。`UNKNOWN`（逾時或平台回應不明）時拒絕重傳，必須先確認是否已上線。

## 欄位命名

`CarouselPostPackage` 的 `vi_*` 欄位現在指「發布語言」欄位（依帳號 MarketProfile 可為越南文、泰文等），`zh_tw_*` 指審稿用的繁體中文對照。欄位名稱沿用舊版以避免大規模遷移。

## 排程

`uv run python -m app scheduler tick` 每次執行都是冪等的，由 Windows 工作排程器（或 launchd）每 10 分鐘觸發。`app/scheduler.py` 的 `plan()` 依各帳號當地時間列出動作與狀態，`tick()` 只執行到期且未完成的：

- 研究：當地研究時間（預設 17:00）後，最近 24 小時沒有成功的研究就跑一次（網路搜尋，不可用不退回模擬資料）。
- 製作：準備時間（預設 20:00）後為隔天每條內容線挑題、產文案、生圖、品質檢查；今天尚未到發文時間但還沒有貼文的內容線也會補做。挑題用真實來源案例、適配度 40 分以上、同帳號 90 天不重複、優先配對建議時段，挑不到就失敗並通知。
- 同步 Notion：帳號有綁定時，製作完成後同一輪 tick 就同步。
- 發布：貼文 READY 且到發文時間，2 小時內補發，超過記為錯過。能力為僅匯出的帳號只匯出發布包；公開發布需帳號能力為 public 且開啟「自動發布」。
- 每個動作的紀錄在 `sync_log`（auto_research、auto_produce、notion_sync、auto_publish）；失敗最多重試 3 次、間隔 30 分鐘；同一帳號、步驟、目標狀態沒變就不重複通知。
- 單一執行：`data/scheduler.lock`（超過 2 小時視為殘留）；操作台正在執行工作時 tick 略過。
- 帳號預設不自動；在 設定 → 自動排程 開啟。

共用的製作流程在 `app/pipeline.py`，網頁操作台與排程器都呼叫它。
