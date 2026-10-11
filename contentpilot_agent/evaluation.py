from __future__ import annotations

import argparse
import json
from collections.abc import Callable

from pydantic import BaseModel, Field

from .models import ContentRequest
from .providers import ContentProvider, MockContentProvider
from .quality import check_content_rules
from .workflow import run_workflow


class EvaluationCase(BaseModel):
    name: str
    request: ContentRequest
    forbidden_claims: list[str] = Field(default_factory=list)


class EvaluationReport(BaseModel):
    total_cases: int
    passed_cases: int
    pass_rate: float
    average_score: float
    average_revisions: float
    tool_call_rate: float
    failures: list[str] = Field(default_factory=list)
    objective_pass_rate: float = 0
    model_pass_rate: float = 0
    total_tokens: int = 0
    average_elapsed_ms: float = 0
    limitations: str = (
        "自动检查覆盖结构、平台、规则和人工预设禁用表述；事实正确性仍需人工标注，不代表业务效果。"
    )


DEFAULT_CASES = [
    EvaluationCase(
        name="咖啡通勤场景",
        request=ContentRequest(
            product_name="低糖咖啡",
            product_description="低糖、便携、适合工作日饮用",
            audience="25 到 35 岁的上班族",
        ),
    ),
    EvaluationCase(
        name="护肤入门场景",
        request=ContentRequest(
            product_name="温和保湿乳",
            product_description="清爽不黏、适合换季使用",
            audience="需要基础护肤的学生和上班族",
            platforms=["xiaohongshu", "wechat"],
        ),
    ),
    EvaluationCase(
        name="效率工具场景",
        request=ContentRequest(
            product_name="桌面效率工具",
            product_description="帮助用户整理任务和减少重复操作",
            audience="远程办公和自由职业者",
            platforms=["douyin", "wechat"],
        ),
    ),
]


def run_evaluation(
    cases: list[EvaluationCase] | None = None,
    provider_factory: Callable[[], ContentProvider] = MockContentProvider,
) -> EvaluationReport:
    cases = DEFAULT_CASES if cases is None else cases
    results, objective, failures = [], [], []
    for case in cases:
        try:
            result = run_workflow(case.request, provider=provider_factory())
        except Exception as exc:  # noqa: BLE001 - one failed case must not discard the whole report
            failures.append(f"{case.name}: {type(exc).__name__}")
            continue
        results.append(result)
        text = json.dumps([c.model_dump() for c in result.contents], ensure_ascii=False)
        valid = not check_content_rules(result.contents) and not any(
            term in text for term in case.forbidden_claims
        )
        objective.append(valid)
        if not valid:
            failures.append(case.name + ": 规则或禁止表述检查未通过")
    passed = sum(objective)
    total = len(cases)
    return EvaluationReport(
        total_cases=total,
        passed_cases=passed,
        pass_rate=round(passed / total, 3) if total else 0.0,
        objective_pass_rate=round(passed / total, 3) if total else 0.0,
        model_pass_rate=round(sum(r.review.passed for r in results) / total, 3) if total else 0,
        total_tokens=sum(
            (r.metrics.get("token_usage") or {}).get("total_tokens", 0) for r in results
        ),
        average_elapsed_ms=sum(r.metrics["elapsed_ms"] for r in results) / len(results)
        if results
        else 0,
        average_score=round(sum(result.review.score for result in results) / len(results), 2)
        if results
        else 0.0,
        average_revisions=round(sum(result.iterations for result in results) / len(results), 2)
        if results
        else 0.0,
        tool_call_rate=round(sum(bool(result.tool_calls) for result in results) / len(results), 3)
        if results
        else 0.0,
        failures=failures,
    )


def main() -> None:
    from pathlib import Path

    from .config import Settings
    from .providers import provider_from_env

    parser = argparse.ArgumentParser(description="基于人工预设检查点的离线评测")
    parser.add_argument("--provider", choices=["mock", "compatible"], default="mock")
    parser.add_argument("--cases", help="EvaluationCase 列表的 JSON 文件")
    args = parser.parse_args()
    Settings.from_env()
    cases = (
        [
            EvaluationCase.model_validate(c)
            for c in json.loads(Path(args.cases).read_text(encoding="utf-8"))
        ]
        if args.cases
        else None
    )
    report = run_evaluation(cases, provider_factory=lambda: provider_from_env(args.provider))
    print(
        json.dumps({"provider": args.provider, **report.model_dump()}, ensure_ascii=False, indent=2)
    )


if __name__ == "__main__":
    main()
