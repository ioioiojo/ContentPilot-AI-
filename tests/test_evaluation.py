from contentpilot_agent.evaluation import run_evaluation


def test_evaluation_reports_quality_and_tool_metrics():
    report = run_evaluation()

    assert report.total_cases == 3
    assert report.pass_rate == 1.0
    assert report.average_score >= 80
    assert report.tool_call_rate == 1.0


def test_objective_checks_and_failed_cases_are_reported_separately():
    from contentpilot_agent.evaluation import DEFAULT_CASES, EvaluationCase

    case = EvaluationCase(
        name="禁止凭空表述", request=DEFAULT_CASES[0].request, forbidden_claims=["低糖"]
    )
    report = run_evaluation([case])
    assert report.objective_pass_rate == 0
    assert report.model_pass_rate == 1
    assert report.failures

    def broken():
        raise RuntimeError("private-secret")

    report = run_evaluation([case], provider_factory=broken)
    assert report.total_cases == 1 and report.passed_cases == 0
    assert "private-secret" not in str(report.failures)
