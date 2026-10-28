"""仓库访问统一抽象层。


核心设计：
- ``RepositoryProvider``：协议接口，本地 / GitHub 两种实现共享同一套方法。
- ``LocalRepositoryProvider``：包装现有 ``RepositoryAnalyzer``，零行为变更。
- ``GitHubRepositoryProvider``：通过 GitHub MCP 读取远程仓库，按需下载或按需访问。

不负责 RAG 索引、不负责 Agent 编排，只提供文件级访问。
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class RepositoryProvider(Protocol):
    """仓库访问抽象接口。

    所有工具函数和索引构建都通过该协议访问仓库文件，
    不再直接绑定 ``REPO_PATH`` 或全局 ``RepositoryAnalyzer`` 单例。
    """

    # --- 身份信息 ---

    @property
    def repository_id(self) -> str:
        """仓库唯一标识（用于 RAG 索引隔离）。"""
        ...

    @property
    def source(self) -> str:
        """仓库来源：local / github / ..."""
        ...

    @property
    def repo_info(self) -> dict:
        """仓库基本信息（名称、版本/提交 SHA、大小等）。"""
        ...

    @property
    def has_local_index(self) -> bool:
        """是否有本地文件可用于 RAG 向量化索引。

        - local 仓库：**永远 True**（直接读本地文件）
        - GitHub 仓库 clone 成功：True
        - GitHub 按需模式（没 clone）：**False**（没有本地文件，RAG 索引无从谈起）

        Agent 工厂会根据这个决定是否把 ``search_code_rag`` 工具放进工具集。
        """
        ...

    # --- 目录 / 文件 ---

    def get_repo_tree(self, max_depth: int = 3) -> str:
        """返回带格式的目录树文本。"""
        ...

    def index_files(self) -> list[dict]:
        """返回可参与索引的文件列表（path / extension / size）。"""
        ...

    def read_file(self, relative_path: str, max_chars: int = 30000) -> str:
        """安全读取指定文件内容。

        必须处理：文件不存在、路径越界、非文本/二进制、忽略目录。
        """
        ...

    def search_code(
        self,
        keyword: str,
        extensions: set[str] | None = None,
        max_results: int = 20,
        context_lines: int = 3,
    ) -> list[dict]:
        """按精确关键词做文本匹配搜索（类似 grep）。"""
        ...


class LocalRepositoryProvider:
    """本地仓库访问实现 —— 包装现有 ``RepositoryAnalyzer``，零行为变更。"""

    source: str = "local"

    def __init__(
        self,
        repo_path: str | Path,
        repository_id: str | None = None,
        analyzer: object | None = None,
    ):
        # 延迟 import：避免循环依赖
        from app.tools.repository import RepositoryAnalyzer

        self.repo_path = Path(repo_path).resolve()
        if analyzer is not None:
            self._analyzer = analyzer
        else:
            self._analyzer = RepositoryAnalyzer(str(self.repo_path))

        self._repository_id = repository_id or self.repo_path.name

    @property
    def repository_id(self) -> str:
        return self._repository_id

    @property
    def repo_info(self) -> dict:
        return {
            "name": self.repo_path.name,
            "path": str(self.repo_path),
            "source": self.source,
            "commit": None,  # 本地仓库不追踪 git commit
            "indexed": False,  # 由外部（RAG 索引模块）设置
        }

    @property
    def has_local_index(self) -> bool:
        """本地仓库永远可以做 RAG 索引（直接读本地文件）。"""
        return True

    # --- 以下全部委托给 analyzer ---

    def get_repo_tree(self, max_depth: int = 3) -> str:
        return self._analyzer.get_repo_tree(max_depth)

    def index_files(self) -> list[dict]:
        return self._analyzer.index_files()

    def read_file(self, relative_path: str, max_chars: int = 30000) -> str:
        return self._analyzer.read_file(relative_path, max_chars=max_chars)

    def search_code(
        self,
        keyword: str,
        extensions: set[str] | None = None,
        max_results: int = 20,
        context_lines: int = 3,
    ) -> list[dict]:
        return self._analyzer.search_code(
            keyword=keyword,
            extensions=extensions,
            max_results=max_results,
            context_lines=context_lines,
        )


# 延迟导出 GitHubProvider：单独文件，减少循环依赖
def __getattr__(name: str):
    if name == "GitHubRepositoryProvider":
        from app.repository.github_provider import GitHubRepositoryProvider
        return GitHubRepositoryProvider
    raise AttributeError(name)

