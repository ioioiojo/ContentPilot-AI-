from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Platform = Literal["xiaohongshu", "douyin", "wechat"]


class ReferenceSnapshot(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    id: str = Field(max_length=32)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=6000)
    url: str = Field(min_length=1, max_length=2000)
    author: str = Field(default="", max_length=120)
    keyword: str = Field(default="", max_length=100)
    source_type: Literal["manual", "gateway", "tikhub"]
    captured_at: float


class ContentRequest(BaseModel):
    """The business brief supplied by an operator."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    product_name: str = Field(min_length=1, max_length=120, description="商品或服务名称")
    product_description: str = Field(min_length=1, max_length=12000, description="商品核心卖点")
    audience: str = Field(min_length=1, max_length=300, description="目标用户")
    goal: str = Field(default="提升点击和转化", min_length=1, max_length=300)
    platforms: list[Platform] = Field(
        default_factory=lambda: ["xiaohongshu", "douyin"], min_length=1, max_length=3
    )
    content_count: int = Field(default=3, ge=1, le=10)
    tone: str = Field(default="真实、简洁、有说服力", min_length=1, max_length=200)
    skill: dict = Field(default_factory=dict)
    performance_context: list[dict] = Field(default_factory=list, max_length=10)
    tools_mode: Literal["local", "mcp"] = "local"
    references: list[ReferenceSnapshot] = Field(default_factory=list, max_length=5)
    rewrite_guidance: str = Field(default="", max_length=1000)
    knowledge_context: list[dict] = Field(default_factory=list, max_length=5)
    live_topics: list[dict] = Field(default_factory=list, max_length=10)

    @field_validator("platforms")
    @classmethod
    def unique_platforms(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("平台不能重复")
        return value


class ContentItem(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    platform: Platform
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=12000)
    hashtags: list[str] = Field(default_factory=list, max_length=20)
    call_to_action: str = Field(default="了解更多", max_length=300)


class ReviewResult(BaseModel):
    score: int = Field(ge=0, le=100)
    passed: bool
    issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


class RunResult(BaseModel):
    run_id: str | None = None
    request: ContentRequest
    plan: list[str]
    hot_topics: list[str] = Field(default_factory=list)
    tool_calls: list[str] = Field(default_factory=list)
    contents: list[ContentItem]
    review: ReviewResult
    iterations: int
    logs: list[str] = Field(default_factory=list)
    metrics: dict = Field(default_factory=dict)
    versions: list[dict] = Field(default_factory=list)
