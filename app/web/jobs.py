"""背景工作執行器：同一時間只跑一個工作（生圖與研究都很重），所有工作寫入 sync_log。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.config import AppSettings, redact_sensitive
from app.state import PostStateRepository


class JobBusyError(RuntimeError):
    """已有工作正在執行。"""


@dataclass
class JobHandle:
    log_id: int
    account_id: str
    job: str
    label: str
    started_at: str
    message: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def progress(self, message: str) -> None:
        with self._lock:
            self.message = redact_sensitive(message)


class JobRunner:
    def __init__(self, repo: PostStateRepository, settings: AppSettings | None = None) -> None:
        self.repo = repo
        self.settings = settings
        self._lock = threading.Lock()
        self._current: JobHandle | None = None
        self._last_finished_id = 0

    def current(self) -> JobHandle | None:
        with self._lock:
            return self._current

    def submit(
        self,
        *,
        account_id: str,
        job: str,
        label: str,
        fn: Callable[[JobHandle], str],
        wait: bool = False,
    ) -> JobHandle:
        """啟動背景工作。`fn(handle)` 回傳一行摘要；丟出例外視為失敗。"""
        with self._lock:
            if self._current is not None:
                raise JobBusyError(f"「{self._current.label}」還在執行，完成後才能開始新的工作。")
            log_id = self.repo.log_job_start(account_id=account_id, job=job, label=label)
            handle = JobHandle(
                log_id=log_id,
                account_id=account_id,
                job=job,
                label=label,
                started_at=datetime.now(UTC).isoformat(),
            )
            self._current = handle

        def _run() -> None:
            failure = ""
            try:
                summary = fn(handle)
                self.repo.log_job_finish(log_id, status="ok", summary=redact_sensitive(summary or ""))
            except Exception as exc:  # noqa: BLE001
                err = redact_sensitive(str(exc))
                self.repo.log_job_finish(log_id, status="error", error=err)
                failure = err
            finally:
                with self._lock:
                    self._last_finished_id = log_id
                    self._current = None
            if failure:  # 工作已收尾，通知慢或失敗都不影響它
                self._notify_failure(handle, failure)

        thread = threading.Thread(target=_run, name=f"job-{job}", daemon=True)
        thread.start()
        if wait:
            thread.join()
        return handle

    def _notify_failure(self, handle: JobHandle, err: str) -> None:
        if self.settings is None:
            return
        try:
            from app.notify import send_notification

            acc = self.repo.get_account(handle.account_id) if handle.account_id else None
            who = f"（{acc.handle}）" if acc else ""
            send_notification(
                self.repo, self.settings, title=f"{handle.label}失敗{who}", message=err[:800], level="error"
            )
        except Exception:  # noqa: BLE001
            pass

    @property
    def last_finished_id(self) -> int:
        with self._lock:
            return self._last_finished_id
