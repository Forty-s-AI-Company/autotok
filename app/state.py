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

from app.models import (
    CarouselPostPackage,
    PostPerformanceObservation,
    PostStatus,
    ResearchCase,
    SeedKeywordEntry,
    TopicOpportunity,
)


class PostStateRepository:
    """貼文狀態與防重發鎖定儲存庫（SQLite 原型實作）。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path.expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS content_posts (
                    post_id TEXT PRIMARY KEY,
                    slot TEXT NOT NULL,
                    scheduled_at_taipei TEXT NOT NULL,
                    vi_title TEXT NOT NULL,
                    zh_tw_title TEXT NOT NULL,
                    vi_caption TEXT NOT NULL,
                    zh_tw_caption TEXT NOT NULL,
                    hashtags_json TEXT NOT NULL,
                    slides_json TEXT NOT NULL,
                    media_dir TEXT NOT NULL,
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
                    post_id, slot, scheduled_at_taipei, vi_title, zh_tw_title,
                    vi_caption, zh_tw_caption, hashtags_json, slides_json,
                    media_dir, is_mock, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(post_id) DO UPDATE SET
                    slot = excluded.slot,
                    scheduled_at_taipei = excluded.scheduled_at_taipei,
                    vi_title = excluded.vi_title,
                    zh_tw_title = excluded.zh_tw_title,
                    vi_caption = excluded.vi_caption,
                    zh_tw_caption = excluded.zh_tw_caption,
                    hashtags_json = excluded.hashtags_json,
                    slides_json = excluded.slides_json,
                    media_dir = excluded.media_dir,
                    is_mock = excluded.is_mock,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    package.post_id,
                    package.slot.value,
                    package.scheduled_at_taipei,
                    package.vi_title,
                    package.zh_tw_title,
                    package.vi_caption,
                    package.zh_tw_caption,
                    json.dumps(package.hashtags, ensure_ascii=False),
                    json.dumps(slides_data, ensure_ascii=False),
                    str(package.media_dir),
                    1 if package.is_mock else 0,
                    package.status.value,
                    now_iso,
                ),
            )

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
        """儲存或更新研究案例（確保 `source_url` 唯一，且缺失指標以 NULL 儲存，不預設填 0）。"""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO research_cases (
                    case_id, source_url, creator, market, source_type,
                    published_at, collected_at, views, likes, comments,
                    shares, follower_count, attributed_new_followers,
                    missing_fields_json, hook_notes, narrative_category,
                    evidence_level, confidence, is_mock
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_url) DO UPDATE SET
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
            )
            for r in rows
        ]

    def upsert_topic_opportunity(self, topic: TopicOpportunity) -> None:
        """儲存或更新候選主題機會（`topic_opportunities`）。"""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO topic_opportunities (
                    topic_id, title_vi, title_zh_tw, target_audience, angle,
                    recommended_slot, research_case_ids_json, score_breakdown_json,
                    weighted_score, dedup_90d_passed, dedup_reason,
                    experiment_hypothesis, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(topic_id) DO UPDATE SET
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
                ),
            )

    def get_recent_topics_and_urls(
        self, *, days: int = 90
    ) -> tuple[list[tuple[str, str]], set[str]]:
        """查詢近 90 天內已通過查重之主題清單 `(topic_id, title_vi)` 與已被引用的來源 URL 集合。"""
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            topic_rows = conn.execute(
                """
                SELECT topic_id, title_vi, research_case_ids_json
                FROM topic_opportunities
                WHERE created_at >= ? AND dedup_90d_passed = 1
                """,
                (cutoff,),
            ).fetchall()

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
