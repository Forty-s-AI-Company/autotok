"""即時文案產生（Editorial Agent）：依選題與帳號市場，產出結構化的輪播圖文草稿。

不再使用寫死的固定文案。沒有可用的文案引擎時回明確錯誤，不退回範例文案；
範例文案（`DailySlotEditorialSpec`）只供「模擬」流程使用。
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.config import AppSettings
from app.engines import find_codex_bin
from app.errors import AutotokSystemError
from app.markets import MarketProfile
from app.models import MAX_SLIDE_COUNT, MIN_SLIDE_COUNT
from app.production import audit_editorial_compliance

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MAX_PARSE_RETRIES = 2
MAX_COMPLIANCE_REWRITES = 1

LANGUAGE_NAMES_EN: dict[str, str] = {
    "vi": "Vietnamese", "th": "Thai", "id": "Indonesian", "en": "English", "ja": "Japanese", "ko": "Korean",
}  # fmt: skip

# 嚴重違規：虛構職缺、保證收入、保證簽證。命中就拒絕並要求重寫。
SEVERE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"保證.{0,6}(高薪|月入|收入|過件|過簽|通過|錄取)"),
    re.compile(r"(包過|包辦簽證|免工作證)"),
    re.compile(r"月入\s*(十萬|\d+\s*萬)"),
    re.compile(r"(guarantee[ds]?|100%\s*guarantee).{0,24}(visa|salary|income|job)", re.IGNORECASE),
    re.compile(r"(cam kết|bao đậu|đảm bảo).{0,20}(visa|lương|thu nhập|việc làm)", re.IGNORECASE),
]
# 需要提醒：涉及簽證、居留、薪資、法規。不拒絕，但要求發布前核對官方資訊。
SENSITIVE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(簽證|居留|工作證|ARC|基本工資|薪資|勞基法|仲介)", re.IGNORECASE),
    re.compile(r"\b(visa|ARC|salary|minimum wage|work permit)\b", re.IGNORECASE),
    re.compile(r"(visa|lương|giấy phép lao động|cư trú)", re.IGNORECASE),
    re.compile(r"(วีซ่า|เงินเดือน|ใบอนุญาตทำงาน)"),
]
SENSITIVE_FLAG = "涉及簽證、居留或薪資，發布前請對照官方資訊確認內容正確"


class EditorialUnavailableError(AutotokSystemError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="EDITORIAL_UNAVAILABLE")


class EditorialRejectedError(AutotokSystemError):
    def __init__(self, message: str, *, reasons: list[str] | None = None) -> None:
        super().__init__(message, code="EDITORIAL_REJECTED")
        self.reasons = reasons or [message]


class DraftSlide(BaseModel):
    """單頁圖文：發布語言標題與內文、繁中完整對照（建議格式「短標題：說明」）、英文場景描述。"""

    title: str = Field(min_length=1, description="發布語言標題")
    body: str = Field(min_length=1, description="發布語言內文")
    zh_tw: str = Field(min_length=1, description="繁體中文完整對照")
    scene_prompt: str = Field(default="", description="英文場景描述，給生圖引擎")


class EditorialDraft(BaseModel):
    title: str = Field(min_length=1, max_length=90, description="發布語言標題（TikTok 上限 90 字元）")
    zh_tw_title: str = Field(min_length=1)
    caption: str = Field(min_length=1, max_length=4000, description="發布語言貼文")
    zh_tw_caption: str = Field(min_length=1)
    hashtags: list[str]
    core_angle: str = ""
    cta: str = ""
    slides: list[DraftSlide]
    risk_flags: list[str] = Field(default_factory=list)

    @field_validator("hashtags")
    @classmethod
    def _five_hashtags(cls, v: list[str]) -> list[str]:
        if len(v) != 5:
            raise ValueError(f"必須恰好 5 個 hashtag，目前為 {len(v)} 個")
        out: list[str] = []
        for tag in v:
            clean = tag.strip().lstrip("#").strip()
            if not clean:
                raise ValueError("hashtag 不得為空白")
            if re.search(r"\s", clean) or "#" in clean:
                raise ValueError(f"hashtag 不得含空白或多餘的 #：{tag!r}")
            out.append(f"#{clean}")
        if len({t.lower() for t in out}) != len(out):
            raise ValueError("hashtag 不得重複")
        return out

    @field_validator("slides")
    @classmethod
    def _slide_count(cls, v: list[DraftSlide]) -> list[DraftSlide]:
        if not MIN_SLIDE_COUNT <= len(v) <= MAX_SLIDE_COUNT:
            raise ValueError(f"張數必須介於 {MIN_SLIDE_COUNT}–{MAX_SLIDE_COUNT}，目前為 {len(v)}")
        return v

    def all_texts(self) -> list[str]:
        texts = [self.title, self.zh_tw_title, self.caption, self.zh_tw_caption, self.core_angle, self.cta]
        for s in self.slides:
            texts.extend([s.title, s.body, s.zh_tw])
        return [t for t in texts if t]


@dataclass
class TopicBrief:
    """選題輸入：題目與切角，或使用者自填的重點（每行一點）。"""

    title: str = ""
    angle: str = ""
    points: list[str] = field(default_factory=list)
    source_summary: str = ""


@dataclass
class EditorialRequest:
    topic: TopicBrief
    market: MarketProfile
    track_name: str
    account_handle: str = ""
    brand_label: str = ""
    slide_count: int | None = None


def build_prompt(req: EditorialRequest, feedback: str = "") -> str:
    m = req.market
    lang_en = LANGUAGE_NAMES_EN.get(m.target_language, m.target_language)
    count_rule = (
        f"正好 {req.slide_count} 張（封面 1 張、尾頁收藏追蹤 1 張，其餘為重點）"
        if req.slide_count
        else f"依內容決定 {MIN_SLIDE_COUNT}–{MAX_SLIDE_COUNT} 張（封面 1 張、尾頁收藏追蹤 1 張，其餘每張只講一個重點）"
    )
    topic_lines = []
    if req.topic.title:
        topic_lines.append(f"題目：{req.topic.title}")
    if req.topic.angle:
        topic_lines.append(f"切角：{req.topic.angle}")
    if req.topic.points:
        topic_lines.append("使用者提供的重點（每行一點，請據此展開，不要偏離）：\n" + "\n".join(f"- {p}" for p in req.topic.points))
    if req.topic.source_summary:
        topic_lines.append(f"參考的爆款案例摘要（只學結構與切角，不得抄寫原文）：\n{req.topic.source_summary}")
    compliance = "\n".join(f"- {n}" for n in m.compliance_notes) or "- 不虛構職缺、薪資、簽證資格或成功案例"
    prompt = f"""你是 TikTok 輪播圖文的企劃編輯。請為帳號 {req.account_handle or '（未指定）'}（品牌：{req.brand_label or '未指定'}）寫一篇輪播貼文。

受眾：{m.audience or '未指定'}
帳號內容方向與訴求（請依此決定主題比重、語氣與是否加 CTA）：{m.content_brief or '未設定'}
發布語言：{m.target_language_label}（{lang_en}）。審稿語言：繁體中文。
語氣：{m.tone}
內容線：{req.track_name}
{chr(10).join(topic_lines)}

合規規則（違反會被退回重寫）：
{compliance}
- 不得承諾收入、職缺或簽證結果。涉及法規時只說公開資訊，並提醒以官方管道為準。

輸出規格：只輸出一個 JSON 物件，不要 Markdown、不要任何說明文字。欄位：
{{
  "title": "{lang_en} 標題，90 字元內",
  "zh_tw_title": "繁體中文標題",
  "caption": "{lang_en} 貼文內文，可含換行，不含 hashtag",
  "zh_tw_caption": "繁體中文貼文對照",
  "hashtags": ["恰好 5 個，簡明、相關，以 # 開頭"],
  "core_angle": "繁體中文，一句話說明切角",
  "cta": "{lang_en} 的行動呼籲（尾頁用）",
  "risk_flags": ["你自己發現的風險，沒有就給空陣列"],
  "slides": [
    {{
      "title": "{lang_en} 頁面標題，40 字元內",
      "body": "{lang_en} 頁面內文，90 字元內，口語、具體",
      "zh_tw": "繁體中文完整對照，格式「短標題：說明」",
      "scene_prompt": "English, one sentence describing a realistic photographic scene for this slide (no text in the image)"
    }}
  ]
}}
張數：{count_rule}。每頁文字要短，能清楚放進 1080×1350 圖片；每頁內容不得重複；場景描述彼此要有明顯差異。"""
    if m.scene_style:
        prompt += f"\n圖片風格參考（用在 scene_prompt）：{m.scene_style}"
    if feedback:
        prompt += f"\n\n上一次的輸出有問題，請修正後重新輸出完整 JSON：\n{feedback}"
    return prompt


def extract_json(text: str) -> dict[str, object]:
    """從模型輸出擷取第一個完整 JSON 物件（容忍前後文字與 ``` 圍欄）。"""
    cleaned = re.sub(r"```(?:json)?", "", text)
    start = cleaned.find("{")
    if start < 0:
        raise ValueError("輸出中找不到 JSON 物件")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                data = json.loads(cleaned[start : i + 1])
                if not isinstance(data, dict):
                    raise ValueError("JSON 不是物件")
                return data
    raise ValueError("JSON 物件沒有結尾")


def check_compliance(draft: EditorialDraft, market: MarketProfile) -> tuple[list[str], list[str]]:
    """回傳 (嚴重違規, 提醒)。嚴重違規要退回重寫；提醒會寫進 risk_flags。"""
    texts = draft.all_texts()
    severe = list(audit_editorial_compliance(texts))
    combined = "\n".join(texts)
    for pat in SEVERE_PATTERNS:
        m = pat.search(combined)
        if m:
            severe.append(f"偵測到保證類或虛構承諾：「{m.group(0)}」")
    warnings = []
    if any(p.search(combined) for p in SENSITIVE_PATTERNS):
        warnings.append(SENSITIVE_FLAG)
    return severe, warnings


class EditorialProvider(ABC):
    name: str = "base"

    @abstractmethod
    def is_available(self) -> tuple[bool, str]:
        """(可用, 說明)"""

    @abstractmethod
    def complete(self, prompt: str) -> str:
        """送出提示詞，回傳模型原始文字。"""

    def generate(self, req: EditorialRequest) -> EditorialDraft:
        ok, why = self.is_available()
        if not ok:
            raise EditorialUnavailableError(why)
        feedback = ""
        parse_failures = 0
        rewrites = 0
        while True:
            raw = self.complete(build_prompt(req, feedback))
            try:
                draft = EditorialDraft.model_validate(extract_json(raw))
            except (ValueError, ValidationError) as exc:
                parse_failures += 1
                if parse_failures > MAX_PARSE_RETRIES:
                    raise EditorialRejectedError(
                        f"文案引擎連續 {parse_failures} 次輸出無法解析：{str(exc)[:300]}", reasons=[str(exc)[:300]]
                    ) from exc
                feedback = f"輸出無法解析或不符規格：{str(exc)[:400]}。請只輸出符合規格的 JSON。"
                continue
            if req.slide_count and len(draft.slides) != req.slide_count:
                parse_failures += 1
                if parse_failures > MAX_PARSE_RETRIES:
                    raise EditorialRejectedError(f"要求 {req.slide_count} 張，但引擎給了 {len(draft.slides)} 張")
                feedback = f"張數必須正好 {req.slide_count} 張，你給了 {len(draft.slides)} 張。"
                continue
            severe, warnings = check_compliance(draft, req.market)
            if severe:
                if rewrites >= MAX_COMPLIANCE_REWRITES:
                    raise EditorialRejectedError("文案含有不可發布的承諾，重寫後仍未通過合規檢查", reasons=severe)
                rewrites += 1
                feedback = "合規檢查未通過，請移除下列內容後重寫：\n" + "\n".join(f"- {s}" for s in severe)
                continue
            for w in warnings:
                if w not in draft.risk_flags:
                    draft.risk_flags.append(w)
            return draft


Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def _run_subprocess(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=600)


class CodexCliEditorialProvider(EditorialProvider):
    name = "Codex CLI"

    def __init__(self, *, codex_bin: str = "", model: str = "", workdir: Path | None = None, runner: Runner | None = None) -> None:
        self._explicit_bin = codex_bin
        self.model = model
        self.workdir = workdir or Path.cwd()
        self._runner = runner or _run_subprocess

    def is_available(self) -> tuple[bool, str]:
        if find_codex_bin(self._explicit_bin) is None:
            return False, "找不到 Codex CLI。請安裝 ChatGPT App，或在 .env 設定 CODEX_BIN。"
        return True, "Codex CLI"

    def complete(self, prompt: str) -> str:
        path = find_codex_bin(self._explicit_bin)
        if path is None:
            raise EditorialUnavailableError("找不到 Codex CLI。")
        cmd = [str(path), "exec"]
        if self.model:
            cmd += ["-m", self.model]
        # 文案不需要讀專案檔：在空的暫存目錄執行，避免外部文字誘導讀到 .env 或憑證
        with tempfile.TemporaryDirectory(prefix="autotok-editorial-") as scratch:
            cmd += ["--skip-git-repo-check", "--ephemeral", "-C", scratch, "-s", "read-only", prompt]
            try:
                proc = self._runner(cmd)
            except subprocess.TimeoutExpired as exc:
                raise AutotokSystemError("Codex CLI 產生文案逾時（10 分鐘）", code="CODEX_TIMEOUT") from exc
        if proc.returncode != 0:
            raise AutotokSystemError(
                f"Codex CLI 產生文案失敗 (exit={proc.returncode})：{(proc.stderr or proc.stdout)[-400:]}",
                code="CODEX_EXEC_FAILED",
            )
        return proc.stdout


class AnthropicEditorialProvider(EditorialProvider):
    name = "Anthropic API"

    def __init__(self, *, api_key: str, model: str = "claude-sonnet-5-5", transport: httpx.BaseTransport | None = None) -> None:
        self.api_key = api_key.strip()
        self.model = model
        self._transport = transport

    def is_available(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "沒有設定 ANTHROPIC_API_KEY。"
        return True, "Anthropic API"

    def complete(self, prompt: str) -> str:
        body = {"model": self.model, "max_tokens": 6000, "messages": [{"role": "user", "content": prompt}]}
        headers = {"x-api-key": self.api_key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
        with httpx.Client(timeout=120, transport=self._transport) as client:
            resp = client.post(ANTHROPIC_API_URL, headers=headers, json=body)
        if resp.status_code == 401:
            raise EditorialUnavailableError("Anthropic API 金鑰無效（401），請檢查 ANTHROPIC_API_KEY。")
        if resp.status_code >= 400:
            raise AutotokSystemError(f"Anthropic API 錯誤 {resp.status_code}：{resp.text[:300]}", code="ANTHROPIC_API_ERROR")
        parts = resp.json().get("content", [])
        return "".join(p.get("text", "") for p in parts if p.get("type") == "text")


def get_editorial_provider(settings: AppSettings, *, workdir: Path | None = None) -> EditorialProvider:
    """依序嘗試 Codex CLI、Anthropic API；都不可用就丟出說明清楚的錯誤，不退回範例文案。"""
    codex = CodexCliEditorialProvider(codex_bin=settings.codex_bin, model=settings.codex_model, workdir=workdir)
    ok_codex, why_codex = codex.is_available()
    if ok_codex:
        return codex
    claude = AnthropicEditorialProvider(api_key=settings.anthropic_api_key, model=settings.anthropic_model)
    ok_claude, why_claude = claude.is_available()
    if ok_claude:
        return claude
    raise EditorialUnavailableError(f"沒有可用的文案引擎。{why_codex} {why_claude}")


def editorial_status(settings: AppSettings) -> tuple[bool, str]:
    """給畫面顯示用：(可用, 引擎名稱或原因)。"""
    try:
        return True, get_editorial_provider(settings).name
    except EditorialUnavailableError as exc:
        return False, str(exc)
