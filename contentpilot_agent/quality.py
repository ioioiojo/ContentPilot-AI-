from __future__ import annotations

from .models import ContentItem, ReferenceSnapshot

BLOCKED_CLAIMS = ("绝对有效", "100%有效", "稳赚不赔", "保证治愈")


def check_content_rules(contents: list[ContentItem]) -> list[str]:
    """Run deterministic checks before content is released."""

    issues: list[str] = []
    if not contents:
        return ["内容列表不能为空"]
    for index, item in enumerate(contents, start=1):
        text = f"{item.title}\n{item.body}\n{item.call_to_action}\n{' '.join(item.hashtags)}"
        for phrase in BLOCKED_CLAIMS:
            if phrase in text:
                issues.append(f"第 {index} 条内容包含高风险绝对化表述：{phrase}")
        if not item.title.strip() or not item.body.strip():
            issues.append(f"第 {index} 条内容缺少标题或正文")
        if not item.call_to_action.strip():
            issues.append(f"第 {index} 条内容缺少行动建议")
        if item.platform == "xiaohongshu" and len(item.body) > 1200:
            issues.append(f"第 {index} 条小红书正文超过 1200 字")
        if item.platform == "douyin" and len(item.body) < 20:
            issues.append(f"第 {index} 条抖音脚本过短，缺少可执行信息")
    return issues


def check_reference_rules(contents: list[ContentItem], references: list[ReferenceSnapshot]):
    """Flag long verbatim spans; this heuristic is not proof of originality."""
    if not references:
        return []

    def normalize(value):
        return "".join(char.lower() for char in value if char.isalnum())

    spans = set()
    titles = set()
    for reference in references:
        text = normalize(reference.body)
        spans.update(text[i : i + 32] for i in range(max(0, len(text) - 31)))
        title = normalize(reference.title)
        if len(title) >= 12:
            titles.add(title)
    issues = []
    for index, item in enumerate(contents, 1):
        text = normalize(item.body)
        if normalize(item.title) in titles or any(
            text[i : i + 32] in spans for i in range(max(0, len(text) - 31))
        ):
            issues.append(f"第 {index} 条内容与参考帖存在长段原文或完整标题重复，请改写后再审核")
    return issues
