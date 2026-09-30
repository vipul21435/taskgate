"""JUnit XML, GitHub annotations and reading report.json back."""

from __future__ import annotations

import json
from dataclasses import replace
from xml.etree import ElementTree

import pytest

from taskgate.report import (
    ReportError,
    findings,
    from_dict,
    from_json,
    headline,
    to_annotations,
    to_dict,
    to_json,
    to_junit,
    to_text,
)
from taskgate.results import CheckReport, ConfigSummary, GateResult, Severity, Status, TaskReport
from test_report import FAIL_402, PASS_101, REPORT, WARN_201

SKIP_501 = GateResult(
    "TG501", "grader-deterministic", Severity.ERROR, Status.SKIP, "skipped: requires TG401 to pass"
)
FLAKY_501 = GateResult(
    "TG501",
    "grader-deterministic",
    Severity.ERROR,
    Status.FAIL,
    "1 test flipped: t::a",
    fix_hint="Make tests independent.",
    details=("t::a: failed in run 2 (seed 2); reproduce: taskgate grade x --seed 2",),
)
RICH = CheckReport(
    version="9.9.9",
    mode="all",
    tasks=(
        TaskReport("tasks/bad", None, (PASS_101, FAIL_402, SKIP_501)),
        TaskReport("tasks/flaky", None, (PASS_101, FLAKY_501), cached=True),
        TaskReport("tasks/ok", None, (PASS_101, WARN_201)),
    ),
    config=ConfigSummary(
        source="taskgate.toml",
        disabled=("TG104",),
        severity=(("TG402", Severity.WARNING),),
        options=(
            ("files.max_file_bytes", 1024),
            ("runner.cpus", 1.5),
            ("files.binary_allow", ("*.png",)),
        ),
    ),
    runner="docker",
)


def test_junit_has_a_suite_per_task_and_a_case_per_gate() -> None:
    xml = to_junit(RICH)
    assert xml == to_junit(RICH)
    assert xml.startswith('<?xml version="1.0" encoding="utf-8"?>\n<testsuites name="taskgate"')
    root = ElementTree.fromstring(xml)
    assert root.attrib == {
        "name": "taskgate",
        "tests": "7",
        "failures": "2",
        "errors": "0",
        "skipped": "1",
    }
    suites = root.findall("testsuite")
    assert [s.get("name") for s in suites] == ["tasks/bad", "tasks/flaky", "tasks/ok"]
    assert [s.get("failures") for s in suites] == ["1", "1", "0"]
    bad = {case.get("name"): case for case in suites[0]}
    failure = bad["TG402 baseline-fails"].find("failure")
    assert failure is not None
    assert failure.attrib == {
        "message": "the grader passes an untouched workspace (1 skipped)",
        "type": "error",
    }
    assert failure.text == "fix: Assert on the output."
    skipped = bad["TG501 grader-deterministic"].find("skipped")
    assert skipped is not None
    assert skipped.get("message") == "skipped: requires TG401 to pass"
    assert list(bad["TG101 layout-complete"]) == []
    flaky = suites[1]
    assert flaky[0].tag == "properties"
    assert flaky[0][0].attrib == {"name": "cached", "value": "true"}
    flaky_failure = flaky.find("testcase[@name='TG501 grader-deterministic']/failure")
    assert flaky_failure is not None
    assert flaky_failure.text == (
        "t::a: failed in run 2 (seed 2); reproduce: taskgate grade x --seed 2\n"
        "fix: Make tests independent."
    )
    warned = suites[2].find("testcase[@name='TG201 big-file']")
    assert warned is not None
    out = warned.find("system-out")
    assert out is not None
    assert out.text == "warning: a | b\nc\nfix: Shrink it."
    assert warned.find("failure") is None


def test_junit_leaves_out_removed_tasks_and_characters_xml_cannot_hold() -> None:
    odd = replace(PASS_101, status=Status.FAIL, message="bell \x07 here")
    report = CheckReport(
        version="1",
        mode="diff",
        tasks=(
            TaskReport("tasks/gone", "removed"),
            TaskReport("tasks/odd", "added", (odd,)),
        ),
    )
    root = ElementTree.fromstring(to_junit(report))
    (suite,) = root.findall("testsuite")
    failure = suite.find("testcase/failure")
    assert failure is not None
    assert failure.get("message") == "bell ? here"
    assert failure.text is None


def test_findings_locate_every_failed_gate_on_the_manifest() -> None:
    found = findings(RICH, "examples/sample-repo/")
    assert [(f.path, f.severity, f.title) for f in found] == [
        ("examples/sample-repo/tasks/bad/task.toml", Severity.ERROR, "TG402 baseline-fails"),
        (
            "examples/sample-repo/tasks/flaky/task.toml",
            Severity.ERROR,
            "TG501 grader-deterministic",
        ),
        ("examples/sample-repo/tasks/ok/task.toml", Severity.WARNING, "TG201 big-file"),
    ]
    assert found[2].message == "a | b c"
    at_root = CheckReport(version="1", mode="all", tasks=(TaskReport(".", None, (FAIL_402,)),))
    assert [f.path for f in findings(at_root, ".")] == ["task.toml"]


def test_annotations_are_escaped_workflow_commands() -> None:
    report = CheckReport(
        version="1",
        mode="all",
        tasks=(
            TaskReport(
                "tasks/a,b",
                None,
                (
                    replace(FAIL_402, message="100% sure: it passes", fix_hint="x\ny"),
                    replace(WARN_201, severity=Severity.INFO, fix_hint=None),
                ),
            ),
        ),
    )
    assert to_annotations(report).splitlines() == [
        "::error file=tasks/a%2Cb/task.toml,line=1,title=TG402 baseline-fails::"
        "100%25 sure: it passes%0Afix: x%0Ay",
        "::notice file=tasks/a%2Cb/task.toml,line=1,title=TG201 big-file::a | b c",
    ]
    assert to_annotations(CheckReport(version="1", mode="all")) == ""


def test_a_report_reads_back_exactly() -> None:
    for report in (REPORT, RICH, replace(REPORT, pull_request=12)):
        assert from_json(to_json(report)) == report
        assert to_dict(from_dict(to_dict(report))) == to_dict(report)


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("{", "not a TaskGate report.json (not JSON: "),
        ("[]", "not a TaskGate report.json (TypeError: "),
        ("{}", "not a TaskGate report.json (KeyError: 'tasks')"),
    ],
)
def test_other_files_are_not_reports(text: str, problem: str) -> None:
    with pytest.raises(ReportError) as caught:
        from_json(text)
    assert str(caught.value).startswith(problem)
    bad_status = json.loads(to_json(REPORT))
    bad_status["tasks"][0]["gates"][0]["status"] = "maybe"
    with pytest.raises(ReportError, match="'maybe' is not a valid Status"):
        from_dict(bad_status)


def test_a_pull_request_file_list_is_named_in_the_header() -> None:
    text = to_text(replace(REPORT, pull_request=4)).splitlines()[0]
    assert text == (
        "taskgate 9.9.9: diff against main (merge base 0123456), files of pull request #4, "
        "3 changed tasks, 1 other file, local runner"
    )
    assert json.loads(to_json(replace(REPORT, pull_request=4)))["pull_request"] == 4
    assert headline(REPORT) == "TaskGate: FAIL, 1 blocking failure"
