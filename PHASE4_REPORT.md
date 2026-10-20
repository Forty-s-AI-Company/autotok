# 第四階段 Notion 持久化圖片同步、共享防重發鎖與本機 Web GUI 報告（PHASE4_REPORT.md）

> **專案名稱**：TikTok 越南市場 AI 成長與自動化發布系統（`@carry.vietnam.tw`）  
> **報告日期**：2026-10-20（`Asia/Taipei`）  
> **階段狀態**：**Phase 4 完成（36 張實體 JPEG 與三時段頁面已成功寫入線上 Notion 資料庫 + 本機 Web GUI 已建立）**

---

## 1. 線上 Notion 資料庫實機同步結果（已驗證）

透過 [`app/notion_sync.py`](app/notion_sync.py) 串接 Notion 官方 **File Upload API**（`POST /v1/file_uploads` ➔ `/send`）與 Pages API，已將 `2026-10-21` 三個時段（共 **18 張 1080×1350 越南文正式 JPEG + 18 張 1080×1350 繁體中文對照 JPEG = 36 張實體圖片**）持久化上傳並寫入您的 `@carry.vietnam.tw｜TikTok 輪播內容資料庫`：

| 時段 (`Asia/Taipei`) | 分類 | 繁體中文主題 (`主題`) | 狀態 | Notion Page ID 與直接開啟連結 | 持久化圖片上傳數 |
|---|---|---|---|---|---|
| **`2026-10-21 07:00`** | `家庭情緒` | 在台異鄉打拼必看：保持安定心態與家人溫暖連結的 6 個習慣 | `圖片完成` | [`3ff0510e-6af5-81df-aedc-d66892694686`](https://app.notion.com/p/6-3ff0510e6af581dfaedcd66892694686) | 6 張越文圖 + 6 張中文對照圖 |
| **`2026-10-21 11:30`** | `台灣生活` | 在台越南人必收！台灣超商 6 個超神實用功能全整理 | `圖片完成` | [`3ff0510e-6af5-81a4-9866-cb72cfe59038`](https://app.notion.com/p/6-3ff0510e6af581a49866cb72cfe59038) | 6 張越文圖 + 6 張中文對照圖 |
| **`2026-10-21 18:00`** | `圖文行銷` | 用手機記錄台灣生活：6 步驟做出高收藏率六頁圖文輪播 | `圖片完成` | [`3ff0510e-6af5-81f0-b87a-c84336da3418`](https://app.notion.com/p/6-3ff0510e6af581f0b87ac84336da3418) | 6 張越文圖 + 6 張中文對照圖 |

### 每篇 Notion 頁面包含之完整內容：
1. **資料庫屬性欄位**：`主題`、`越南文標題`、`狀態`（`圖片完成`）、`發布日期`（`2026-10-21`）、`時段`（`07:00` / `11:30` / `18:00`）、`分類`（`家庭情緒` / `台灣生活` / `圖文行銷`）、`核心切角`、`CTA`、`越南文圖片`（6 個實體 JPEG 檔案）、`中文版圖片`（6 個實體 JPEG 檔案）、`中文對照`（已勾選 `True`）、頁面封面（`cover` 設為第 1 張越南文圖）。
2. **頁面內頁區塊 (`children`)**：
   - 頂部摘要 Callout（含 Post ID、時段、越中標題）
   - 可一鍵複製的 TikTok 越南文貼文與 5 個 Hashtag 程式碼區塊 + 繁中貼文翻譯
   - `Slide 01 / 06` 至 `Slide 06 / 06`：每頁皆嵌入**實際越南文 1080×1350 JPEG 圖片區塊 (`image` block)**，並附帶越文原文、繁體中文逐頁翻譯與 SHA-256 校驗碼。

---

## 2. 跨執行環境（GitHub Actions vs. Windows）共享防重發機制

- 實作於 [`NotionSyncService.is_slot_already_published_or_locked_in_notion()`](app/notion_sync.py)：
  - 由於 GitHub Actions 每次執行皆為無狀態臨時機器（ephemeral runner），本系統以 **Notion 資料庫作為跨環境共享持久化狀態中心**。
  - 當排程器（GitHub Actions 或 Windows 常開備援機）準備同步或發布指定 `發布日期` + `時段` 時，會先向 Notion 查詢該時段頁面：
    - 若頁面狀態已為 `已發布` 或 `已排程`，共享鎖立即攔截，防止雙重上傳。
    - 若頁面處於 `備稿` 或 `圖片完成`，則更新既有頁面而不重複新增多餘空白頁，且絕不清除任何歷史頁面。

---

## 3. 圖形介面（GUI）雙重支援

依據 [`MASTER_SPEC.md`](MASTER_SPEC.md) 第 10 節（「可選簡單 Web UI，MVP 先 CLI + Markdown 報告；不用過度建置 Next.js SaaS」），本專案同時提供兩種圖形化操作體驗：

1. **雲端／手機端圖形介面：Notion 視覺化內容資料庫**
   - 直接打開您的 Notion 頁面（電腦或手機 Notion App），即可在 Gallery / Table 視圖看到每篇貼文的封面、6 張越南文圖片、6 張中文對照圖與一鍵複製文案。
2. **本機輕量級 Web 圖形控制台（[`app/gui.py`](app/gui.py)）**
   - 執行 `uv run python -m app gui`，即可在本機啟動 `http://127.0.0.1:8765` 圖形化網頁預覽介面，將每篇貼文的 6 張越南文正式圖與 6 張繁體中文對照圖以 4:5 比例並排展示，並檢視安全閘門與研究庫統計。
