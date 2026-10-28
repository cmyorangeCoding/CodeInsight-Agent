"""
长期记忆存取层：PostgresStore 访问、精确读取、语义检索、保存/更新/状态管理。

设计约定：
- 命名空间：
    ("users",   user_id,    "preferences")
    ("users",   user_id,    "profile")
    ("projects", project_id, "facts")
    ("projects", project_id, "decisions")
- 已知键的精确读取使用 ``store.get``（不依赖向量检索）。
- 只有开放式历史检索才使用 ``store.search(..., query=...)`` 的语义检索能力。
- memory_key 直接作为 Store 的 key，天然去重；同一 key 只会有一条记录。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from app.memory import memory_policy
from app.memory.models import (
    MemoryCategory,
    MemoryRecord,
    MemoryScope,
    MemorySource,
    MemoryStatus,
    utcnow_iso,
)


class MemoryStoreError(RuntimeError):
    """长期记忆存储不可用（数据库连接失败等）。"""


@dataclass
class MemorySaveResult:
    """保存操作结果。"""

    action: str          # created / updated / duplicate / rejected
    scope: str
    category: str
    memory_key: str
    message: str
    memory_id: str | None = None



""" namespace的组装"""
def user_preferences_namespace(user_id: str) -> tuple[str, ...]:
    return ("users", user_id, "preferences")


def user_profile_namespace(user_id: str) -> tuple[str, ...]:
    return ("users", user_id, "profile")


def project_facts_namespace(project_id: str) -> tuple[str, ...]:
    return ("projects", project_id, "facts")


def project_decisions_namespace(project_id: str) -> tuple[str, ...]:
    return ("projects", project_id, "decisions")




class LongTermMemory:
    """长期记忆的统一存取入口。

    用户/项目标识只从可信运行上下文（构造参数）取得，工具层不接受模型自由填写。
    """

    def __init__(self, store, user_id: str, project_id: str, tracer=None):
        if store is None:
            raise MemoryStoreError("LongTermMemory 需要一个可用的 Store 实例。")
        self.store = store
        self.user_id = user_id
        self.project_id = project_id
        self.tracer = tracer

    # ------------------------------------------------------------------ 工具
    def _log(self, event_type: str, **data) -> None:
        """可观测性打点：只记录结构化元数据，不记录完整记忆内容。"""
        if self.tracer is not None:
            try:
                self.tracer.add_event(event_type, **data)
            except Exception:
                pass

    def namespace_for(self, scope: str, category: str) -> tuple[str, ...]:
        """scope + category → Store 命名空间。"""
        if scope == MemoryScope.USER.value:
            if category == MemoryCategory.PREFERENCE.value:
                return user_preferences_namespace(self.user_id)
            if category == MemoryCategory.PROFILE.value:
                return user_profile_namespace(self.user_id)
        elif scope == MemoryScope.PROJECT.value:
            if category == MemoryCategory.FACT.value:
                return project_facts_namespace(self.project_id)
            if category == MemoryCategory.DECISION.value:
                return project_decisions_namespace(self.project_id)
        raise memory_policy.MemoryPolicyError(
            f"无法为 scope={scope}, category={category} 解析命名空间。"
        )

    # -------------------------------------------------------------- 读取
    def get(self, scope: str, category: str, memory_key: str) -> MemoryRecord | None:
        """按稳定键精确读取单条记忆（不走向量检索）。"""
        namespace = self.namespace_for(scope, category)
        try:
            #去指定命名空间下，查询 key=memory_key 的那条数据
            item = self.store.get(namespace, memory_key)
        except Exception as exc:  # noqa: BLE001 - 统一转为可处理错误
            raise MemoryStoreError(f"读取记忆失败：{type(exc).__name__}: {exc}") from exc
        if item is None:
            return None
        return MemoryRecord.from_value(item.key, item.value)

    def _namespace_items(
        self,
        namespace: tuple[str, ...],
        *,
        query: str | None,
        limit: int,
        only_active: bool,
    ) -> list[MemoryRecord]:
        """在单个命名空间内检索记忆。

        query 为空 → 使用 namespace 扫描（精确读取，不触发向量检索）。
        query 非空 → 使用 Store 的语义检索。
        """
        started = time.perf_counter()
        try:
            if query:
                filter_dict = {"status": MemoryStatus.ACTIVE.value} if only_active else None
                items = self.store.search(
                    namespace, query=query, filter=filter_dict, limit=limit
                )
            else:
                items = self.store.search(namespace, limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise MemoryStoreError(f"检索记忆失败：{type(exc).__name__}: {exc}") from exc

        records: list[MemoryRecord] = []
        for item in items:
            record = MemoryRecord.from_value(item.key, item.value, score=getattr(item, "score", None))
            if only_active and record.status != MemoryStatus.ACTIVE.value:
                continue
            records.append(record)

        self._log(
            "memory_namespace_search",
            namespace="/".join(namespace),
            has_query=bool(query),
            result_count=len(records),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return records

    def search(
        self,
        *,
        scope: str,
        categories: list[str] | None = None,
        query: str | None = None,
        limit: int = 5,
        only_active: bool = True,
    ) -> list[MemoryRecord]:
        """跨类别检索记忆，返回去重、排序后的结果。"""
        if categories is None:
            categories = sorted(memory_policy.ALLOWED_CATEGORIES[scope])

        collected: list[MemoryRecord] = []
        seen: set[tuple[str, str]] = set()

        for category in categories:
            namespace = self.namespace_for(scope, category)
            for record in self._namespace_items(
                namespace,
                query=query,
                limit=max(limit, 1) * 2,
                only_active=only_active,
            ):
                dedupe_key = ("/".join(namespace), record.memory_key)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                collected.append(record)

        # 排序：有语义得分按得分降序，否则按更新时间降序
        def sort_key(record: MemoryRecord):
            score = record.score if record.score is not None else -1.0
            return (score, record.updated_at)

        collected.sort(key=sort_key, reverse=True)
        return collected[: max(limit, 1)]


    def load_user_preferences(self) -> list[MemoryRecord]:
        """按固定命名空间读取当前用户偏好（精确读取，不走向量检索）。"""
        started = time.perf_counter()
        records = self._namespace_items(
            user_preferences_namespace(self.user_id),
            query=None,
            limit=50,
            only_active=True,
        )
        # 偏好通常很少，按更新时间正序输出更稳定
        records.sort(key=lambda r: r.updated_at)
        self._log(
            "memory_preferences_loaded",
            user_id=self.user_id,
            preference_count=len(records),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return records




    # -------------------------------------------------------------- 写入
    def save(
        self,
        *,
        scope: str,
        category: str,
        memory_key: str,
        content: str,
        source: str,
        confidence: float | None = None,
        source_reference: str | None = None,
    ) -> MemorySaveResult:
        """保存或更新一条长期记忆。

        流程：校验 → 精确读取同键记录 → 冲突判定 → 受控写入。
        """
        # 1. 参数与类型校验
        memory_policy.validate_scope_category(scope, category)
        memory_policy.validate_source(category, source)
        memory_key = memory_policy.validate_key(memory_key)
        content = memory_policy.validate_content(content)
        confidence = memory_policy.normalize_confidence(confidence)

        namespace = self.namespace_for(scope, category)

        # 2. 精确读取是否已有同键记忆
        existing = self.get(scope, category, memory_key)

        # 3. 冲突判定
        if existing is not None:
            decision = memory_policy.decide_update(
                existing_content=existing.content,
                existing_source=existing.source,
                existing_status=existing.status,
                incoming_content=content,
                incoming_source=source,
            )
            if decision == memory_policy.UpdateDecision.DUPLICATE:
                self._log("memory_save", action="duplicate", scope=scope, category=category, key=memory_key)
                return MemorySaveResult(
                    action="duplicate", scope=scope, category=category,
                    memory_key=memory_key, memory_id=existing.memory_id,
                    message="内容与来源与已有记忆一致，跳过重复保存。",
                )
            if decision == memory_policy.UpdateDecision.REJECTED:
                self._log(
                    "memory_save", action="rejected", scope=scope, category=category,
                    key=memory_key, reason="weaker_source",
                )
                return MemorySaveResult(
                    action="rejected", scope=scope, category=category,
                    memory_key=memory_key, memory_id=existing.memory_id,
                    message=(
                        "新记忆来源可信度低于现有已确认记忆，已保留现有结论。"
                        "如确需替换，请先由用户明确确认。"
                    ),
                )

        # 4. 构造写入载荷（受控更新：旧版本写入 history 并标记 superseded）
        now = utcnow_iso()
        history: list[dict] = []
        created_at = now
        if existing is not None:
            created_at = existing.created_at
            history = list(existing.history)
            old_snapshot = existing.snapshot()
            old_snapshot["status"] = MemoryStatus.SUPERSEDED.value
            history.append(old_snapshot)

        record = MemoryRecord(
            memory_id=memory_key,
            scope=scope,
            category=category,
            memory_key=memory_key,
            content=content,
            source=source,
            status=MemoryStatus.ACTIVE.value,
            confidence=confidence,
            source_reference=source_reference,
            created_at=created_at,
            updated_at=now,
            history=history,
        )

        # 5. 写入
        try:
            self.store.put(namespace, memory_key, record.to_value())
        except Exception as exc:  # noqa: BLE001
            raise MemoryStoreError(f"写入记忆失败：{type(exc).__name__}: {exc}") from exc

        action = "created" if existing is None else "updated"
        self._log(
            "memory_save", action=action, scope=scope, category=category,
            key=memory_key, source=source,
            superseded_count=len(history),
        )
        return MemorySaveResult(
            action=action, scope=scope, category=category,
            memory_key=memory_key, memory_id=memory_key,
            message="已新增记忆。" if action == "created" else "已更新记忆，旧版本标记为 superseded。",
        )
