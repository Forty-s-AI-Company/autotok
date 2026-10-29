# TikTok 照片輪播發布

依 TikTok 官方文件整理（查證日 2026-10-20），2026-10-29 補上收件匣模式的實測結果。標「未驗證」的是沒有用真實帳號實測過的項目。

## 目前使用的模式：收件匣草稿（`TIKTOK_POST_MODE=inbox`）

App 未通過審核時，直接發布（`DIRECT_POST`）只能送私人，且帳號必須是私人帳號。目前改走 `MEDIA_UPLOAD`，把輪播傳到使用者 TikTok 的收件匣，由使用者在 App 內選隱私、標示 AI 生成內容後自己按發布。授權只需要 `user.info.basic`、`video.upload`。

以下是 2026-10-29 用真實帳號（Sandbox）實測到的行為，文件沒有寫明：

| 項目 | 實測結果 |
|---|---|
| 上傳後狀態 | `SEND_TO_USER_INBOX`，代表上傳成功、等使用者處理 |
| 使用者發布後狀態 | **不會變**，仍是 `SEND_TO_USER_INBOX`，查了 6 分鐘以上沒有變成 `PUBLISH_COMPLETE`，所以程式無法自動得知「已發布」 |
| 對應做法 | 控制台內容庫的「標記為已發布」按鈕（`/library/mark-published`）：狀態改 `PUBLISHED`、刪除託管圖片，Notion 隨排程同步 |
| 通知入口 | 草稿只會以系統通知「影片已準備就緒」出現，**通知只會打開最舊的待處理草稿**，不是最新的 |
| 重複草稿 | 同一篇重送，TikTok 會多一份草稿，沒有去重；舊草稿無法在編輯器內刪除，只能發布成「僅限自己」再刪貼文 |
| 圖片保留 | 草稿編輯時 TikTok 可能再讀取原圖，**發布前不能刪託管圖片**，否則編輯器會全黑 |

因此排程在收件匣模式下，前一篇草稿還沒標記為已發布就不送下一篇（`scheduler._pending_inbox_draft`），並通知一次。不要手動重送已在收件匣的貼文。

## 流程（直接發布模式）

1. 使用者授權（Login Kit），scope：`video.publish`（直接發布）、`video.upload`（送到收件匣）。收件匣模式只要求 `user.info.basic,video.upload`。
2. `POST /v2/post/publish/creator_info/query/`：取得目前可用的 `privacy_level_options`。
3. `POST /v2/post/publish/content/init/`：`media_type=PHOTO`、`post_mode=DIRECT_POST`、`source=PULL_FROM_URL`，回傳 `publish_id`。
4. `POST /v2/post/publish/status/fetch/`：輪詢，`PUBLISH_COMPLETE` 為成功，`FAILED` 看 `fail_reason`。

本專案對應：`app/integrations/tiktok.py`（授權與 API 呼叫）、`app/publisher.py`（閘門、託管、輪詢、狀態）。

## 限制

| 項目 | 內容 | 來源 |
|---|---|---|
| 圖片數 | 1 到 35 張公開網址，`photo_cover_index` 必填 | [photo post](https://developers.tiktok.com/doc/content-posting-api-reference-photo-post) |
| 標題／描述 | 標題 90、描述 4000（UTF-16 字元） | 同上 |
| 圖片來源 | 只支援 `PULL_FROM_URL`，網址須屬已驗證的網域或網址前綴 | [get started](https://developers.tiktok.com/doc/content-posting-api-get-started) |
| 未審核的 App | 所有貼文只能是私人可見；24 小時內最多 5 位使用者可發布 | [content sharing guidelines](https://developers.tiktok.com/doc/content-sharing-guidelines) |
| 隱私等級 | 必須是 `creator_info` 回傳的選項之一，且審核前只能 `SELF_ONLY` | photo post |
| 每日上限 | 每個帳號約 15 篇（所有 API 用戶端共用）；超過回 `spam_risk_too_many_posts` | content sharing guidelines |
| 頻率 | init 每分鐘 6 次、status 30 次、creator_info 20 次（每個 access token） | 各端點文件 |
| 收件匣模式 | `MEDIA_UPLOAD` 需要 TikTok App 31.8 以上，每 24 小時最多 5 筆待處理分享 | photo post |
| AI 標示 | `is_aigc=true` 會標示為 AI 生成；本專案圖片是 AI 生成，一律帶上 | photo post |
| 圖片格式限制 | 文件未明列；範例只用 WebP。本專案送 JPEG（未驗證） | — |
| 錯誤碼 | `url_ownership_unverified`、`unaudited_client_can_only_post_to_private_accounts`、`privacy_level_option_mismatch`、`reached_active_user_cap` 等 | photo post |

## 審核（Audit）

沒有審核時，全部貼文只能 `SELF_ONLY`。Direct Post 的審核看使用者介面是否符合規範：顯示帳號暱稱、隱私選項不預設、留言開關、商業內容揭露、發布前聲明同意音樂使用確認、不加浮水印、提供內容預覽。`artifacts/carry_studio_demo.mp4` 是目前準備的操作示範，由 `scripts/build_demo_video.py` 用實際執行中的網站與操作台截圖組成（畫面都是真的，不是程式畫出來的）。審核通過前 `TIKTOK_APP_AUDITED` 維持 `false`，程式一律只送 `SELF_ONLY`。

## 目前卡在哪

1. **App 審核**：已送審（2026-10-28，In review），等待結果。通過前走收件匣模式，不使用直接發布。
2. **網域驗證與圖片託管**：已完成。根網域 `carry-digital-nomad.in.net` 已驗證，圖片網址前綴目前仍用舊子網域 `autotok.…/media`（已驗證、已實測）；改用新子網域 `studio.…` 尚未測試。
3. **回呼網址**：TikTok 不接受 `http://127.0.0.1`，改由 Vercel 授權頁（`api/index.py`）轉送到本機 `127.0.0.1:31026`。

## 圖片託管方案

| 方案 | 優點 | 缺點 |
|---|---|---|
| 同步資料夾（已實作）：發布時複製到 `IMAGE_HOST_SYNC_DIR`，使用者負責把它發佈到驗證網域 | 不需金鑰、可測試、不綁特定服務 | 要自己讓該資料夾對外可讀，例如靜態站 repo 的工作目錄加上自動 push |
| 同步資料夾＋自動 push（已實作、已實測）：`IMAGE_HOST_GIT_PUSH=true`，資料夾在靜態站 git repo 內時，發布前自動 commit、push，輪詢到圖片網址真的能開才通知 TikTok，發布完成後自動刪除並再推一次 | 全自動、不需額外帳號 | 每篇貼文在 repo 留下兩個 commit、觸發兩次部署；實測約 50 秒上線 |
| 把圖片放進現有 Vercel 站台（`public/media/`）再部署 | 已有網域 | 每次發布都要 commit 與部署，repo 會累積圖片；部署延遲 |
| 物件儲存（R2、S3）綁自訂子網域 | 乾淨、可設過期 | 要額外帳號與費用，要驗證子網域 |

設定：`TIKTOK_VERIFIED_IMAGE_URL_PREFIX`（必須是 https）與 `IMAGE_HOST_SYNC_DIR`。兩者都有才算已設定。

自動 push 模式另加 `IMAGE_HOST_GIT_PUSH=true`、`IMAGE_HOST_GIT_BRANCH`（預設 `main`，必須是部署用分支，否則拒絕推送）。注意事項：

- 只 commit 這篇貼文的資料夾（pathspec），不會帶到工作目錄的其他改動；push 被拒絕（遠端有新 commit）時會中止、不通知 TikTok，請先 `git pull`。
- 判斷「已上線」要三項都符合：HTTP 200、內容類型是 `image/*`、大小與本機檔案一致。靜態站找不到檔案時常回 200 的錯誤頁，不能只看狀態碼。
- 等待上限 300 秒；逾時就中止，貼文標為失敗、可重試，不會通知 TikTok。
- 發布後的清理只是盡力而為，失敗時圖片會留在 repo，但不影響已完成的發布結果。
- 圖片會短暫出現在公開 repo 與網站上，不要用在不能公開的內容。

## 發布閘門

`publish_post` 會依序檢查，任何一項不成立都回 `blocked`，不呼叫 TikTok：

1. 帳號有授權且能力為 `public` 或 `self_only`（否則匯出發布包）
2. `ALLOW_LIVE_PUBLISH=true`
3. 貼文不是模擬資料
4. 品質檢查通過
5. 貼文狀態不是 `PUBLISHED`、`PUBLISHING`、`UNKNOWN`
6. 圖片託管已設定
7. `creator_info` 的隱私選項包含要用的等級
8. 取得發布鎖

結果不明（送出後連線中斷、輪詢逾時）一律標 `UNKNOWN`，不會重傳，需要人工確認 TikTok 上是否已有這篇。

## 自動化還缺的

- 等 App 審核結果；通過後把 `TIKTOK_APP_AUDITED` 改為 `true`，再評估是否改回直接發布（可全自動，也不需要手動標記）
- 直接發布模式用真實帳號實測 JPEG、`is_aigc`、輪詢行為（未驗證；收件匣模式的 JPEG 已實測可用）
- 收件匣模式需要使用者每篇手動發布並按「標記為已發布」，沒有辦法全自動
