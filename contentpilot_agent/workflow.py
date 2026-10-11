from __future__ import annotations

import hashlib
import json
import time
from functools import wraps
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .models import ContentItem, ContentRequest, ReviewResult, RunResult
from .providers import ContentProvider, provider_from_env
from .quality import check_content_rules, check_reference_rules
from .storage import RunStore
from .tools import ContentTools


class WorkflowState(TypedDict, total=False):
    request: ContentRequest
    plan: list[str]
    hot_topics: list[str]
    tool_calls: list[str]
    contents: list[ContentItem]
    review: ReviewResult
    iteration: int
    max_iterations: int
    logs: list[str]
    next_node: str


def build_workflow(
    provider: ContentProvider,
    tools: ContentTools | None = None,
    max_iterations: int = 3,
    observer=None,
    checkpoint=None,
):
    if not 0 <= max_iterations <= 5:
        raise ValueError("max_iterations 必须在 0 到 5 之间")
    tools = tools or ContentTools()

    def traced(name, function):
        @wraps(function)
        def wrapped(state):
            started = time.perf_counter()
            if observer:
                observer(name + "_started", 0, {})
            result = function(state)
            merged = {**state, **result}
            next_node = {"plan": "generate", "generate": "review", "revise": "review"}.get(name)
            if name == "review":
                next_node = (
                    "finish"
                    if merged["review"].passed
                    or merged.get("iteration", 0) >= merged.get("max_iterations", 3)
                    else "revise"
                )
            if checkpoint:

                def serial(value):
                    if hasattr(value, "model_dump"):
                        return value.model_dump()
                    if isinstance(value, list):
                        return [serial(v) for v in value]
                    return value

                checkpoint(
                    {
                        "next_node": next_node,
                        "request_hash": hashlib.sha256(
                            json.dumps(state["request"].model_dump(), sort_keys=True).encode()
                        ).hexdigest(),
                        "payload": {
                            k: serial(v)
                            for k, v in merged.items()
                            if k not in {"request", "next_node"}
                        },
                        "provider_usage": getattr(provider, "usage", None),
                        "model_calls": getattr(provider, "calls", 0),
                    }
                )
            payload = {}
            for key in ("plan", "contents", "review", "iteration"):
                if key in result:
                    value = result[key]
                    if isinstance(value, list):
                        value = [v.model_dump() if hasattr(v, "model_dump") else v for v in value]
                    elif hasattr(value, "model_dump"):
                        value = value.model_dump()
                    payload[key] = value
            if observer:
                observer(name, round((time.perf_counter() - started) * 1000, 2), payload)
            return result

        return wrapped

    def validate_contents(request, contents):
        if len(contents) != request.content_count:
            raise ValueError("模型输出数量与请求不一致")
        if any(item.platform not in request.platforms for item in contents):
            raise ValueError("模型输出了未请求的平台")

    def plan_node(state: WorkflowState) -> WorkflowState:
        request = state["request"]
        hot_topics = (
            [t["title"] for t in request.live_topics]
            if request.live_topics
            else tools.collect_context(request.product_name, request.platforms)
        )
        plan = provider.create_plan(request, hot_topics=hot_topics)
        if len(plan) != request.content_count or any(not p.strip() for p in plan):
            raise ValueError("模型返回的选题数量或格式不正确")
        return {
            "plan": plan,
            "hot_topics": hot_topics,
            "tool_calls": list(tools.calls),
            "iteration": 0,
            "max_iterations": max_iterations,
            "logs": [
                f"已读取{'带来源的实时热点' if request.live_topics else '本地示例话题（非实时热点）'}，得到 {len(hot_topics)} 个参考话题",
                f"已生成 {len(plan)} 个内容选题",
                f"内容技能：{request.skill.get('name', '默认')}；参考 {len(request.performance_context)} 条历史效果记录",
                f"已读取 {len(request.references)} 条参考帖快照；只参考主题与结构，商品事实以商品资料为准",
                f"知识库检索命中 {len(request.knowledge_context)} 条资料，已保留摘录与来源快照",
            ],
        }

    def generate_node(state: WorkflowState) -> WorkflowState:
        contents = provider.generate_content(state["request"], state["plan"])
        validate_contents(state["request"], contents)
        logs = [*state.get("logs", []), f"已生成 {len(contents)} 条平台内容"]
        return {"contents": contents, "logs": logs}

    def review_node(state: WorkflowState) -> WorkflowState:
        review = provider.review_content(state["request"], state["contents"])
        review = review.model_copy(update={"passed": review.passed and review.score >= 80})
        rule_issues = check_content_rules(state["contents"]) + check_reference_rules(
            state["contents"], state["request"].references
        )
        if rule_issues:
            review = review.model_copy(
                update={
                    "score": min(review.score, 75),
                    "passed": False,
                    "issues": [*review.issues, *rule_issues],
                }
            )
        logs = [
            *state.get("logs", []),
            f"第 {state.get('iteration', 0) + 1} 次审核：{review.score} 分",
        ]
        return {"review": review, "logs": logs}

    def revise_node(state: WorkflowState) -> WorkflowState:
        contents = provider.revise_content(state["request"], state["contents"], state["review"])
        validate_contents(state["request"], contents)
        iteration = state.get("iteration", 0) + 1
        logs = [*state.get("logs", []), f"根据审核意见完成第 {iteration} 次修改"]
        return {"contents": contents, "iteration": iteration, "logs": logs}

    def route_after_review(state: WorkflowState) -> str:
        review = state["review"]
        if review.passed or state.get("iteration", 0) >= state.get("max_iterations", 3):
            return "finish"
        return "revise"

    graph = StateGraph(WorkflowState)
    graph.add_node("plan", traced("plan", plan_node))
    graph.add_node("generate", traced("generate", generate_node))
    graph.add_node("review", traced("review", review_node))
    graph.add_node("revise", traced("revise", revise_node))
    graph.add_conditional_edges(
        START,
        lambda state: state.get("next_node", "plan"),
        {
            "plan": "plan",
            "generate": "generate",
            "review": "review",
            "revise": "revise",
            "finish": END,
        },
    )
    graph.add_edge("plan", "generate")
    graph.add_edge("generate", "review")
    graph.add_conditional_edges(
        "review",
        route_after_review,
        {"revise": "revise", "finish": END},
    )
    graph.add_edge("revise", "review")
    return graph.compile()


def run_workflow(
    request: ContentRequest,
    provider: ContentProvider | None = None,
    tools: ContentTools | None = None,
    store: RunStore | None = None,
    max_iterations: int = 3,
    observer=None,
    checkpoint=None,
    initial_checkpoint=None,
) -> RunResult:
    provider = provider or provider_from_env()
    tools = tools or ContentTools()
    events = []
    started = time.perf_counter()

    def on_step(stage, elapsed_ms, payload):
        events.append({"stage": stage, "elapsed_ms": elapsed_ms, **payload})
        if observer:
            observer(stage, elapsed_ms, payload)

    app = build_workflow(
        provider,
        tools=tools,
        max_iterations=max_iterations,
        observer=on_step,
        checkpoint=checkpoint,
    )
    initial = {
        "request": request,
        "iteration": 0,
        "max_iterations": max_iterations,
        "logs": [],
    }
    resumed_from = None
    if initial_checkpoint:
        expected = hashlib.sha256(
            json.dumps(request.model_dump(), sort_keys=True).encode()
        ).hexdigest()
        if initial_checkpoint.get("request_hash") != expected:
            raise ValueError("断点与任务输入不一致")
        payload = dict(initial_checkpoint["payload"])
        if "contents" in payload:
            payload["contents"] = [ContentItem.model_validate(v) for v in payload["contents"]]
        if "review" in payload:
            payload["review"] = ReviewResult.model_validate(payload["review"])
        resumed_from = initial_checkpoint["next_node"]
        initial.update(payload)
        initial["next_node"] = resumed_from
        if hasattr(provider, "usage") and initial_checkpoint.get("provider_usage"):
            provider.usage = initial_checkpoint["provider_usage"]
        if hasattr(provider, "calls"):
            provider.calls = initial_checkpoint.get("model_calls", 0)
    state = app.invoke(initial)
    result = RunResult(
        request=request,
        plan=state["plan"],
        hot_topics=state.get("hot_topics", []),
        tool_calls=state.get("tool_calls", []),
        contents=state["contents"],
        review=state["review"],
        iterations=state.get("iteration", 0),
        logs=state.get("logs", []),
        versions=[event for event in events if "contents" in event],
        metrics={
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "steps": [
                {"stage": e["stage"], "elapsed_ms": e["elapsed_ms"]}
                for e in events
                if not e["stage"].endswith("_started")
            ],
            "token_usage": getattr(provider, "usage", None),
            "model_calls": getattr(provider, "calls", 0),
            "estimated_cost": None,
            "model": getattr(provider, "model", None),
            "skill": request.skill,
            "feedback_samples": len(request.performance_context),
            "reference_samples": len(request.references),
            "knowledge_samples": len(request.knowledge_context),
            "resumed_from": resumed_from,
        },
    )
    if store is not None:
        store.save(result)
    return result
