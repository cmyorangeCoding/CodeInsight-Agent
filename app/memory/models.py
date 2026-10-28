"""
长期记忆的统一数据模型。

本阶段实现两大类、四种记忆：

用户记忆（scope=user）
    - preference：用户偏好（回答风格、解释深度等）
    - profile   ：用户背景（技术栈、项目方向等）

项目记忆（scope=project）
    - fact      ：项目事实（模块职责、调用关系等，尽量附带可验证来源）
    - decision  ：架构决策（已确认 / 讨论中 / 已被取代）

统一字段：
    memory_id / scope / category / memory_key / content / source /
    confidence / status / created_at / updated_at
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class MemoryScope(str, Enum):
    """记忆作用域。"""

    USER = "user"
    PROJECT = "project"

    __str__ = str.__str__


class MemoryCategory(str, Enum):
    """记忆类别。"""

    PREFERENCE = "preference"
    PROFILE = "profile"
    FACT = "fact"
    DECISION = "decision"

    __str__ = str.__str__


class MemoryStatus(str, Enum):
    """记忆状态。

    active     当前有效
    superseded 已被更新的结论取代
    expired    已过期
    """

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"

    __str__ = str.__str__


class MemorySource(str, Enum):
    """记忆来源类型。

    user_stated     用户明确表达
    user_confirmed  用户明确确认
    code_verified   有代码证据验证
    model_proposed  模型自行提出（未经确认）
    """

    USER_STATED = "user_stated"
    USER_CONFIRMED = "user_confirmed"
    CODE_VERIFIED = "code_verified"
    MODEL_PROPOSED = "model_proposed"

    __str__ = str.__str__


def utcnow_iso() -> str:
    """返回统一的 UTC ISO 时间字符串（秒级）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class MemoryRecord:
    """统一的长期记忆记录。

    该对象既是写入 Store 的载荷（``to_value``），也是从 Store 读取后的视图
    （``from_value``）。
    """

    memory_id: str #稳定的记忆标识
    scope: str  #user 或 project
    category: str  #preference、profile、fact 或 decision
    memory_key: str #用于精确读取、去重和更新的稳定键
    content: str
    source: str #来源类型，例如用户明确表达、代码验证、用户确认
    status: str = MemoryStatus.ACTIVE.value  #active、superseded、expired
    confidence: float | None = None  #辅助可信度信息
    source_reference: str | None = None
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)
    # 历史版本（被取代的旧结论快照），用于保留旧决策状态与来源
    history: list[dict[str, Any]] = field(default_factory=list)
    # 语义检索得分（仅检索结果有值，不写入 Store）
    score: float | None = None

    def to_value(self) -> dict[str, Any]:
        """转成 Store 的 value 字典。

        注意：PostgresStore 配置的索引字段是 ``text``，
        因此这里必须包含一个顶层 ``text`` 字段供向量化。
        """
        value: dict[str, Any] = {
            "text": self.content,
            "memory_id": self.memory_id,
            "scope": self.scope,
            "category": self.category,
            "memory_key": self.memory_key,
            "content": self.content,
            "source": self.source,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "history": list(self.history),
        }
        if self.confidence is not None:
            value["confidence"] = self.confidence
        if self.source_reference:
            value["source_reference"] = self.source_reference
        return value

    @classmethod
    def from_value(cls, key: str, value: dict[str, Any], score: float | None = None) -> MemoryRecord:
        """从 Store 的 value 字典还原记录。"""
        return cls(
            memory_id=value.get("memory_id", key),
            scope=value.get("scope", ""),
            category=value.get("category", ""),
            memory_key=value.get("memory_key", key),
            content=value.get("content", ""),
            source=value.get("source", MemorySource.MODEL_PROPOSED.value),
            status=value.get("status", MemoryStatus.ACTIVE.value),
            confidence=value.get("confidence"),
            source_reference=value.get("source_reference"),
            created_at=value.get("created_at", utcnow_iso()),
            updated_at=value.get("updated_at", utcnow_iso()),
            history=list(value.get("history", [])),
            score=score,
        )

    def snapshot(self) -> dict[str, Any]:
        """当前版本的快照，用于写入新记录的 history。"""
        return {
            "content": self.content,
            "source": self.source,
            "confidence": self.confidence,
            "status": self.status,
            "source_reference": self.source_reference,
            "updated_at": self.updated_at,
        }
