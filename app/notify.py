"""通知（Telegram）。`send_notification` 永遠不丟例外；未啟用或設定不全回 False。"""

from __future__ import annotations

import httpx

from app import credentials
from app.config import AppSettings, redact_sensitive
from app.state import PostStateRepository

API = "https://api.telegram.org"
TOKEN_REF = "telegram:bot"
MAX_LEN = 3800
RETRIES = 2


class TelegramError(RuntimeError):
    """可直接顯示給使用者的錯誤（不含 token）。"""


def get_config(repo: PostStateRepository) -> dict[str, object]:
    return {
        "enabled": repo.get_app_setting("telegram_enabled", "0") == "1",
        "chat_id": repo.get_app_setting("telegram_chat_id", ""),
        "on_success": repo.get_app_setting("notify_on_success", "0") == "1",
        "has_token": bool(credentials.get_secret(TOKEN_REF)),
    }


def mask_token(token: str) -> str:
    return f"…{token[-4:]}" if token else ""


def _clean(text: str, token: str = "") -> str:
    out = redact_sensitive(text)
    return out.replace(token, "[REDACTED]") if token else out


def _explain(status: int, body: dict[str, object]) -> str:
    desc = str(body.get("description", "")).lower()
    if status == 401:
        return "Bot Token 無效"
    if status == 404:
        return "Bot Token 無效"
    if "chat not found" in desc:
        return "Chat ID 不正確"
    if "bot was blocked" in desc or "can't initiate" in desc or "user is deactivated" in desc:
        return "機器人沒有被使用者啟動過，請先在 Telegram 對它傳一則訊息"
    if status == 429:
        return "Telegram 要求稍後再試"
    return f"Telegram 回應 {status}：{str(body.get('description', ''))[:120]}"


def _call(token: str, method: str, payload: dict[str, object], transport: httpx.BaseTransport | None) -> dict[str, object]:
    try:
        with httpx.Client(transport=transport, timeout=10) as client:
            resp = client.post(f"{API}/bot{token}/{method}", json=payload)
    except httpx.HTTPError as exc:
        raise TelegramError(f"連不上 Telegram（{type(exc).__name__}）") from None
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code != 200 or not body.get("ok"):
        raise TelegramError(_explain(resp.status_code, body if isinstance(body, dict) else {}))
    return body


def send_text(token: str, chat_id: str, title: str, message: str, *, transport: httpx.BaseTransport | None = None) -> None:
    """送出一則純文字訊息；失敗丟 TelegramError。最多嘗試 2 次（僅網路錯誤重試）。"""
    text = _clean(f"[AutoTok] {title}\n{message}".rstrip(), token)[:MAX_LEN]
    last: TelegramError | None = None
    for _ in range(RETRIES):
        try:
            _call(token, "sendMessage", {"chat_id": chat_id, "text": text}, transport)
            return
        except TelegramError as exc:
            last = exc
            if not str(exc).startswith("連不上"):
                break
    raise last or TelegramError("傳送失敗")


def _log(repo: PostStateRepository, title: str, ok: bool, error: str = "") -> None:
    try:
        log_id = repo.log_job_start(account_id="", job="notify", label=title[:80])
        repo.log_job_finish(log_id, status="ok" if ok else "error", summary="已送出" if ok else "", error=error)
    except Exception:  # noqa: BLE001
        pass


def send_notification(
    repo: PostStateRepository,
    settings: AppSettings,
    *,
    title: str,
    message: str,
    level: str = "error",
    transport: httpx.BaseTransport | None = None,
) -> bool:
    """依設定送出通知。`level` 為 error / info；info 只在「成功也通知」開啟時送出。"""
    try:
        cfg = get_config(repo)
        if not cfg["enabled"] or not cfg["chat_id"]:
            return False
        if level != "error" and not cfg["on_success"]:
            return False
        token = credentials.get_secret(TOKEN_REF)
        if not token:
            return False
        try:
            send_text(token, str(cfg["chat_id"]), title, message, transport=transport)
        except TelegramError as exc:
            _log(repo, title, False, _clean(str(exc), token))
            return False
        _log(repo, title, True)
        return True
    except Exception:  # noqa: BLE001
        return False


def detect_chats(token: str, *, transport: httpx.BaseTransport | None = None) -> list[dict[str, str]]:
    """列出最近傳訊息給機器人的聊天室。"""
    body = _call(token, "getUpdates", {"limit": 50}, transport)
    seen: dict[str, dict[str, str]] = {}
    for upd in body.get("result", []) or []:
        msg = upd.get("message") or upd.get("channel_post") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if "id" not in chat:
            continue
        name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) or chat.get("username") or str(chat["id"])
        seen[str(chat["id"])] = {"id": str(chat["id"]), "name": str(name)}
    return list(seen.values())
