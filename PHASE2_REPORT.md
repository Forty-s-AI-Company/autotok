# 第二階段研究 Agent 與內容庫交付報告（PHASE2_REPORT.md）

> **專案名稱**：TikTok 越南市場 AI 成長與自動化發布系統（`@carry.vietnam.tw`）  
> **報告日期**：2026-10-20（`Asia/Taipei`）  
> **階段狀態**：**Phase 2 核心開發與測試完成（Notion 實機連線為 `BLOCKED_NEEDS_AUTH`，待填入 `NOTION_TOKEN`）**

---

## 1. Phase 2 完成項目與程式碼對應

依據 [`MASTER_SPEC.md`](MASTER_SPEC.md) 第 4, 5, 7, 10, 11 節規範，本階段已完成以下實作：

1. **越南市場搜尋關鍵字字典（[`app/research.py`](app/research.py) & [`app/state.py`](app/state.py)）**：
   - 完整內建 `MASTER_SPEC.md` 第 5.2 節之 12 組越南文種子關鍵字（如 `người Việt ở Đài Loan`、`cuộc sống ở Đài Loan`、`ẩm thực Đài Loan`、`người Việt nhớ nhà` 等），包含繁體中文翻譯、搜尋意圖、時效性與建議對應時段（07:00 / 11:30 / 18:00）。
   - 提供 CLI `uv run python -m app seed-keywords` 寫入持久化資料庫並支援動態擴充關鍵字。
2. **研究來源介面 `ResearchProvider` 與 30–50 篇批次研究管線（[`app/research.py`](app/research.py)）**：
   - 實作 `JsonFileResearchProvider`（載入公開可驗證或外部擷取之真實案例 JSON/JSONL；若真實案例不足 30 篇，如實回報 `shortfall_notice`，絕不捏造湊數）與 `MockResearchProvider`（產生 35 篇標記 `is_mock=True`、`evidence_level="mock_fixture"` 的測試案例）。
   - **廣告與自然流量嚴格分離**：`SourceType.AD`（Top Ads 廣告樣本）獨立標記，不混入自然流量（`SourceType.ORGANIC`）爆款結構拆解。
3. **互動率口徑分離與缺失值保護（[`compute_engagement_metrics()`](app/research.py)）**：
   - 缺失指標在模型與 SQLite 中一律保存為 `None` / `NULL`，**絕不預設填 `0`**。
   - 嚴格區分 `full_engagement_rate`（讚+留言+分享齊全）與 `partial_engagement_rate`（部分指標缺失，附帶禁止跨口徑比較警語）；無歸因資料時 `follower_conversion_rate` 強制為 `None`。
4. **選題研究性評分與 90 天去重（[`compute_topic_score()`](app/research.py) & [`check_90_day_dedup()`](app/research.py)）**：
   - 實作 35% 在台受眾相關性、25% 追蹤動機、20% 分享收藏、10% 輪播適配、10% 資料可信度之加權評分（附非官方演算法分數聲明）。
   - 實作 90 天內來源 URL 與越南文主題名稱相似度（$\ge 70\%$ 攔截）去重機制。
5. **Notion 真實 Schema 唯讀探測器（[`app/notion_schema.py`](app/notion_schema.py)）**：
   - 實作 `NotionInspector`，針對目標資料庫 `f1bc34396f154e62bca90c9deea19d7a` 發起唯讀 Schema 探測並自動產生欄位映射建議，不猜欄位、不修改或清除任何歷史內容。
   - 當未設定 `NOTION_TOKEN` 時，明確回報 `BLOCKED_NEEDS_AUTH`。

---

## 2. 可重現執行命令與測試結果

```bash
export PATH="$HOME/.local/bin:$PATH"

# 1. 執行靜態檢查與完整測試套件（Phase 1 + Phase 2 共 18 項測試）
uv run ruff check .
uv run pytest -v

# 2. 初始化並檢視 12 組越南文搜尋關鍵字字典
uv run python -m app seed-keywords

# 3. 執行 35 篇批次研究、互動率口徑分析、90 天去重與三時段選題評分
uv run python -m app research-batch --provider mock --max-cases 35

# 4. 探測 Notion 真實 Schema（未設定 NOTION_TOKEN 時安全攔截並回報 BLOCKED_NEEDS_AUTH）
uv run python -m app inspect-notion
```

- **測試結果**：`18 passed in 1.13s`（[`tests/test_phase1.py`](tests/test_phase1.py) 11 項 + [`tests/test_phase2.py`](tests/test_phase2.py) 7 項全數通過）。

---

## 3. 待授權事項與進入 Phase 3 建議

- **Notion 真實 Schema 實機讀取（`BLOCKED_NEEDS_AUTH`）**：若您希望現在讀取既有 Notion 資料庫 `f1bc34396f154e62bca90c9deea19d7a` 的實際欄位，請於 `.env` 填入 `NOTION_TOKEN` 後執行 `uv run python -m app inspect-notion`。
- **是否適合進入 Phase 3（越南文六頁製圖與品質 Gate）**：**適合**。目前 `topic_opportunities` 已能產出對應每日三時段（07:00 / 11:30 / 18:00）的候選主題，可直接銜接 Phase 3 的 Editorial 六頁越中腳本生成、場景圖與越南文分離式排版渲染。
