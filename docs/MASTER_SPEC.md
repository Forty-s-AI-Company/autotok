> 這份規格是「越南文 × 在台越南人」市場的原始需求，現在對應市場範本 `vi-tw`。多帳號、多語系市場的現況設計見 ARCHITECTURE.md。內容線時間、固定 6 張、固定 5 個 hashtag 等描述已不是系統的硬性限制。

# TikTok 越南市場 AI 成長與自動化發布系統｜完整產品與工程規格

版本：v1.0｜2026-10-20｜定位：個人自用、非 SaaS

> 本文件是專案的單一需求基準（source of truth）。實作時應維持需求、事實、假設與阻礙事項的區別；未驗證能力不得標示為完成。

## 1. 專案目標與非目標

**主要目標**：為 TikTok `@carry.vietnam.tw` 建立不依賴 Metricool 的內容研究→增粉策略→越南文六頁輪播製作→品質檢查→Notion 建檔→排程發布→成效回饋系統。Mac 開發；GitHub Actions 優先雲端執行；Windows 常開本機備援。以合法授權官方 API 為優先；若不可取得公開發布權限，提供合規半自動發布並明確標示限制。

**非目標**：多租戶 SaaS、會員登入、收費訂閱、代理大量帳號、保證爆款、無審核權限硬宣稱可公開自動發布、使用未授權方法繞過平台反濫用。

**關鍵驗證假設**：`makiisthenes/TiktokAutoUploader`（autotok）README 顯示影片上傳、排程、Python/JS/Go、MCP、Docker；**未確認支援六張照片輪播**。此項必須在第一階段實際驗證，不能預設可用。非官方上傳涉及網站變更、帳號安全與服務條款風險；研究允許，但不得開發 CAPTCHA 破解、偽裝或規避反濫用機制。

## 2. 受眾、品牌與營運原則

- 主要：在台越南移工、留學生、新住民、青年及上班族；次要：越南本地對台灣感興趣的人。
- 帳號：`@carry.vietnam.tw`；品牌標示：`Carry | Đài Loan × Việt Nam`。
- 近期目標：真實越南語粉絲、自然曝光、分享、收藏、留言、長期信任；長期目標：台越交流及透明的商業合作機會。
- 不虛構職缺、簽證資格、薪資、收入、平台規則或成功案例。具體商業合作需適時揭露 Amway／獨立經營性質、成本、收入不確定性及在台合法工作限制。

## 3. 固定內容產線

時間全部採 `Asia/Taipei`：

| 時間 | 工作或主題 |
|---|---|
| 每週一次（可設定） | 深度研究 30–50 篇真實案例（不足如實標示） |
| 每日研究時段，20:00 以前 | 增量蒐集、更新研究庫、選題候選 |
| 每日 20:00 | 準備隔日三篇正式內容（製圖與資料同步） |
| 次日 07:00 | 家庭／夢想／情緒／成長 |
| 次日 11:30 | 台灣生活／文化／美食／旅行 |
| 次日 18:00 | 手機拍攝／創作／網路行銷／數位成長 |
| 發布後 | 驗證狀態、取得可用指標、回饋策略 |

每篇六張**獨立** 4:5、1080×1350 JPEG，越南文圖中文字、六頁逐頁繁體中文對照、越南文標題與貼文、繁中翻譯、**恰好五個簡明相關 hashtag**。禁止將六宮格誤作六張。不得在品質不足時以幾何圖示模板湊數。

## 4. 多 Agent 分工與資料流

1. **Market Research Agent**：TikTok 公開搜尋、Creative Center、Creator Search Insights（有權限時）、Top Ads（單獨標註廣告樣本）、授權／公開來源。保留 URL、擷取日期、發布日期、創作者、觀看／讚／留言／分享／粉絲（可得則記錄）、可驗證狀態、缺失欄位。
2. **Growth Strategy Agent**：分群比較、爆款結構拆解、目標受眾契合、分享與追蹤誘因、可轉成六張圖程度。不能將高觀看等同帶粉，也不能以缺失指標默認 0。
3. **Editorial Agent**：90 天主題及 URL 去重、三時段選題、越南文在地化、六頁腳本、繁中逐頁翻譯、原創改寫與主題風險審核。
4. **Visual Production & QA Agent**：先場景攝影／擬真素材，後由可控排版系統疊加越南文；六張風格一致而構圖多樣。驗證尺寸、編碼、拼寫、安全區、圖文一致性、重複、文化正確性。
5. **Publishing & Operations**（建議用確定性程式，不交由自由生成 Agent 直接任意發布）：內容佇列、帳號授權、時間／重試／狀態機、Notion 同步、通知、稽核紀錄。
6. **Analytics Agent**：官方或有授權來源之指標；區分公開數據、創作者後台可驗證數據、估計。用自身實際增粉與受眾地區等資料調整下一週企劃。

資料流：研究案例→機會主題→內容版本→六張正式圖→品質 Gate→排程→平台確認→成效→研究／選題權重更新。

## 5. 市場研究與可信度標準

### 5.1 搜尋來源
- TikTok 原生搜尋／可存取的公開創作者；Creative Center；Creator Search Insights（可用時）；Top Ads（**廣告**，不得混同自然流量）；其他可靠公開來源。
- 若無法瀏覽影片，不得編造看過的開場、節奏、留言。保留可直接開啟的來源或明確的「未驗證」。

### 5.2 搜尋字典（原始種子）
- `người Việt ở Đài Loan`｜在台越南人
- `người Việt tại Đài Loan`｜在台灣的越南人
- `cuộc sống ở Đài Loan`｜台灣生活
- `cộng đồng người Việt Đài Loan`｜在台越南人社群
- `làm việc tại Đài Loan`｜在台工作
- `lương ở Đài Loan`｜台灣薪資
- `xuất khẩu lao động Đài Loan`｜赴台海外勞務
- `việc làm thêm ở Đài Loan`｜台灣兼職
- `ẩm thực Đài Loan`｜台灣美食
- `du lịch Đài Loan`｜台灣旅行
- `khác biệt Việt Nam Đài Loan`｜台越差異
- `người Việt nhớ nhà`｜越南人思鄉
- 每次研究擴充關鍵字並記錄中文翻譯、意圖、時效性及來源。

### 5.3 研究節奏
- **每週深度批次**：以近 7–30 天、必要時 90 天為範圍，目標 30–50 個具來源的真實案例；達不到也照實報數量。
- **每日增量**：檢查新 URL、新創作者、新切角，避免重複取得昂貴資料或在 20:00 製圖前執行過久。
- **週期性復盤**：根據已公開且可以核實的指標，更新主題優先級，保留研究假設與修訂紀錄。

### 5.4 分析方式
- 可完整取得資料時：互動率＝(讚＋留言＋分享)/觀看；缺失指標時改用「部分互動率」並明確註明，不能跨口徑比較。
- 新增追蹤轉換率＝可合理歸因新增追蹤/觀看；無歸因資料不得聲稱確認。
- 選題研究性評分：在台受眾相關性 35%、追蹤動機 25%、分享收藏 20%、輪播適配 10%、資料可信度 10%。**此為可調內部假設，不是官方演算法分數**。
- 分析 3 秒鉤子／第一張封面、敘事類別、互動動機、原創可轉換性與著作權限制。

## 6. 圖文規格與品質 Gate

- 07:00：生活化人物、異鄉工作與家庭情緒；11:30：真實台灣街景、夜市、食物、交通；18:00：真實手機與創作者工作情境。
- 禁止：單一平面背景＋通用圖示、全組共用同一模板、巨大無意義留白、AI 偽文字、錯誤地標、內容裁切、未經查證政策資訊。
- 圖片生成與文字排版分離；可使用 Pillow 或 HTML/CSS 渲染，採支援越南文聲調的合法字型。
- 六張品質檢查：存在性、JPEG 真實 MIME、1080×1350、編號 01–06、內文文法與安全區、同題一致、可視化多樣性、檔案讀取與持久存儲。
- 品質不合格→記錄原因→限次重製問題頁；未達合格→`QUALITY_FAILED`，不得發布。
- AI 模型及圖像檢測成本另行統計；API 未設定時使用 Mock 供測試，不得宣稱 Mock 是正式成品。

## 7. Notion／資料庫模型

既有 Notion 內容資料庫：`https://app.notion.com/p/f1bc34396f154e62bca90c9deea19d7a`；data source `4d575a73-4f98-4ff6-b3f8-da68c40c7125`。**先讀取真實 schema；不可猜欄位或任意清除既有歷史內容。**

三個邏輯資料庫（可用 Notion + SQLite／雲端持久化組合）：

1. `research_cases`：來源 URL（唯一）、creator、market、source_type（organic/ad）、published_at、observed_at、view/like/comment/share/follower、缺失狀態、hook_notes（證據）、confidence。
2. `topic_opportunities`：topic_id、受眾、切角、research_case_ids、研究分數、90 天查重結果、實驗假設、候選狀態。
3. `content_posts`：post_id、預定時間、分類、topic_id、六頁原文與譯文、六張圖、正式圖 checksum／版本、五標籤、品質報告、Notion page ID、platform post ID、發布狀態及錯誤、觀察期指標。

Notion 每篇內頁須有六張實際越南文圖片（而非圖片說明）、逐頁翻譯、貼文、版本與真實狀態；資料庫封面或圖片欄位也需確實設值。圖片需持久、可存取，非 GitHub Actions 暫存。

## 8. 發布架構、排程與備援

- Mac 開發；GitHub Private repo；GitHub Actions 為主；Windows Task Scheduler 為備援。
- GitHub Actions cron 使用 UTC；需以 Asia/Taipei 驗證。GitHub 定時啟動**不保證準點**，需要可觀測的延遲處理與截止策略。
- 官方 TikTok Content Posting API 優先，審核／照片 Direct Post 權限須核實。平台對私人用途可能有限制；不能預設必定可公開發文。
- autotok 屬非官方 TikTok 網頁上傳流程，需審閱程式、授權、合規、登入狀態與風險；**目前僅已確認影片功能，照片輪播必須獨立驗證**。
- 若六張輪播不能安全自動發布：提供下載套件／TikTok 內完成操作／已核准官方上傳流程等明確半自動備案。
- 內容準備與發布工作分離；共享發文鎖與狀態避免 GH Actions / Windows 雙重上傳。不能只靠 Actions ephemeral local DB 防重；應使用具交易一致性的共享持久化資料庫。
- `DRAFT → RESEARCHED → GENERATING → READY_TO_REVIEW/QUALITY_FAILED → READY → SCHEDULED → PUBLISHING → PUBLISHED/FAILED/UNKNOWN`；未知狀態先查詢，不能盲目重傳。
- 只在實際公開狀態可驗證時標記 `PUBLISHED`。平台異常需通知而非假裝成功。

## 9. Security、授權與合規

- 不提交 Cookie、Session、Token、API Key 到 Git；建立 `.env.example`、`.gitignore`、secret scanning、log redaction、最小權限。
- 臨時 GitHub Runner 不能永久保存 Session；憑證保存必須經授權且加密；不得把 session plaintext 放入 artifacts。
- 不破解 CAPTCHA、不繞過 2FA，不規避平台風控，不隱瞞非官方方案可能導致帳號受限。
- 開源授權檢查 AGPL-3.0；如修改和部署 autotok 必須遵循實際授權義務。
- 以測試帳號／Mock 預設開發；外部公開發布、憑證輸入、購買主機／付費 API 等需使用者授權。

## 10. 技術架構建議（可依研究調整並記錄 ADR）

- Python 3.12+、uv、pytest、ruff、Pydantic settings；CLI 優先；SQLite 本機原型。
- `UploaderAdapter`：official_api、autotok_compat（實測後）、manual_export/mock。
- `ResearchProvider`：TikTok 可授權／公開資料與 mock；所有研究結果附 `source_url`,`collected_at`,`evidence_level`。
- `ImageProvider`：可替換生成模型；`Renderer`：排版越南文；`QualityGate`：機器驗證＋可選 Vision。
- Notion adapter、post state repository、durable artifact storage、notification adapter。
- 可選簡單 Web UI，MVP 先 CLI + Markdown 報告；不用過度建置 Next.js SaaS。

## 11. 階段、交付物、驗收

### Phase 1｜發布可行性驗證與工程骨架（**立即執行**）
- 審核 autotok、官方 API、照片輪播的實際可用性與風險。
- 建立 repository、Python 專案、macOS 啟動、測試、CI、Windows 相容性骨架、可替換 UploaderAdapter、`mock` 六張 JPEG 工作流程、CLI。
- 如具備授權與測試帳號，執行六張照片真實端到端實驗；否則將未測試標記 BLOCKED，記錄待授權步驟；**不得不經同意在正式帳號發文**。
- 交付 `PHASE1_REPORT.md`：證據／程式碼位置／可運行命令／真實測試結果／風險／Go-NoGo 建議。

### Phase 2｜研究 Agent 與內容庫
- 研究來源介面、30–50 篇批次研究（有資料才建）、關鍵字庫、案例證據、90 天去重、主題機會評分、Notion schema 映射。

### Phase 3｜越南文六頁製圖與品質
- 內容企劃、翻譯、場景圖片生成、分離式文字排版、正式 1080×1350 六張 JPEG、品質 Gate 與重試。

### Phase 4｜Notion、排程與發布整合
- 完整資料庫同步、持久化圖像、雙執行環境去重、通知、GitHub Actions/Windows 執行器；可公開自動發布依核准能力決定。

### Phase 5｜成效回饋與優化
- 合法取得的後台／公開指標、研究對照、自身新增粉絲歸因可信度、主題表現與 A/B 實驗策略。

### 每階段通用完成定義
- 真實執行命令與測試報告；實際完成功能與 mock 清楚分離；無 hardcoded secrets；可重現安裝；文件和 `TASKS.md` 更新；未解阻礙不宣稱完成。

## 12. 專案文件規範

至少：`README.md`, `MASTER_SPEC.md`, `ARCHITECTURE.md`, `PROJECT_PLAN.md`, `TASKS.md`, `RISKS.md`, `DECISIONS.md`, `SETUP_MAC.md`, `SETUP_WINDOWS.md`, `PHASE1_REPORT.md`。可增加 `docs/RESEARCH_SCHEMA.md`, `docs/SECURITY.md`, `docs/OPERATIONS.md`。

## 13. 第一階段的關鍵 Go／No-Go 門檻

- **GO**：六張獨立圖片在合法取得的發布通道可由程式提交且能驗證公開狀態，或已有已獲審核可行之官方整合路徑。
- **CONDITIONAL**：可建立圖片與送出草稿／半自動流程，但無法自動公開；後續開發時將發布器標示為半自動。
- **NO-GO／BLOCKED**：目標通道不支援或涉及不可接受的安全合規風險；暫不投資大量製圖排程整合，回報替代方案。

## 14. 成本與限制揭露

不使用 Metricool 並不代表整套系統零成本：圖像 AI API、可能的儲存空間、通知、雲端任務額度及維護時間需計價。GitHub Actions 不保證準點、不能替代可靠持續執行機器；Windows 備援需要保持開機且不可與 GitHub Actions 同時重複發布。
