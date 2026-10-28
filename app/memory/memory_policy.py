"""
长期记忆策略：分类校验、保存规则、冲突处理、敏感信息过滤。

原则：
- 记忆不是指令，也不一定是真实事实。
- 不能因为模型自己给出 confidence=1.0 就认定为真实。
- 未确认的建议不得保存为已确认的项目决策。
- 冲突时优先依据可信来源和最新的已确认状态处理，无法安全判断则保留现有记忆。
"""
from __future__ import annotations

import re

from app.memory.models import (
    MemoryCategory,
    MemoryScope,
    MemorySource,
    MemoryStatus,
)


class MemoryPolicyError(ValueError):
    """记忆策略校验失败（参数非法、越权、敏感信息、禁止保存等）。"""

#域 - 分类白名单
#规定不同 scope 下能存哪几类记忆
ALLOWED_CATEGORIES: dict[str, set[str]] = {
    # USER域只能存：偏好、个人档案
    MemoryScope.USER.value: {
        MemoryCategory.PREFERENCE.value,
        MemoryCategory.PROFILE.value,
    },
   # PROJECT域只能存：事实、项目决策
    MemoryScope.PROJECT.value: {
        MemoryCategory.FACT.value,
        MemoryCategory.DECISION.value,
    },
}

#分类 - 来源白名单
#每一类记忆，只接受指定来源写入
ALLOWED_SOURCES: dict[str, set[str]] = {
    #仅允许用户自述 / 用户确认（模型不能自己生成偏好档案）
    MemoryCategory.PREFERENCE.value: {
        MemorySource.USER_STATED.value,
        MemorySource.USER_CONFIRMED.value,
    },
    MemoryCategory.PROFILE.value: {
        MemorySource.USER_STATED.value,
        MemorySource.USER_CONFIRMED.value,
    },
    #允许用户口述、用户确认、代码校验
    MemoryCategory.FACT.value: {
        MemorySource.CODE_VERIFIED.value,
        MemorySource.USER_CONFIRMED.value,
        MemorySource.USER_STATED.value,
    },
    #只能是用户自述 / 用户确认
    MemoryCategory.DECISION.value: {
        MemorySource.USER_CONFIRMED.value,
        MemorySource.USER_STATED.value,
    },
}

# 来源可信度权重（仅用于冲突时判断新旧强弱，不作为真实性依据）
SOURCE_STRENGTH: dict[str, int] = {
    MemorySource.MODEL_PROPOSED.value: 0,
    MemorySource.USER_STATED.value: 1,
    MemorySource.CODE_VERIFIED.value: 2,
    MemorySource.USER_CONFIRMED.value: 3,
}

# 敏感信息特征（API Key / 密码 / Token 等），命中则拒绝写入长期记忆
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9._\-]{8,}"),
    re.compile(r"\b(?:api[_-]?key|secret|password|passwd|token)\b\s*[:=]", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{10,}", re.IGNORECASE),
]

_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.:\-]{1,100}$")

MAX_CONTENT_CHARS = 2000


def contains_secret(text: str) -> bool:
    """粗略判断文本是否包含密钥类敏感信息。"""
    return any(p.search(text) for p in _SECRET_PATTERNS)


def validate_key(memory_key: str) -> str:
    """校验 memory_key，保证键稳定、可读、无空格。"""
    if not memory_key or not isinstance(memory_key, str):
        raise MemoryPolicyError("memory_key 不能为空。")
    memory_key = memory_key.strip()
    if not _KEY_PATTERN.match(memory_key):
        raise MemoryPolicyError(
            "memory_key 只能是字母/数字/下划线/点/冒号/连字符，长度 1-100。"
        )
    return memory_key


def validate_content(content: str) -> str:
    """校验记忆内容，过滤敏感信息与超长文本。"""
    if not content or not isinstance(content, str) or not content.strip():
        raise MemoryPolicyError("content 不能为空。")
    content = content.strip()
    if len(content) > MAX_CONTENT_CHARS:
        raise MemoryPolicyError(
            f"content 过长（{len(content)} 字符），上限 {MAX_CONTENT_CHARS}。"
        )
    if contains_secret(content):
        raise MemoryPolicyError(
            "内容疑似包含 API Key / 密码 / Token 等敏感信息，拒绝写入长期记忆。"
        )
    return content


def validate_scope_category(scope: str, category: str) -> None:
    """校验 scope 与 category 组合是否合法。"""
    if scope not in ALLOWED_CATEGORIES:
        raise MemoryPolicyError(f"非法 scope：{scope}（只能是 user / project）。")
    if category not in ALLOWED_CATEGORIES[scope]:
        allowed = " / ".join(sorted(ALLOWED_CATEGORIES[scope]))
        raise MemoryPolicyError(
            f"scope={scope} 不允许 category={category}，允许：{allowed}。"
        )


def validate_source(category: str, source: str) -> None:
    """校验 source 是否被该 category 接受。

    关键规则：模型未经确认的建议（model_proposed）不得保存为项目决策。
    """
    if source not in SOURCE_STRENGTH:
        raise MemoryPolicyError(
            f"非法 source：{source}（只能是 user_stated / user_confirmed / "
            "code_verified / model_proposed）。"
        )
    if source not in ALLOWED_SOURCES[category]:
        allowed = " / ".join(sorted(ALLOWED_SOURCES[category]))
        raise MemoryPolicyError(
            f"category={category} 不接受 source={source}，允许：{allowed}。"
            "（未确认的模型建议不能保存为已确认的项目决策。）"
        )


def normalize_confidence(confidence: float | None) -> float | None:
    """校验 confidence 取值范围。"""
    if confidence is None:
        return None
    try:
        value = float(confidence)
    except (TypeError, ValueError) as exc:
        raise MemoryPolicyError("confidence 必须是 0-1 之间的数字。") from exc
    if not 0.0 <= value <= 1.0:
        raise MemoryPolicyError("confidence 必须是 0-1 之间的数字。")
    return value


class UpdateDecision(str):
    """简单常量容器：冲突处理结果。"""

    CREATE = "created"
    UPDATE = "updated"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


def decide_update(
    existing_content: str,
    existing_source: str,
    existing_status: str,
    incoming_content: str,
    incoming_source: str,
) -> str:
    """根据已存在记录与新记录，决定更新策略。

    返回 UpdateDecision 常量之一：
        duplicate  内容与来源完全相同 → 跳过
        update     允许受控更新（旧记录标记 superseded 并写入 history）
        rejected   新记录来源更弱 → 拒绝覆盖，保留现有记忆
    """
    if existing_content.strip() == incoming_content.strip() and existing_source == incoming_source:
        return UpdateDecision.DUPLICATE

    old_strength = SOURCE_STRENGTH.get(existing_source, -1)
    new_strength = SOURCE_STRENGTH.get(incoming_source, -1)

    # 已失效的旧记录允许被重新激活/更新
    if existing_status != MemoryStatus.ACTIVE.value:
        return UpdateDecision.UPDATE

    # 新来源更弱时不允许覆盖已有确认结论
    if new_strength < old_strength:
        return UpdateDecision.REJECTED

    return UpdateDecision.UPDATE
