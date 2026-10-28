"""
暴露给 Agent 的长期记忆工具。

三个工具严格分离职责：
    save_long_term_memory   保存/更新用户记忆或项目记忆（写入）
    search_project_memory   按需检索当前项目的事实与架构决策（读取）
    search_user_memory      按需检索当前用户自己的背景/偏好（读取）

安全边界：
- user_id / project_id 一律由构造时注入的可信上下文决定，
  工具参数中不暴露，模型无法通过传入任意 ID 越权访问其他用户或项目。
- 工具只做检索/保存，不伪造结果；失败时返回明确的可处理错误文本。
"""
from __future__ import annotations

from langchain_core.tools import BaseTool
from pydantic import ConfigDict

from app.memory.long_term_memory import LongTermMemory, MemoryStoreError
from app.memory.memory_policy import MemoryPolicyError
from app.memory.models import MemoryCategory, MemoryRecord, MemoryScope

MAX_DISPLAY_CONTENT = 600


def _clip(text: str, limit: int = MAX_DISPLAY_CONTENT) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…（已截断）"


def _format_records(records: list[MemoryRecord], header: str) -> str:
    if not records:
        return (
            "没有找到相关的已保存记忆。"
            "请不要凭空编造历史结论；如需保存，可调用 save_long_term_memory。"
        )
    lines = [f"{header}（共 {len(records)} 条）"]
    for i, record in enumerate(records, 1):
        lines.append(
            f"\n[{i}] {record.memory_key} "
            f"(category={record.category}, source={record.source}, status={record.status}"
            f"{', score=%.4f' % record.score if record.score is not None else ''})"
        )
        lines.append(f"内容：{_clip(record.content)}")
        if record.source_reference:
            lines.append(f"来源：{_clip(record.source_reference, 200)}")
        lines.append(f"更新时间：{record.updated_at}")
    return "\n".join(lines)


class SaveLongTermMemoryTool(BaseTool):
    """统一保存长期记忆（用户记忆 + 项目记忆）。"""

    name: str = "save_long_term_memory"
    description: str = (
        "保存或更新【长期记忆】，用于跨会话保留有长期价值的信息。"
        "参数 scope：user / project；category：preference / profile / fact / decision。"
        "仅在有长期价值时调用，不要每轮无条件保存，不要保存整段聊天记录。"
        "规则："
        "1) 用户明确表达的稳定偏好（如回答风格、解释深度）→ scope=user, category=preference, "
        "source=user_stated。仅本次临时的要求（如“这次简短点”）不要保存。"
        "2) 用户明确确认的项目决策 → scope=project, category=decision, source=user_confirmed。"
        "3) 有代码证据支持的项目事实 → scope=project, category=fact, source=code_verified，"
        "并在 source_reference 写明文件路径/符号名。"
        "4) 你自己推测但未经确认的建议，禁止保存为已确认的项目决策。"
        "memory_key 用稳定的短标识（如 answer_style、api_uses_sqlalchemy）。"
    )

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    def __init__(self, memory: LongTermMemory):
        super().__init__()
        self.memory = memory

    def _run(
        self,
        scope: str,
        category: str,
        memory_key: str,
        content: str,
        source: str,
        confidence: float | None = None,
        source_reference: str | None = None,
    ) -> str:
        try:
            result = self.memory.save(
                scope=scope,
                category=category,
                memory_key=memory_key,
                content=content,
                source=source,
                confidence=confidence,
                source_reference=source_reference,
            )
        except MemoryPolicyError as exc:
            return f"拒绝保存：{exc}"
        except MemoryStoreError as exc:
            return f"保存失败：{exc}（记忆未写入，请勿声称已保存成功）"

        return (
            f"保存结果：{result.action}（{result.message}）"
            f" scope={result.scope}, category={result.category}, key={result.memory_key}"
        )


class SearchProjectMemoryTool(BaseTool):
    """检索当前项目的历史事实与架构决策。"""

    name: str = "search_project_memory"
    description: str = (
        "检索【当前项目】已保存的历史事实与架构决策（scope=project）。"
        "在以下情况优先调用："
        "1) 用户提到「之前 / 上次 / 原来的方案 / 已经确定 / 我们之前讨论过」等历史指代；"
        "2) 当前任务依赖之前确定的项目架构或技术选型；"
        "3) 当前上下文不足以确定某个历史项目事实；"
        "4) 用户要求继续之前的开发工作，但当前对话缺少所需设计信息。"
        "通常不需要调用的情形：独立的编程知识问答、普通语法解释、"
        "不依赖项目历史的通用问题、当前对话已提供足够信息。"
        "category 可选 fact / decision；limit 为返回条数上限（默认 5）。"
    )

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    def __init__(self, memory: LongTermMemory):
        super().__init__()
        self.memory = memory

    def _run(
        self,
        query: str,
        category: str | None = None,
        limit: int = 5,
        memory_key: str | None = None,
        include_history: bool = False,
    ) -> str:
        try:
            limit = max(1, min(int(limit), 20))
            # 指向明确记忆键时优先精确读取
            if memory_key:
                category = category or MemoryCategory.FACT.value
                record = self.memory.get(MemoryScope.PROJECT.value, category, memory_key)
                return _format_records(
                    [record] if record else [], f"项目记忆（精确键 {memory_key}）"
                )

            if category is not None and category not in (
                MemoryCategory.FACT.value,
                MemoryCategory.DECISION.value,
            ):
                return "参数错误：项目记忆的 category 只能是 fact 或 decision。"

            records = self.memory.search(
                scope=MemoryScope.PROJECT.value,
                categories=[category] if category else None,
                query=query or None,
                limit=limit,
                only_active=not include_history,
            )
        except MemoryStoreError as exc:
            return f"检索失败：{exc}（没有可用的项目记忆，请勿编造历史结论）"

        return _format_records(records, "项目记忆检索结果")


class SearchUserMemoryTool(BaseTool):
    """检索当前用户自己的背景/偏好。"""

    name: str = "search_user_memory"
    description: str = (
        "检索【当前用户自己】的背景（profile）或偏好（preference）。"
        "仅在问题确实依赖用户的技术背景、当前项目进度或长期偏好时调用；"
        "例如用户要求继续之前的开发工作。"
        "普通的语法/知识问答通常不需要调用。只返回当前用户自己的记忆。"
    )

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    def __init__(self, memory: LongTermMemory):
        super().__init__()
        self.memory = memory

    def _run(self, query: str, category: str | None = None, limit: int = 5) -> str:
        try:
            limit = max(1, min(int(limit), 20))
            if category is not None and category not in (
                MemoryCategory.PROFILE.value,
                MemoryCategory.PREFERENCE.value,
            ):
                return "参数错误：用户记忆的 category 只能是 profile 或 preference。"

            records = self.memory.search(
                scope=MemoryScope.USER.value,
                categories=[category] if category else None,
                query=query or None,
                limit=limit,
                only_active=True,
            )
        except MemoryStoreError as exc:
            return f"检索失败：{exc}（没有可用的用户记忆，请勿编造）"

        return _format_records(records, "用户记忆检索结果")
