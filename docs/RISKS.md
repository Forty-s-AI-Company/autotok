# 風險管理與限制登錄表（RISKS.md）

> 依據 [`MASTER_SPEC.md`](MASTER_SPEC.md) 第 8、9、13、14 節整理。

---

| 風險編號 | 風險類別 | 風險描述與實證依據 | 嚴重度 | 緩解策略與系統設計對策 |
|---|---|---|---|---|
| **R-01** | **非官方工具限制與風控** | `makiisthenes/TiktokAutoUploader` (`autotok`) **不支援照片輪播**（僅支援 MP4 影片，見 Issue #70 與 `autotok/uploader.py`），且透過非官方網頁端 `X-Bogus` 簽章與 Session Cookie 操作，曾有社群回報畫質降低或帳號受限（Issue #78, #109）。 | 高 | 在 [`app/adapters.py`](../app/adapters.py) 將 `autotok_candidate` 明確封鎖並拋出 `UnsupportedFeatureError`；不採用非官方 Cookie 破解或偽裝上傳照片。 |
| **R-02** | **官方 API 未審核僅限私密發布 (`SELF_ONLY`)** | TikTok Content Posting API (`POST /v2/post/publish/content/init/`) 雖原生支援 6 張 JPEG 輪播（`media_type="PHOTO"`），但未通過官方 App Audit 前，`DIRECT_POST` 僅能發布為 `SELF_ONLY`（私人可見），無法直接設為 `PUBLIC_TO_EVERYONE`。 | 高 | 1. 短期採 `post_mode="MEDIA_UPLOAD"`（官方 API 傳送 6 張圖至創作者 TikTok 手機收件匣，按一下即可公開）或 `manual_export` 半自動匯出包。<br>2. 同步準備申請 TikTok Developer Portal App Audit。 |
| **R-03** | **官方 API 圖片 URL 網域驗證要求** | 官方 API 的照片上傳僅支援 `source="PULL_FROM_URL"`，且所有圖片 URL 必須位於已在 TikTok Developer Portal 完成 Domain Verification（DNS TXT 或路徑前綴驗證）之 HTTPS 網域。 | 中 | Phase 4 整合 Cloudflare R2 / AWS S3 / GitHub Pages 自訂子網域作為持久化圖片空間，並在 Developer Portal 完成網域驗證。 |
| **R-04** | **GitHub Actions Cron 延遲與無狀態特性** | GitHub Actions `schedule` 不保證準點（尖峰時段可能延遲 5–30 分鐘），且每次 Runner 皆為全新臨時環境（ephemeral），本地 SQLite 無法跨次或跨 Windows 備援機保存狀態。 | 中 | 1. 內容生成提前於前一日 `20:00` (`Asia/Taipei`) 完成並寫入持久化儲存。<br>2. Phase 4 將 [`PostStateRepository`](../app/state.py) 背後的儲存替換為雲端共享資料庫（或 Notion 原子狀態欄位），搭配 `acquire_publish_lock` 防止雲端與 Windows 重複發文。 |
| **R-05** | **開源授權感染風險 (`AGPL-3.0`)** | `makiisthenes/TiktokAutoUploader` 採用 `AGPL-3.0` 授權，若直接複製修改其程式碼並透過網路提供服務，將觸發 AGPL 強制開源義務。 | 中 | 本專案完全獨立撰寫，未複製或依賴 `autotok` 套件，保持私有專案授權純淨。 |
| **R-06** | **憑證與個資外洩風險** | 若將 TikTok OAuth Token、Notion Token 或 Session Cookie 寫入 Git、CI Logs 或 GitHub Actions Artifacts，將導致帳號被盜用。 | 高 | 已建立 [`.gitignore`](../.gitignore) 與 [`app/config.py`](../app/config.py) 的 `redact_sensitive` 日誌遮罩；CI 工作流禁止將任何含憑證檔案上傳為公開 Artifact。 |
| **R-07** | **本機操作台被其他網頁呼叫** | 任何網頁都能對 `127.0.0.1` 發請求，若沒有防護可能消耗生圖額度或寫入 Notion。 | 高 | 只接受本機 Host、寫入請求要帶每次啟動隨機 token 並檢查 Origin（`app/web/security.py`）。 |
| **R-08** | **研究資料不完整或不可信** | 網路搜尋與手動匯入不一定有完整的觀看、互動、粉絲數；付費第三方資料源可能違反 TikTok 服務條款。 | 中 | 缺值一律保留為空（不當 0）、標示證據等級；第三方資料源預設關閉，由使用者自行評估風險。 |
| **R-09** | **文案引擎產出不實或違規內容** | 涉及簽證、居留、薪資、匯款的題材可能產出無依據或承諾性的說法。 | 高 | 產出後做合規檢查（共用規則＋帳號的合規提醒），敏感題材標示「需合規審核」，嚴重者拒絕。 |
