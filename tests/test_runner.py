import os
import time
from pathlib import Path

import pytest

from taskfactory import (
    FAITHFUL_GRADER,
    HARD_LINK_SOLVE,
    LENIENT_GRADER,
    ORDER_DEPENDENT_GRADER,
    make_task,
)
from taskgate import snapshot
from taskgate.determinism import parse_junit
from taskgate.runner import (
    JUNIT_OVER_LIMIT,
    MAX_JUNIT_BYTES,
    LocalRunner,
    RunResult,
    Stub,
    execute,
    read_junit,
    seeded_env,
)


@pytest.fixture
def runner() -> LocalRunner:
    return LocalRunner()


def test_solution_run_passes_the_grader(tmp_path: Path, runner: LocalRunner) -> None:
    task = make_task(tmp_path / "echo")
    result = runner.run(task, solution="reference", timeout_sec=60)
    assert result.solution_exit == 0
    assert result.grader_exit == 0
    assert result.grader_passed
    assert not result.timed_out
    assert result.summary == "1 passed"
    assert result.created == ("output/greeting.txt",)
    assert result.error is None


def test_baseline_run_fails_the_grader(tmp_path: Path, runner: LocalRunner) -> None:
    task = make_task(tmp_path / "echo")
    result = runner.run(task, solution="none", timeout_sec=60)
    assert result.solution_exit is None
    assert result.grader_exit == 1
    assert result.summary == "1 failed"


def test_runs_leave_the_task_tree_untouched(tmp_path: Path, runner: LocalRunner) -> None:
    task = make_task(tmp_path / "echo")
    before = sorted(p.relative_to(task).as_posix() for p in task.rglob("*"))
    runner.run(task, solution="reference", timeout_sec=60)
    after = sorted(p.relative_to(task).as_posix() for p in task.rglob("*"))
    assert after == before


def test_lenient_grader_passes_an_empty_workspace(tmp_path: Path, runner: LocalRunner) -> None:
    task = make_task(tmp_path / "echo", grader=LENIENT_GRADER)
    result = runner.run(task, solution="none", timeout_sec=60)
    assert result.grader_exit == 0
    assert result.summary == "1 skipped"


def test_failing_solution_stops_before_the_grader(tmp_path: Path, runner: LocalRunner) -> None:
    task = make_task(tmp_path / "echo", solve="echo broken >&2\nexit 3\n")
    result = runner.run(task, solution="reference", timeout_sec=60)
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
    assert runner.run(task, solution="none", timeout_sec=60).grader_passed


def test_slow_solution_is_killed_at_the_deadline(tmp_path: Path, runner: LocalRunner) -> None:
    task = make_task(tmp_path / "echo", solve="sleep 30\n")
    result = runner.run(task, solution="reference", timeout_sec=0.5)
    assert result.timed_out
    assert result.solution_exit is None
    assert result.grader_exit is None


def test_slow_grader_is_killed_at_the_deadline(tmp_path: Path, runner: LocalRunner) -> None:
    grader = "import time\n\ndef test_slow() -> None:\n    time.sleep(30)\n"
    task = make_task(tmp_path / "echo", grader=grader)
    result = runner.run(task, solution="none", timeout_sec=1.5)
    assert result.timed_out
    assert result.grader_exit is None


def test_pytest_environment_variables_do_not_leak(
    tmp_path: Path, runner: LocalRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "--this-flag-does-not-exist")
    task = make_task(tmp_path / "echo")
    assert runner.run(task, solution="reference", timeout_sec=60).grader_passed


def test_summary_of_empty_output() -> None:
    result = RunResult(solution_exit=None, grader_exit=None, timed_out=True, output="\n\n")
    assert result.summary == "no output"


def test_local_runner_does_not_build_images(tmp_path: Path, runner: LocalRunner) -> None:
    built = runner.build(make_task(tmp_path / "echo"))
    assert built.ok
    assert built.image is None
    assert built.message == "not built: the local runner does not use Docker"
    assert runner.name == "local"


def test_created_files_leave_out_seeded_changed_and_cache_files(
    tmp_path: Path, runner: LocalRunner
) -> None:
    solve = (
        "echo changed > input/name.txt\n"
        "mkdir -p out/deep __pycache__\n"
        "echo x > out/deep/a.txt\n"
        "echo y > b.txt\n"
        "echo z > __pycache__/c.pyc\n"
    )
    grader = "def test_ok() -> None:\n    pass\n"
    task = make_task(tmp_path / "echo", solve=solve, grader=grader)
    result = runner.run(task, solution="reference", timeout_sec=60)
    assert result.created == ("b.txt", "out/deep/a.txt")


def test_stub_creates_the_files_empty(tmp_path: Path, runner: LocalRunner) -> None:
    exists_only = (
        "from pathlib import Path\n\n"
        "def test_exists() -> None:\n"
        "    assert Path('output/greeting.txt').is_file()\n"
    )
    stub = Stub(("output/greeting.txt",))
    strict = make_task(tmp_path / "strict")
    assert runner.run(strict, solution=stub, timeout_sec=60).summary == "1 failed"
    lenient = make_task(tmp_path / "lenient", grader=exists_only)
    result = runner.run(lenient, solution=stub, timeout_sec=60)
    assert result.grader_passed
    assert result.created == ("output/greeting.txt",)


def test_stub_script_quotes_paths_and_creates_parent_directories() -> None:
    script = Stub(("a b/c.txt", "top.txt", "a b/d/e.txt")).script()
    assert script.splitlines()[2:] == [
        "set -eu",
        "mkdir -p -- 'a b' 'a b/d'",
        ": > 'a b/c.txt'",
        ": > top.txt",
        ": > 'a b/d/e.txt'",
    ]
    assert Stub().script().splitlines()[-1] == "set -eu"


def test_execute_feeds_stdin_and_can_keep_stderr_apart() -> None:
    done = execute(
        ["sh", "-c", "cat; echo oops >&2"], timeout=30, stdin=b"piped\n", merge_stderr=False
    )
    assert (done.code, done.stdout, done.stderr) == (0, "piped\n", "oops\n")


def test_execute_calls_the_timeout_hook_before_killing() -> None:
    called: list[bool] = []
    done = execute(["sleep", "30"], timeout=0.2, on_timeout=lambda: called.append(True))
    assert done.code is None
    assert called == [True]


def test_a_run_that_ends_while_the_timeout_hook_runs_is_a_timeout() -> None:
    """The process exits during ``on_timeout``; on macOS ``killpg`` on its zombie-only
    group then fails with EPERM, which used to escape as an OSError."""
    done = execute(["sh", "-c", "sleep 0.3"], timeout=0.2, on_timeout=lambda: time.sleep(0.5))
    assert done.code is None


@pytest.mark.parametrize("error", [PermissionError, ProcessLookupError])
def test_a_group_that_cannot_be_signalled_at_the_deadline_is_a_timeout(
    monkeypatch: pytest.MonkeyPatch, error: type[OSError]
) -> None:
    real_killpg = os.killpg

    def killpg(pgid: int, sig: int) -> None:
        real_killpg(pgid, sig)
        raise error(1, "Operation not permitted")

    monkeypatch.setattr(os, "killpg", killpg)
    done = execute(["sleep", "30"], timeout=0.2)
    assert done.code is None


def test_tool_caches_are_not_copied_into_a_run(tmp_path: Path, runner: LocalRunner) -> None:
    """The local runner leaves out exactly what the on-disk walk (and so --all mode's
    hygiene gates) leaves out, so a run never uses content no gate has seen."""
    grader = (
        "from pathlib import Path\n\n"
        "def test_no_caches() -> None:\n"
        "    found = sorted(p.as_posix() for p in Path().rglob('*') if p.is_file())\n"
        "    assert found == ['input/name.txt', 'input/notes.txt']\n"
    )
    task = make_task(tmp_path / "echo", grader=grader)
    workspace = task / "environment" / "workspace" / "input"
    for relative in (".mypy_cache/x.json", ".ruff_cache/y", "__pycache__/z.pyc", ".DS_Store"):
        (workspace / relative).parent.mkdir(parents=True, exist_ok=True)
        (workspace / relative).write_text("cache\n", encoding="utf-8")
    (workspace / "notes.txt").write_text("kept\n", encoding="utf-8")
    result = runner.run(task, solution="none", timeout_sec=60)
    assert result.grader_passed, result.output


SEED_GRADER = """\
import os
import random
from pathlib import Path


def test_seeds_are_set() -> None:
    seed = os.environ["TASKGATE_SEED"]
    assert os.environ["PYTHONHASHSEED"] == seed
    assert random.random() == random.Random(int(seed)).random()


def test_each_rerun_starts_from_the_solved_workspace() -> None:
    assert Path("output/greeting.txt").read_text(encoding="utf-8") == "world\\n"
    marker = Path("output/graded.marker")
    assert not marker.exists()
    marker.write_text("x", encoding="utf-8")
"""
"""Passes only when the seeds are set, ``random`` is seeded with the run's seed and
every rerun gets a fresh copy of the solved workspace."""


def test_regrade_runs_the_solution_once_and_the_grader_per_seed(
    tmp_path: Path, runner: LocalRunner
) -> None:
    task = make_task(tmp_path / "echo", grader=SEED_GRADER)
    regraded = runner.regrade(task, seeds=(7, 8, 9), timeout_sec=60)
    assert regraded.solution.solution_exit == 0
    assert regraded.error is None
    assert [(run.seed, run.exit) for run in regraded.runs] == [(7, 0), (8, 0), (9, 0)]
    for run in regraded.runs:
        assert set(parse_junit(run.junit).values()) == {"passed"}, run.output


def test_regrade_shuffles_the_test_order_by_seed(tmp_path: Path, runner: LocalRunner) -> None:
    task = make_task(tmp_path / "echo", grader=ORDER_DEPENDENT_GRADER)
    regraded = runner.regrade(task, seeds=(4, 5), timeout_sec=60)
    orders = [list(parse_junit(run.junit)) for run in regraded.runs]
    assert orders == [
        [
            "tests/test_outputs.py::test_greeting_matches",
            "tests/test_outputs.py::test_output_is_read",
        ],
        [
            "tests/test_outputs.py::test_output_is_read",
            "tests/test_outputs.py::test_greeting_matches",
        ],
    ]
    assert [run.exit for run in regraded.runs] == [1, 0]


def test_regrade_stops_at_a_failing_solution(tmp_path: Path, runner: LocalRunner) -> None:
    task = make_task(tmp_path / "echo", solve="echo broken\nexit 3\n")
    regraded = runner.regrade(task, seeds=(1, 2), timeout_sec=60)
    assert (regraded.solution.solution_exit, regraded.solution.output) == (3, "broken")
    assert regraded.runs == ()


def test_regrade_stops_when_the_budget_runs_out(tmp_path: Path, runner: LocalRunner) -> None:
    grader = "import time\n\ndef test_slow() -> None:\n    time.sleep(30)\n"
    task = make_task(tmp_path / "echo", grader=grader)
    regraded = runner.regrade(task, seeds=(1, 2, 3), timeout_sec=0.5)
    assert [(run.seed, run.exit, run.junit) for run in regraded.runs] == [(1, None, "")]


def test_regrade_restores_a_directory_the_grader_locked(
    tmp_path: Path, runner: LocalRunner
) -> None:
    grader = (
        "from pathlib import Path\n\n"
        "def test_locks_a_directory() -> None:\n"
        "    assert not Path('locked').exists()\n"
        "    Path('locked').mkdir()\n"
        "    Path('locked/file').write_text('x')\n"
        "    Path('locked').chmod(0o500)\n"
    )
    task = make_task(tmp_path / "echo", grader=grader)
    regraded = runner.regrade(task, seeds=(1, 2, 3), timeout_sec=60)
    assert regraded.error is None
    assert [run.exit for run in regraded.runs] == [0, 0, 0]


def test_regrade_reports_a_workspace_it_cannot_restore_without_temp_paths(
    tmp_path: Path, runner: LocalRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    grader = (
        "from pathlib import Path\n\n"
        "def test_marks() -> None:\n"
        "    Path('output/graded.marker').write_text('x')\n"
    )
    task = make_task(tmp_path / "echo", grader=grader)
    real = os.unlink

    def unlink(path: object, *args: object, **kwargs: object) -> None:
        if str(path).endswith("graded.marker") and "dir_fd" not in kwargs:
            raise PermissionError(1, "Operation not permitted")
        real(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "unlink", unlink)
    regraded = runner.regrade(task, seeds=(1, 2), timeout_sec=60)
    assert [run.exit for run in regraded.runs] == [0]
    assert regraded.error == (
        "could not restore the workspace before rerun 2: "
        "output/graded.marker: Operation not permitted"
    )


def test_read_junit_skips_missing_and_oversized_files(tmp_path: Path) -> None:
    path = tmp_path / "junit.xml"
    assert read_junit(path) == ("", None)
    path.write_text("<testsuite/>", encoding="utf-8")
    assert read_junit(path) == ("<testsuite/>", None)
    with path.open("wb") as handle:
        handle.truncate(MAX_JUNIT_BYTES + 1)
    assert read_junit(path) == ("", JUNIT_OVER_LIMIT)
    assert JUNIT_OVER_LIMIT == "JUnit XML over the 8 MiB limit (not read)"


def test_seeded_env_puts_the_plugin_first_on_pythonpath(tmp_path: Path) -> None:
    env = seeded_env({"PYTHONPATH": "/x", "A": "b"}, 3, tmp_path)
    assert env == {
        "A": "b",
        "PYTHONHASHSEED": "3",
        "TASKGATE_SEED": "3",
        "PYTHONPATH": f"{tmp_path}:/x",
    }
    assert seeded_env({}, 0, tmp_path)["PYTHONPATH"] == str(tmp_path)


def test_regrade_reports_a_workspace_it_cannot_save_without_temp_paths(
    tmp_path: Path, runner: LocalRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    solve = "#!/bin/sh\nset -eu\nmkdir -p output/hidden\nchmod 000 output/hidden\n"
    task = make_task(tmp_path / "echo", solve=solve)
    real = os.geteuid()
    monkeypatch.setattr(snapshot.os, "geteuid", lambda: real + 1)
    regraded = runner.regrade(task, seeds=(1, 2), timeout_sec=60)
    assert regraded.solution.solution_exit == 0
    assert regraded.runs == ()
    assert regraded.error == (
        "could not save the solved workspace for the reruns: output/hidden: Permission denied"
    )


def test_every_rerun_sees_the_solved_workspace_exactly(tmp_path: Path, runner: LocalRunner) -> None:
    task = make_task(tmp_path / "echo", solve=HARD_LINK_SOLVE, grader=FAITHFUL_GRADER)
    regraded = runner.regrade(task, seeds=(1, 2, 3, 4, 5), timeout_sec=60)
    assert regraded.error is None
    outcomes = [parse_junit(run.junit) for run in regraded.runs]
    assert [run.exit for run in regraded.runs] == [0] * 5, regraded.runs[-1].output
    assert all(set(found.values()) == {"passed"} for found in outcomes)
    assert all(len(found) == 1 for found in outcomes)


def test_regrade_reports_junit_over_the_limit(tmp_path: Path, runner: LocalRunner) -> None:
    grader = (
        "def test_big(record_property) -> None:\n"
        f"    record_property('output', 'x' * ({MAX_JUNIT_BYTES} + 1))\n"
    )
    task = make_task(tmp_path / "echo", grader=grader)
    regraded = runner.regrade(task, seeds=(1,), timeout_sec=60)
    (run,) = regraded.runs
    assert (run.exit, run.junit, run.junit_problem) == (0, "", JUNIT_OVER_LIMIT)
