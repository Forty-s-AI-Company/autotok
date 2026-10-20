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
from datetime import UTC, datetime
from pathlib import Path

from app.models import CarouselPostPackage, PostStatus


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
