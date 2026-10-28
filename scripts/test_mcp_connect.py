"""快速连通性验证脚本。"""
import asyncio
from mcp.client.stdio import stdio_client
from mcp import ClientSession, StdioServerParameters


async def test():
    params = StdioServerParameters(
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
    )
    print("正在启动 GitHub MCP Server (via npx)...")
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            print("初始化会话...")
            await session.initialize()
            print("✓ 会话已建立，列出可用工具...")
            tools = await session.list_tools()
            desc = None
            for t in tools.tools:
                desc = (t.description or "")[:80]
                print(f"  - {t.name}: {desc}")
            print(f"\n共 {len(tools.tools)} 个工具")


if __name__ == "__main__":
    asyncio.run(test())
