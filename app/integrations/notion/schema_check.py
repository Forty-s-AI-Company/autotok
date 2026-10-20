"""比對 Notion 資料庫實際欄位與系統寫入所需欄位（唯一來源：`app.notion_schema.REQUIRED_NOTION_PROPERTIES`）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.notion_schema import ACCEPTED_ALTERNATE_TYPES, NOT_API_CREATABLE_TYPES, REQUIRED_NOTION_PROPERTIES

TYPE_LABEL = {
    "title": "標題",
    "rich_text": "文字",
    "status": "狀態",
    "date": "日期",
    "select": "單選",
    "files": "檔案",
    "checkbox": "核取方塊",
}


@dataclass(frozen=True)
class MissingProperty:
    name: str
    type: str

    @property
    def creatable(self) -> bool:
        return self.type not in NOT_API_CREATABLE_TYPES

    @property
    def type_label(self) -> str:
        return TYPE_LABEL.get(self.type, self.type)

    @property
    def label(self) -> str:
        return f"{self.name}（{self.type_label}）"


@dataclass(frozen=True)
class TypeMismatch:
    name: str
    expected: str
    actual: str


@dataclass
class SchemaCheck:
    missing: list[MissingProperty] = field(default_factory=list)
    type_mismatch: list[TypeMismatch] = field(default_factory=list)
    present: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.type_mismatch

    @property
    def total(self) -> int:
        return len(REQUIRED_NOTION_PROPERTIES)

    @property
    def creatable_missing(self) -> list[MissingProperty]:
        return [m for m in self.missing if m.creatable]

    def stored_names(self) -> list[str]:
        """寫進 account_notion.schema_missing 的清單（給今天頁與狀態列顯示）。"""
        names = [m.name for m in self.missing]
        names += [f"{m.name}（型別應為{TYPE_LABEL.get(m.expected, m.expected)}）" for m in self.type_mismatch]
        return names


def check_schema(properties: dict[str, dict[str, Any]]) -> SchemaCheck:
    """`properties` 為 Notion database payload 的 `properties`。"""
    result = SchemaCheck()
    for name, expected in REQUIRED_NOTION_PROPERTIES.items():
        meta = properties.get(name)
        if meta is None:
            result.missing.append(MissingProperty(name, expected))
            continue
        actual = str(meta.get("type", "unknown"))
        if actual != expected and actual not in ACCEPTED_ALTERNATE_TYPES.get(name, set()):
            result.type_mismatch.append(TypeMismatch(name, expected, actual))
        else:
            result.present.append(name)
    return result
