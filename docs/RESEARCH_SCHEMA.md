# 市場研究結構、指標口徑與 Notion Schema 映射規範（docs/RESEARCH_SCHEMA.md）

> 依據 [`MASTER_SPEC.md`](MASTER_SPEC.md) 第 4, 5, 7, 10, 11 節建立，對應 Phase 2 實作。

---

## 1. 越南市場搜尋字典（`seed_keywords`）

初始內建 12 組種子關鍵字（定義於 [`app/research.py`](../app/research.py) `INITIAL_SEED_KEYWORDS`），並支援透過 `uv run python -m app seed-keywords --add-vi <越文> --add-zh <繁中>` 動態擴充：

| 越南文關鍵字 (`keyword_vi`) | 繁體中文翻譯 (`translation_zh_tw`) | 搜尋意圖 (`intent`) | 建議對應時段 (`recommended_slot`) |
|---|---|---|---|
| `người Việt ở Đài Loan` | 在台越南人 | 社群認同與在台生活綜合資訊 | `11:30_TAIWAN_LIFE_CULTURE` |
| `người Việt tại Đài Loan` | 在台灣的越南人 | 在台越南族群日常經驗與互助 | `11:30_TAIWAN_LIFE_CULTURE` |
| `cuộc sống ở Đài Loan` | 台灣生活 | 日常食衣住行、交通與生活適應指南 | `11:30_TAIWAN_LIFE_CULTURE` |
| `cộng đồng người Việt Đài Loan` | 在台越南人社群 | 同鄉聚會、活動與支援網絡 | `07:00_EMOTION_GROWTH` |
| `làm việc tại Đài Loan` | 在台工作 | 職場環境、合法工作權益與個人成長 | `07:00_EMOTION_GROWTH` |
| `lương ở Đài Loan` | 台灣薪資 | 公開基本工資法規知識與生活開支規劃 | `07:00_EMOTION_GROWTH` |
| `xuất khẩu lao động Đài Loan` | 赴台海外勞務 | 赴台前準備、防詐騙提醒與心情調適 | `07:00_EMOTION_GROWTH` |
| `việc làm thêm ở Đài Loan` | 台灣兼職 | 留學生合法工作證規定與數位技能培養 | `18:00_CREATOR_DIGITAL_GROWTH` |
| `ẩm thực Đài Loan` | 台灣美食 | 夜市小吃、越南人合口味的台灣料理推薦 | `11:30_TAIWAN_LIFE_CULTURE` |
| `du lịch Đài Loan` | 台灣旅行 | 假日搭捷運/火車一日遊景點與拍照攻略 | `11:30_TAIWAN_LIFE_CULTURE` |
| `khác biệt Việt Nam Đài Loan` | 台越差異 | 台越文化、飲食、垃圾車、超商服務對比 | `11:30_TAIWAN_LIFE_CULTURE` |
| `người Việt nhớ nhà` | 越南人思鄉 | 異鄉打拼情緒共鳴、家人牽掛與自我鼓勵 | `07:00_EMOTION_GROWTH` |

---

## 2. 研究案例模型（`research_cases`）與可信度鐵律

實作於 [`app/models.py`](../app/models.py) `ResearchCase` 與 [`app/state.py`](../app/state.py) `research_cases` 資料表：

1. **缺失指標絕不預設為 `0`**：
   - `views`, `likes`, `comments`, `shares`, `follower_count`, `attributed_new_followers` 在資料庫中皆為 Nullable `INTEGER`。若公開來源未提供分享數或粉絲數，必須維持 `None`（SQL `NULL`），並將欄位名稱記入 `missing_fields`。
2. **自然流量與廣告樣本嚴格分離**：
   - `source_type` 明確區分為 `"organic"` 與 `"ad"`（例如 Creative Center Top Ads）。[`build_topic_opportunities_from_cases()`](../app/research.py) 在計算自然爆款結構時會自動過濾掉 `"ad"` 樣本，防止付費買量數據誤導自然流量選題。
3. **互動率口徑（`compute_engagement_metrics`）**：
   - **`full_engagement_rate`**：當 `views > 0` 且 `likes`, `comments`, `shares` 三者皆齊全時，$\text{互動率} = \frac{\text{讚} + \text{留言} + \text{分享}}{\text{觀看}}$。
   - **`partial_engagement_rate`**：當 `views > 0` 但缺漏任一互動欄位時，僅加總可得欄位並明確標註「部分互動率（不可跨口徑與完整互動率直接比較）」。
   - **`follower_conversion_rate`**：僅在具備可合理歸因之 `attributed_new_followers` 時計算，外部公開研究案例一律為 `None`。

---

## 3. 選題研究性評分（`topic_opportunities`）與 90 天去重

1. **五維度加權公式（內部研究假設，非 TikTok 官方演算法分數）**：
   $$\text{Score} = 0.35 \times \text{在台受眾相關性} + 0.25 \times \text{追蹤動機} + 0.20 \times \text{分享收藏} + 0.10 \times \text{輪播適配} + 0.10 \times \text{資料可信度}$$
2. **90 天雙重查重（`check_90_day_dedup`）**：
   - 比對近 90 天內已入庫之 `topic_opportunities`：若來源 URL 已被使用過，或越南文主題名稱詞彙重疊度 $\ge 70\%$，則自動標記為 `DUPLICATE_REJECTED`。

---

## 4. Notion 真實 Schema 唯讀探測與映射（`app/notion_schema.py`）

- 目標資料庫 ID：`f1bc34396f154e62bca90c9deea19d7a`（Data source `4d575a73-4f98-4ff6-b3f8-da68c40c7125`）。
- 執行 `uv run python -m app inspect-notion` 時，系統只發送唯讀 `GET /v1/databases/{database_id}`，解析真實欄位型別（`title`, `status`, `select`, `date`, `files`, `url` 等），並列出建議映射與尚未建立的欄位，**絕不擅自猜測欄位寫入或清除任何既有歷史頁面**。
