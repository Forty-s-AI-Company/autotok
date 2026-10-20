"""每帳號憑證儲存：優先使用作業系統金鑰圈（keyring），無可用後端時退回本機 0600 檔案。

SQLite 只存「參照名稱」（例如 `notion:carry-vietnam-tw`），不存 Token 本身。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

SERVICE_NAME = "autotok"
_FALLBACK_PATH = Path(__file__).resolve().parent.parent / "data" / "secrets.json"


def _keyring_ok() -> bool:
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring

        return not isinstance(keyring.get_keyring(), FailKeyring)
    except Exception:
        return False


_LOCK = threading.Lock()


def _read_fallback(path: Path) -> dict[str, str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except ValueError:
        pass
    # 內容損毀：保留一份備份，不要讓下一次寫入悄悄清掉其他帳號的憑證
    backup = path.with_name(path.name + f".corrupt-{int(time.time())}")
    try:
        path.replace(backup)
    except OSError:
        pass
    return {}


def _write_fallback(path: Path, data: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data))
    os.replace(tmp, path)


def set_secret(ref: str, value: str, *, fallback_path: Path | None = None) -> None:
    fallback_path = fallback_path or _FALLBACK_PATH
    if _keyring_ok():
        import keyring

        keyring.set_password(SERVICE_NAME, ref, value)
        return
    with _LOCK:
        data = _read_fallback(fallback_path)
        data[ref] = value
        _write_fallback(fallback_path, data)


def get_secret(ref: str, *, fallback_path: Path | None = None) -> str:
    fallback_path = fallback_path or _FALLBACK_PATH
    if _keyring_ok():
        import keyring

        try:
            return keyring.get_password(SERVICE_NAME, ref) or ""
        except Exception:
            return ""
    return _read_fallback(fallback_path).get(ref, "")


def delete_secret(ref: str, *, fallback_path: Path | None = None) -> None:
    fallback_path = fallback_path or _FALLBACK_PATH
    if _keyring_ok():
        import keyring

        try:
            keyring.delete_password(SERVICE_NAME, ref)
        except Exception:
            pass
        return
    with _LOCK:
        data = _read_fallback(fallback_path)
        if data.pop(ref, None) is not None:
            _write_fallback(fallback_path, data)
