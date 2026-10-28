"""GitHub MCP 客户端封装。

⚠️ 最重要的设计：后台线程事件循环（sync 桥接模式）
    问题：MCP SDK 全是 async 函数；如果每次同步调用都用 asyncio.run() 新建事件循环，
    MCP 的 stdio 连接会随循环销毁而断开，第二次调用必死。
    方案：connect_sync() 启动一个守护线程，在里面跑一个【永不退出的事件循环】；
    之后每个同步调用用 asyncio.run_coroutine_threadsafe() 把协程"投递"到那个
    后台0循环执行、阻塞等结果 —— 相当于把一个响应式客户端常驻在独立线程池里，
    业务线程往它提交 Callable 并同步 get()，连接因此可以跨调用复用。

职责：
- 通过 stdio 启动官方 GitHub MCP Server（npx @modelcontextprotocol/server-github）
- 建立异步会话、发现可用工具、过滤只读工具
- 封装工具调用：带超时、重试、错误分类
- 支持显式传入 GitHub Token 或通过环境变量读取
- 统一处理权限错误、仓库不存在、网络异常

设计原则：
- 一个实例 = 一个 MCP 连接；连接生命周期由外部 async context 管理
- 所有工具调用都是异步的（asyncio），上层如果需要同步可用 connect_sync 桥接
- 严禁缓存 GitHub Token 到日志或返回值中
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.config import GitHubMCPConfig, settings

logger = logging.getLogger(__name__)


# 默认允许的只读工具白名单；不在此列表的工具（写入/删除/PR）一律不暴露
_DEFAULT_READONLY_TOOLS: frozenset[str] = frozenset({
    "get_file_contents",
    "list_commits",
    "search_code",
    "search_repositories",
    "get_pull_request_files",
    "get_pull_request_status",
    "get_pull_request",
    "list_pull_requests",
    "get_issue",
    "list_issues",
})


class GitHubMCPError(Exception):
    """GitHub MCP 调用的统一异常基类。"""

    def __init__(self, message: str, *, tool: str | None = None,
                 error_type: str | None = None, raw: Any = None):
        super().__init__(message)
        self.tool = tool             # 触发异常的工具名
        self.error_type = error_type
        self.raw = raw               # 原始异常信息，用于调试


class GitHubMCP:
    """GitHub MCP 客户端管理器。

    使用方式有两种：

    1. 异步模式（FastAPI async endpoint）::

           async with GitHubMCP(config) as client:
               result = await client.call_tool("get_file_contents", {...})

    2. 同步模式（普通路由）—— 在后台线程里跑事件循环，
       不随每次调用重启 MCP 子进程::

           mcp = GitHubMCP()
           mcp.connect_sync()  # 启动后台线程，保持 MCP 连接
           result = mcp.call_tool_sync("get_file_contents", {...})
           mcp.close_sync()    # 停止线程，关闭连接
    """

    def __init__(self, config: GitHubMCPConfig | None = None):
        # 配置对象，优先传入，否则读取全局 settings.github
        self.config = config or settings.github

        # MCP 会话资源
        self._session: ClientSession | None = None
        self._read: Any = None
        self._write: Any = None
        self._stdio_ctx: Any = None

        # 当前会话可用工具列表（已经过滤白名单）
        self._available_tools: list[str] = []
        # 初始化标记：True = 连接就绪
        self._initialized = False

        # 同步桥接：后台事件循环 + 后台线程
        self._bg_loop: asyncio.AbstractEventLoop | None = None
        self._bg_thread: Any = None

    # ===================== 异步上下文管理器 =====================

    async def __aenter__(self) -> "GitHubMCP":
        """async with 进入时自动建立连接"""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """async with 退出时自动关闭连接"""
        await self.close()

    # ===================== 异步连接/关闭 =====================

    async def connect(self) -> None:
        """启动 MCP Server 子进程并建立会话（异步版本）。"""
        if self._initialized:
            return
        # 第一步：组装启动参数（命令、环境变量）
        self._do_connect()
        # 第二步：在当前async循环里拉起子进程、建立会话
        await self._do_async_connect()

    async def close(self) -> None:
        """关闭异步会话，释放stdio资源。
        注意：这个方法不会处理后台同步线程的loop，同步关闭走close_sync。
        """
        try:
            if self._session is not None:
                await self._session.__aexit__(None, None, None)
        except Exception:
            pass
        try:
            if self._stdio_ctx is not None:
                await self._stdio_ctx.__aexit__(None, None, None)
        except Exception:
            pass
        self._session = None
        self._stdio_ctx = None
        self._initialized = False

    def _do_connect(self) -> None:
        """组装MCP Server启动参数，同步方法，connect / 异步方法：connect_sync 共用。
           配置命令、参数、注入GITHUB_TOKEN到子进程环境。
        """
        if self.config.server_command:
            command = self.config.server_command
            args = list(self.config.server_args)
        else:
            # 默认：官方github mcp server
            command = "npx"
            args = ["-y", "@modelcontextprotocol/server-github"]

        # 复制环境变量，避免污染主进程os.environ
        env = os.environ.copy()
        if self.config.token:
            # 同时设两个变量：GITHUB_TOKEN（通用约定）+ GITHUB_PERSONAL_ACCESS_TOKEN
            # （废弃旧版 @modelcontextprotocol/server-github **只认后者**，不认 GITHUB_TOKEN！）
            env["GITHUB_TOKEN"] = self.config.token
            env["GITHUB_PERSONAL_ACCESS_TOKEN"] = self.config.token

        self._params = StdioServerParameters(command=command, args=args, env=env)

    async def _do_async_connect(self) -> None:
        """真正建立stdio管道 + ClientSession，初始化会话、拉取工具列表。"""
        if self._initialized:
            return
        if not hasattr(self, "_params"):
            self._do_connect()

        try:
            # 启动子进程，拿到读写流
            stdio_ctx = stdio_client(self._params)
            transports = await stdio_ctx.__aenter__()
            self._read, self._write = transports
            self._stdio_ctx = stdio_ctx

            # 创建会话并初始化握手
            self._session = ClientSession(self._read, self._write)
            await self._session.__aenter__()
            await self._session.initialize()
            self._initialized = True

            # 查询并过滤可用工具
            await self._discover_tools()
            logger.info("GitHub MCP connected, %d tools available", len(self._available_tools))
        except Exception as exc:
            self._session = None
            self._initialized = False
            raise GitHubMCPError(
                f"无法连接 GitHub MCP Server: {type(exc).__name__}: {exc}",
                error_type="connection_error",
            ) from exc

    async def _discover_tools(self) -> None:
        """列出并过滤只读工具。"""
        assert self._session is not None
        # await：等待session异步调用list_tools，从远端MCP服务拿到工具列表
        resp = await self._session.list_tools()
        all_names = {t.name for t in resp.tools}
        if self.config.allowed_tools:
            allowed = set(self.config.allowed_tools)
        else:
            allowed = _DEFAULT_READONLY_TOOLS
        self._available_tools = sorted(all_names & allowed)





    # ===================== 同步桥接：后台常驻事件循环 =====================

    def connect_sync(self) -> None:
        """同步连接：启动后台线程，在那个线程里跑事件循环。"""
        if self._initialized:
            return
        import threading

        #组装环境
        self._do_connect()

        def _run_loop():
            # 线程内部新建独立事件循环
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._bg_loop = loop
            try:
                # 先完成MCP初始化握手
                loop.run_until_complete(self._do_async_connect())
                # 循环常驻，等待投递进来的协程任务
                loop.run_forever()
            except Exception as exc:
                logger.error("MCP 后台循环异常退出: %s", exc)
            finally:
                try:
                    loop.close()
                except Exception:
                    pass

        self._bg_thread = threading.Thread(target=_run_loop, daemon=True)
        self._bg_thread.start()

        # 轮询等待初始化完成，超时直接抛错
        deadline = time.time() + self.config.connect_timeout
        while time.time() < deadline:
            if self._initialized:
                return
            time.sleep(0.1)
        if not self._initialized:
            raise GitHubMCPError(
                "MCP 连接超时（后台线程未完成初始化）",
                error_type="connection_error",
            )

    def close_sync(self) -> None:
        """同步关闭：向后台循环发送关闭协程，停止loop，回收线程。"""
        if self._bg_loop is None:
            return
        try:
            future = asyncio.run_coroutine_threadsafe(
                self.close(), self._bg_loop
            )
            future.result(timeout=5.0)
        except Exception:
            pass
        try:
            self._bg_loop.call_soon_threadsafe(self._bg_loop.stop)
        except Exception:
            pass
        self._bg_loop = None
        self._bg_thread = None



    # ===================== 工具调用接口 =====================

    @property
    def available_tools(self) -> list[str]:
        """返回当前会话允许调用的工具名列表"""
        return list(self._available_tools)

    @property
    def is_connected(self) -> bool:
        """连接就绪状态判断"""
        return self._initialized and self._session is not None

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> Any:
        """异步调用MCP工具。
            校验：连接状态、工具是否在白名单；增加超时控制；异常分类。
            返回：提取MCP返回内容块里的文本，拼接成字符串。
        """
        if not self.is_connected:
            raise GitHubMCPError("MCP 未连接", tool=name, error_type="not_connected")
        if name not in self._available_tools:
            raise GitHubMCPError(
                f"工具 '{name}' 不可用（可用: {self._available_tools}）",
                tool=name, error_type="tool_not_found",
            )
        timeout = timeout or self.config.tool_timeout
        assert self._session is not None

        try:
            result = await asyncio.wait_for(
                self._session.call_tool(name, arguments=arguments),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise GitHubMCPError(
                f"工具调用超时 ({timeout}s): {name}", tool=name, error_type="timeout",
            ) from exc
        except Exception as exc:
            msg = str(exc).lower()
            etype = "unknown"
            if any(k in msg for k in ("404", "not found", "no such", "does not exist")):
                etype = "repo_not_found"
            elif any(k in msg for k in ("401", "403", "unauthorized", "forbidden", "permission")):
                etype = "permission_denied"
            elif any(k in msg for k in ("timeout", "timed out", "connection")):
                etype = "network_error"
            raise GitHubMCPError(
                f"工具调用失败: {type(exc).__name__}: {exc}",
                tool=name, error_type=etype, raw=str(exc),
            ) from exc

        return self._extract_text(result)

    @staticmethod
    def _extract_text(result: Any) -> str:
        """
        解析MCP返回对象，把content里所有text块拼接成纯文本。
        MCP支持多种content类型（图片/文本），这里只取文本。
        """
        content_blocks = getattr(result, "content", [])
        if not content_blocks:
            return ""
        texts: list[str] = []
        for block in content_blocks:
            text = getattr(block, "text", None)
            if text:
                texts.append(text)
        return "\n".join(texts)

    def call_tool_sync(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> Any:
        """同步调用工具入口。
        原理：把 call_tool 协程投递到后台常驻事件循环，主线程阻塞等待future。
        额外+5s超时余量，留给线程调度开销。
        """
        if not self.is_connected or self._bg_loop is None:
            raise GitHubMCPError(
                "MCP 未连接；先调用 connect_sync()",
                tool=name, error_type="not_connected",
            )
        timeout = timeout or self.config.tool_timeout
        future = asyncio.run_coroutine_threadsafe(
            self.call_tool(name, arguments, timeout=timeout),
            self._bg_loop,
        )
        try:
            return future.result(timeout=timeout + 5.0)
        except Exception as exc:
            # run_coroutine_threadsafe 把内部异常重新抛出
            if isinstance(exc, GitHubMCPError):
                raise
            raise GitHubMCPError(
                f"同步调用失败: {type(exc).__name__}: {exc}",
                tool=name, error_type="unknown", raw=str(exc),
            ) from exc


async def quick_test(owner: str, repo: str, filepath: str = "") -> None:
    """测试函数：快速验证公开仓库读取能力，不需要token。"""
    async with GitHubMCP() as client:
        print(f"可用只读工具: {client.available_tools}")
        print(f"\n尝试读取 {owner}/{repo} {'目录' if not filepath else filepath}...")
        try:
            result = await client.call_tool(
                "get_file_contents",
                {"owner": owner, "repo": repo, "path": filepath},
            )
            snippet = (result[:300] + "...") if len(result) > 300 else result
            print(f"✓ 成功！前300字符:\n{snippet}")
        except GitHubMCPError as e:
            print(f"✗ 失败: [{e.error_type}] {e}")


if __name__ == "__main__":
    # 测试：读取 langchain-ai/langchain 的根目录
    asyncio.run(quick_test("langchain-ai", "langchain"))
