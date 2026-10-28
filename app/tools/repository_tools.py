"""代码仓库分析工具集（工厂模式）。

核心变更：不再持有模块级全局 ``repository`` 单例，
所有工具通过 ``build_repository_tools(provider=...)`` 接收仓库访问提供者，
支持每次任务绑定不同仓库来源（本地 / GitHub）。
"""
from __future__ import annotations

from langchain_core.tools import BaseTool
from pydantic import ConfigDict

from app.context.builder import ContextBuilder
from app.observability.token_budget import TokenBudget
from app.rag.retriever import CodeRetriever
from app.repository import RepositoryProvider


class _GetRepoTreeTool(BaseTool):
    name: str = "get_repo_tree"
    description: str = (
        "获取当前代码仓库的目录结构。"
        "当需要了解项目有哪些目录、文件，"
        "或者需要先了解项目整体结构时使用。"
    )
    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    def __init__(self, provider: RepositoryProvider):
        super().__init__()
        self._provider = provider

    def _run(self, max_depth: int = 3) -> str:
        return self._provider.get_repo_tree(max_depth=max_depth)


class _IndexRepositoryFilesTool(BaseTool):
    name: str = "index_repository_files"
    description: str = (
        "建立代码仓库文件索引。"
        "用于了解仓库中有哪些代码文件以及文件类型。"
    )
    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    def __init__(self, provider: RepositoryProvider):
        super().__init__()
        self._provider = provider

    def _run(self) -> str:
        files = self._provider.index_files()
        if not files:
            return "没有找到可分析的代码文件。"
        lines = []
        for file in files:
            lines.append(
                f"{file['path']} [{file['extension']}] {file['size']} bytes"
            )
        return "\n".join(lines)


class _ReadFileTool(BaseTool):
    name: str = "read_file"
    description: str = (
        "读取代码仓库中的指定文件（精读）。\n"
        "relative_path 必须相对于【仓库根目录】，且不要带仓库文件夹名前缀：\n"
        "  正确：app/models/llm.py\n"
        "  错误：CodeInsight-Agent/app/models/llm.py\n"
        "仅在你已经明确知道文件路径时使用；如果还不确定相关代码在哪个文件，\n"
        "应先用 search_code_rag 定位，再回来精读具体文件。"
    )
    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    def __init__(self, provider: RepositoryProvider):
        super().__init__()
        self._provider = provider

    def _run(self, relative_path: str) -> str:
        try:
            return self._provider.read_file(relative_path)
        except FileNotFoundError:
            return (
                f"文件不存在：{relative_path}。"
                "注意：路径必须相对于仓库根目录，不要带仓库文件夹名前缀。"
                "请改用 search_code_rag 定位相关代码所在的文件，"
                "或用 get_repo_tree 确认目录结构。"
            )
        except ValueError as e:
            return f"读取失败：{e}"
        except Exception as e:
            return f"读取文件时发生未知错误：{type(e).__name__}: {e}"


class SearchCodeTool(BaseTool):
    """按精确关键词/标识符做文本匹配搜索（类似 grep）。"""

    name: str = "search_code"
    description: str = (
        "按精确关键词/标识符做文本匹配搜索（类似 grep），返回匹配处的代码证据。"
        "这是【兜底工具】：只有在 search_code_rag 返回为空或证据不足时，"
        "才用它做精确文本补充。请勿作为首选。"
    )
    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    def __init__(
        self,
        provider: RepositoryProvider,
        token_budget: TokenBudget | None = None,
    ):
        super().__init__()
        self._provider = provider
        self.token_budget = token_budget
        self.builder = ContextBuilder(max_chars=12000, token_budget=token_budget)

    def _run(self, keyword: str, max_results: int = 20, context_lines: int = 3) -> str:
        results = self._provider.search_code(
            keyword=keyword,
            max_results=max_results,
            context_lines=context_lines,
        )
        if not results:
            return f"没有找到关键词：{keyword}"
        return self.builder.build(results)


class SearchCodeRAGTool(BaseTool):
    """基于向量 + 关键词混合检索（RAG）的代码搜索工具。

    依赖外部注入的 CodeRetriever，不在每次调用中创建数据库连接。
    """

    name: str = "search_code_rag"
    description: str = (
        "【首选检索工具 / 定位第一步】基于向量 + 关键词的混合检索（RAG），"
        "能理解自然语言语义，在已索引的代码中快速定位与问题最相关的代码片段。"
        "返回文件路径、起止行号、符号名和代码内容。"
        "凡涉及「本仓库某功能怎么实现 / 在哪里处理 / 某个类·函数·变量在哪定义或被使用」"
        "这类问题，第一步都先调用本工具定位，再按返回的路径用 read_file 精读，"
        "不要一上来就逐个文件通读。"
        "只有问题完全没有具体锚点（例如「这个项目整体是做什么的」）时，"
        "才先 get_repo_tree 看结构，之后仍需用本工具定位关键实现文件。"
    )
    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    def __init__(self, retriever: CodeRetriever):
        super().__init__()
        self.retriever = retriever

    def _run(self, query: str) -> str:
        results = self.retriever.hybrid_search(query)
        if not results:
            return (
                f"没有找到与「{query}」相关的代码片段。"
                "可能尚未建立索引，或问题与仓库内容无关。"
            )
        lines = [f"找到 {len(results)} 段相关代码："]
        for i, r in enumerate(results, 1):
            symbol = r.get("symbol_name") or ""
            lines.append(
                f"\n--- [{i}] {r['file_path']}"
                f"{':' + str(r['line_start']) if r.get('line_start') else ''}"
                f"{'-' + str(r['line_end']) if r.get('line_end') else ''}"
                f"{' 符号: ' + symbol if symbol else ''}"
                f" (rrf={r.get('rrf_score', 0):.4f}) ---"
            )
            content = r.get("content", "")
            if len(content) > 3000:
                content = content[:3000] + "\n...[truncated]"
            lines.append(content)
        return "\n".join(lines)
