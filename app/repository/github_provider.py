"""GitHub 仓库访问实现 —— 通过 GitHub MCP 读取远程仓库。

职责：
- 实现 ``RepositoryProvider`` 协议接口
- 用 ``GitHubMCP`` 客户端按需访问目录树、文件、提交等
- 不执行仓库中的任何代码、不写入任何文件

与本地模式的区别：
- 按需读取（不下载全仓库），适合简单查询和小范围分析
- 深度跨文件分析需要完整索引，此时调用 ``prepare_deep_analysis()`` 临时克隆到工作区

错误处理：
- 无 Token 时只能访问公开仓库
- 权限不足 / 仓库不存在 / 速率限制 → 明确抛出 GitHubMCPError
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from app.mcp.github_client import GitHubMCP, GitHubMCPError
from app.repository import RepositoryProvider

logger = logging.getLogger(__name__)


# GitHub MCP get_file_contents 返回的 type 字段
_FILE_TYPE = "file"
_DIR_TYPE = "dir"


class GitHubRepositoryProvider:
    """通过 GitHub MCP 访问远程仓库。"""

    source: str = "github"

    def __init__(
        self,
        owner: str,
        repo: str,
        *,
        branch: str = "",  # 空字符串 = 让 git clone 自动选远程 HEAD 默认分支（main/master/develop 都能 clone）
        repository_id: str | None = None,
        mcp: GitHubMCP | None = None,
        clone_path: str | None = None,
    ):
        self.owner = owner
        self.repo = repo
        self.branch = branch
        self._repository_id = repository_id or f"github://{owner}/{repo}"
        self._mcp = mcp  # 允许外部注入已连接的 MCP 实例
        self._owned_mcp: bool = mcp is None # 标记MCP是不是当前类自己创建的，自己创建的才由本类负责关闭
        self._clone_path = Path(clone_path) if clone_path else None # 克隆到本地后的路径，不为空时优先走本地文件读取
        self._commit_sha: str | None = None # 缓存当前分支最新commit hash
        self._info_cached: dict | None = None # 仓库基础信息缓存
        # 异步锁：保证 connect 只做一次
        self._connect_lock = None

    @property
    def repository_id(self) -> str:
        return self._repository_id

    @property
    def repo_info(self) -> dict:
        if self._info_cached:
            return self._info_cached
        return {
            "name": f"{self.owner}/{self.repo}",
            "owner": self.owner,
            "repo": self.repo,
            "branch": self.branch,
            "source": self.source,
            "commit": self._commit_sha,
            "indexed": self._clone_path is not None,
        }

    @property
    def has_local_index(self) -> bool:
        """GitHub 仓库是否有本地 clone 可以做 RAG 索引。

        - clone 成功 → True（有本地文件，可以 ensure_index + search_code_rag）
        - clone 失败 / 未 clone → False（按需 MCP 模式，RAG 索引无从谈起）
        """
        return self._clone_path is not None and self._clone_path.exists()


    # --- MCP lifecycle ---
    # --- 备用异步通路，当前业务不调用 ---

    async def _ensure_connected(self) -> GitHubMCP:
        """异步版本：保证MCP连接就绪，没有连接就新建并连接"""
        if self._mcp is not None and self._mcp.is_connected:
            return self._mcp
        if self._mcp is None:
            self._mcp = GitHubMCP()
        await self._mcp.connect()
        # 顺便查一下最新 commit
        try:
            await self._fetch_commit_sha()
        except Exception:
            pass
        return self._mcp

    def _ensure_connected_sync(self) -> GitHubMCP:
        """同步版本：保证MCP连接就绪，调用connect_sync走后台常驻async循环"""
        if self._mcp is not None and self._mcp.is_connected:
            return self._mcp
        if self._mcp is None:
            self._mcp = GitHubMCP()
            self._owned_mcp = True
        if not self._mcp.is_connected:
            #同步连接
            self._mcp.connect_sync()
        # 同步取 commit（用 call_tool_sync）
        if self._commit_sha is None:
            try:
                result = self._mcp.call_tool_sync(
                    "list_commits",
                    {"owner": self.owner, "repo": self.repo, "path": "", "per_page": 1},
                )
                data = self._parse_json(result)
                if isinstance(data, list) and data:
                    first = data[0]
                    self._commit_sha = first.get("sha") or first.get("commit", {}).get("sha")
            except Exception:
                pass
        return self._mcp

    async def _fetch_commit_sha(self) -> None:
        """异步拉取当前分支最新commit哈希，更新缓存"""
        try:
            assert self._mcp is not None
            result = await self._mcp.call_tool(
                "list_commits",
                {"owner": self.owner, "repo": self.repo, "path": "", "per_page": 1},
            )
            data = self._parse_json(result)
            if isinstance(data, list) and data:
                # list_commits 返回的是带 sha 字段的列表
                first = data[0]
                self._commit_sha = first.get("sha") or first.get("commit", {}).get("sha")
            self._info_cached = None  # 让 repo_info 重新构建
        except Exception:
            logger.debug("获取 commit SHA 失败（不阻塞）", exc_info=True)

    def _parse_json(self, text: str) -> Any:
        """尽可能从工具返回里提取 JSON；如果不是 JSON 就原样返回。"""
        if not text:
            return ""
        # 尝试直接 parse
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            pass
        # 有些 MCP 工具返回包裹了 ```json ... ```
        m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except (json.JSONDecodeError, ValueError):
                pass
        return text


    # --- 读取文件 / 目录 ---
    async def _read_remote(self, relative_path: str = "") -> Any:
        """异步：调用 get_file_contents 获取目录或文件。"""
        assert self._mcp is not None
        arguments = {
            "owner": self.owner,
            "repo": self.repo,
            "path": relative_path,
        }
        if self.branch and self.branch != "main":
            arguments["ref"] = self.branch
        raw = await self._mcp.call_tool("get_file_contents", arguments)
        return self._parse_json(raw)

    def _read_remote_sync(self, relative_path: str = "") -> Any:
        """同步：调用 get_file_contents（走后台线程）。"""
        assert self._mcp is not None
        arguments = {
            "owner": self.owner,
            "repo": self.repo,
            "path": relative_path,
        }
        if self.branch and self.branch != "main":
            arguments["ref"] = self.branch
        raw = self._mcp.call_tool_sync("get_file_contents", arguments)
        return self._parse_json(raw)

    #-----------------------读取文件入口--------------------------------------
    def read_file(self, relative_path: str, max_chars: int = 30000) -> str:
        """对外同步接口：读取文件；如果已经克隆到本地，优先读本地文件"""
        if self._clone_path is not None:
            local = self._clone_path / relative_path
            if not local.exists():
                raise FileNotFoundError(f"远程克隆中找不到文件：{relative_path}")
            return local.read_text(encoding="utf-8")[:max_chars]

        self._ensure_connected_sync()
        data = self._read_remote_sync(relative_path)
        content = self._extract_file_content(data, relative_path)
        if len(content) > max_chars:
            content = content[:max_chars] + "\n\n[Content truncated]"
        return content
    #-------------------------------------------------------------------------


    def _extract_file_content(self, data: Any, path_hint: str) -> str:
        """从 get_file_contents （MCP）返回里提取文件内容（基64解码）。"""
        if isinstance(data, list):
            raise ValueError(f"路径是目录不是文件：{path_hint}")
        if isinstance(data, dict):
            t = data.get("type", "")
            if t == _DIR_TYPE:
                raise ValueError(f"路径是目录不是文件：{path_hint}")
            if data.get("encoding") == "base64":
                raw = base64.b64decode(data.get("content", ""))
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    return "[Binary or non-UTF-8 file]"
            return data.get("content", "") or ""
        if isinstance(data, str):
            return data
        return str(data)

    #------------------------异步读取文件入口-------------------------
    async def _read_file_async(self, relative_path: str) -> str:
        await self._ensure_connected()
        data = await self._read_remote(relative_path)
        return self._extract_file_content(data, relative_path)
    #----------------------------------------------------------


    #------------------------同步获取仓库目录树入口------------------------
    def get_repo_tree(self, max_depth: int = 3) -> str:
        """对外同步接口：生成仓库目录树；本地克隆模式直接复用LocalProvider"""
        if self._clone_path is not None:
            from app.repository import LocalRepositoryProvider
            local = LocalRepositoryProvider(str(self._clone_path), repository_id=self._repository_id)
            return local.get_repo_tree(max_depth=max_depth)

        self._ensure_connected_sync()
        return self._get_repo_tree_sync("", max_depth, 0)
    #----------------------------------------------------------------------



    def _get_repo_tree_sync(self, path: str, max_depth: int, depth: int) -> str:
        """同步目录树构建（递归）。"""
        if depth > max_depth:
            return ""
        if depth == 0:
            lines = [f"📁 {self.repo}/  (远程仓库；以下路径相对于它)"]
        else:
            lines = []

        ignore = {".git", ".idea", ".venv", "__pycache__", "node_modules",
                  "target", "build", "dist"}

        try:
            data = self._read_remote_sync(path)
        except GitHubMCPError as e:
            lines.append(f"  [读取失败: {e}]")
            return "\n".join(lines)

        if not isinstance(data, list):
            return "\n".join(lines)

        children = sorted(data, key=lambda c: (c.get("type") != _DIR_TYPE, c.get("name", "").lower()))
        for child in children:
            name = child.get("name", "")
            if name in ignore:
                continue
            t = child.get("type", "")
            indent = "  " * depth
            child_path = child.get("path", "")
            if t == _DIR_TYPE:
                lines.append(f"{indent}📁 {name}/")
                sub = self._get_repo_tree_sync(child_path, max_depth, depth + 1)
                if sub:
                    for line in sub.splitlines():
                        lines.append(f"{indent}{line}")
            else:
                lines.append(f"{indent}📄 {name}")
        return "\n".join(lines)


    #-------------------------建立文件仓库索引入口------------------------------------
    def index_files(self) -> list[dict]:
        """对外同步接口：遍历仓库，筛选代码文件返回文件清单"""
        if self._clone_path is not None:
            from app.repository import LocalRepositoryProvider
            local = LocalRepositoryProvider(str(self._clone_path), repository_id=self._repository_id)
            return local.index_files()

        self._ensure_connected_sync()
        return self._index_files_sync()
    #-----------------------------------------------------------------------------

    def _index_files_sync(self) -> list[dict]:
        ignore_dirs = {".git", ".idea", ".venv", "__pycache__", "node_modules",
                       "target", "build", "dist"}
        code_exts = {".py", ".java", ".js", ".ts", ".tsx", ".jsx", ".vue",
                     ".xml", ".yml", ".yaml", ".json", ".md"}
        results: list[dict] = []

        def walk(path: str, depth: int = 0) -> None:
            if depth > 3:
                return
            try:
                data = self._read_remote_sync(path)
            except Exception as e:
                logger.debug("跳过 %s: %s", path, e)
                return
            if not isinstance(data, list):
                return
            for item in data:
                name = item.get("name", "")
                item_path = item.get("path", "")
                t = item.get("type", "")
                if name in ignore_dirs:
                    continue
                if t == _DIR_TYPE:
                    walk(item_path, depth + 1)
                else:
                    ext = os.path.splitext(name)[1].lower()
                    if ext in code_exts:
                        results.append({
                            "path": item_path,
                            "extension": ext,
                            "size": item.get("size", 0),
                        })

        walk("")
        return results

    async def _get_repo_tree_async(self, path: str, max_depth: int, depth: int) -> str:
        if depth > max_depth:
            return ""
        if depth == 0:
            lines = [f"📁 {self.repo}/  (远程仓库；以下路径相对于它)"]
        else:
            lines = []

        # GitHub 忽略的黑名单目录（和 LocalProvider 对齐）
        ignore = {".git", ".idea", ".venv", "__pycache__", "node_modules",
                  "target", "build", "dist"}

        try:
            data = await self._read_remote(path)
        except GitHubMCPError as e:
            lines.append(f"  [读取失败: {e}]")
            return "\n".join(lines)

        if not isinstance(data, list):
            # 单文件 → 忽略（顶层不应是文件）
            return "\n".join(lines)

        children = sorted(data, key=lambda c: (c.get("type") != _DIR_TYPE, c.get("name", "").lower()))
        for child in children:
            name = child.get("name", "")
            if name in ignore:
                continue
            t = child.get("type", "")
            indent = "  " * depth
            child_path = child.get("path", "")
            if t == _DIR_TYPE:
                lines.append(f"{indent}📁 {name}/")
                sub = await self._get_repo_tree_async(child_path, max_depth, depth + 1)
                if sub:
                    # sub 里的每一行都需要加缩进
                    for line in sub.splitlines():
                        lines.append(f"{indent}{line}")
            else:
                lines.append(f"{indent}📄 {name}")
        return "\n".join(lines)

    @property
    def repo_path(self) -> Path:
        """本地工作区路径 —— 仅在 ``prepare_deep_analysis()`` 克隆完成后可用。"""
        if self._clone_path is None:
            raise AttributeError("远程按需模式没有本地 repo_path，请先调用 prepare_deep_analysis()")
        return self._clone_path

    #---------------------------搜索代码入口---------------------------------
    def search_code(
        self,
        keyword: str,
        extensions: set[str] | None = None,
        max_results: int = 20,
        context_lines: int = 3,
    ) -> list[dict]:
        """跨仓库代码搜索（需要 Token；无 Token 时返回一条「不可用说明」结果）。"""
        mcp = self._ensure_connected_sync()

        if not mcp.config.token:
            # 无 Token 时 GitHub 不支持 code search。返回一条「不可用说明」而不是空列表 ——
            # 空列表会被工具渲染成「没有找到关键词」，诱导 Agent 不断换关键词重试。
            logger.warning("GitHub code search 需要 GITHUB_TOKEN，当前无 Token")
            return [{
                "file": "",
                "line": 0,
                "content": keyword,
                "snippet": (
                    "GitHub 代码搜索不可用（未配置 GITHUB_TOKEN，GitHub 要求登录后才能搜索代码）。"
                    "请改用 get_repo_tree 查看目录结构，再用 read_file 直接读取相关文件。"
                ),
            }]

        try:
            result = mcp.call_tool_sync(
                "search_code", #mcp调用github的工具
                {
                    "q": f"{keyword} repo:{self.owner}/{self.repo}",
                    "per_page": min(max_results, 100),
                },
            )
        except GitHubMCPError as e:
            logger.warning("search_code 失败: %s", e)
            return []
        data = self._parse_json(result)
        if not isinstance(data, dict):
            return []
        items = data.get("items", [])
        enriched: list[dict] = []
        for item in items[:max_results]:
            path = item.get("path", "")
            # GitHub code search 返回的 fragments 是 HTML，去掉高亮
            snippet = ""
            for f in item.get("text_matches", [])[:1]:
                snippet = re.sub(r"<[^>]+>", "", f.get("fragment", ""))
                break
            enriched.append({
                "file": f"github://{self.owner}/{self.repo}/{path}",
                "line": item.get("line", 0),
                "content": keyword,
                "snippet": snippet,
            })
        return enriched

    # --- 深度分析：临时克隆仓库到本地 ---
    @staticmethod
    def _configure_git_proxy() -> None:
        """自动配置 git 代理：探测活梯子端口，设上或清掉。

        逻辑：
          1. 扫常见梯子本地端口（Clash 7892/7890、mihomo 7897、v2ray 1080 等）
          2. 找到活端口 → 设 git http.proxy=https.proxy=http://127.0.0.1:{port}
          3. 没找到 → 清掉 git proxy（让 TUN/系统代理/直连接管）
          4. 已经设着活代理 → 不动

        这样无论开不开梯子，git clone 都能走最优路径：
          - 开梯子 → 自动走 HTTP 代理
          - 关梯子 → TUN 接管或直连（国内 Gitee 直连就行）
        """
        import socket as _socket

        # 常见梯子本地 HTTP 代理端口（HTTP 代理优先，git 兼容最好）
        PROBE_PORTS = [7892, 7890, 7897, 10808, 20171]

        def _port_alive(port: int) -> bool:
            try:
                s = _socket.create_connection(("127.0.0.1", port), timeout=1)
                s.close()
                return True
            except OSError:
                return False

        # 先查当前 git proxy 设的啥
        try:
            cur = subprocess.run(
                ["git", "config", "--global", "--get", "https.proxy"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
        except Exception:
            cur = ""

        # 从常见端口里找活的
        live_port = next((p for p in PROBE_PORTS if _port_alive(p)), None)

        if live_port:
            # 有活梯子 → 设 git proxy
            target = f"http://127.0.0.1:{live_port}"
            if cur != target:
                subprocess.run(
                    ["git", "config", "--global", "http.proxy", target],
                    capture_output=True, timeout=5,
                )
                subprocess.run(
                    ["git", "config", "--global", "https.proxy", target],
                    capture_output=True, timeout=5,
                )
                logger.info("检测到梯子端口 %d 可达，已设置 git 代理 → %s", live_port, target)
            # else: 已经设对了，不动
        else:
            # 没梯子 → 清掉旧代理
            if cur:
                subprocess.run(["git", "config", "--global", "--unset", "http.proxy"],
                               capture_output=True, timeout=5)
                subprocess.run(["git", "config", "--global", "--unset", "https.proxy"],
                               capture_output=True, timeout=5)
                logger.info("未检测到活梯子端口，已清除 git 全局代理")

    async def prepare_deep_analysis(self, workspace: str | None = None) -> Path:
        """异步克隆仓库到本地目录，克隆完成后切换成本地读取模式，用于全量索引
        克隆完成后 self._clone_path 被设置，后续 read_file 等方法走本地路径。
        """
        # 自动配置 git 代理（探测活梯子端口 → 设上；没梯子 → 清掉）
        self._configure_git_proxy()

        ws = workspace or self._clone_path or str(Path.home() / ".codeinsight" / "workspace")
        Path(ws).mkdir(parents=True, exist_ok=True)
        target = Path(ws) / f"{self.owner}__{self.repo}"

        # 已完整克隆过 → 直接返回
        if target.exists() and (target / ".git").exists():
            self._clone_path = target
            return target

        # 残留目录（上次 clone 失败的半成品）→ 清掉重来
        if target.exists():
            import shutil
            shutil.rmtree(target, ignore_errors=True)
            logger.info("清除残留目录 %s，准备重新克隆", target)

        # 构造 clone URL：有 token 就内嵌 x-access-token 认证
        url = f"https://github.com/{self.owner}/{self.repo}.git"
        token = self._mcp.config.token if self._mcp and self._mcp.config.token else None
        if token:
            url = f"https://x-access-token:{token}@github.com/{self.owner}/{self.repo}.git"

        # subprocess env：加 buffer 防大仓库断开 + 设 timeout 长一点
        env = os.environ.copy()
        # GIT_CONFIG_PARAMETERS 格式: key=value key=value ...
        env["GIT_CONFIG_PARAMETERS"] = (
            f"'http.postBuffer=524288000' 'http.lowSpeedLimit=0' 'http.lowSpeedTime=999999'"
        )

        clone_cmd = ["git", "clone", "--depth", "1"]
        if self.branch:
            clone_cmd.extend(["--branch", self.branch])
        clone_cmd.extend([url, str(target)])
        logger.info("开始 git clone: %s", url.replace(token or "", "***") if token else url)

        try:
            proc = await asyncio.create_subprocess_exec(
                *clone_cmd,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300.0)
            if proc.returncode != 0:
                raise RuntimeError(f"git clone 失败: {stderr.decode(errors='replace')}")
        except asyncio.TimeoutError:
            if target.exists():
                import shutil
                shutil.rmtree(target, ignore_errors=True)
            raise RuntimeError("git clone 超时 (>300s)")

        self._clone_path = target
        # 触发本地模式的 commit sha 提取
        try:
            self._commit_sha = (
                subprocess.check_output(
                    ["git", "-C", str(target), "rev-parse", "HEAD"],
                    stderr=subprocess.DEVNULL,
                )
                .decode()
                .strip()[:12]
            )
        except Exception:
            pass

        logger.info("远程仓库已克隆到 %s (commit %s)", target, self._commit_sha)
        return target

    def cleanup_clone(self) -> None:
        """清理临时克隆目录。"""
        if self._clone_path and self._clone_path.exists():
            import shutil
            shutil.rmtree(self._clone_path, ignore_errors=True)
            self._clone_path = None

    def close(self) -> None:
        """关闭 MCP 后台线程（如果是 owned 的）。"""
        if self._owned_mcp and self._mcp is not None:
            try:
                self._mcp.close_sync()
            except Exception:
                pass
            self._mcp = None

    async def __aenter__(self):
        await self._ensure_connected()
        return self

    async def __aexit__(self, *_):
        if self._owned_mcp and self._mcp is not None:
            await self._mcp.close()


async def quick_test():
    """快速测试：langchain-ai/langchain 根目录文件列表。"""
    async with GitHubRepositoryProvider("langchain-ai", "langchain") as g:
        print(f"repo_info: {g.repo_info}")
        print(f"\n目录树（depth=1）:\n{g.get_repo_tree(max_depth=1)}")


if __name__ == "__main__":
    asyncio.run(quick_test())
