"""測試一律不碰真實的作業系統金鑰圈與專案的 data/secrets.json。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import credentials


@pytest.fixture(autouse=True)
def _isolate_credentials(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials, "_keyring_ok", lambda: False)
    monkeypatch.setattr(credentials, "_FALLBACK_PATH", Path(tmp_path_factory.mktemp("secrets")) / "secrets.json")
