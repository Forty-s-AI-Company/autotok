"""爆款研究資料來源：網路搜尋（LLM）、手動匯入、第三方 API、模擬資料。

共同原則（MASTER_SPEC 第 4、5 節）：
- 缺失數據一律保留 None，不補 0；`missing_fields` 如實列出。
- 沒有 `source_url` 的項目直接丟棄。
- 廣告樣本標 `SourceType.AD`，不與自然流量混同。
- 任何來源取不到資料時回傳實際數量，不退回假資料。模擬資料只有使用者明確選擇時才會產生。

各 provider 的驗證狀態：
- ImportProvider：已用單元測試驗證解析邏輯。
- LlmWebSearchProvider：解析與組裝邏輯已用 mock 驗證；實際呼叫 `codex exec -c web_search="live"` 與 Anthropic web_search 工具
  的端到端結果**未驗證**（需要真實額度與網路）。
- ThirdPartyApiProvider（Apify）：**未驗證**，欄位對應依 actor 公開文件推定，預設關閉。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import Any

import httpx

from app.config import AppSettings
from app.engines import find_codex_bin, run_codex_cmd
from app.markets import MarketProfile
from app.models import EvidenceLevel, ResearchCase, SourceType
from app.research import MockResearchProvider, ResearchProvider

NARRATIVE_CATEGORIES = {
    "practical_checklist",
    "comparison_carousel",
    "food_guide",
    "travel_itinerary",
    "emotional_story",
    "general",
}
MAX_COUNT = 10**12  # 超過視為不可信，當缺值
MAX_IMPORT_ROWS = 500
METRIC_FIELDS = ("views", "likes", "comments", "shares")
CONFIDENCE_BY_LEVEL = {
    EvidenceLevel.VERIFIED_PUBLIC: 0.85,
    EvidenceLevel.PARTIAL_PUBLIC: 0.65,
    EvidenceLevel.AD_LIBRARY: 0.6,
    EvidenceLevel.MANUAL_IMPORT: 0.6,
    EvidenceLevel.UNVERIFIED: 0.4,
    EvidenceLevel.MOCK_FIXTURE: 0.5,
}


@dataclass(frozen=True)
class Availability:
    ok: bool
    reason: str = ""


# ---------------------------------------------------------------------------
# 共用：數字、日期、欄位正規化
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"^\s*([\d][\d,\.]*)\s*([kKmMbB萬万]?)\s*$")


def parse_count(value: object) -> int | None:
    """把 `482K`、`1.2M`、`1,234`、`3.5萬` 等轉成整數；無法解析、負數、空值一律回傳 None（不補 0）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= MAX_COUNT else None
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and 0 <= value <= MAX_COUNT else None
    text = str(value).strip()
    if not text or text in {"-", "—", "N/A", "n/a", "null", "None"}:
        return None
    m = _NUM_RE.match(text)
    if not m:
        return None
    digits, unit = m.group(1), m.group(2).lower()
    try:
        num = float(digits.replace(",", ""))
    except ValueError:
        return None
    mult = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000, "萬": 10_000, "万": 10_000}[unit]
    scaled = num * mult
    if not math.isfinite(scaled) or scaled > MAX_COUNT:
        return None
    return int(round(scaled))


def parse_date(value: object) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass
    m = re.match(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$", text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=UTC).isoformat()
        except ValueError:
            return None
    return None


def case_id_for(url: str) -> str:
    return "case-" + hashlib.sha1(url.strip().encode("utf-8")).hexdigest()[:12]


def _truthy(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "是", "ad", "廣告"}


def market_code(market: MarketProfile) -> str:
    return f"{market.target_language.upper()}_{market.country.upper()}"


def normalize_case(
    raw: dict[str, Any],
    *,
    provider: str,
    market: MarketProfile,
    default_level: EvidenceLevel,
    collected_at: str | None = None,
    metrics_from_page: bool = True,
    confidence_cap: float | None = None,
) -> ResearchCase | None:
    """把鬆散的 dict 轉成 ResearchCase；沒有有效 http(s) URL 時回傳 None。

    `metrics_from_page=False` 時（模型自述數字不是來自頁面），所有互動數據一律丟棄。
    程式沒有實際請求並驗證來源頁面，所以自動蒐集的案例最高只標 PARTIAL_PUBLIC，不給 VERIFIED_PUBLIC。
    """
    url = str(raw.get("url") or raw.get("source_url") or "").strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return None

    counts = {k: parse_count(raw.get(k)) for k in (*METRIC_FIELDS, "follower_count")}
    if not metrics_from_page:
        counts = dict.fromkeys(counts)
    missing = [k for k in (*METRIC_FIELDS, "follower_count") if counts[k] is None]

    is_ad = _truthy(raw.get("is_ad")) or str(raw.get("source_type", "")).lower() == "ad"
    if is_ad:
        level = EvidenceLevel.AD_LIBRARY
    elif default_level in (EvidenceLevel.VERIFIED_PUBLIC, EvidenceLevel.PARTIAL_PUBLIC):
        have = [k for k in METRIC_FIELDS if counts[k] is not None]
        if have:
            level = EvidenceLevel.PARTIAL_PUBLIC
        else:
            level = EvidenceLevel.UNVERIFIED
    else:
        level = default_level

    category = str(raw.get("narrative_category") or "general").strip()
    if category not in NARRATIVE_CATEGORIES:
        category = "general"

    return ResearchCase(
        case_id=case_id_for(url),
        source_url=url,
        title=str(raw.get("title") or "").strip(),
        title_zh=str(raw.get("title_zh") or "").strip(),
        provider=provider,
        creator=str(raw.get("creator") or raw.get("author") or "unknown").strip() or "unknown",
        market=market_code(market),
        source_type=SourceType.AD if is_ad else SourceType.ORGANIC,
        published_at=parse_date(raw.get("published_at")),
        collected_at=collected_at or datetime.now(UTC).isoformat(),
        views=counts["views"],
        likes=counts["likes"],
        comments=counts["comments"],
        shares=counts["shares"],
        follower_count=counts["follower_count"],
        missing_fields=missing,
        hook_notes=str(raw.get("hook_notes") or "").strip(),
        narrative_category=category,
        evidence_level=level,
        confidence=min(CONFIDENCE_BY_LEVEL[level], confidence_cap) if confidence_cap is not None else CONFIDENCE_BY_LEVEL[level],
        is_mock=level == EvidenceLevel.MOCK_FIXTURE,
    )


def dedupe_cases(cases: list[ResearchCase]) -> list[ResearchCase]:
    seen: set[str] = set()
    out: list[ResearchCase] = []
    for c in cases:
        key = c.source_url.rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def extract_json(text: str) -> Any:
    """從模型回覆中取出第一個可解析的 JSON 物件或陣列（容許 ``` 圍欄與前後說明文字）。"""
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                obj, _ = decoder.raw_decode(text[i:])
            except ValueError:
                continue
            return obj
    raise ValueError("回覆中找不到 JSON")


def cases_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("cases", [])
    if not isinstance(payload, list):
        return []
    return [p for p in payload if isinstance(p, dict)]


# ---------------------------------------------------------------------------
# 基底類別
# ---------------------------------------------------------------------------


class SourceProvider(ResearchProvider):
    """帶有介面顯示資訊的研究來源。"""

    key: str = ""
    label: str = ""
    verified: bool = True
    note: str = ""

    def availability(self) -> Availability:
        return Availability(True)

    def is_available(self) -> bool:
        return self.availability().ok


# ---------------------------------------------------------------------------
# 網路搜尋（LLM）
# ---------------------------------------------------------------------------

CodexRunner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def build_search_prompt(market: MarketProfile, max_cases: int) -> str:
    keywords = "、".join(market.seed_keywords) or "（未設定，請依受眾自行判斷）"
    return f"""你是 TikTok 內容研究員。請用網路搜尋，找出近期在這個市場表現好的公開圖文輪播或短影音。

市場：{market.audience or '未指定'}；發布語言：{market.target_language_label}（{market.target_language}）；地區：{market.country}。
種子關鍵字：{keywords}
本次研究訴求：{market.content_brief or '（未提供，請依受眾自行判斷）'}
請同時涵蓋訴求中提到的不同內容類型，不要只集中在單一類型；若訴求要求某類內容有或沒有 CTA，請在 hook_notes 註明案例有沒有 CTA。

規則：
1. 只列出你實際開啟過、確實存在的公開頁面，每筆必須附完整 URL。找不到 URL 的不要列。
2. 觀看、讚、留言、分享、粉絲數只能填頁面上實際看得到的數字；看不到就填 null，不可推測、不可估計、不可填 0。
3. metrics_source 填 "page"（數字來自頁面）或 "none"（沒有取得數字）。
4. 廣告樣本請把 is_ad 設為 true。
5. title 用原文標題；title_zh 是繁體中文翻譯。
6. narrative_category 只能是：practical_checklist、comparison_carousel、food_guide、travel_itinerary、emotional_story、general。
7. hook_notes 用繁體中文一兩句寫出封面或開頭的鉤子與結構，只寫你真的看到的內容。
8. 最多 {max_cases} 筆；找不到就少給，不要湊數。

只輸出一個 JSON 物件，不要其他文字：
{{"cases": [{{"url": "", "title": "", "title_zh": "", "creator": "", "published_at": "YYYY-MM-DD 或 null",
"views": null, "likes": null, "comments": null, "shares": null, "follower_count": null,
"is_ad": false, "narrative_category": "general", "hook_notes": "", "metrics_source": "page"}}]}}"""


class LlmWebSearchProvider(SourceProvider):
    """用 LLM 的網路搜尋整理公開案例。優先本機 Codex CLI（`codex exec -c web_search="live"`），其次 Anthropic API。

    端到端行為未驗證：`--search` 只存在於互動模式，exec 要用設定 `web_search="live"`（本機 codex 0.155 接受 disabled/cached/indexed/live）；
    Anthropic web_search 工具的版本字串依文件推定。
    """

    key = "web"
    label = "網路搜尋"
    provider_name = "web_search"
    verified = False
    note = "依賴 LLM 網搜，互動數據可能不齊，缺的欄位會如實標示。端到端尚未實測。"

    ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
    WEB_SEARCH_TOOL = "web_search_20250305"

    def __init__(
        self,
        settings: AppSettings,
        market: MarketProfile,
        *,
        transport: httpx.BaseTransport | None = None,
        codex_runner: CodexRunner | None = None,
        timeout_sec: int = 900,
    ) -> None:
        self.settings = settings
        self.market = market
        self._transport = transport
        self._codex_runner = codex_runner
        self.timeout_sec = timeout_sec

    def backend(self) -> str:
        if find_codex_bin(self.settings.codex_bin) is not None:
            return "codex"
        if self.settings.anthropic_api_key.strip():
            return "anthropic"
        return ""

    def availability(self) -> Availability:
        if self.backend():
            return Availability(True)
        return Availability(
            False,
            "找不到可用的搜尋後端。請安裝 ChatGPT App（Codex CLI），或在 .env 設定 ANTHROPIC_API_KEY。",
        )

    # --- 後端 ---

    def _run_codex(self, prompt: str) -> str:
        codex = find_codex_bin(self.settings.codex_bin)
        if codex is None:
            raise RuntimeError("找不到 Codex CLI")
        with tempfile.TemporaryDirectory(prefix="autotok-research-") as tmp:
            out_file = Path(tmp) / "last_message.txt"
            cmd = [
                str(codex), "exec", "-c", 'web_search="live"', "-s", "read-only", "--ephemeral", "--skip-git-repo-check",
                "-m", self.settings.codex_model, "-C", tmp, "-o", str(out_file), prompt,
            ]  # fmt: skip
            runner = self._codex_runner or (
                lambda c: run_codex_cmd(c, timeout=self.timeout_sec)
            )
            proc = runner(cmd)
            if proc.returncode != 0:
                raise RuntimeError(f"Codex CLI 執行失敗（exit={proc.returncode}）：{(proc.stderr or proc.stdout)[-300:]}")
            if out_file.is_file():
                return out_file.read_text(encoding="utf-8")
            return proc.stdout

    def _run_anthropic(self, prompt: str) -> str:
        body = {
            "model": self.settings.anthropic_model,
            "max_tokens": 8000,
            "tools": [{"type": self.WEB_SEARCH_TOOL, "name": "web_search", "max_uses": 8}],
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "x-api-key": self.settings.anthropic_api_key.strip(),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        with httpx.Client(transport=self._transport, timeout=self.timeout_sec) as client:
            resp = client.post(self.ANTHROPIC_URL, headers=headers, json=body)
        if resp.status_code >= 400:
            raise RuntimeError(f"Anthropic API 回應 {resp.status_code}：{resp.text[:300]}")
        blocks = resp.json().get("content", [])
        return "\n".join(str(b.get("text", "")) for b in blocks if b.get("type") == "text")

    def collect_cases(self, *, max_cases: int = 30) -> list[ResearchCase]:
        avail = self.availability()
        if not avail.ok:
            raise RuntimeError(avail.reason)
        prompt = build_search_prompt(self.market, max_cases)
        text = self._run_codex(prompt) if self.backend() == "codex" else self._run_anthropic(prompt)
        try:
            items = cases_from_payload(extract_json(text))
        except ValueError as exc:
            raise RuntimeError("搜尋後端沒有回傳可解析的 JSON，請重試。") from exc
        now = datetime.now(UTC).isoformat()
        cases = [
            c
            for raw in items
            if (
                c := normalize_case(
                    raw,
                    provider=self.provider_name,
                    market=self.market,
                    default_level=EvidenceLevel.PARTIAL_PUBLIC,
                    collected_at=now,
                    metrics_from_page=str(raw.get("metrics_source", "")).lower() == "page",
                    confidence_cap=0.6,
                )
            )
            is not None
        ]
        return dedupe_cases(cases)[:max_cases]


# ---------------------------------------------------------------------------
# 手動匯入
# ---------------------------------------------------------------------------

_ALIASES = {
    "url": ("url", "link", "source_url", "webvideourl", "連結", "網址"),
    "title": ("title", "text", "caption", "標題"),
    "title_zh": ("title_zh", "zh_title", "中文標題", "繁中標題"),
    "creator": ("creator", "author", "username", "帳號", "創作者"),
    "views": ("views", "play_count", "playcount", "view", "觀看", "播放"),
    "likes": ("likes", "like", "digg_count", "diggcount", "讚", "按讚"),
    "comments": ("comments", "comment", "comment_count", "commentcount", "留言"),
    "shares": ("shares", "share", "share_count", "sharecount", "分享"),
    "follower_count": ("follower_count", "followers", "fans", "粉絲"),
    "published_at": ("published_at", "date", "create_time", "createtimeiso", "發布日期", "發布時間"),
    "narrative_category": ("narrative_category", "category", "類別"),
    "hook_notes": ("hook_notes", "notes", "note", "備註", "鉤子"),
    "is_ad": ("is_ad", "ad", "廣告"),
}


def _canon_row(row: dict[str, Any]) -> dict[str, Any]:
    lowered = {str(k).strip().lower(): v for k, v in row.items() if k is not None}
    out: dict[str, Any] = {}
    for canon, names in _ALIASES.items():
        for name in names:
            if name in lowered and lowered[name] not in (None, ""):
                out[canon] = lowered[name]
                break
    return out


class ImportTooLargeError(ValueError):
    """匯入列數超過上限。"""


class ImportProvider(SourceProvider):
    """解析使用者貼上或上傳的 URL 清單、CSV、JSON。只有 URL 時，其他欄位全部標為缺失。"""

    key = "import"
    label = "手動匯入"
    provider_name = "manual_import"
    verified = True

    def __init__(self, market: MarketProfile, text: str = "") -> None:
        self.market = market
        self.text = text

    def parse_rows(self) -> list[dict[str, Any]]:
        text = self.text.lstrip("﻿").strip()
        if not text:
            return []
        if text[0] in "[{":
            rows = [_canon_row(r) for r in cases_from_payload(json.loads(text))]
            if len(rows) > MAX_IMPORT_ROWS:
                raise ImportTooLargeError(f"一次最多匯入 {MAX_IMPORT_ROWS} 列")
            return rows
        first_line = text.splitlines()[0].lower()
        if "," in first_line or "\t" in first_line:
            delimiter = "\t" if first_line.count("\t") > first_line.count(",") else ","
            reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
            names = {str(h).strip().lower() for h in (reader.fieldnames or [])}
            if names & set(_ALIASES["url"]):
                rows = [_canon_row(r) for r in islice(reader, MAX_IMPORT_ROWS + 1)]
                if len(rows) > MAX_IMPORT_ROWS:
                    raise ImportTooLargeError(f"一次最多匯入 {MAX_IMPORT_ROWS} 列")
                return rows
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) > MAX_IMPORT_ROWS:
            raise ImportTooLargeError(f"一次最多匯入 {MAX_IMPORT_ROWS} 列")
        return [{"url": ln} for ln in lines]

    def collect_cases(self, *, max_cases: int = 200) -> list[ResearchCase]:
        now = datetime.now(UTC).isoformat()
        cases = [
            c
            for raw in self.parse_rows()
            if (
                c := normalize_case(
                    raw,
                    provider=self.provider_name,
                    market=self.market,
                    default_level=EvidenceLevel.MANUAL_IMPORT,
                    collected_at=now,
                )
            )
            is not None
        ]
        return dedupe_cases(cases)[:max_cases]


# ---------------------------------------------------------------------------
# 第三方 API（Apify）
# ---------------------------------------------------------------------------


class ThirdPartyApiProvider(SourceProvider):
    """透過 Apify 的 TikTok 爬取 actor 取得公開影片數據（預設關閉，需要 APIFY_TOKEN）。

    未驗證：actor 名稱、輸入參數與輸出欄位（webVideoUrl、text、authorMeta、playCount、diggCount、
    commentCount、shareCount、createTimeISO）依 actor 公開文件推定，尚未以真實 token 測過。
    使用第三方爬取可能牴觸 TikTok 服務條款，風險由使用者自行評估。
    """

    key = "thirdparty"
    label = "第三方 API"
    provider_name = "apify"
    verified = False
    note = "Apify 範本，欄位對應尚未實測。使用前請自行評估服務條款風險。"

    DEFAULT_ACTOR = "clockworks~tiktok-scraper"

    def __init__(
        self,
        settings: AppSettings,
        market: MarketProfile,
        *,
        actor: str = DEFAULT_ACTOR,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.market = market
        self.actor = actor
        self._transport = transport

    def availability(self) -> Availability:
        if self.settings.apify_token.strip():
            return Availability(True)
        return Availability(False, "尚未設定 APIFY_TOKEN。")

    def collect_cases(self, *, max_cases: int = 30) -> list[ResearchCase]:
        avail = self.availability()
        if not avail.ok:
            raise RuntimeError(avail.reason)
        queries = list(self.market.seed_keywords) or [self.market.audience]
        url = f"https://api.apify.com/v2/acts/{self.actor}/run-sync-get-dataset-items"
        payload = {"searchQueries": queries[:5], "resultsPerPage": max(1, max_cases // max(1, len(queries[:5])))}
        headers = {"Authorization": f"Bearer {self.settings.apify_token.strip()}"}
        with httpx.Client(transport=self._transport, timeout=300) as client:
            resp = client.post(url, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(f"Apify 回應 {resp.status_code}：{resp.text[:300]}")
        items = resp.json()
        if not isinstance(items, list):
            raise RuntimeError("Apify 回傳格式不是陣列")
        now = datetime.now(UTC).isoformat()
        cases: list[ResearchCase] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            author = it.get("authorMeta") if isinstance(it.get("authorMeta"), dict) else {}
            raw = {
                "url": it.get("webVideoUrl"),
                "title": it.get("text"),
                "creator": author.get("name"),
                "views": it.get("playCount"),
                "likes": it.get("diggCount"),
                "comments": it.get("commentCount"),
                "shares": it.get("shareCount"),
                "follower_count": author.get("fans"),
                "published_at": it.get("createTimeISO"),
            }
            c = normalize_case(
                raw, provider=self.provider_name, market=self.market,
                default_level=EvidenceLevel.PARTIAL_PUBLIC, collected_at=now, confidence_cap=0.6,
            )  # fmt: skip
            if c is not None:
                cases.append(c)
        return dedupe_cases(cases)[:max_cases]


# ---------------------------------------------------------------------------
# 模擬資料（只有使用者明確選擇才會使用）
# ---------------------------------------------------------------------------


class MockSourceProvider(SourceProvider):
    key = "mock"
    label = "模擬資料"
    provider_name = "mock"
    verified = True
    note = "測試版面用的範例資料，不代表真實市場。"

    def collect_cases(self, *, max_cases: int = 35) -> list[ResearchCase]:
        out: list[ResearchCase] = []
        for c in MockResearchProvider().collect_cases(max_cases=max_cases):
            out.append(
                c.model_copy(
                    update={
                        "provider": self.provider_name,
                        "title": c.title or (c.hook_notes[:48] or c.case_id),
                        "title_zh": c.title_zh or c.hook_notes,
                        "is_mock": True,
                        "evidence_level": EvidenceLevel.MOCK_FIXTURE,
                    }
                )
            )
        return out


PROVIDER_KEYS = ("web", "import", "thirdparty", "mock")


def build_providers(settings: AppSettings, market: MarketProfile) -> dict[str, SourceProvider]:
    return {
        "web": LlmWebSearchProvider(settings, market),
        "import": ImportProvider(market),
        "thirdparty": ThirdPartyApiProvider(settings, market),
        "mock": MockSourceProvider(),
    }
