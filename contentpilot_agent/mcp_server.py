from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .tools import ContentTools

mcp = FastMCP("contentpilot-tools")
tools = ContentTools()


@mcp.tool()
def search_hot_topics(query: str, platform: str = "xiaohongshu") -> list[str]:
    """返回指定平台的本地示例话题（非实时热点）。"""

    return tools.search_hot_topics(query, platform)


@mcp.tool()
def collect_content_context(query: str, platforms: list[str]) -> list[str]:
    """为选题合并多个平台的本地示例话题（非网络检索）。"""

    return tools.collect_context(query, platforms)


if __name__ == "__main__":
    mcp.run()
