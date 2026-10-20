"""市場設定（MarketProfile）：每個 TikTok 帳號各自的受眾語言、時區、發文時段與內容規則。

帳號 → 一個 MarketProfile。審稿語言預設為繁體中文，發布語言（target_language）依市場而定。
發文時段的「類型」固定為三條內容線（早／午／晚，對應 `ContentSlot`），但實際時間可依帳號調整。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models import ContentSlot

SLOT_KINDS: tuple[ContentSlot, ContentSlot, ContentSlot] = (
    ContentSlot.MORNING_0700,
    ContentSlot.NOON_1130,
    ContentSlot.EVENING_1800,
)

# 每條內容線的預設名稱；帳號可在 MarketProfile.track_names 覆寫
DEFAULT_TRACK_NAMES: dict[str, str] = {
    ContentSlot.MORNING_0700.value: "家庭與情緒",
    ContentSlot.NOON_1130.value: "生活與文化",
    ContentSlot.EVENING_1800.value: "創作與成長",
}


class MarketProfile(BaseModel):
    """單一帳號的市場設定。"""

    template_id: str = Field(default="custom", description="來源範本，僅作紀錄")
    audience: str = Field(default="", description="受眾描述，會餵給選題與文案")
    country: str = Field(default="TW", description="受眾主要所在地（ISO 國碼）")
    target_language: str = Field(default="vi", description="發布語言代碼")
    target_language_label: str = Field(default="越南文")
    review_language: str = Field(default="zh-TW", description="審稿語言")
    timezone: str = Field(default="Asia/Taipei")
    slot_times: dict[str, str] = Field(
        default_factory=lambda: {
            ContentSlot.MORNING_0700.value: "07:00",
            ContentSlot.NOON_1130.value: "11:30",
            ContentSlot.EVENING_1800.value: "18:00",
        },
        description="內容線 → 當地發文時間 HH:MM",
    )
    track_names: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_TRACK_NAMES))
    hashtag_count: int = Field(default=5, ge=0, le=10)
    script: str = Field(default="latin", description="文字系統：latin / thai / cjk，用來挑字型")
    content_brief: str = Field(
        default="",
        description="內容方向與訴求（自由文字）：想吸引誰、要什麼互動、內容類型的比重、哪些要有 CTA。研究與文案都會參考",
    )
    auto_enabled: bool = Field(default=False, description="排程器是否自動處理這個帳號")
    auto_research_time: str = Field(default="17:00", description="每日自動研究的當地時間")
    auto_prepare_time: str = Field(default="20:00", description="每日自動準備明天內容的當地時間")
    auto_publish: bool = Field(default=False, description="排程器是否自動發布（需帳號可公開發布）")
    tone: str = Field(default="溫暖、實用、同理，不誇大")
    compliance_notes: list[str] = Field(default_factory=list)
    scene_style: str = Field(default="", description="圖片場景風格描述，給生圖引擎參考")
    seed_keywords: list[str] = Field(default_factory=list)

    def track_name(self, slot: ContentSlot) -> str:
        return self.track_names.get(slot.value, DEFAULT_TRACK_NAMES[slot.value])

    def time_for(self, slot: ContentSlot) -> str:
        return self.slot_times.get(slot.value, slot.value.split("_")[0])


_COMMON_COMPLIANCE = [
    "不虛構職缺、薪資、簽證資格或成功案例",
    "涉及法規、居留或勞動權益時，只提供公開資訊並提醒以官方管道為準",
]

MARKET_TEMPLATES: dict[str, MarketProfile] = {
    "vi-tw": MarketProfile(
        template_id="vi-tw",
        audience="在台越南移工、留學生、新住民與青年上班族",
        country="TW",
        target_language="vi",
        target_language_label="越南文",
        script="latin",
        compliance_notes=[*_COMMON_COMPLIANCE, "商業合作需揭露經營性質與收入不確定性"],
        scene_style="寫實的台灣日常場景攝影（超商、捷運、租屋處、夜市），暖色自然光",
        seed_keywords=["người Việt ở Đài Loan", "cuộc sống ở Đài Loan", "làm việc tại Đài Loan"],
    ),
    "th-tw": MarketProfile(
        template_id="th-tw",
        audience="在台泰國移工、留學生與新住民",
        country="TW",
        target_language="th",
        target_language_label="泰文",
        script="thai",
        slot_times={
            ContentSlot.MORNING_0700.value: "08:00",
            ContentSlot.NOON_1130.value: "12:30",
            ContentSlot.EVENING_1800.value: "19:30",
        },
        compliance_notes=list(_COMMON_COMPLIANCE),
        scene_style="寫實的台灣日常與美食場景攝影，明亮自然光",
        seed_keywords=["คนไทยในไต้หวัน", "ชีวิตในไต้หวัน", "ทำงานที่ไต้หวัน"],
    ),
    "id-tw": MarketProfile(
        template_id="id-tw",
        audience="在台印尼移工、留學生與新住民",
        country="TW",
        target_language="id",
        target_language_label="印尼文",
        script="latin",
        compliance_notes=list(_COMMON_COMPLIANCE),
        scene_style="寫實的台灣日常場景攝影，暖色自然光",
        seed_keywords=["orang Indonesia di Taiwan", "hidup di Taiwan", "kerja di Taiwan"],
    ),
    "en-global": MarketProfile(
        template_id="en-global",
        audience="對台灣生活與旅遊有興趣的英語使用者",
        country="TW",
        target_language="en",
        target_language_label="英文",
        script="latin",
        compliance_notes=list(_COMMON_COMPLIANCE),
        scene_style="寫實的台灣城市與生活場景攝影",
        seed_keywords=["living in Taiwan", "Taiwan travel tips", "expat in Taiwan"],
    ),
}

MARKET_TEMPLATE_LABELS: dict[str, str] = {
    "vi-tw": "越南文 · 在台越南人",
    "th-tw": "泰文 · 在台泰國人",
    "id-tw": "印尼文 · 在台印尼人",
    "en-global": "英文 · 台灣生活與旅遊",
}


def profile_from_template(template_id: str) -> MarketProfile:
    base = MARKET_TEMPLATES.get(template_id)
    return base.model_copy(deep=True) if base else MarketProfile()


def default_profile_for_language(target_language: str) -> MarketProfile:
    for tpl in MARKET_TEMPLATES.values():
        if tpl.target_language == target_language:
            return tpl.model_copy(deep=True)
    return MarketProfile(target_language=target_language, target_language_label=target_language)
