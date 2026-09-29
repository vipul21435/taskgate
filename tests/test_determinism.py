"""The determinism gate (TG501) on a stable, an order-dependent and a hash-seed-dependent
grader, plus the JUnit reading and flip logic behind it."""

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from typer.testing import CliRunner

from taskfactory import (
    HASH_ORDER_GRADER,
    HASH_ORDER_SOLVE,
    ORDER_DEPENDENT_GRADER,
    make_task,
)
from taskgate.cli import app
from taskgate.config import Config, DeterminismOptions
from taskgate.determinism import (
    NOT_RUN,
    Flip,
    JUnitError,
    Rerun,
    flips,
    node_id,
    parse_junit,
    rerun,
    runs_text,
)
from taskgate.engine import run_gates
from taskgate.gates import BUILTIN_GATES, TaskContext
from taskgate.gates.determinism import grader_deterministic
from taskgate.results import GateResult, Severity, Status
from taskgate.runner import BuildResult, GraderRun, Regrade, Runner, RunResult, Solution

GATES = tuple(g for g in BUILTIN_GATES if g.code in ("TG101", "TG401", "TG501"))
cli = CliRunner()


def tg501(task: Path, config: Config | None = None, runner: Runner | None = None) -> GateResult:
    results = run_gates(task, gates=GATES, config=config, runner=runner, label="tasks/x")
    return {r.code: r for r in results}["TG501"]


def test_the_gate_is_registered_as_a_blocking_determinism_gate() -> None:
    assert (grader_deterministic.code, grader_deterministic.name) == (
        "TG501",
        "grader-deterministic",
    )
    assert grader_deterministic.severity is Severity.ERROR
    assert grader_deterministic.requires == ("TG401",)
    assert grader_deterministic in BUILTIN_GATES


def test_a_stable_grader_passes_every_rerun(tmp_path: Path) -> None:
    result = tg501(make_task(tmp_path / "echo"))
    assert (result.status, result.message, result.details) == (
        Status.PASS,
        "the grader passed all 5 reruns with identical per-test outcomes "
        "(1 test, seeds 1-5, shuffled order)",
        (),
    )


def test_an_order_dependent_grader_fails_with_the_runs_and_seeds_that_flipped(
    tmp_path: Path,
) -> None:
    result = tg501(make_task(tmp_path / "echo", grader=ORDER_DEPENDENT_GRADER))
    assert result.status is Status.FAIL
    assert result.blocking
    assert result.message == (
        "4 of 5 reruns failed; 1 test flipped: tests/test_outputs.py::test_greeting_matches"
    )
    assert result.details == (
        "tests/test_outputs.py::test_greeting_matches: failed in runs 1, 2, 3, 4 "
        "(seeds 1, 2, 3, 4); passed in run 5 (seed 5); "
        "reproduce: taskgate grade tasks/x --seed 1 --runner local",
    )
    assert result.fix_hint is not None
    assert "TASKGATE_SEED" in result.fix_hint


def test_a_hash_seed_dependent_grader_fails_every_rerun(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo", solve=HASH_ORDER_SOLVE, grader=HASH_ORDER_GRADER)
    result = tg501(task)
    assert result.message == (
        "5 of 5 reruns failed; 1 test flipped: tests/test_outputs.py::test_words_in_order"
    )
    assert result.details == (
        "tests/test_outputs.py::test_words_in_order: failed in runs 1, 2, 3, 4, 5 "
        "(seeds 1, 2, 3, 4, 5) but the grader passed TG401's run (file order, "
        "PYTHONHASHSEED=0); reproduce: taskgate grade tasks/x --seed 1 --runner local",
    )


def test_runs_and_the_first_seed_come_from_the_config(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo", grader=ORDER_DEPENDENT_GRADER)
    config = Config(determinism=DeterminismOptions(runs=3, seed=5))
    result = tg501(task, config)
    assert result.message == (
        "1 of 3 reruns failed; 1 test flipped: tests/test_outputs.py::test_greeting_matches"
    )
    assert result.details[0].startswith(
        "tests/test_outputs.py::test_greeting_matches: passed in runs 1, 3 (seeds 5, 7); "
        "failed in run 2 (seed 6); reproduce: taskgate grade tasks/x --seed 6 "
    )
    stable = tg501(make_task(tmp_path / "stable"), config)
    assert stable.message.endswith("(1 test, seeds 5-7, shuffled order)")


@pytest.mark.parametrize(
    ("seed", "code", "outcome"),
    [(1, 1, "failed "), (5, 0, "passed ")],
)
def test_the_reproduce_command_repeats_the_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: int, code: int, outcome: str
) -> None:
    make_task(tmp_path / "tasks" / "x", grader=ORDER_DEPENDENT_GRADER)
    monkeypatch.chdir(tmp_path)
    result = cli.invoke(app, ["grade", "tasks/x", "--seed", str(seed), "--runner", "local"])
    assert result.exit_code == code, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == (
        f"taskgate grade tasks/x: reference solution, then the grader with seed {seed} "
        f"(shuffled order, PYTHONHASHSEED={seed}, TASKGATE_SEED={seed}), local runner"
    )
    assert f"{outcome}  tests/test_outputs.py::test_greeting_matches" in lines
    assert lines[-1] == ("result: PASS" if code == 0 else "result: FAIL (pytest exited 1)")


def test_grade_rejects_a_directory_without_a_manifest(tmp_path: Path) -> None:
    result = cli.invoke(app, ["grade", str(tmp_path), "--seed", "1", "--runner", "local"])
    assert result.exit_code == 2
    assert "is not a task directory (no task.toml)" in result.output
    negative = cli.invoke(app, ["grade", str(tmp_path), "--seed", "-1"])
    assert negative.exit_code == 2


def test_grade_reports_a_failing_solution(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo", solve="echo nope >&2\nexit 4\n")
    result = cli.invoke(app, ["grade", str(task), "--seed", "2", "--runner", "local"])
    assert result.exit_code == 1
    assert result.stdout.splitlines()[-2:] == [
        "nope",
        "result: FAIL (solution/solve.sh exited 4: nope)",
    ]


def test_grade_reports_a_grader_that_does_not_finish(tmp_path: Path) -> None:
    grader = "import time\n\ndef test_slow() -> None:\n    time.sleep(30)\n"
    task = make_task(tmp_path / "echo", grader=grader, timeout=1)
    result = cli.invoke(app, ["grade", str(task), "--seed", "2", "--runner", "local"])
    assert result.exit_code == 1
    assert result.stdout.splitlines()[-2:] == [
        "(no JUnit XML)",
        "result: FAIL (the grader did not finish within 2 x task.timeout_sec = 2 s)",
    ]


def test_grade_reports_a_solution_that_does_not_finish(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo", solve="sleep 30\n", timeout=1)
    result = cli.invoke(app, ["grade", str(task), "--seed", "2", "--runner", "local"])
    assert result.exit_code == 1
    assert result.stdout.splitlines()[-1] == (
        "result: FAIL (solve.sh did not finish within 2 x task.timeout_sec = 2 s)"
    )


# The gate on canned reruns: every way a rerun can go wrong.

PASSING_XML = (
    '<testsuites><testsuite><testcase classname="tests.test_a" name="test_one" '
    'file="tests/test_a.py"/></testsuite></testsuites>'
)


@dataclass
class RegradeRunner:
    """A runner whose regrade returns ``regrade`` and whose TG401 run passes."""

    regrade_result: Regrade
    seeds: list[tuple[int, ...]] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "canned"

    def build(self, task_dir: Path) -> BuildResult:
        return BuildResult(ok=True, message="canned")

    def run(self, task_dir: Path, *, solution: Solution, timeout_sec: float) -> RunResult:
        return RunResult(0, 0, timed_out=False, output="1 passed")

    def regrade(self, task_dir: Path, *, seeds: tuple[int, ...], timeout_sec: float) -> Regrade:
        self.seeds.append(seeds)
        return self.regrade_result


SOLVED = RunResult(0, None, timed_out=False, output="")


def canned(*runs: GraderRun, error: str | None = None) -> RegradeRunner:
    return RegradeRunner(Regrade(SOLVED, runs, error=error))


def run_ok(seed: int) -> GraderRun:
    return GraderRun(seed, 0, PASSING_XML, "1 passed")


def test_the_gate_asks_the_runner_for_the_configured_seeds(tmp_path: Path) -> None:
    runner = canned(*(run_ok(seed) for seed in range(1, 6)))
    result = tg501(make_task(tmp_path / "echo"), runner=runner)
    assert result.status is Status.PASS
    assert runner.seeds == [(1, 2, 3, 4, 5)]


def test_a_rerun_that_does_not_finish_stops_the_rest(tmp_path: Path) -> None:
    runner = canned(run_ok(1), run_ok(2), GraderRun(3, None, "", "still running"))
    result = tg501(make_task(tmp_path / "echo"), runner=runner)
    assert result.message == (
        "run 3 (seed 3) did not finish within the budget; 2 reruns did not start"
    )
    assert result.details == (
        "run 3 (seed 3): did not finish within the budget (6 x task.timeout_sec = 360 s); "
        "reproduce: taskgate grade tasks/x --seed 3 --runner canned",
    )


def test_a_rerun_without_junit_and_a_failure_without_a_failing_test(tmp_path: Path) -> None:
    runner = canned(
        run_ok(1),
        GraderRun(2, 4, "", "usage error"),
        GraderRun(3, 3, PASSING_XML, "INTERNALERROR"),
        run_ok(4),
        run_ok(5),
    )
    result = tg501(make_task(tmp_path / "echo"), runner=runner)
    assert result.message == "2 of 5 reruns failed; run 2 (seed 2) left no readable JUnit XML"
    assert result.details == (
        "run 3 (seed 3): pytest exited 3 with no failing test; "
        "reproduce: taskgate grade tasks/x --seed 3 --runner canned",
        "run 2 (seed 2): pytest exited 4 with no JUnit XML; "
        "reproduce: taskgate grade tasks/x --seed 2 --runner canned",
    )


def test_many_flipped_tests_are_capped_in_the_message(tmp_path: Path) -> None:
    names = [f"test_{n}" for n in range(5)]
    cases = "".join(f'<testcase classname="t" name="{n}"/>' for n in names)
    broken = "".join(f'<testcase classname="t" name="{n}"><error/></testcase>' for n in names)
    runner = canned(
        GraderRun(1, 0, f"<testsuite>{cases}</testsuite>", ""),
        GraderRun(2, 1, f"<testsuite>{broken}</testsuite>", ""),
    )
    config = Config(determinism=DeterminismOptions(runs=2))
    result = tg501(make_task(tmp_path / "echo"), config, runner=runner)
    assert result.message == (
        "1 of 2 reruns failed; 5 tests flipped: t::test_0, t::test_1, t::test_2 and 2 more"
    )
    assert len(result.details) == 5


def test_a_runner_error_between_reruns_fails_the_gate(tmp_path: Path) -> None:
    runner = canned(run_ok(1), error="could not restore the workspace before rerun 2")
    result = tg501(make_task(tmp_path / "echo"), runner=runner)
    assert result.message == (
        "the runner stopped after 1 rerun: could not restore the workspace before rerun 2"
    )


@pytest.mark.parametrize(
    ("solution", "message"),
    [
        (
            RunResult(None, None, timed_out=True, output=""),
            "the reference solution, run again for the reruns, did not finish "
            "(6 x task.timeout_sec = 360 s)",
        ),
        (
            RunResult(2, None, timed_out=False, output="flaky solve"),
            "the reference solution, run again for the reruns, failed: "
            "solution/solve.sh exited 2: flaky solve",
        ),
        (
            RunResult(None, None, timed_out=False, output="", error="container exited 137"),
            "the reference solution, run again for the reruns, could not run: container exited 137",
        ),
    ],
)
def test_a_solution_that_fails_on_the_second_run_fails_the_gate(
    tmp_path: Path, solution: RunResult, message: str
) -> None:
    runner = RegradeRunner(Regrade(solution))
    result = tg501(make_task(tmp_path / "echo"), runner=runner)
    assert (result.status, result.message) == (Status.FAIL, message)


def test_the_context_regrades_once_per_set_of_seeds(tmp_path: Path) -> None:
    runner = canned(run_ok(1), run_ok(2))
    ctx = TaskContext(make_task(tmp_path / "echo"), runner=runner)
    assert ctx.regrade([1, 2]) is ctx.regrade((1, 2))
    assert runner.seeds == [(1, 2)]
    assert ctx.path_label == "echo"


def test_grade_reports_a_runner_that_stopped_before_the_grader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = canned(error="could not save the solved workspace for the reruns")
    monkeypatch.setattr("taskgate.cli.select_runner", lambda choice, options: (runner, None))
    result = cli.invoke(app, ["grade", str(make_task(tmp_path / "echo")), "--seed", "3"])
    assert result.exit_code == 1
    assert result.stdout.splitlines()[-1] == (
        "result: FAIL (could not save the solved workspace for the reruns)"
    )
    assert result.stdout.splitlines()[0].endswith("canned runner")
    monkeypatch.setattr(
        "taskgate.cli.select_runner", lambda choice, options: (RegradeRunner(Regrade(SOLVED)), None)
    )
    result = cli.invoke(app, ["grade", str(make_task(tmp_path / "echo")), "--seed", "3"])
    assert result.stdout.splitlines()[-1] == "result: FAIL (the grader did not run)"


def test_the_gate_skips_when_the_reference_solution_fails(tmp_path: Path) -> None:
    result = tg501(make_task(tmp_path / "echo", solve="exit 1\n"))
    assert (result.status, result.message) == (Status.SKIP, "skipped: requires TG401 to pass")


# JUnit XML and flips.


def test_parse_junit_rebuilds_pytest_ids_and_outcomes() -> None:
    xml = """<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite>
    <testcase classname="tests.test_a.TestK" name="test_m" file="tests/test_a.py"/>
    <testcase classname="tests.test_a" name="test_p[a b]" file="tests/test_a.py"/>
    <testcase classname="tests.test_a" name="test_skip" file="tests/test_a.py">
      <skipped type="pytest.skip" message="nope"/></testcase>
    <testcase classname="tests.test_a" name="test_err" file="tests/test_a.py">
      <error message="setup"/></testcase>
    <testcase classname="tests.test_a" name="test_fail" file="tests/test_a.py">
      <failure message="x"/></testcase>
    <testcase classname="tests.test_a" name="test_fail" file="tests/test_a.py">
      <error message="teardown"/></testcase>
    <testcase classname="tests.test_a" name="test_skip" file="tests/test_a.py"/>
    <testcase classname="" name="tests.test_b" file="tests/test_b.py">
      <error message="collection failure"/></testcase>
    </testsuite></testsuites>"""
    assert parse_junit(xml) == {
        "tests/test_a.py::TestK::test_m": "passed",
        "tests/test_a.py::test_p[a b]": "passed",
        "tests/test_a.py::test_skip": "skipped",
        "tests/test_a.py::test_err": "error",
        "tests/test_a.py::test_fail": "error",
        "tests/test_b.py": "error",
    }


@pytest.mark.parametrize(
    ("file", "classname", "name", "expected"),
    [
        (None, "tests.test_a", "test_x", "tests.test_a::test_x"),
        ("tests/helpers.py", "tests.test_a", "test_x", "tests.test_a::test_x"),
        ("conftest", "", "test_x", "test_x"),
        ("tests/test_a.py", "", "other", "other"),
    ],
)
def test_node_id_falls_back_to_the_classname(
    file: str | None, classname: str, name: str, expected: str
) -> None:
    assert node_id(file, classname, name) == expected


def test_parse_junit_refuses_missing_broken_and_hostile_xml() -> None:
    with pytest.raises(JUnitError, match="no JUnit XML"):
        parse_junit("  \n")
    with pytest.raises(JUnitError, match="unreadable JUnit XML"):
        parse_junit("<testsuite><testcase")
    levels = ['<!ENTITY a "aaaaaaaaaa">'] + [
        f'<!ENTITY {b} "{("&" + a + ";") * 10}">' for a, b in zip("abcdefg", "bcdefgh", strict=True)
    ]
    bomb = f'<?xml version="1.0"?><!DOCTYPE t [{"".join(levels)}]><t>&h;</t>'
    with pytest.raises(JUnitError, match="amplification"):
        parse_junit(bomb)
    external = (
        '<?xml version="1.0"?><!DOCTYPE t [<!ENTITY x SYSTEM "file:///etc/passwd">]><t>&x;</t>'
    )
    with pytest.raises(JUnitError, match="undefined entity"):
        parse_junit(external)


def test_rerun_reads_outcomes_or_says_why_not() -> None:
    assert rerun(1, GraderRun(4, 0, PASSING_XML, "")) == Rerun(
        1, 4, 0, {"tests/test_a.py::test_one": "passed"}
    )
    assert rerun(2, GraderRun(5, None, "", "")).problem == "did not finish within the budget"
    assert rerun(3, GraderRun(6, 2, "<x", "")).problem == (
        "pytest exited 2 with unreadable JUnit XML (unclosed token: line 1, column 0)"
    )


def outcomes(number: int, **tests: str) -> Rerun:
    return Rerun(number, number + 10, 0, tests)


def test_flips_group_outcomes_and_pick_the_first_bad_run() -> None:
    reruns = [
        outcomes(1, a="passed", b="passed", c="skipped", d="failed"),
        outcomes(2, a="passed", b="failed", c="skipped", d="failed"),
        outcomes(3, a="passed", c="skipped", d="failed"),
        Rerun(4, 14, None, {}, "did not finish within the budget"),
    ]
    found = flips(reruns)
    assert [flip.test for flip in found] == ["b", "d"]
    b, d = found
    assert b.groups == (
        ("passed", (reruns[0],)),
        ("failed", (reruns[1],)),
        (NOT_RUN, (reruns[2],)),
    )
    assert b.reproduce is reruns[1]
    assert b.describe() == (
        "b: passed in run 1 (seed 11); failed in run 2 (seed 12); not run in run 3 (seed 13)"
    )
    assert d.reproduce is reruns[0]
    assert d.describe().startswith("d: failed in runs 1, 2, 3 (seeds 11, 12, 13) but")


def test_a_test_skipped_in_some_runs_flips_too() -> None:
    reruns = [outcomes(1, a="skipped"), outcomes(2, a="passed")]
    (flip,) = flips(reruns)
    assert flip == Flip("a", (("skipped", (reruns[0],)), ("passed", (reruns[1],))))
    assert flip.reproduce is reruns[0]


def test_runs_text_is_singular_or_plural() -> None:
    assert runs_text([outcomes(2)]) == "run 2 (seed 12)"
    assert runs_text([outcomes(2), outcomes(5)]) == "runs 2, 5 (seeds 12, 15)"
