import pytest

from contentpilot_agent.models import ContentRequest
from contentpilot_agent.providers import MockContentProvider
from contentpilot_agent.storage import RunStore
from contentpilot_agent.tools import ContentTools
from contentpilot_agent.workflow import run_workflow


def test_workflow_revises_until_content_passes():
    request = ContentRequest(
        product_name="低糖咖啡",
        product_description="低糖、方便携带、适合工作日饮用",
        audience="25 到 35 岁的上班族",
    )

    tools = ContentTools()
    result = run_workflow(request, provider=MockContentProvider(), tools=tools)

    assert result.review.passed is True
    assert result.review.score >= 80
    assert result.iterations == 1
    assert len(result.plan) == 3
    assert len(result.contents) == 3
    assert any("修改" in log for log in result.logs)
    assert result.hot_topics
    assert result.tool_calls == tools.calls


def test_run_can_be_saved_and_loaded(tmp_path):
    request = ContentRequest(
        product_name="桌面效率工具",
        product_description="帮助整理任务",
        audience="远程办公者",
    )
    store = RunStore(tmp_path / "runs.db")
    result = run_workflow(request, provider=MockContentProvider(), store=store)

    assert result.run_id
    restored = store.get(result.run_id)
    assert restored is not None
    assert restored.request.product_name == "桌面效率工具"
    assert restored.review.passed is True


def test_mock_honors_ten_items_and_workflow_rejects_empty_platforms():
    request = ContentRequest(
        product_name="内容工具",
        product_description="整理信息",
        audience="运营人员",
        content_count=10,
    )
    result = run_workflow(request, provider=MockContentProvider())
    assert len(result.plan) == 10
    assert len(result.contents) == 10
    assert all(item.platform in request.platforms for item in result.contents)
    with pytest.raises(ValueError):
        ContentRequest(product_name="A", product_description="B", audience="C", platforms=[])


def test_review_flag_cannot_override_low_score():
    from contentpilot_agent.models import ReviewResult

    class InconsistentProvider(MockContentProvider):
        def review_content(self, request, contents):
            return ReviewResult(score=40, passed=True, issues=[], suggestions=[])

    request = ContentRequest(product_name="A", product_description="B", audience="C")
    result = run_workflow(request, provider=InconsistentProvider(), max_iterations=0)
    assert result.review.passed is False
