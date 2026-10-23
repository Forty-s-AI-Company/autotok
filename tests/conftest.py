"""測試一律不碰真實的作業系統金鑰圈與專案的 data/secrets.json，也不受本機 .env 的圖片託管推送開關影響。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import credentials


@pytest.fixture(autouse=True)
def _isolate_credentials(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials, "_keyring_ok", lambda: False)
    monkeypatch.setattr(credentials, "_FALLBACK_PATH", Path(tmp_path_factory.mktemp("secrets")) / "secrets.json")


@pytest.fixture(autouse=True)
def _never_push_images(monkeypatch: pytest.MonkeyPatch) -> None:
    # 環境變數優先於 .env；避免本機啟用自動 push 後，測試對真實的 git repo 與網站動手
    monkeypatch.setenv("IMAGE_HOST_GIT_PUSH", "false")
    monkeypatch.setenv("ALLOW_LIVE_PUBLISH", "false")  # 本機 .env 開啟真實發布時，測試也不能受影響
