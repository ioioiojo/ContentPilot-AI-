from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field


@dataclass
class ContentTools:
    """Business tools used by the workflow and exposed through MCP.

    The local implementation is deterministic so it can be tested without external
    services. The same functions are exported by ``mcp_server.py`` for remote use.
    """

    calls: list[str] = field(default_factory=list)
    mode: str = "local"

    def search_hot_topics(self, query: str, platform: str) -> list[str]:
        self.calls.append(f"search_hot_topics:{platform}:{query}")
        platform_topics = {
            "xiaohongshu": ["通勤好物", "低负担生活", "真实使用体验"],
            "douyin": ["一分钟实测", "上班族效率", "开箱测评"],
            "wechat": ["消费决策指南", "生活方式", "产品深度解析"],
        }
        return platform_topics.get(platform, ["使用体验", "产品测评", "购买指南"])

    def collect_context(self, query: str, platforms: list[str]) -> list[str]:
        if self.mode == "mcp":
            topics = asyncio.run(self._mcp_context(query, platforms))
            self.calls.append("mcp:collect_content_context")
            return topics
        topics: list[str] = []
        for platform in platforms:
            topics.extend(self.search_hot_topics(query, platform))
        return list(dict.fromkeys(topics))

    async def _mcp_context(self, query, platforms):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=sys.executable, args=["-m", "contentpilot_agent.mcp_server"]
        )
        async with asyncio.timeout(20):
            async with stdio_client(params) as (read, write):  # noqa: SIM117 - dependent MCP streams
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "collect_content_context", {"query": query, "platforms": platforms}
                    )
                    if result.isError:
                        raise ValueError("MCP 上下文工具调用失败")
                    structured = getattr(result, "structuredContent", None)
                    values = structured.get("result") if isinstance(structured, dict) else None
                    if values is None:
                        values = [
                            json.loads(part.text)
                            for part in result.content
                            if getattr(part, "type", None) == "text"
                        ]
                        if len(values) == 1 and isinstance(values[0], list):
                            values = values[0]
                    if not isinstance(values, list) or not all(isinstance(x, str) for x in values):
                        raise ValueError("MCP 工具输出格式无效")
                    return values
