"""本機 SQLite 狀態持久化與發文鎖（Publish Lock）原型。

遵循 MASTER_SPEC.md 第 7、8、10 節與 PHASE1_ANTIGRAVITY_PROMPT.md 第 7 點：
- 記錄貼文狀態轉換：
  `DRAFT → RESEARCHED → GENERATING → READY_TO_REVIEW/QUALITY_FAILED → READY → SCHEDULED → PUBLISHING → PUBLISHED/FAILED/UNKNOWN`
- 實作共享發文鎖介面（`acquire_publish_lock`），防止排程器重複發布同一篇 `post_id`。
- 重要架構警語：本機 SQLite 僅供單機開發與原型驗證；GitHub Actions 的 ephemeral runner 每次執行皆為全新環境，
  在 Phase 4 正式啟用雲端與 Windows 雙機備援前，必須將此 Repository 背後替換為共享具交易一致性的雲端持久化資料庫（如 Supabase PostgreSQL / Turso / Notion + 條件鎖）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app import credentials
from app.markets import MarketProfile, default_profile_for_language
from app.models import (
    DEFAULT_ACCOUNT_ID,
    CarouselPostPackage,
    EvidenceLevel,
    PostPerformanceObservation,
    PostStatus,
    ResearchCase,
    SeedKeywordEntry,
    SlideItem,
    TikTokAccountProfile,
    TopicOpportunity,
)

DEFAULT_ACCOUNTS_SEED: list[TikTokAccountProfile] = [
    TikTokAccountProfile(
        account_id="carry-vietnam-tw",
        handle="@carry.vietnam.tw",
        display_name="Carry | Đài Loan × Việt Nam",
        market_focus="在台越南移工、留學生、新住民生活與成長",
        primary_language="zh-TW",
        target_language="vi",
        notion_database_id="f1bc34396f154e62bca90c9deea19d7a",
        is_active=True,
    ),
]


class PostStateRepository:
    """貼文狀態與防重發鎖定儲存庫（SQLite 原型實作）。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path.expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._seed_default_accounts()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tiktok_accounts (
                    account_id TEXT PRIMARY KEY,
                    handle TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    market_focus TEXT NOT NULL,
                    primary_language TEXT NOT NULL DEFAULT 'zh-TW',
                    target_language TEXT NOT NULL DEFAULT 'vi',
                    notion_database_id TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS content_posts (
                    post_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL DEFAULT 'carry-vietnam-tw',
                    slot TEXT NOT NULL,
                    scheduled_at_taipei TEXT NOT NULL,
                    vi_title TEXT NOT NULL,
                    zh_tw_title TEXT NOT NULL,
                    vi_caption TEXT NOT NULL,
                    zh_tw_caption TEXT NOT NULL,
                    hashtags_json TEXT NOT NULL,
                    slides_json TEXT NOT NULL,
                    media_dir TEXT NOT NULL,
                    core_angle TEXT NOT NULL DEFAULT '',
                    cta TEXT NOT NULL DEFAULT '',
                    is_mock INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL,
                    platform_post_id TEXT,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS publish_locks (
                    post_id TEXT PRIMARY KEY,
                    runner_id TEXT NOT NULL,
                    acquired_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS seed_keywords (
                    keyword_vi TEXT PRIMARY KEY,
                    translation_zh_tw TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    timeliness TEXT NOT NULL,
                    source TEXT NOT NULL,
                    recommended_slot TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS research_cases (
                    case_id TEXT PRIMARY KEY,
                    source_url TEXT NOT NULL UNIQUE,
                    creator TEXT NOT NULL,
                    market TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    published_at TEXT,
                    collected_at TEXT NOT NULL,
                    views INTEGER,
                    likes INTEGER,
                    comments INTEGER,
                    shares INTEGER,
                    follower_count INTEGER,
                    attributed_new_followers INTEGER,
                    missing_fields_json TEXT NOT NULL,
                    hook_notes TEXT NOT NULL,
                    narrative_category TEXT NOT NULL,
                    evidence_level TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    is_mock INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS topic_opportunities (
                    topic_id TEXT PRIMARY KEY,
                    title_vi TEXT NOT NULL,
                    title_zh_tw TEXT NOT NULL,
                    target_audience TEXT NOT NULL,
                    angle TEXT NOT NULL,
                    recommended_slot TEXT NOT NULL,
                    research_case_ids_json TEXT NOT NULL,
                    score_breakdown_json TEXT NOT NULL,
                    weighted_score REAL NOT NULL,
                    dedup_90d_passed INTEGER NOT NULL,
                    dedup_reason TEXT NOT NULL,
                    experiment_hypothesis TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS post_observations (
                    observation_id TEXT PRIMARY KEY,
                    post_id TEXT NOT NULL,
                    slot TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    window_hours INTEGER NOT NULL,
                    source_tier TEXT NOT NULL,
                    views INTEGER,
                    likes INTEGER,
                    comments INTEGER,
                    shares INTEGER,
                    saves INTEGER,
                    attributed_new_followers INTEGER,
                    attribution_confidence TEXT NOT NULL,
                    top_audience_regions_json TEXT NOT NULL,
                    experiment_variant TEXT NOT NULL,
                    is_mock INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS account_settings (
                    account_id TEXT PRIMARY KEY,
                    market_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS account_notion (
                    account_id TEXT PRIMARY KEY,
                    auth_method TEXT NOT NULL DEFAULT 'token',
                    workspace_name TEXT NOT NULL DEFAULT '',
                    token_ref TEXT NOT NULL DEFAULT '',
                    database_id TEXT NOT NULL DEFAULT '',
                    database_title TEXT NOT NULL DEFAULT '',
                    parent_page_id TEXT NOT NULL DEFAULT '',
                    schema_missing_json TEXT NOT NULL DEFAULT '[]',
                    schema_checked_at TEXT,
                    verified_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS account_tiktok (
                    account_id TEXT PRIMARY KEY,
                    token_ref TEXT NOT NULL DEFAULT '',
                    publish_capability TEXT NOT NULL DEFAULT 'export_only',
                    expires_at TEXT,
                    verified_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sync_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL DEFAULT '',
                    job TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_sync_log_job ON sync_log (account_id, job, started_at);

                CREATE TABLE IF NOT EXISTS case_fit (
                    account_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    score REAL NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    flag TEXT NOT NULL DEFAULT '',
                    recommended_slot TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, case_id)
                );
                """
            )
            # 相容既有 SQLite 資料庫欄位遷移
            existing_cols = {
                str(r["name"])
                for r in conn.execute("PRAGMA table_info(content_posts)").fetchall()
            }
            if "account_id" not in existing_cols:
                conn.execute(
                    "ALTER TABLE content_posts ADD COLUMN account_id TEXT NOT NULL DEFAULT 'carry-vietnam-tw'"
                )
            if "core_angle" not in existing_cols:
                conn.execute(
                    "ALTER TABLE content_posts ADD COLUMN core_angle TEXT NOT NULL DEFAULT ''"
                )
            if "cta" not in existing_cols:
                conn.execute(
                    "ALTER TABLE content_posts ADD COLUMN cta TEXT NOT NULL DEFAULT ''"
                )
            for table, col, ddl in (
                ("research_cases", "title", "TEXT NOT NULL DEFAULT ''"),
                ("research_cases", "title_zh", "TEXT NOT NULL DEFAULT ''"),
                ("research_cases", "provider", "TEXT NOT NULL DEFAULT ''"),
                ("topic_opportunities", "account_id", "TEXT NOT NULL DEFAULT ''"),
            ):
                cols = {str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if col not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            # 早期的選題沒有帳號欄位，全部歸給預設帳號，讓 90 天去重仍然看得到它們
            conn.execute(
                "UPDATE topic_opportunities SET account_id = ? WHERE account_id = ''",
                (DEFAULT_ACCOUNT_ID,),
            )

    def _seed_default_accounts(self) -> None:
        with self._connect() as conn:
            # 早期版本植入過兩個沒有內容的示範帳號，沒有貼文時直接移除
            for demo_id in ("carry-tw-food-travel", "carry-digital-creator"):
                in_use = any(
                    conn.execute(f"SELECT 1 FROM {table} WHERE account_id = ? LIMIT 1", (demo_id,)).fetchone()
                    for table in ("content_posts", "account_settings", "account_notion", "account_tiktok")
                )
                if not in_use:
                    conn.execute("DELETE FROM tiktok_accounts WHERE account_id = ?", (demo_id,))
            count_row = conn.execute("SELECT COUNT(*) AS cnt FROM tiktok_accounts").fetchone()
            if count_row and int(count_row["cnt"]) > 0:
                active = conn.execute("SELECT 1 FROM tiktok_accounts WHERE is_active = 1").fetchone()
                if active is None:
                    conn.execute(
                        "UPDATE tiktok_accounts SET is_active = 1 WHERE account_id = "
                        "(SELECT account_id FROM tiktok_accounts ORDER BY account_id LIMIT 1)"
                    )
                return
        for acc in DEFAULT_ACCOUNTS_SEED:
            self.upsert_account(acc)

    def upsert_account(self, profile: TikTokAccountProfile) -> None:
        """新增或更新 TikTok 帳號設定。"""
        now_iso = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            if profile.is_active:
                conn.execute("UPDATE tiktok_accounts SET is_active = 0")
            conn.execute(
                """
                INSERT INTO tiktok_accounts (
                    account_id, handle, display_name, market_focus,
                    primary_language, target_language, notion_database_id,
                    is_active, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    handle = excluded.handle,
                    display_name = excluded.display_name,
                    market_focus = excluded.market_focus,
                    primary_language = excluded.primary_language,
                    target_language = excluded.target_language,
                    notion_database_id = excluded.notion_database_id,
                    is_active = excluded.is_active,
                    updated_at = excluded.updated_at
                """,
                (
                    profile.account_id,
                    profile.handle if profile.handle.startswith("@") else f"@{profile.handle}",
                    profile.display_name,
                    profile.market_focus,
                    profile.primary_language,
                    profile.target_language,
                    profile.notion_database_id,
                    1 if profile.is_active else 0,
                    now_iso,
                ),
            )

    def list_accounts(self) -> list[TikTokAccountProfile]:
        """列出所有已綁定的 TikTok 帳號。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tiktok_accounts ORDER BY is_active DESC, account_id ASC"
            ).fetchall()
        return [
            TikTokAccountProfile(
                account_id=str(r["account_id"]),
                handle=str(r["handle"]),
                display_name=str(r["display_name"]),
                market_focus=str(r["market_focus"]),
                primary_language=str(r["primary_language"]),
                target_language=str(r["target_language"]),
                notion_database_id=str(r["notion_database_id"]),
                is_active=bool(r["is_active"]),
            )
            for r in rows
        ]

    def get_active_account(self) -> TikTokAccountProfile:
        """取得目前作用中的 TikTok 帳號。"""
        accounts = self.list_accounts()
        for acc in accounts:
            if acc.is_active:
                return acc
        return accounts[0] if accounts else DEFAULT_ACCOUNTS_SEED[0]

    def set_active_account(self, account_id: str) -> TikTokAccountProfile:
        """切換目前作用中的 TikTok 帳號。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT account_id FROM tiktok_accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if row is not None:
                conn.execute("UPDATE tiktok_accounts SET is_active = 0")
                conn.execute(
                    "UPDATE tiktok_accounts SET is_active = 1 WHERE account_id = ?",
                    (account_id,),
                )
        return self.get_active_account()

    def upsert_post(self, package: CarouselPostPackage) -> None:
        """新增或更新貼文資料包狀態。"""
        now_iso = datetime.now(UTC).isoformat()
        slides_data = [
            {
                "index": s.index,
                "vi_text": s.vi_text,
                "zh_tw_translation": s.zh_tw_translation,
                "image_path": str(s.image_path) if s.image_path else None,
                "sha256": s.sha256,
            }
            for s in package.slides
        ]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO content_posts (
                    post_id, account_id, slot, scheduled_at_taipei, vi_title, zh_tw_title,
                    vi_caption, zh_tw_caption, hashtags_json, slides_json,
                    media_dir, core_angle, cta, is_mock, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(post_id) DO UPDATE SET
                    account_id = excluded.account_id,
                    slot = excluded.slot,
                    scheduled_at_taipei = excluded.scheduled_at_taipei,
                    vi_title = excluded.vi_title,
                    zh_tw_title = excluded.zh_tw_title,
                    vi_caption = excluded.vi_caption,
                    zh_tw_caption = excluded.zh_tw_caption,
                    hashtags_json = excluded.hashtags_json,
                    slides_json = excluded.slides_json,
                    media_dir = excluded.media_dir,
                    core_angle = excluded.core_angle,
                    cta = excluded.cta,
                    is_mock = excluded.is_mock,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    package.post_id,
                    package.account_id or DEFAULT_ACCOUNT_ID,
                    package.slot.value,
                    package.scheduled_at_taipei,
                    package.vi_title,
                    package.zh_tw_title,
                    package.vi_caption,
                    package.zh_tw_caption,
                    json.dumps(package.hashtags, ensure_ascii=False),
                    json.dumps(slides_data, ensure_ascii=False),
                    str(package.media_dir),
                    package.core_angle,
                    package.cta,
                    1 if package.is_mock else 0,
                    package.status.value,
                    now_iso,
                ),
            )

    def list_posts(self, *, account_id: str | None = None) -> list[CarouselPostPackage]:
        """查詢已儲存的貼文資料包（支援依帳號篩選）。"""
        query = "SELECT * FROM content_posts"
        params: tuple[object, ...] = ()
        if account_id:
            query += " WHERE account_id = ?"
            params = (account_id,)
        query += " ORDER BY scheduled_at_taipei DESC, updated_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        results: list[CarouselPostPackage] = []
        for r in rows:
            raw_slides = json.loads(r["slides_json"])
            slides = [
                SlideItem(
                    index=int(s["index"]),
                    vi_text=str(s["vi_text"]),
                    zh_tw_translation=str(s["zh_tw_translation"]),
                    image_path=Path(s["image_path"]) if s.get("image_path") else None,
                    sha256=s.get("sha256"),
                )
                for s in raw_slides
            ]
            if not (3 <= len(slides) <= 10):
                continue
            results.append(
                CarouselPostPackage(
                    post_id=str(r["post_id"]),
                    account_id=str(r["account_id"] or DEFAULT_ACCOUNT_ID),
                    slot=r["slot"],
                    scheduled_at_taipei=str(r["scheduled_at_taipei"]),
                    vi_title=str(r["vi_title"]),
                    zh_tw_title=str(r["zh_tw_title"]),
                    vi_caption=str(r["vi_caption"]),
                    zh_tw_caption=str(r["zh_tw_caption"]),
                    hashtags=json.loads(r["hashtags_json"]),
                    slides=slides,
                    media_dir=Path(str(r["media_dir"])),
                    core_angle=str(r["core_angle"] or ""),
                    cta=str(r["cta"] or ""),
                    is_mock=bool(r["is_mock"]),
                    status=PostStatus(str(r["status"])),
                )
            )
        return results

    def update_status(
        self,
        post_id: str,
        status: PostStatus,
        *,
        platform_post_id: str | None = None,
        last_error: str | None = None,
    ) -> None:
        """更新貼文狀態與錯誤紀錄（若處於 UNKNOWN 狀態則保留供人工或查詢確認，不盲目重傳）。"""
        now_iso = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE content_posts
                SET status = ?,
                    platform_post_id = COALESCE(?, platform_post_id),
                    last_error = ?,
                    updated_at = ?
                WHERE post_id = ?
                """,
                (status.value, platform_post_id, last_error, now_iso, post_id),
            )

    def get_post_status(self, post_id: str) -> PostStatus | None:
        """查詢指定貼文目前狀態。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM content_posts WHERE post_id = ?", (post_id,)
            ).fetchone()
            if row is None:
                return None
            return PostStatus(row["status"])

    def acquire_publish_lock(self, post_id: str, runner_id: str) -> bool:
        """嘗試取得指定 post_id 的發布鎖；若已被其他執行器鎖定或狀態為 UNKNOWN/PUBLISHED 則拒絕。"""
        current_status = self.get_post_status(post_id)
        if current_status in {PostStatus.PUBLISHED, PostStatus.PUBLISHING, PostStatus.UNKNOWN}:
            return False

        now_iso = datetime.now(UTC).isoformat()
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO publish_locks (post_id, runner_id, acquired_at) VALUES (?, ?, ?)",
                    (post_id, runner_id, now_iso),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def release_publish_lock(self, post_id: str) -> None:
        """釋放指定 post_id 的發布鎖。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM publish_locks WHERE post_id = ?", (post_id,))

    # ==========================================================================
    # Phase 2：搜尋關鍵字字典、研究案例庫 (`research_cases`) 與主題機會 (`topic_opportunities`)
    # ==========================================================================

    def upsert_seed_keywords(self, keywords: list[SeedKeywordEntry]) -> int:
        """儲存或更新搜尋關鍵字字典（MASTER_SPEC.md 第 5.2 節）。"""
        now_iso = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            for kw in keywords:
                conn.execute(
                    """
                    INSERT INTO seed_keywords (
                        keyword_vi, translation_zh_tw, intent, timeliness,
                        source, recommended_slot, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(keyword_vi) DO UPDATE SET
                        translation_zh_tw = excluded.translation_zh_tw,
                        intent = excluded.intent,
                        timeliness = excluded.timeliness,
                        source = excluded.source,
                        recommended_slot = excluded.recommended_slot,
                        updated_at = excluded.updated_at
                    """,
                    (
                        kw.keyword_vi,
                        kw.translation_zh_tw,
                        kw.intent,
                        kw.timeliness,
                        kw.source,
                        kw.recommended_slot.value,
                        now_iso,
                    ),
                )
        return len(keywords)

    def list_seed_keywords(self) -> list[SeedKeywordEntry]:
        """讀取目前已登錄的所有搜尋關鍵字。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM seed_keywords ORDER BY keyword_vi ASC"
            ).fetchall()
        return [
            SeedKeywordEntry(
                keyword_vi=r["keyword_vi"],
                translation_zh_tw=r["translation_zh_tw"],
                intent=r["intent"],
                timeliness=r["timeliness"],
                source=r["source"],
                recommended_slot=r["recommended_slot"],
            )
            for r in rows
        ]

    def upsert_research_case(self, case: ResearchCase) -> None:
        """儲存或更新研究案例（確保 `source_url` 唯一，且缺失指標以 NULL 儲存，不預設填 0）。

        同一個 URL 再次寫入時，新資料缺值的數字欄位保留既有非空值（後來資料較少的搜尋不會把已有的數字清掉）；
        若因此保留了舊數字，證據等級與可信度取兩者中較高者，缺失欄位清單依合併後的結果重算。
        """
        numeric = ("views", "likes", "comments", "shares", "follower_count", "attributed_new_followers")
        with self._connect() as conn:
            prev = conn.execute(
                "SELECT views, likes, comments, shares, follower_count, attributed_new_followers, "
                "missing_fields_json, evidence_level, confidence, provider, is_mock "
                "FROM research_cases WHERE source_url = ?",
                (case.source_url,),
            ).fetchone()
            if prev is not None:
                merged = {k: getattr(case, k) if getattr(case, k) is not None else prev[k] for k in numeric}
                kept_old = any(getattr(case, k) is None and prev[k] is not None for k in numeric)
                update: dict[str, object] = dict(merged)
                if kept_old and float(prev["confidence"]) > case.confidence:
                    update["evidence_level"] = EvidenceLevel(prev["evidence_level"])
                    update["confidence"] = float(prev["confidence"])
                    update["provider"] = prev["provider"] or case.provider
                    update["is_mock"] = bool(prev["is_mock"])
                old_missing = json.loads(prev["missing_fields_json"] or "[]")
                still_missing: list[str] = []
                for f in [*case.missing_fields, *old_missing]:
                    if f not in still_missing and (f not in merged or merged[f] is None):
                        still_missing.append(f)
                update["missing_fields"] = still_missing
                case = case.model_copy(update=update)
            conn.execute(
                """
                INSERT INTO research_cases (
                    case_id, source_url, creator, market, source_type,
                    published_at, collected_at, views, likes, comments,
                    shares, follower_count, attributed_new_followers,
                    missing_fields_json, hook_notes, narrative_category,
                    evidence_level, confidence, is_mock, title, title_zh, provider
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_url) DO UPDATE SET
                    title = excluded.title,
                    title_zh = excluded.title_zh,
                    provider = excluded.provider,
                    case_id = excluded.case_id,
                    creator = excluded.creator,
                    market = excluded.market,
                    source_type = excluded.source_type,
                    published_at = excluded.published_at,
                    collected_at = excluded.collected_at,
                    views = excluded.views,
                    likes = excluded.likes,
                    comments = excluded.comments,
                    shares = excluded.shares,
                    follower_count = excluded.follower_count,
                    attributed_new_followers = excluded.attributed_new_followers,
                    missing_fields_json = excluded.missing_fields_json,
                    hook_notes = excluded.hook_notes,
                    narrative_category = excluded.narrative_category,
                    evidence_level = excluded.evidence_level,
                    confidence = excluded.confidence,
                    is_mock = excluded.is_mock
                """,
                (
                    case.case_id,
                    case.source_url,
                    case.creator,
                    case.market,
                    case.source_type.value,
                    case.published_at,
                    case.collected_at,
                    case.views,
                    case.likes,
                    case.comments,
                    case.shares,
                    case.follower_count,
                    case.attributed_new_followers,
                    json.dumps(case.missing_fields, ensure_ascii=False),
                    case.hook_notes,
                    case.narrative_category,
                    case.evidence_level.value,
                    case.confidence,
                    1 if case.is_mock else 0,
                    case.title,
                    case.title_zh,
                    case.provider,
                ),
            )

    def list_research_cases(self, *, days: int | None = 90) -> list[ResearchCase]:
        """讀取研究案例清單（預設篩選近 90 天內擷取的案例）。"""
        query = "SELECT * FROM research_cases"
        params: tuple[object, ...] = ()
        if days is not None:
            cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
            query += " WHERE collected_at >= ?"
            params = (cutoff,)
        query += " ORDER BY collected_at DESC, case_id ASC"

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        return [
            ResearchCase(
                case_id=r["case_id"],
                source_url=r["source_url"],
                creator=r["creator"],
                market=r["market"],
                source_type=r["source_type"],
                published_at=r["published_at"],
                collected_at=r["collected_at"],
                views=r["views"],
                likes=r["likes"],
                comments=r["comments"],
                shares=r["shares"],
                follower_count=r["follower_count"],
                attributed_new_followers=r["attributed_new_followers"],
                missing_fields=json.loads(r["missing_fields_json"]),
                hook_notes=r["hook_notes"],
                narrative_category=r["narrative_category"],
                evidence_level=r["evidence_level"],
                confidence=float(r["confidence"]),
                is_mock=bool(r["is_mock"]),
                title=str(r["title"] or ""),
                title_zh=str(r["title_zh"] or ""),
                provider=str(r["provider"] or ""),
            )
            for r in rows
        ]

    def upsert_topic_opportunity(self, topic: TopicOpportunity, *, account_id: str = "") -> None:
        """儲存或更新候選主題機會（`topic_opportunities`），可記錄所屬帳號。"""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO topic_opportunities (
                    topic_id, title_vi, title_zh_tw, target_audience, angle,
                    recommended_slot, research_case_ids_json, score_breakdown_json,
                    weighted_score, dedup_90d_passed, dedup_reason,
                    experiment_hypothesis, status, created_at, account_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(topic_id) DO UPDATE SET
                    account_id = excluded.account_id,
                    title_vi = excluded.title_vi,
                    title_zh_tw = excluded.title_zh_tw,
                    target_audience = excluded.target_audience,
                    angle = excluded.angle,
                    recommended_slot = excluded.recommended_slot,
                    research_case_ids_json = excluded.research_case_ids_json,
                    score_breakdown_json = excluded.score_breakdown_json,
                    weighted_score = excluded.weighted_score,
                    dedup_90d_passed = excluded.dedup_90d_passed,
                    dedup_reason = excluded.dedup_reason,
                    experiment_hypothesis = excluded.experiment_hypothesis,
                    status = excluded.status,
                    created_at = excluded.created_at
                """,
                (
                    topic.topic_id,
                    topic.title_vi,
                    topic.title_zh_tw,
                    topic.target_audience,
                    topic.angle,
                    topic.recommended_slot.value,
                    json.dumps(topic.research_case_ids, ensure_ascii=False),
                    topic.score_breakdown.model_dump_json(),
                    topic.score_breakdown.weighted_total,
                    1 if topic.dedup_90d_passed else 0,
                    topic.dedup_reason,
                    topic.experiment_hypothesis,
                    topic.status.value,
                    topic.created_at,
                    account_id,
                ),
            )

    def get_topic_opportunity(self, topic_id: str) -> TopicOpportunity | None:
        """依 ID 讀取候選主題。"""
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM topic_opportunities WHERE topic_id = ?", (topic_id,)).fetchone()
        if r is None:
            return None
        return TopicOpportunity(
            topic_id=str(r["topic_id"]),
            title_vi=str(r["title_vi"]),
            title_zh_tw=str(r["title_zh_tw"]),
            target_audience=str(r["target_audience"]),
            angle=str(r["angle"]),
            recommended_slot=r["recommended_slot"],
            research_case_ids=json.loads(r["research_case_ids_json"]),
            score_breakdown=json.loads(r["score_breakdown_json"]),
            dedup_90d_passed=bool(r["dedup_90d_passed"]),
            dedup_reason=str(r["dedup_reason"]),
            experiment_hypothesis=str(r["experiment_hypothesis"]),
            status=r["status"],
            created_at=str(r["created_at"]),
        )

    def get_recent_topics_and_urls(
        self, *, days: int = 90, account_id: str | None = None
    ) -> tuple[list[tuple[str, str]], set[str]]:
        """查詢近 90 天內已通過查重之主題清單 `(topic_id, title_vi)` 與已被引用的來源 URL 集合。

        傳入 `account_id` 時只比對該帳號的歷史主題。
        """
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        sql = (
            "SELECT topic_id, title_vi, research_case_ids_json FROM topic_opportunities "
            "WHERE created_at >= ? AND dedup_90d_passed = 1"
        )
        params: tuple[object, ...] = (cutoff,)
        if account_id is not None:
            sql += " AND account_id = ?"
            params = (cutoff, account_id)
        with self._connect() as conn:
            topic_rows = conn.execute(sql, params).fetchall()

            topics: list[tuple[str, str]] = []
            referenced_case_ids: set[str] = set()
            for r in topic_rows:
                topics.append((str(r["topic_id"]), str(r["title_vi"])))
                for cid in json.loads(r["research_case_ids_json"]):
                    referenced_case_ids.add(str(cid))

            used_urls: set[str] = set()
            if referenced_case_ids:
                placeholders = ",".join("?" for _ in referenced_case_ids)
                url_rows = conn.execute(
                    f"SELECT source_url FROM research_cases WHERE case_id IN ({placeholders})",
                    tuple(referenced_case_ids),
                ).fetchall()
                used_urls = {str(ur["source_url"]) for ur in url_rows}

        return topics, used_urls

    # ==========================================================================
    # Phase 5：發布後觀察期成效紀錄 (`post_observations`)
    # ==========================================================================

    def upsert_post_observation(self, obs: PostPerformanceObservation) -> None:
        """儲存或更新貼文觀察期成效指標（缺失欄位保留 NULL，絕不預設填 0）。"""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO post_observations (
                    observation_id, post_id, slot, observed_at, window_hours,
                    source_tier, views, likes, comments, shares, saves,
                    attributed_new_followers, attribution_confidence,
                    top_audience_regions_json, experiment_variant, is_mock
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(observation_id) DO UPDATE SET
                    post_id = excluded.post_id,
                    slot = excluded.slot,
                    observed_at = excluded.observed_at,
                    window_hours = excluded.window_hours,
                    source_tier = excluded.source_tier,
                    views = excluded.views,
                    likes = excluded.likes,
                    comments = excluded.comments,
                    shares = excluded.shares,
                    saves = excluded.saves,
                    attributed_new_followers = excluded.attributed_new_followers,
                    attribution_confidence = excluded.attribution_confidence,
                    top_audience_regions_json = excluded.top_audience_regions_json,
                    experiment_variant = excluded.experiment_variant,
                    is_mock = excluded.is_mock
                """,
                (
                    obs.observation_id,
                    obs.post_id,
                    obs.slot.value,
                    obs.observed_at,
                    obs.window_hours,
                    obs.source_tier.value,
                    obs.views,
                    obs.likes,
                    obs.comments,
                    obs.shares,
                    obs.saves,
                    obs.attributed_new_followers,
                    obs.attribution_confidence.value,
                    json.dumps(obs.top_audience_regions, ensure_ascii=False),
                    obs.experiment_variant,
                    1 if obs.is_mock else 0,
                ),
            )

    def list_post_observations(
        self, *, include_mock: bool = True
    ) -> list[PostPerformanceObservation]:
        """查詢已記錄之貼文觀察期成效清單。"""
        query = "SELECT * FROM post_observations"
        params: tuple[object, ...] = ()
        if not include_mock:
            query += " WHERE is_mock = 0"
        query += " ORDER BY observed_at DESC, observation_id ASC"

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        return [
            PostPerformanceObservation(
                observation_id=str(r["observation_id"]),
                post_id=str(r["post_id"]),
                slot=r["slot"],
                observed_at=str(r["observed_at"]),
                window_hours=int(r["window_hours"]),
                source_tier=r["source_tier"],
                views=r["views"],
                likes=r["likes"],
                comments=r["comments"],
                shares=r["shares"],
                saves=r["saves"],
                attributed_new_followers=r["attributed_new_followers"],
                attribution_confidence=r["attribution_confidence"],
                top_audience_regions=json.loads(r["top_audience_regions_json"]),
                experiment_variant=str(r["experiment_variant"]),
                is_mock=bool(r["is_mock"]),
            )
            for r in rows
        ]

    # ==========================================================================
    # 帳號、市場設定、外部綁定、同步紀錄、案例適配度
    # ==========================================================================

    def get_account(self, account_id: str) -> TikTokAccountProfile | None:
        for acc in self.list_accounts():
            if acc.account_id == account_id:
                return acc
        return None

    def delete_account(self, account_id: str) -> bool:
        """刪除沒有任何貼文的帳號；有貼文時拒絕，避免遺失內容。"""
        with self._connect() as conn:
            if conn.execute(
                "SELECT 1 FROM content_posts WHERE account_id = ? LIMIT 1", (account_id,)
            ).fetchone():
                return False
            for table in ("account_settings", "account_notion", "account_tiktok", "case_fit"):
                conn.execute(f"DELETE FROM {table} WHERE account_id = ?", (account_id,))
            conn.execute("DELETE FROM tiktok_accounts WHERE account_id = ?", (account_id,))
        for prefix in ("notion", "tiktok"):
            credentials.delete_secret(f"{prefix}:{account_id}")
        self._seed_default_accounts()
        return True

    def get_market_profile(self, account_id: str) -> MarketProfile:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT market_json FROM account_settings WHERE account_id = ?", (account_id,)
            ).fetchone()
        if row is not None:
            return MarketProfile.model_validate_json(str(row["market_json"]))
        acc = self.get_account(account_id)
        return default_profile_for_language(acc.target_language if acc else "vi")

    def save_market_profile(self, account_id: str, profile: MarketProfile) -> None:
        now_iso = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO account_settings (account_id, market_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    market_json = excluded.market_json, updated_at = excluded.updated_at
                """,
                (account_id, profile.model_dump_json(), now_iso),
            )
            conn.execute(
                "UPDATE tiktok_accounts SET target_language = ?, updated_at = ? WHERE account_id = ?",
                (profile.target_language, now_iso, account_id),
            )

    def get_notion_binding(self, account_id: str) -> dict[str, object] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM account_notion WHERE account_id = ?", (account_id,)
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["schema_missing"] = json.loads(str(data.pop("schema_missing_json") or "[]"))
        return data

    def save_notion_binding(self, account_id: str, **fields: object) -> None:
        """部分更新 Notion 綁定。可用欄位：auth_method、workspace_name、token_ref、database_id、
        database_title、parent_page_id、schema_missing（list）、schema_checked_at、verified_at。"""
        allowed = {
            "auth_method", "workspace_name", "token_ref", "database_id", "database_title",
            "parent_page_id", "schema_missing", "schema_checked_at", "verified_at",
        }  # fmt: skip
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"未知的 Notion 綁定欄位：{sorted(unknown)}")
        current = self.get_notion_binding(account_id) or {}
        merged: dict[str, object] = {
            "auth_method": "token", "workspace_name": "", "token_ref": "", "database_id": "",
            "database_title": "", "parent_page_id": "", "schema_missing": [],
            "schema_checked_at": None, "verified_at": None,
        }  # fmt: skip
        merged.update({k: v for k, v in current.items() if k in merged})
        merged.update(fields)
        now_iso = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO account_notion (
                    account_id, auth_method, workspace_name, token_ref, database_id, database_title,
                    parent_page_id, schema_missing_json, schema_checked_at, verified_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    auth_method = excluded.auth_method, workspace_name = excluded.workspace_name,
                    token_ref = excluded.token_ref, database_id = excluded.database_id,
                    database_title = excluded.database_title, parent_page_id = excluded.parent_page_id,
                    schema_missing_json = excluded.schema_missing_json,
                    schema_checked_at = excluded.schema_checked_at,
                    verified_at = excluded.verified_at, updated_at = excluded.updated_at
                """,
                (
                    account_id, merged["auth_method"], merged["workspace_name"], merged["token_ref"],
                    merged["database_id"], merged["database_title"], merged["parent_page_id"],
                    json.dumps(merged["schema_missing"], ensure_ascii=False),
                    merged["schema_checked_at"], merged["verified_at"], now_iso,
                ),
            )  # fmt: skip

    def clear_notion_binding(self, account_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM account_notion WHERE account_id = ?", (account_id,))

    def get_tiktok_binding(self, account_id: str) -> dict[str, object] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM account_tiktok WHERE account_id = ?", (account_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def save_tiktok_binding(
        self,
        account_id: str,
        *,
        token_ref: str = "",
        publish_capability: str = "export_only",
        expires_at: str | None = None,
        verified_at: str | None = None,
    ) -> None:
        """publish_capability：`public`（可公開）、`self_only`（僅自己可見）、`export_only`（僅匯出）。"""
        now_iso = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO account_tiktok (
                    account_id, token_ref, publish_capability, expires_at, verified_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    token_ref = excluded.token_ref, publish_capability = excluded.publish_capability,
                    expires_at = excluded.expires_at, verified_at = excluded.verified_at,
                    updated_at = excluded.updated_at
                """,
                (account_id, token_ref, publish_capability, expires_at, verified_at, now_iso),
            )

    # --- app_settings：全站層級的設定（例如選用的模型）---

    def get_app_setting(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row is not None else default

    def set_app_setting(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value, datetime.now(UTC).isoformat()),
            )

    # --- sync_log：所有背景工作與資料來源的更新紀錄（畫面上的「最後更新時間」來源）---

    def log_job_start(self, *, account_id: str, job: str, label: str = "", at: datetime | None = None) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO sync_log (account_id, job, label, started_at, status) VALUES (?, ?, ?, ?, 'running')",
                (account_id, job, label, (at or datetime.now(UTC)).isoformat()),
            )
            return int(cur.lastrowid or 0)

    def log_job_finish(
        self, log_id: int, *, status: str, summary: str = "", error: str = "", at: datetime | None = None
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sync_log SET finished_at = ?, status = ?, summary = ?, error = ? WHERE id = ?",
                ((at or datetime.now(UTC)).isoformat(), status, summary, error, log_id),
            )

    def last_job(self, *, account_id: str, job: str, only_ok: bool = False) -> dict[str, object] | None:
        sql = "SELECT * FROM sync_log WHERE account_id = ? AND job = ?"
        if only_ok:
            sql += " AND status = 'ok'"
        sql += " ORDER BY id DESC LIMIT 1"
        with self._connect() as conn:
            row = conn.execute(sql, (account_id, job)).fetchone()
        return dict(row) if row is not None else None

    def last_job_by_label(self, *, account_id: str, job: str, label: str) -> dict[str, object] | None:
        """某個標籤（例如 post_id）最近一次成功的工作紀錄。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sync_log WHERE account_id = ? AND job = ? AND label = ? AND status = 'ok' "
                "ORDER BY id DESC LIMIT 1",
                (account_id, job, label),
            ).fetchone()
        return dict(row) if row is not None else None

    def job_rows(self, *, account_id: str, job: str, label: str) -> list[dict[str, object]]:
        """某個工作＋標籤的所有紀錄（舊到新）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sync_log WHERE account_id = ? AND job = ? AND label = ? ORDER BY id",
                (account_id, job, label),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_jobs_like(self, *, account_id: str, prefix: str, limit: int = 5) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sync_log WHERE account_id = ? AND job LIKE ? ORDER BY id DESC LIMIT ?",
                (account_id, prefix + "%", limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def running_jobs(self, *, since_iso: str) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sync_log WHERE status = 'running' AND started_at >= ? ORDER BY id", (since_iso,)
            ).fetchall()
        return [dict(r) for r in rows]

    def post_updated_at(self, post_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT updated_at FROM content_posts WHERE post_id = ?", (post_id,)).fetchone()
        return str(row["updated_at"]) if row is not None else None

    def list_jobs(self, *, account_id: str | None = None, limit: int = 20) -> list[dict[str, object]]:
        sql = "SELECT * FROM sync_log"
        params: tuple[object, ...] = ()
        if account_id is not None:
            sql += " WHERE account_id = ?"
            params = (account_id,)
        sql += " ORDER BY id DESC LIMIT ?"
        with self._connect() as conn:
            rows = conn.execute(sql, (*params, limit)).fetchall()
        return [dict(r) for r in rows]

    def fail_stale_running_jobs(self) -> int:
        """伺服器重啟後，把殘留的 running 紀錄標為中斷。"""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE sync_log SET status = 'error', finished_at = ?, error = '伺服器重新啟動，工作中斷' "
                "WHERE status = 'running'",
                (datetime.now(UTC).isoformat(),),
            )
            return cur.rowcount

    # --- case_fit：研究案例對各帳號的適配度 ---

    def replace_case_fits(self, account_id: str, fits: list[dict[str, object]]) -> None:
        """整批取代某帳號的適配度。每筆需含 case_id、score，可含 reason、flag、recommended_slot。"""
        now_iso = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute("DELETE FROM case_fit WHERE account_id = ?", (account_id,))
            conn.executemany(
                "INSERT INTO case_fit (account_id, case_id, score, reason, flag, recommended_slot, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        account_id, str(f["case_id"]), float(f["score"]), str(f.get("reason", "")),
                        str(f.get("flag", "")), str(f.get("recommended_slot", "")), now_iso,
                    )
                    for f in fits
                ],
            )  # fmt: skip

    def list_case_fits(self, account_id: str) -> dict[str, dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM case_fit WHERE account_id = ?", (account_id,)).fetchall()
        return {str(r["case_id"]): dict(r) for r in rows}
