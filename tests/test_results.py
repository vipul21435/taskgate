from taskgate.results import CheckReport, GateResult, Severity, Status, TaskReport


def result(code: str, severity: Severity, status: Status) -> GateResult:
    return GateResult(code=code, name="n", severity=severity, status=status, message="m")


def test_only_failing_errors_block() -> None:
    assert result("TG101", Severity.ERROR, Status.FAIL).blocking
    assert not result("TG101", Severity.WARNING, Status.FAIL).blocking
    assert not result("TG101", Severity.ERROR, Status.SKIP).blocking
    assert not result("TG101", Severity.ERROR, Status.PASS).blocking


def test_task_and_check_verdicts() -> None:
    good = TaskReport("tasks/a", "added", (result("TG101", Severity.ERROR, Status.PASS),))
    warned = TaskReport("tasks/b", None, (result("TG101", Severity.WARNING, Status.FAIL),))
    bad = TaskReport(
        "tasks/c",
        "modified",
        (
            result("TG101", Severity.ERROR, Status.FAIL),
            result("TG102", Severity.ERROR, Status.FAIL),
        ),
    )
    removed = TaskReport("tasks/d", "removed")

    assert [t.verdict for t in (good, warned, bad, removed)] == ["pass", "pass", "fail", "skip"]
    assert not removed.checked
    assert bad.blocking_failures == 2

    assert CheckReport("0.1.0", "diff", tasks=(good, warned, removed)).passed
    failing = CheckReport("0.1.0", "diff", tasks=(good, bad))
    assert not failing.passed
    assert failing.blocking_failures == 2
