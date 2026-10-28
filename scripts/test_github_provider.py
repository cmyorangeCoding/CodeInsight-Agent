"""快速验证 GitHub MCP 集成端到端。"""
import asyncio
from app.repository import GitHubRepositoryProvider
from app.mcp.github_client import GitHubMCPError


async def test():
    print("=" * 60)
    print("  GitHub MCP 集成验证")
    print("=" * 60)

    # 测试1：公开仓库读取（不需要 Token）
    async with GitHubRepositoryProvider("langchain-ai", "langchain") as g:
        info = g.repo_info
        print(f"\n[测试1] 公开仓库 langchain-ai/langchain")
        print(f"  repo_info: {info}")
        tree = g.get_repo_tree(max_depth=1)
        print(f"  目录树 (depth=1):\n{tree}")

        # 读一个具体文件
        try:
            content = g.read_file("README.md", max_chars=500)
            print(f"  README.md 前500字符:\n{content[:300]}...")
        except Exception as e:
            print(f"  read_file 失败: {e}")

        # 索引文件（远程模式深度限制3层）
        files = g.index_files()
        print(f"  index_files 返回 {len(files)} 个文件（远程限制）")

    print("\n✓ 公开仓库读取测试通过")

    # 测试2：不存在的仓库 → 应该报明确错误
    print("\n[测试2] 不存在的仓库（应返回明确错误）")
    try:
        async with GitHubRepositoryProvider("nonexistent-user-xyz-12345", "fake-repo") as bad:
            bad.get_repo_tree(max_depth=1)
        print("  ✗ 应该报错但没报错")
    except GitHubMCPError as e:
        print(f"  ✓ 收到明确错误: [{e.error_type}] {e}")
    except Exception as e:
        print(f"  收到异常: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(test())
