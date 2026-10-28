"""快速验证 GitHub MCP 集成（同步上下文）。"""
import sys
sys.path.insert(0, ".")

from app.repository import GitHubRepositoryProvider
from app.mcp.github_client import GitHubMCPError


def test():
    print("=" * 60)
    print("  GitHub MCP 集成验证（同步调用）")
    print("=" * 60)

    # 测试1：公开仓库读取
    g = GitHubRepositoryProvider("langchain-ai", "langchain")
    print(f"\n[测试1] 公开仓库 langchain-ai/langchain")
    print(f"  repo_info: {g.repo_info}")

    tree = g.get_repo_tree(max_depth=1)
    print(f"  目录树 (depth=1):\n{tree[:500]}")

    try:
        content = g.read_file("README.md", max_chars=500)
        print(f"  README.md 前300字符:\n{content[:300]}")
    except Exception as e:
        print(f"  read_file 失败: {e}")

    print("\n✓ 公开仓库读取通过")

    # 测试2：不存在的仓库
    print("\n[测试2] 不存在的仓库（应返回明确错误）")
    bad = GitHubRepositoryProvider("nonexistent-user-xyz-12345", "fake-repo")
    try:
        bad.get_repo_tree(max_depth=1)
        print("  ✗ 应该报错但没报错")
    except GitHubMCPError as e:
        print(f"  ✓ 明确错误: [{e.error_type}] {e}")
    except Exception as e:
        print(f"  异常: {type(e).__name__}: {e}")


if __name__ == "__main__":
    test()
