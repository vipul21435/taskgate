from dataclasses import dataclass, field
from pathlib import Path

from taskfactory import LENIENT_GRADER, make_task
from taskgate.engine import run_gates
from taskgate.gates import (
    BUILTIN_GATES,
    Check,
    FunctionGate,
    Gate,
    TaskContext,
    gate,
    is_binary,
    missing_layout,
)
from taskgate.results import GateResult, Severity, Status
from taskgate.runner import RunResult


@dataclass
class FakeRunner:
    """Returns canned results and records every call."""

    with_solution: RunResult
    baseline: RunResult
    calls: list[tuple[Path, bool, float]] = field(default_factory=list)

    def run(self, task_dir: Path, *, with_solution: bool, timeout_sec: float) -> RunResult:
        self.calls.append((task_dir, with_solution, timeout_sec))
        return self.with_solution if with_solution else self.baseline


PASSED = RunResult(0, 0, timed_out=False, output="3 passed in 0.01s")
FAILED = RunResult(None, 1, timed_out=False, output="3 failed in 0.02s")


def by_code(results: tuple[GateResult, ...]) -> dict[str, GateResult]:
    return {result.code: result for result in results}


def test_builtin_gates_follow_the_protocol_and_run_in_code_order() -> None:
    codes = [g.code for g in BUILTIN_GATES]
    assert codes == sorted(set(codes))
    assert all(isinstance(g, Gate) for g in BUILTIN_GATES)
    assert all(g.fix_hint and g.summary for g in BUILTIN_GATES)


def test_good_task_passes_every_gate(tmp_path: Path) -> None:
    results = run_gates(make_task(tmp_path / "echo"))
    assert [(r.code, r.status) for r in results] == [
        ("TG101", Status.PASS),
        ("TG102", Status.PASS),
        ("TG401", Status.PASS),
        ("TG402", Status.PASS),
    ]
    assert all(r.fix_hint is None and not r.blocking for r in results)
    assert by_code(results)["TG401"].message == "reference solution passes the grader (1 passed)"
    assert by_code(results)["TG402"].message == "an untouched workspace fails the grader (1 failed)"


def test_lenient_grader_fails_the_baseline_gate(tmp_path: Path) -> None:
    results = by_code(run_gates(make_task(tmp_path / "echo", grader=LENIENT_GRADER)))
    assert results["TG401"].status is Status.PASS
    failed = results["TG402"]
    assert failed.status is Status.FAIL
    assert failed.blocking
    assert failed.message == "the grader passes an untouched workspace (1 skipped)"
    assert failed.fix_hint is not None


def test_incomplete_layout_skips_the_runtime_gates(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo", solve=None, grader=None)
    results = by_code(run_gates(task))
    assert results["TG101"].status is Status.FAIL
    assert results["TG101"].message == "missing solution/solve.sh, tests/test_*.py"
    for code in ("TG401", "TG402"):
        assert results[code].status is Status.SKIP
        assert results[code].message == "skipped: requires TG101 to pass"
        assert not results[code].blocking


def test_missing_directories_are_not_reported_twice(tmp_path: Path) -> None:
    task = tmp_path / "bare"
    task.mkdir()
    (task / "task.toml").write_text("[task]\n", encoding="utf-8")
    assert missing_layout(task) == ("instruction.md", "environment/", "solution/", "tests/")


def test_grader_files_named_with_a_test_suffix_count(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo", grader=None)
    (task / "tests" / "nested").mkdir()
    (task / "tests" / "nested" / "output_test.py").write_text("", encoding="utf-8")
    assert missing_layout(task) == ()


def test_manifest_problems_are_joined_and_do_not_block_runtime_gates(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo", task_id="other-name")
    runner = FakeRunner(with_solution=PASSED, baseline=FAILED)
    results = by_code(run_gates(task, runner=runner))
    assert results["TG102"].status is Status.FAIL
    assert "does not match the directory name 'echo'" in results["TG102"].message
    assert results["TG401"].status is Status.PASS
    assert results["TG402"].status is Status.PASS


def test_runner_gets_the_manifest_timeout(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo", timeout=42)
    runner = FakeRunner(with_solution=PASSED, baseline=FAILED)
    run_gates(task, runner=runner)
    assert runner.calls == [(task, True, 42), (task, False, 42)]


def test_solution_failures_are_described(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo")
    cases = {
        RunResult(None, None, timed_out=True, output=""): (
            "the solution run exceeded the 60s budget (task.timeout_sec)"
        ),
        RunResult(2, None, timed_out=False, output="boom"): "solution/solve.sh exited 2: boom",
        RunResult(0, 5, timed_out=False, output="no tests ran in 0.01s"): (
            "the grader collected no tests"
        ),
        RunResult(0, 1, timed_out=False, output="1 failed, 2 passed in 0.03s"): (
            "the grader failed (1 failed, 2 passed)"
        ),
    }
    for run, message in cases.items():
        results = by_code(run_gates(task, runner=FakeRunner(with_solution=run, baseline=FAILED)))
        assert results["TG401"].status is Status.FAIL
        assert results["TG401"].message == message


def test_baseline_failures_are_described(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo")
    cases = {
        RunResult(None, None, timed_out=True, output=""): (
            "the baseline run exceeded the 60s budget (task.timeout_sec)"
        ),
        RunResult(None, 5, timed_out=False, output="no tests ran"): (
            "the grader collected no tests"
        ),
    }
    for run, message in cases.items():
        results = by_code(run_gates(task, runner=FakeRunner(with_solution=PASSED, baseline=run)))
        assert results["TG402"].status is Status.FAIL
        assert results["TG402"].message == message


def ok(_ctx: TaskContext) -> Check:
    return Check.ok("fine")


def failing(_ctx: TaskContext) -> Check:
    return Check.fail("always fails")


def test_custom_gate_list_and_requirements(tmp_path: Path) -> None:
    gates = (
        FunctionGate("TG901", "first", Severity.WARNING, "s", "h", failing),
        FunctionGate("TG902", "second", Severity.INFO, "s", "h", ok, requires=("TG901",)),
        FunctionGate("TG903", "third", Severity.ERROR, "s", "h", ok, requires=("TG999",)),
    )
    results = run_gates(tmp_path, gates=gates)
    assert [(r.code, r.status) for r in results] == [
        ("TG901", Status.FAIL),
        ("TG902", Status.SKIP),
        ("TG903", Status.SKIP),
    ]
    assert not results[0].blocking
    assert results[0].fix_hint == "h"
    assert results[1].fix_hint is None


def test_a_gate_may_skip_itself(tmp_path: Path) -> None:
    @gate("TG904", "not-applicable", severity=Severity.ERROR, summary="s", fix_hint="h")
    def not_applicable(_ctx: TaskContext) -> Check:
        return Check.skip("nothing to lint")

    assert Check.ok("x").passed
    assert not Check.skip("x").passed
    (result,) = run_gates(tmp_path, gates=(not_applicable,))
    assert (result.status, result.message, result.blocking) == (
        Status.SKIP,
        "nothing to lint",
        False,
    )


def test_a_crashing_or_misbehaving_gate_fails_without_hiding_the_others(tmp_path: Path) -> None:
    def crash(_ctx: TaskContext) -> Check:
        raise ValueError("boom")

    def wrong_type(_ctx: TaskContext) -> Check:
        return "yes"  # type: ignore[return-value]

    gates = (
        FunctionGate("TG905", "crash", Severity.ERROR, "s", "h", crash),
        FunctionGate("TG906", "wrong-type", Severity.WARNING, "s", "h", wrong_type),
        FunctionGate("TG907", "fine", Severity.ERROR, "s", "h", ok),
    )
    results = by_code(run_gates(tmp_path, gates=gates))
    assert results["TG905"].message == "gate raised ValueError: boom"
    assert results["TG905"].blocking
    assert results["TG906"].message == "gate returned str, not a Check"
    assert results["TG907"].status is Status.PASS


def test_context_lists_task_files_without_caches_or_symlinks(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo")
    (task / "tests" / "__pycache__").mkdir()
    (task / "tests" / "__pycache__" / "x.pyc").write_bytes(b"\0")
    (task / "tests" / "stale.pyc").write_bytes(b"\0")
    (task / ".DS_Store").write_bytes(b"\0")
    (task / "link.txt").symlink_to(task / "instruction.md")
    ctx = TaskContext(task)
    assert [p.as_posix() for p in ctx.files] == [
        "environment/Dockerfile",
        "environment/workspace/input/name.txt",
        "instruction.md",
        "solution/solve.sh",
        "task.toml",
        "tests/test_outputs.py",
    ]
    assert ctx.files is ctx.files
    assert ctx.read_bytes(ctx.files[2]).startswith(b"Copy")


def test_binary_detection_follows_the_git_rule() -> None:
    assert is_binary(b"abc\0def")
    assert not is_binary(b"plain text\n")
    assert not is_binary(b"x" * 8000 + b"\0")
