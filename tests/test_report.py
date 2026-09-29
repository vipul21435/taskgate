import json

from taskgate.report import describe_config, to_dict, to_json, to_markdown, to_text
from taskgate.results import (
    CheckReport,
    ConfigSummary,
    GateResult,
    Severity,
    Status,
    TaskReport,
)

PASS_101 = GateResult("TG101", "layout-complete", Severity.ERROR, Status.PASS, "layout complete")
FAIL_402 = GateResult(
    "TG402",
    "baseline-fails",
    Severity.ERROR,
    Status.FAIL,
    "the grader passes an untouched workspace (1 skipped)",
    fix_hint="Assert on the output.",
)
WARN_201 = GateResult(
    "TG201", "big-file", Severity.WARNING, Status.FAIL, "a | b\nc", fix_hint="Shrink it."
)

REPORT = CheckReport(
    version="9.9.9",
    mode="diff",
    base="main",
    merge_base="0123456789abcdef",
    tasks=(
        TaskReport("tasks/bad", "added", (PASS_101, FAIL_402), ("tasks/bad/task.toml",)),
        TaskReport("tasks/gone", "removed", (), ("tasks/gone/task.toml",)),
        TaskReport("tasks/ok", "modified", (PASS_101, WARN_201)),
    ),
    other_files=("README.md",),
)


def test_text_report() -> None:
    assert to_text(REPORT) == (
        "taskgate 9.9.9: diff against main (merge base 0123456), "
        "3 changed tasks, 1 other file\n"
        "tasks/bad  added  FAIL\n"
        "  pass  TG101  layout-complete  layout complete\n"
        "  FAIL  TG402  baseline-fails   the grader passes an untouched workspace (1 skipped)\n"
        "        fix: Assert on the output.\n"
        "tasks/gone  removed  SKIP\n"
        "  removed tasks are not checked\n"
        "tasks/ok  modified  PASS\n"
        "  pass  TG101  layout-complete  layout complete\n"
        "  fail  TG201  big-file         a | b c\n"
        "        fix: Shrink it.\n"
        "result: FAIL, 1 blocking failure\n"
    )


def test_text_report_for_an_empty_all_mode_run() -> None:
    report = CheckReport(version="9.9.9", mode="all")
    assert to_text(report) == (
        "taskgate 9.9.9: all tasks, 0 tasks\n"
        "no task directories to check\n"
        "result: PASS, 0 blocking failures\n"
    )


def test_markdown_report() -> None:
    markdown = to_markdown(REPORT)
    assert markdown.startswith("## TaskGate: FAIL\n\n1 blocking failure; diff against main")
    assert "| `tasks/bad` | added | **fail** | TG402 |" in markdown
    assert "| `tasks/gone` | removed | skip | - |" in markdown
    assert "| `tasks/ok` | modified | pass | - |" in markdown
    assert "| TG402 baseline-fails | **fail** (error) | the grader passes" in markdown
    assert "| TG201 big-file | **fail** (warning) | a \\| b c |" in markdown
    assert "- **TG402**: Assert on the output." in markdown
    assert "### `tasks/gone`" not in markdown
    assert markdown.endswith("<sub>taskgate 9.9.9</sub>\n")


def test_markdown_report_without_tasks() -> None:
    markdown = to_markdown(CheckReport(version="9.9.9", mode="diff", base="main", merge_base="ab"))
    assert "## TaskGate: PASS" in markdown
    assert "No task directories to check." in markdown
    assert "| Task |" not in markdown


def test_json_report_round_trips_and_is_stable() -> None:
    data = json.loads(to_json(REPORT))
    assert data == to_dict(REPORT)
    assert data["result"] == "fail"
    assert data["blocking_failures"] == 1
    assert data["merge_base"] == "0123456789abcdef"
    assert [task["verdict"] for task in data["tasks"]] == ["fail", "skip", "pass"]
    assert data["tasks"][0]["gates"][1] == {
        "code": "TG402",
        "name": "baseline-fails",
        "severity": "error",
        "status": "fail",
        "blocking": True,
        "message": "the grader passes an untouched workspace (1 skipped)",
        "fix_hint": "Assert on the output.",
    }
    assert data["other_files"] == ["README.md"]
    assert to_json(REPORT) == to_json(REPORT)


def test_config_is_named_in_every_format() -> None:
    config = ConfigSummary(source="taskgate.toml at main", disabled=("TG104", "TG203"))
    report = CheckReport(version="9.9.9", mode="all", config=config)
    assert describe_config(config) == "taskgate.toml at main; disabled TG104, TG203"
    assert to_text(report).splitlines()[1] == "config: taskgate.toml at main; disabled TG104, TG203"
    assert "Config: taskgate.toml at main; disabled TG104, TG203.\n" in to_markdown(report)
    assert to_dict(report)["config"] == {
        "source": "taskgate.toml at main",
        "disabled": ["TG104", "TG203"],
        "severity": {},
    }


def test_default_config_adds_no_lines() -> None:
    assert describe_config(ConfigSummary()) is None
    assert "config" not in to_text(REPORT).lower()
    assert "Config:" not in to_markdown(REPORT)
