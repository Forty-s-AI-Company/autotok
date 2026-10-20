# 第三階段越南文六頁製圖、品質 Gate 與 Notion 真實 Schema 驗證報告（PHASE3_REPORT.md）

> **專案名稱**：TikTok 越南市場 AI 成長與自動化發布系統（`@carry.vietnam.tw`）  
> **報告日期**：2026-10-20（`Asia/Taipei`）  
> **階段狀態**：**Phase 2 Notion 真實 Schema 實機讀取完成 + Phase 3 越南文六頁製圖與 Quality Gate 完成**

---

## 1. Notion 既有資料庫真實 Schema 實機讀取結果（100% 匹配）

透過您設定於 `.env` 的 `NOTION_TOKEN`，已執行 `uv run python -m app inspect-notion` 唯讀讀取 `@carry.vietnam.tw｜TikTok 輪播內容資料庫`（Database ID: `f1bc34396f154e62bca90c9deea19d7a`，未修改或刪除任何歷史資料），確認真實結構如下：

| 真實 Notion 欄位名稱 | Notion 型別 | 實際可用選項 (`options`) | 對應本系統欄位 |
|---|---|---|---|
| **`主題`** | `title` | — | `post_title_zh`（繁體中文標題） |
| **`越南文標題`** | `rich_text` | — | `post_title_vi`（越南文標題，上限 90 字元） |
| **`狀態`** | `status` | `備稿`、`圖片完成`、`已排程`、`已發布`、`發布失敗` | `status`（貼文生命週期狀態） |
| **`發布日期`** | `date` | — | `scheduled_date`（預定發布日期） |
| **`時段`** | `select` | `07:00`、`11:30`、`18:00`、`19:00`、`20:00`、`21:00` | `time_slot`（每日固定三時段 `07:00` / `11:30` / `18:00`） |
| **`分類`** | `select` | `家庭情緒`、`台灣生活`、`圖文行銷` | `slot_category`（完美對應三時段主題） |
| **`核心切角`** | `rich_text` | — | `core_angle`（選題切角說明） |
| **`CTA`** | `rich_text` | — | `cta_text`（尾頁行動呼籲） |
| **`越南文圖片`** | `files` | — | `vi_carousel_images`（6 張 1080×1350 獨立越南文 JPEG） |
| **`中文版圖片`** | `files` | — | `zh_reference_images`（6 張 1080×1350 獨立繁中對照 JPEG） |
| **`中文對照`** | `checkbox` | — | `has_zh_translation`（是否具備逐頁繁中對照） |
| **`TikTok連結`** | `url` | — | `tiktok_post_url`（發布後驗證之公開連結） |
| **`Metricool連結`** | `url` | — | 舊系統歷史欄位（保留相容，不清除） |

---

## 2. Phase 3 完成項目（越南文六頁製圖、分離式排版與 Quality Gate）

實作於 [`app/production.py`](app/production.py) 與 [`app/cli.py`](app/cli.py) `produce-daily` 子命令：

1. **每日三時段（07:00 / 11:30 / 18:00）Editorial 企劃與合規審核**：
   - `07:00`（`家庭情緒`）：異鄉打拼情緒安定與家庭聯繫 6 頁腳本。
   - `11:30`（`台灣生活`）：台灣超商 6 大實用生活服務 6 頁腳本。
   - `18:00`（`圖文行銷`）：用手機實拍與製作 6 頁 4:5 圖文輪播技巧。
   - 內建 `audit_editorial_compliance()`，自動攔截「保證高薪 (`cam kết lương cao`)」、「保證過簽 (`bao đậu visa`)」等違規宣稱。
2. **背景生成與越南文文字排版嚴格分離**：
   - `SceneImageProvider` 先產出每頁焦點、光影與景深（Bokeh）各自獨立的 1080×1350 底圖。
   - `SlideTypographyRenderer` 再使用支援完整越南文聲調與繁體中文的系統字型，於安全區（Safe Zone，避開右側 TikTok 互動列與底部文案區）精準排版編號 `01–06` 與雙語圖卡。
   - **雙版本同步輸出**：每篇貼文同時產出 `vi/`（6 張越南文正式 JPEG）與 `zh_tw/`（6 張繁體中文對照版 JPEG），完美對應您 Notion 資料庫中的 `越南文圖片` 與 `中文版圖片` 欄位。
3. **Quality Gate 與問題頁限次重製（Retry）**：
   - 自動檢查 6 張存在性、真實 JPEG Magic Bytes、1080×1350 尺寸、SHA-256 不重複、6 頁文字不重複，以及 **6 張圖之間的像素色彩多樣性（拒絕全組共用同一死板純色模板）**。
   - 當單頁出現尺寸或品質異常時，自動定位該問題頁碼並限次重製（`max_retries=2`）；若重試後仍不合格則標記為 `QUALITY_FAILED` 並阻擋發布。

---

## 3. 可重現執行命令與測試結果

```bash
export PATH="$HOME/.local/bin:$PATH"

# 1. 執行靜態檢查與完整測試套件（Phase 1 + Phase 2 + Phase 3 共 23 項測試）
uv run ruff check .
uv run pytest -v

# 2. 唯讀探測真實 Notion 資料庫 Schema
uv run python -m app inspect-notion

# 3. 一次生產隔日三時段（07:00 / 11:30 / 18:00）共 3 篇貼文（18 張越文 JPEG + 18 張繁中對照 JPEG）
uv run python -m app produce-daily --date 2026-10-21 --slot all
```

- **測試結果**：`23 passed in 5.10s`（[`tests/test_phase1.py`](tests/test_phase1.py) 11 項 + [`tests/test_phase2.py`](tests/test_phase2.py) 7 項 + [`tests/test_phase3.py`](tests/test_phase3.py) 5 項全數通過）。
