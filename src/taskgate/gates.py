"""Review gates: stable codes, severities, fix hints and the checks behind them.

Codes are ``TG`` plus three digits, grouped by hundreds and never reused:
TG1xx layout and manifest, TG2xx hygiene, TG3xx environment, TG4xx solution and
baselines, TG5xx determinism, TG6xx cheat probes. A gate may require other
gates; when one of those did not pass, the gate is reported as skipped instead
of producing a second, misleading failure.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from taskgate import manifest
from taskgate.layout import missing_parts
from taskgate.results import GateResult, Severity, Status
from taskgate.runner import PYTEST_NO_TESTS, LocalRunner, Runner, RunResult

SOLUTION_ENTRY = "solution/solve.sh"
GRADER_GLOBS: tuple[str, ...] = ("test_*.py", "*_test.py")


@dataclass
class TaskContext:
    """Everything a gate may look at for one task. Parsed data is computed once."""

    task_dir: Path
    runner: Runner = field(default_factory=LocalRunner)
    _manifest: manifest.ManifestCheck | None = None

    @property
    def manifest(self) -> manifest.ManifestCheck:
        if self._manifest is None:
            self._manifest = manifest.load(self.task_dir)
        return self._manifest

    def run(self, *, with_solution: bool) -> RunResult:
        return self.runner.run(
            self.task_dir, with_solution=with_solution, timeout_sec=self.manifest.timeout_sec
        )


@dataclass(frozen=True, slots=True)
class Check:
    """What a gate's check function returns: pass or fail, and why."""

    passed: bool
    message: str


@dataclass(frozen=True, slots=True)
class Gate:
    """A review gate with a stable code."""

    code: str
    name: str
    severity: Severity
    summary: str
    fix_hint: str
    check: Callable[[TaskContext], Check]
    requires: tuple[str, ...] = ()


def missing_layout(task_dir: Path) -> tuple[str, ...]:
    """Layout parts plus the solution entry point and at least one grader test file."""
    missing = list(missing_parts(task_dir))
    if "solution/" not in missing and not (task_dir / SOLUTION_ENTRY).is_file():
        missing.append(SOLUTION_ENTRY)
    tests = task_dir / "tests"
    if "tests/" not in missing and not any(
        path.is_file() for pattern in GRADER_GLOBS for path in tests.rglob(pattern)
    ):
        missing.append("tests/test_*.py")
    return tuple(missing)


def check_layout(ctx: TaskContext) -> Check:
    missing = missing_layout(ctx.task_dir)
    if missing:
        return Check(False, f"missing {', '.join(missing)}")
    return Check(True, "layout complete")


def check_manifest(ctx: TaskContext) -> Check:
    problems = ctx.manifest.problems
    if problems:
        return Check(False, "; ".join(problems))
    return Check(True, "manifest valid")


def _run_failure(run: RunResult, what: str, timeout_sec: int) -> str:
    if run.timed_out:
        return f"{what} exceeded the {timeout_sec}s budget (task.timeout_sec)"
    if run.solution_exit not in (None, 0):
        return f"solution/solve.sh exited {run.solution_exit}: {run.summary}"
    if run.grader_exit == PYTEST_NO_TESTS:
        return "the grader collected no tests"
    return f"the grader failed ({run.summary})"


def check_solution_passes(ctx: TaskContext) -> Check:
    run = ctx.run(with_solution=True)
    if run.grader_passed:
        return Check(True, f"reference solution passes the grader ({run.summary})")
    return Check(False, _run_failure(run, "the solution run", ctx.manifest.timeout_sec))


def check_baseline_fails(ctx: TaskContext) -> Check:
    run = ctx.run(with_solution=False)
    if run.timed_out:
        return Check(False, _run_failure(run, "the baseline run", ctx.manifest.timeout_sec))
    if run.grader_passed:
        return Check(False, f"the grader passes an untouched workspace ({run.summary})")
    if run.grader_exit == PYTEST_NO_TESTS:
        return Check(False, _run_failure(run, "the baseline run", ctx.manifest.timeout_sec))
    return Check(True, f"an untouched workspace fails the grader ({run.summary})")


GATES: tuple[Gate, ...] = (
    Gate(
        code="TG101",
        name="layout-complete",
        severity=Severity.ERROR,
        summary="task.toml, instruction.md, environment/, solution/solve.sh and tests/test_*.py",
        fix_hint="Add the missing parts; the layout is described in docs/task-layout.md.",
        check=check_layout,
    ),
    Gate(
        code="TG102",
        name="manifest-valid",
        severity=Severity.ERROR,
        summary="task.toml parses and has every required key with a valid value",
        fix_hint="Fix task.toml to match the manifest schema in docs/task-layout.md.",
        check=check_manifest,
    ),
    Gate(
        code="TG401",
        name="solution-passes",
        severity=Severity.ERROR,
        summary="the reference solution passes the grader in a fresh workspace",
        fix_hint=(
            "Run solution/solve.sh in a fresh copy of environment/workspace, then "
            "python -m pytest tests/ from that directory, and fix whichever step fails."
        ),
        check=check_solution_passes,
        requires=("TG101",),
    ),
    Gate(
        code="TG402",
        name="baseline-fails",
        severity=Severity.ERROR,
        summary="the grader fails when no solution has run (untouched workspace)",
        fix_hint=(
            "Make the grader assert on the output the instruction asks for, so a "
            "workspace where nothing was done cannot pass (no skips or early returns)."
        ),
        check=check_baseline_fails,
        requires=("TG101",),
    ),
)


def run_gates(
    task_dir: Path, *, runner: Runner | None = None, gates: tuple[Gate, ...] = GATES
) -> tuple[GateResult, ...]:
    """Run ``gates`` in order on ``task_dir`` and return one result per gate."""
    ctx = TaskContext(task_dir=task_dir, runner=runner or LocalRunner())
    status: dict[str, Status] = {}
    results: list[GateResult] = []
    for gate in gates:
        unmet = [code for code in gate.requires if status.get(code) is not Status.PASS]
        if unmet:
            result = GateResult(
                code=gate.code,
                name=gate.name,
                severity=gate.severity,
                status=Status.SKIP,
                message=f"skipped: requires {', '.join(unmet)} to pass",
            )
        else:
            outcome = gate.check(ctx)
            result = GateResult(
                code=gate.code,
                name=gate.name,
                severity=gate.severity,
                status=Status.PASS if outcome.passed else Status.FAIL,
                message=outcome.message,
                fix_hint=None if outcome.passed else gate.fix_hint,
            )
        status[gate.code] = result.status
        results.append(result)
    return tuple(results)
