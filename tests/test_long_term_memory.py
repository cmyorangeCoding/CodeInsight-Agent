"""
长期记忆核心功能单元测试（不依赖数据库与外部 Embedding 服务）。

覆盖任务书 A（基础功能）与 E（冲突和来源）：
- 保存/读取用户偏好
- 保存项目事实 / 决策
- 精确键读取与更新
- 重复内容跳过
- 无匹配返回明确结果
- 非法类别 / 范围 / 来源被拒绝
- 未确认建议不能保存为已确认决策
- 新决策取代旧决策并保留历史
- 代码事实关联可验证来源
- 敏感信息拒绝写入

运行方式：python tests/test_long_term_memory.py
"""
from langchain_core.embeddings import Embeddings
from langgraph.store.memory import InMemoryStore

from app.memory.long_term_memory import (
    LongTermMemory,
    MemoryStoreError,
    user_preferences_namespace,
    project_facts_namespace,
)
from app.memory.models import MemoryCategory, MemoryScope, MemorySource


class FakeEmbeddings(Embeddings):
    """确定性词袋向量（共享词表，避免哈希冲突），用于在无外部 API 时验证语义检索通路。"""

    DIM = 64

    def __init__(self):
        self._vocab: dict[str, int] = {}

    @staticmethod
    def _tokens(text: str) -> list[str]:
        for sep in ("，", "。", ",", ".", "（", "）", "(", ")"):
            text = text.replace(sep, " ")
        return text.lower().split()

    def _vec(self, text: str) -> list[float]:
        vec = [0.0] * self.DIM
        for token in self._tokens(text):
            if token not in self._vocab:
                self._vocab[token] = len(self._vocab) % self.DIM
            vec[self._vocab[token]] += 1.0
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


def make_memory(user_id="u-1", project_id="proj-1", with_index=True):
    if with_index:
        store = InMemoryStore(
            index={"dims": FakeEmbeddings.DIM, "embed": FakeEmbeddings(), "fields": ["text"]}
        )
    else:
        store = InMemoryStore()
    return store, LongTermMemory(store=store, user_id=user_id, project_id=project_id)


def test_save_and_load_user_preference():
    store, memory = make_memory()

    result = memory.save(
        scope=MemoryScope.USER.value,
        category=MemoryCategory.PREFERENCE.value,
        memory_key="answer_style",
        content="回答风格：清晰、具体、结构化。",
        source=MemorySource.USER_STATED.value,
    )
    assert result.action == "created", result

    prefs = memory.load_user_preferences()
    assert len(prefs) == 1
    assert prefs[0].content == "回答风格：清晰、具体、结构化。"
    assert prefs[0].category == "preference"

    # 存进了固定命名空间
    item = store.get(user_preferences_namespace("u-1"), "answer_style")
    assert item is not None
    assert item.value["text"] == "回答风格：清晰、具体、结构化。"


def test_save_project_fact_and_decision():
    _, memory = make_memory()

    fact = memory.save(
        scope=MemoryScope.PROJECT.value,
        category=MemoryCategory.FACT.value,
        memory_key="db_stack",
        content="项目使用 PostgreSQL + pgvector 作为持久化与向量存储。",
        source=MemorySource.CODE_VERIFIED.value,
        source_reference="app/memory/postgres_resources.py",
    )
    assert fact.action == "created"

    decision = memory.save(
        scope=MemoryScope.PROJECT.value,
        category=MemoryCategory.DECISION.value,
        memory_key="prefs_auto_inject",
        content="用户偏好自动注入，项目记忆设计为按需检索工具。",
        source=MemorySource.USER_CONFIRMED.value,
    )
    assert decision.action == "created"

    facts = memory.search(scope=MemoryScope.PROJECT.value, categories=[MemoryCategory.FACT.value])
    assert [r.memory_key for r in facts] == ["db_stack"]
    assert facts[0].source_reference == "app/memory/postgres_resources.py"

    all_project = memory.search(scope=MemoryScope.PROJECT.value)
    keys = {r.memory_key for r in all_project}
    assert keys == {"db_stack", "prefs_auto_inject"}


def test_exact_key_read_via_get():
    _, memory = make_memory()
    memory.save(
        scope=MemoryScope.PROJECT.value,
        category=MemoryCategory.DECISION.value,
        memory_key="why_pgvector",
        content="采用 PostgreSQL + pgvector。",
        source=MemorySource.USER_CONFIRMED.value,
    )
    record = memory.get(MemoryScope.PROJECT.value, MemoryCategory.DECISION.value, "why_pgvector")
    assert record is not None and record.content == "采用 PostgreSQL + pgvector。"
    assert memory.get(MemoryScope.PROJECT.value, MemoryCategory.DECISION.value, "missing") is None


def test_update_same_key_and_duplicate_skip():
    _, memory = make_memory()

    first = memory.save(
        scope=MemoryScope.USER.value,
        category=MemoryCategory.PREFERENCE.value,
        memory_key="answer_style",
        content="回答要简洁。",
        source=MemorySource.USER_STATED.value,
    )
    assert first.action == "created"

    # 相同内容 + 相同来源 → 跳过，不产生新记录
    dup = memory.save(
        scope=MemoryScope.USER.value,
        category=MemoryCategory.PREFERENCE.value,
        memory_key="answer_style",
        content="回答要简洁。",
        source=MemorySource.USER_STATED.value,
    )
    assert dup.action == "duplicate"

    # 内容变化 → 受控更新，仍只有一条记录
    upd = memory.save(
        scope=MemoryScope.USER.value,
        category=MemoryCategory.PREFERENCE.value,
        memory_key="answer_style",
        content="回答要简洁，并给出完整步骤。",
        source=MemorySource.USER_STATED.value,
    )
    assert upd.action == "updated"

    prefs = memory.load_user_preferences()
    assert len(prefs) == 1
    assert prefs[0].content == "回答要简洁，并给出完整步骤。"


def test_no_match_returns_empty():
    _, memory = make_memory()
    records = memory.search(scope=MemoryScope.PROJECT.value, query="数据库")
    assert records == []


def test_invalid_scope_category_and_source_rejected():
    _, memory = make_memory()

    # 非法 scope
    try:
        memory.save(scope="team", category="fact", memory_key="k", content="c",
                    source=MemorySource.USER_STATED.value)
    except Exception as exc:
        assert "scope" in str(exc).lower() or "非法" in str(exc)
    else:
        raise AssertionError("非法 scope 未被拒绝")

    # 非法 category（user scope 不允许 fact）
    try:
        memory.save(scope=MemoryScope.USER.value, category=MemoryCategory.FACT.value,
                    memory_key="k", content="c", source=MemorySource.CODE_VERIFIED.value)
    except Exception:
        pass
    else:
        raise AssertionError("非法 category 未被拒绝")

    # 未确认建议不能保存为已确认决策
    try:
        memory.save(scope=MemoryScope.PROJECT.value, category=MemoryCategory.DECISION.value,
                    memory_key="k", content="建议改用 Redis。",
                    source=MemorySource.MODEL_PROPOSED.value)
    except Exception:
        pass
    else:
        raise AssertionError("model_proposed 决策未被拒绝")

    # project scope 不允许 preference
    try:
        memory.save(scope=MemoryScope.PROJECT.value, category=MemoryCategory.PREFERENCE.value,
                    memory_key="k", content="c", source=MemorySource.USER_STATED.value)
    except Exception:
        pass
    else:
        raise AssertionError("project+preference 未被拒绝")


def test_secret_content_rejected():
    _, memory = make_memory()
    try:
        memory.save(scope=MemoryScope.PROJECT.value, category=MemoryCategory.FACT.value,
                    memory_key="leak", content="api_key=sk-ws-abcdef1234567890",
                    source=MemorySource.CODE_VERIFIED.value)
    except Exception as exc:
        assert "敏感" in str(exc)
    else:
        raise AssertionError("敏感信息未被拒绝")


def test_confirmed_decision_supersedes_old_and_keeps_history():
    _, memory = make_memory()

    memory.save(
        scope=MemoryScope.PROJECT.value, category=MemoryCategory.DECISION.value,
        memory_key="vector_db", content="旧方案：使用 Faiss 作为向量库。",
        source=MemorySource.USER_CONFIRMED.value,
    )
    result = memory.save(
        scope=MemoryScope.PROJECT.value, category=MemoryCategory.DECISION.value,
        memory_key="vector_db", content="新方案：使用 PostgreSQL + pgvector。",
        source=MemorySource.USER_CONFIRMED.value,
    )
    assert result.action == "updated"

    record = memory.get(MemoryScope.PROJECT.value, MemoryCategory.DECISION.value, "vector_db")
    assert record.content == "新方案：使用 PostgreSQL + pgvector。"
    assert record.status == "active"
    # 旧结论保留在 history 并明确标记 superseded
    assert len(record.history) == 1
    assert record.history[0]["content"] == "旧方案：使用 Faiss 作为向量库。"
    assert record.history[0]["status"] == "superseded"


def test_weaker_source_cannot_override_confirmed():
    _, memory = make_memory()
    memory.save(
        scope=MemoryScope.PROJECT.value, category=MemoryCategory.DECISION.value,
        memory_key="d1", content="已确认：使用 SQLAlchemy。",
        source=MemorySource.USER_CONFIRMED.value,
    )
    result = memory.save(
        scope=MemoryScope.PROJECT.value, category=MemoryCategory.DECISION.value,
        memory_key="d1", content="模型建议：直接手写 SQL 更快。",
        source=MemorySource.USER_STATED.value,
    )
    assert result.action == "rejected"
    record = memory.get(MemoryScope.PROJECT.value, MemoryCategory.DECISION.value, "d1")
    assert record.content == "已确认：使用 SQLAlchemy。"


def test_semantic_search_ranks_relevant_fact():
    _, memory = make_memory()
    memory.save(
        scope=MemoryScope.PROJECT.value, category=MemoryCategory.FACT.value,
        memory_key="db_stack",
        content="持久化数据库使用 PostgreSQL 与 pgvector 向量检索。",
        source=MemorySource.CODE_VERIFIED.value,
    )
    memory.save(
        scope=MemoryScope.PROJECT.value, category=MemoryCategory.FACT.value,
        memory_key="chunking",
        content="代码切片由 chunker 按函数与类边界切分。",
        source=MemorySource.CODE_VERIFIED.value,
    )
    records = memory.search(scope=MemoryScope.PROJECT.value, query="数据库 PostgreSQL", limit=2)
    assert records, "语义检索应返回结果"
    assert records[0].memory_key == "db_stack"


def test_store_unavailable_raises_clear_error():
    class BrokenStore:
        def get(self, *a, **k):
            raise RuntimeError("connection refused")

        def search(self, *a, **k):
            raise RuntimeError("connection refused")

        def put(self, *a, **k):
            raise RuntimeError("connection refused")

    memory = LongTermMemory(store=BrokenStore(), user_id="u", project_id="p")

    try:
        memory.load_user_preferences()
    except MemoryStoreError as exc:
        assert "失败" in str(exc)
    else:
        raise AssertionError("存储不可用时应抛出 MemoryStoreError")

    try:
        memory.save(scope=MemoryScope.USER.value, category=MemoryCategory.PREFERENCE.value,
                    memory_key="k", content="c", source=MemorySource.USER_STATED.value)
    except MemoryStoreError:
        pass
    else:
        raise AssertionError("存储不可用时保存应抛出 MemoryStoreError")


def main():
    tests = [
        test_save_and_load_user_preference,
        test_save_project_fact_and_decision,
        test_exact_key_read_via_get,
        test_update_same_key_and_duplicate_skip,
        test_no_match_returns_empty,
        test_invalid_scope_category_and_source_rejected,
        test_secret_content_rejected,
        test_confirmed_decision_supersedes_old_and_keeps_history,
        test_weaker_source_cannot_override_confirmed,
        test_semantic_search_ranks_relevant_fact,
        test_store_unavailable_raises_clear_error,
    ]
    for test in tests:
        test()
        print(f"✓ {test.__name__}")
    print(f"\n全部通过：{len(tests)} 个用例")


if __name__ == "__main__":
    main()
