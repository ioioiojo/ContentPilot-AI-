from contentpilot_agent.models import ContentItem
from contentpilot_agent.quality import check_content_rules


def test_quality_rules_catch_blocked_claims():
    item = ContentItem(
        platform="xiaohongshu",
        title="体验分享",
        body="这个产品100%有效",
        call_to_action="查看详情",
    )

    issues = check_content_rules([item])

    assert any("100%有效" in issue for issue in issues)
