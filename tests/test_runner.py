from pathlib import Path

import pytest

from taskfactory import LENIENT_GRADER, make_task
from taskgate.runner import LocalRunner, RunResult


@pytest.fixture
def runner() -> LocalRunner:
    return LocalRunner()


def test_solution_run_passes_the_grader(tmp_path: Path, runner: LocalRunner) -> None:
    task = make_task(tmp_path / "echo")
    result = runner.run(task, with_solution=True, timeout_sec=60)
    assert result.solution_exit == 0
    assert result.grader_exit == 0
    assert result.grader_passed
    assert not result.timed_out
    assert result.summary == "1 passed"


def test_baseline_run_fails_the_grader(tmp_path: Path, runner: LocalRunner) -> None:
    task = make_task(tmp_path / "echo")
    result = runner.run(task, with_solution=False, timeout_sec=60)
    assert result.solution_exit is None
    assert result.grader_exit == 1
    assert result.summary == "1 failed"


def test_runs_leave_the_task_tree_untouched(tmp_path: Path, runner: LocalRunner) -> None:
    task = make_task(tmp_path / "echo")
    before = sorted(p.relative_to(task).as_posix() for p in task.rglob("*"))
    runner.run(task, with_solution=True, timeout_sec=60)
    after = sorted(p.relative_to(task).as_posix() for p in task.rglob("*"))
    assert after == before


def test_lenient_grader_passes_an_empty_workspace(tmp_path: Path, runner: LocalRunner) -> None:
    task = make_task(tmp_path / "echo", grader=LENIENT_GRADER)
    result = runner.run(task, with_solution=False, timeout_sec=60)
    assert result.grader_exit == 0
    assert result.summary == "1 skipped"


def test_failing_solution_stops_before_the_grader(tmp_path: Path, runner: LocalRunner) -> None:
    task = make_task(tmp_path / "echo", solve="echo broken >&2\nexit 3\n")
    result = runner.run(task, with_solution=True, timeout_sec=60)
    assert result.solution_exit == 3
    assert result.grader_exit is None
    assert result.summary == "broken"


def test_task_without_a_workspace_seed_gets_an_empty_one(
    tmp_path: Path, runner: LocalRunner
) -> None:
    grader = (
        "from pathlib import Path\n\n"
        "def test_empty() -> None:\n"
        "    assert not any(Path().iterdir())\n"
    )
    task = make_task(tmp_path / "echo", workspace=False, grader=grader)
    assert runner.run(task, with_solution=False, timeout_sec=60).grader_passed


def test_slow_solution_is_killed_at_the_deadline(tmp_path: Path, runner: LocalRunner) -> None:
    task = make_task(tmp_path / "echo", solve="sleep 30\n")
    result = runner.run(task, with_solution=True, timeout_sec=0.5)
    assert result.timed_out
    assert result.solution_exit is None
    assert result.grader_exit is None


def test_slow_grader_is_killed_at_the_deadline(tmp_path: Path, runner: LocalRunner) -> None:
    grader = "import time\n\ndef test_slow() -> None:\n    time.sleep(30)\n"
    task = make_task(tmp_path / "echo", grader=grader)
    result = runner.run(task, with_solution=False, timeout_sec=1.5)
    assert result.timed_out
    assert result.grader_exit is None


def test_pytest_environment_variables_do_not_leak(
    tmp_path: Path, runner: LocalRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "--this-flag-does-not-exist")
    task = make_task(tmp_path / "echo")
    assert runner.run(task, with_solution=True, timeout_sec=60).grader_passed


def test_summary_of_empty_output() -> None:
    result = RunResult(solution_exit=None, grader_exit=None, timed_out=True, output="\n\n")
    assert result.summary == "no output"
