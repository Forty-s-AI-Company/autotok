"""研究案例對特定帳號的適配度（決定性規則，不依賴 LLM）。

這是內部研究用的啟發式分數（0–100），不是 TikTok 官方演算法分數。組成：

| 項目 | 滿分 | 依據 |
|---|---|---|
| 語言 | 30 | 案例標題的文字系統／語言與帳號發布語言是否相符 |
| 關鍵字與受眾 | 25 | 帳號種子關鍵字、受眾描述是否出現在案例文字 |
| 地區 | 10 | 是否提到帳號受眾所在地 |
| 輪播適配 | 15 | 敘事類別是否容易拆成一頁一重點，標題有數字清單加分 |
| 互動與可信度 | 20 | 互動率（缺值不當 0，以中間值計並在理由說明）、觀看規模、資料可信度 |

廣告樣本分數上限 35。涉及簽證／居留／薪資／匯款等題材一律標「需合規審核」。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from app.markets import MarketProfile
from app.models import ContentSlot, EngagementRateType, ResearchCase, SourceType
from app.research import compute_engagement_metrics

FLAG_COMPLIANCE = "需合規審核"
FLAG_AD = "廣告樣本"

_VI_CHARS = set("ăâđêôơưạảãấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ")
_ID_WORDS = {"yang", "dan", "di", "untuk", "ini", "dengan", "kamu", "aku", "tidak", "cara", "orang", "kerja"}
_EN_WORDS = {"the", "and", "to", "for", "how", "you", "in", "of", "is", "your", "with", "tips", "living"}

_SENSITIVE_TERMS = (
    "簽證", "居留", "工作證", "薪資", "基本工資", "匯款", "仲介", "借貸", "貸款", "投資", "保證",
    "visa", "thị thực", "gia hạn", "arc", "lương", "chuyển tiền", "môi giới", "vay", "cư trú",
    "วีซ่า", "เงินเดือน", "โอนเงิน", "นายหน้า", "resident", "salary", "wage", "remittance", "broker", "loans",
    "invest", "investing", "investment", "gaji", "kirim uang", "pinjaman",
    "收入", "月入", "賺錢", "kiếm tiền", "kiếm được", "thu nhập", "income", "earn money", "pendapatan",
)  # fmt: skip

_COUNTRY_TERMS = {
    "TW": ("台灣", "台北", "臺灣", "taiwan", "đài loan", "đài bắc", "taipei", "ไต้หวัน", "ไทเป"),
    "VN": ("越南", "vietnam", "việt nam", "hà nội", "hồ chí minh"),
    "TH": ("泰國", "thailand", "ประเทศไทย", "กรุงเทพ"),
    "ID": ("印尼", "indonesia", "jakarta"),
    "JP": ("日本", "japan", "nhật bản", "tokyo", "東京"),
    "KR": ("韓國", "korea", "seoul", "서울"),
}

_FORMAT_BASE = {
    "practical_checklist": 15,
    "comparison_carousel": 15,
    "food_guide": 13,
    "travel_itinerary": 13,
    "emotional_story": 11,
    "general": 6,
}
_CATEGORY_LABEL = {
    "practical_checklist": "清單式",
    "comparison_carousel": "對比式",
    "food_guide": "美食指南",
    "travel_itinerary": "行程式",
    "emotional_story": "情緒故事",
    "general": "一般",
}
_CREATOR_TERMS = ("創作", "拍照", "拍攝", "手機", "剪輯", "行銷", "自媒體", "creator", "điện thoại", "chụp", "làm video", "content")


@dataclass(frozen=True)
class CaseFit:
    case_id: str
    score: float
    reason: str
    flag: str
    recommended_slot: ContentSlot
    parts: dict[str, float] = field(default_factory=dict)

    def as_row(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "score": self.score,
            "reason": self.reason,
            "flag": self.flag,
            "recommended_slot": self.recommended_slot.value,
        }


def detect_language(text: str) -> str:
    """簡易文字系統／語言判斷：vi、th、zh、ja、ko、id、en、latin（不確定的拉丁字母）、unknown。"""
    if not text.strip():
        return "unknown"
    counts = {"th": 0, "zh": 0, "ja": 0, "ko": 0, "vi": 0, "latin": 0}
    for ch in text:
        cp = ord(ch)
        if 0x0E00 <= cp <= 0x0E7F:
            counts["th"] += 1
        elif 0x3040 <= cp <= 0x30FF:
            counts["ja"] += 1
        elif 0xAC00 <= cp <= 0xD7AF:
            counts["ko"] += 1
        elif 0x4E00 <= cp <= 0x9FFF:
            counts["zh"] += 1
        elif ch.lower() in _VI_CHARS:
            counts["vi"] += 1
        elif ch.isalpha() and cp < 0x250:
            counts["latin"] += 1
    if counts["ja"]:
        return "ja"
    for lang in ("th", "ko"):
        if counts[lang] >= 2:
            return lang
    if counts["zh"] >= 2:
        return "zh"
    if counts["vi"] >= 2:
        return "vi"
    if counts["latin"]:
        words = set(re.findall(r"[a-z]+", text.lower()))
        id_hits, en_hits = len(words & _ID_WORDS), len(words & _EN_WORDS)
        if id_hits > en_hits:
            return "id"
        if en_hits > 0:
            return "en"
        return "latin"
    return "unknown"


def _fold(text: str) -> str:
    """轉小寫並移除聲調符號，讓 'Đài Loan' 與 'dai loan' 都能比對。"""
    text = text.lower().replace("đ", "d")
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"\w+", _fold(text)) if len(w) >= 3]


def _language_score(target: str, detected: str, has_title: bool) -> tuple[float, str]:
    if not has_title:
        return 10.0, "沒有標題文字，無法判斷語言"
    if detected == target:
        return 30.0, "語言與發布語言相符"
    if detected == "latin" and target in {"id", "en", "vi"}:
        return 18.0, "拉丁字母，語言無法確定"
    if detected == "zh" and target != "zh":
        return 8.0, "原文是中文，需翻成發布語言"
    if target in {"id", "en"} and detected in {"id", "en", "latin"}:
        return 12.0, "語言與發布語言不同"
    return 2.0, "語言與發布語言不符"


def _keyword_score(case: ResearchCase, market: MarketProfile) -> tuple[float, list[str], bool]:
    """回傳 (分數, 命中的關鍵字, 是否有設定關鍵字)。"""
    text = " ".join([case.title, case.title_zh, case.hook_notes])
    folded = _fold(text)
    text_words = set(_words(text))
    hits: list[str] = []
    partial = 0
    full = 0
    for kw in market.seed_keywords:
        fk = _fold(kw)
        if fk and fk in folded:
            full += 1
            hits.append(kw)
            continue
        kws = _words(kw)
        if kws and sum(1 for w in kws if w in text_words) * 2 >= len(kws):
            partial += 1
            hits.append(kw)
    audience_hits = 0
    for chunk in re.split(r"[、，,；;\s]+", market.audience):
        chunk = chunk.strip()
        if len(chunk) >= 2 and re.search(r"[一-鿿]", chunk) and chunk in text:
            audience_hits += 1
    has_keywords = bool(market.seed_keywords)
    if not has_keywords and audience_hits == 0:
        return 10.0, [], False
    score = min(25.0, 12 * full + 6 * partial + 4 * min(audience_hits, 2))
    return float(score), hits[:2], has_keywords


def _region_score(case: ResearchCase, market: MarketProfile) -> tuple[float, bool]:
    terms = _COUNTRY_TERMS.get(market.country.upper(), ())
    folded = _fold(" ".join([case.title, case.title_zh, case.hook_notes]))
    hit = any(_fold(t) in folded for t in terms)
    return (10.0 if hit else 0.0), hit


def _format_score(case: ResearchCase) -> float:
    base = float(_FORMAT_BASE.get(case.narrative_category, 6))
    if re.search(r"(?<!\d)\d{1,2}(?!\d)", case.title) or re.search(r"(?<!\d)\d{1,2}(?!\d)", case.title_zh):
        base += 2
    return min(15.0, base)


def _engagement_score(case: ResearchCase) -> tuple[float, str]:
    m = compute_engagement_metrics(case)
    if m.rate_type == EngagementRateType.UNAVAILABLE or m.rate_value is None:
        rate_part, note = 6.0, "互動數據不足，以中間值計"
    else:
        r = m.rate_value
        rate_part = 12.0 if r >= 0.08 else 9.0 if r >= 0.05 else 6.0 if r >= 0.03 else 3.0
        if m.rate_type == EngagementRateType.PARTIAL:
            rate_part *= 0.8
            note = f"部分互動率 {r * 100:.1f}%（缺{'、'.join(m.missing_numerator_fields)}）"
        else:
            note = f"互動率 {r * 100:.1f}%"
    v = case.views
    views_part = 0.0 if v is None else 4.0 if v >= 500_000 else 3.0 if v >= 100_000 else 2.0 if v >= 20_000 else 1.0
    return min(20.0, rate_part + views_part + case.confidence * 4), note


def _is_sensitive(case: ResearchCase) -> bool:
    folded = _fold(" ".join([case.title, case.title_zh, case.hook_notes]))
    for term in _SENSITIVE_TERMS:
        t = _fold(term)
        if t.isascii():
            if re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", folded):
                return True
        elif t in folded:
            return True
    return False


def _recommend_slot(case: ResearchCase) -> ContentSlot:
    text = " ".join([case.title, case.title_zh, case.hook_notes]).lower()
    if case.narrative_category == "emotional_story":
        return ContentSlot.MORNING_0700
    if case.narrative_category in {"food_guide", "travel_itinerary", "comparison_carousel"}:
        return ContentSlot.NOON_1130
    if any(t in text for t in _CREATOR_TERMS):
        return ContentSlot.EVENING_1800
    return ContentSlot.NOON_1130


def score_case(case: ResearchCase, market: MarketProfile) -> CaseFit:
    detected = detect_language(case.title or case.title_zh)
    lang, lang_note = _language_score(market.target_language, detected, bool(case.title.strip()))
    kw, kw_hits, has_kw = _keyword_score(case, market)
    region, region_hit = _region_score(case, market)
    fmt = _format_score(case)
    eng, eng_note = _engagement_score(case)

    total = lang + kw + region + fmt + eng
    flag = ""
    pieces = [lang_note]
    if kw_hits:
        pieces.append("命中關鍵字「" + "」「".join(kw_hits) + "」")
    elif not has_kw:
        pieces.append("帳號尚未設定關鍵字")
    if region_hit:
        pieces.append("提到受眾所在地")
    pieces.append(f"{_CATEGORY_LABEL.get(case.narrative_category, '一般')}結構" + ("易拆成輪播" if fmt >= 13 else ""))
    pieces.append(eng_note)

    if case.source_type == SourceType.AD:
        total = min(total, 35.0)
        flag = FLAG_AD
        pieces.append("廣告樣本只當結構參考，不當自然爆款")
    elif _is_sensitive(case):
        flag = FLAG_COMPLIANCE
        pieces.append("涉及簽證、居留、薪資或匯款，發布前要跑合規審核且不可保證結果")
    if case.is_mock:
        pieces.insert(0, "模擬資料")

    slot = _recommend_slot(case)
    return CaseFit(
        case_id=case.case_id,
        score=round(max(0.0, min(100.0, total)), 1),
        reason="；".join(pieces) + "。",
        flag=flag,
        recommended_slot=slot,
        parts={"language": lang, "keyword": kw, "region": region, "format": fmt, "engagement": eng},
    )


def compute_fits(cases: list[ResearchCase], market: MarketProfile) -> list[CaseFit]:
    return [score_case(c, market) for c in cases]
