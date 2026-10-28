"""CodeInsight Agent 集中配置管理。

所有运行时配置从环境变量读取，本文件只做类型化封装和默认值处理。
严禁在源码中硬编码密钥或路径。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


# --------------------------------------------------------------------------- LLM / Embedding


@dataclass
class LLMConfig:
    """DashScope 模型配置。"""

    api_key: str = ""
    base_url: str = ""
    main_model: str = ""
    summarization_model: str = "qwen-flash"
    embedding_model: str = "text-embedding-v3"
    embedding_dimensions: int = 1024

    @classmethod
    def from_env(cls) -> "LLMConfig":
        return cls(
            api_key=os.getenv("DASHSCOPE_API_KEY", ""),
            base_url=os.getenv("DASHSCOPE_BASE_URL", ""),
            main_model=os.getenv("DASHSCOPE_MODEL", ""),
            summarization_model=os.getenv("SUMMARIZATION_MODEL", "qwen-flash"),
            embedding_model=os.getenv("EMBEDDING_MODEL", "text-embedding-v3"),
            embedding_dimensions=int(os.getenv("EMBEDDING_DIMENSIONS", "1024")),
        )


# --------------------------------------------------------------------------- Database


@dataclass
class DatabaseConfig:
    """PostgreSQL + pgvector 配置。"""

    url: str = ""

    @classmethod
    def from_env(cls) -> "DatabaseConfig":
        url = os.getenv("DATABASE_URL", "")
        # 统一改写为 psycopg3 驱动协议（硬约束）
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        elif url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg://", 1)
        return cls(url=url)


# --------------------------------------------------------------------------- Repository


@dataclass
class RepositoryConfig:
    """仓库访问配置。"""

    # 默认本地仓库路径
    default_local_path: str = ""
    # 默认仓库 ID（用于 RAG 索引隔离）
    default_repository_id: str = "CodeInsight-Agent"

    @classmethod
    def from_env(cls) -> "RepositoryConfig":
        return cls(
            default_local_path=os.getenv("DEFAULT_REPO_PATH", ""),
            default_repository_id=os.getenv("DEFAULT_REPOSITORY_ID", "CodeInsight-Agent"),
        )


# --------------------------------------------------------------------------- GitHub / MCP


@dataclass
class GitHubMCPConfig:
    """GitHub MCP Server 配置。"""

    # GitHub 个人令牌（可选；不填只能读公开仓库）
    token: str = ""
    # MCP Server 启动方式：stdio（子进程）或 sse
    transport: str = "stdio"
    # MCP Server 可执行文件路径（stdio 模式）；未指定则用 npx @modelcontextprotocol/server-github
    server_command: str = ""
    # MCP Server 参数
    server_args: list[str] = field(default_factory=list)
    # 连接超时秒数
    connect_timeout: float = 30.0
    # 工具调用超时秒数
    tool_timeout: float = 60.0
    # 启用的只读工具白名单（空列表 = 使用默认集合）
    allowed_tools: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "GitHubMCPConfig":
        token = os.getenv("GITHUB_TOKEN", "") or os.getenv("GH_TOKEN", "")
        cmd = os.getenv("GITHUB_MCP_COMMAND", "").strip()
        args_str = os.getenv("GITHUB_MCP_ARGS", "").strip()
        args = [a for a in args_str.split() if a] if args_str else []
        timeout = float(os.getenv("GITHUB_MCP_TIMEOUT", "30"))
        tool_timeout = float(os.getenv("GITHUB_MCP_TOOL_TIMEOUT", "60"))
        allowed = os.getenv("GITHUB_MCP_ALLOWED_TOOLS", "").strip()
        allowed_tools = [t.strip() for t in allowed.split(",") if t.strip()] if allowed else []
        return cls(
            token=token,
            transport=os.getenv("GITHUB_MCP_TRANSPORT", "stdio"),
            server_command=cmd,
            server_args=args,
            connect_timeout=timeout,
            tool_timeout=tool_timeout,
            allowed_tools=allowed_tools,
        )


# --------------------------------------------------------------------------- FastAPI / Web


@dataclass
class WebConfig:
    """FastAPI 服务配置。"""

    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: list[str] = field(default_factory=lambda: ["*"])
    # 任务执行超时秒数（包括 Agent 完整流程）
    task_timeout: float = 300.0
    # 同时运行的任务最大数量
    max_concurrent_tasks: int = 5
    # 临时克隆仓库的工作区根目录
    clone_workspace: str = ""
    # 临时工作区清理阈值（MB）
    clone_cleanup_threshold_mb: int = 500

    @classmethod
    def from_env(cls) -> "WebConfig":
        #优先读环境变量 CODEINSIGHT_CLONE_WORKSPACE；没有配置就回落至用户家目录下的 .codeinsight/workspace
        workspace = os.getenv("CODEINSIGHT_CLONE_WORKSPACE", "")
        if not workspace:
            workspace = str(Path.home() / ".codeinsight" / "workspace")
        #CORS 跨域源解析
        cors = os.getenv("CORS_ORIGINS", "*").strip()
        origins = [o.strip() for o in cors.split(",") if o.strip()] if cors else ["*"]
        return cls(
            host=os.getenv("WEB_HOST", "0.0.0.0"),
            port=int(os.getenv("WEB_PORT", "8000")),
            cors_origins=origins,
            task_timeout=float(os.getenv("TASK_TIMEOUT", "300")),
            max_concurrent_tasks=int(os.getenv("MAX_CONCURRENT_TASKS", "5")),
            clone_workspace=workspace,
            clone_cleanup_threshold_mb=int(
                os.getenv("CLONE_CLEANUP_THRESHOLD_MB", "500")
            ),
        )


# --------------------------------------------------------------------------- 全局


@dataclass
class Settings:
    """应用全局配置容器。"""

    llm: LLMConfig = field(default_factory=LLMConfig.from_env)
    db: DatabaseConfig = field(default_factory=DatabaseConfig.from_env)
    repo: RepositoryConfig = field(default_factory=RepositoryConfig.from_env)
    github: GitHubMCPConfig = field(default_factory=GitHubMCPConfig.from_env)
    web: WebConfig = field(default_factory=WebConfig.from_env)

    def reload(self) -> "Settings":
        """重新从环境变量加载（主要用于测试）。"""
        self.llm = LLMConfig.from_env()
        self.db = DatabaseConfig.from_env()
        self.repo = RepositoryConfig.from_env()
        self.github = GitHubMCPConfig.from_env()
        self.web = WebConfig.from_env()
        return self


# 单例：全应用共享
settings = Settings()
