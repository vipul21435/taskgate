"""Core gates: the layout and manifest schema (TG101, TG102) and the runtime checks
that the reference solution passes (TG401), an untouched workspace fails (TG402)
and a stub that creates the reference solution's new files, empty, fails (TG403).
"""

from __future__ import annotations

from pathlib import Path

from taskgate.gates.base import Check, TaskContext, gate
from taskgate.layout import missing_parts
from taskgate.results import Severity
from taskgate.runner import PYTEST_NO_TESTS, RunResult, Stub

SOLUTION_ENTRY = "solution/solve.sh"
GRADER_GLOBS: tuple[str, ...] = ("test_*.py", "*_test.py")


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


@gate(
    "TG101",
    "layout-complete",
    severity=Severity.ERROR,
    summary="task.toml, instruction.md, environment/, solution/solve.sh and tests/test_*.py",
    fix_hint="Add the missing parts; the layout is described in docs/task-layout.md.",
)
def layout_complete(ctx: TaskContext) -> Check:
    missing = missing_layout(ctx.task_dir)
    if missing:
        return Check.fail(f"missing {', '.join(missing)}")
    return Check.ok("layout complete")


@gate(
    "TG102",
    "manifest-valid",
    severity=Severity.ERROR,
    summary="task.toml parses and has every required key with a valid value",
    fix_hint="Fix task.toml to match the manifest schema in docs/task-layout.md.",
)
def manifest_valid(ctx: TaskContext) -> Check:
    problems = ctx.manifest.problems
    if problems:
        return Check.fail("; ".join(problems))
    return Check.ok("manifest valid")


NOT_BUILT = "skipped: the environment did not build (see TG301)"


def run_failure(run: RunResult, what: str, timeout_sec: int) -> str:
    if run.error is not None:
        return f"{what} could not run: {run.error}"
    if run.timed_out:
        return f"{what} exceeded the {timeout_sec}s budget (task.timeout_sec)"
    if run.solution_exit not in (None, 0):
        return f"solution/solve.sh exited {run.solution_exit}: {run.summary}"
    if run.grader_exit == PYTEST_NO_TESTS:
        return "the grader collected no tests"
    return f"the grader failed ({run.summary})"


@gate(
    "TG401",
    "solution-passes",
    severity=Severity.ERROR,
    summary="the reference solution passes the grader in a fresh workspace",
    fix_hint=(
        "Run solution/solve.sh in a fresh copy of environment/workspace, then "
        "python -m pytest tests/ from that directory, and fix whichever step fails."
    ),
    requires=("TG101",),
)
def solution_passes(ctx: TaskContext) -> Check:
    if not ctx.build().ok:
        return Check.skip(NOT_BUILT)
    run = ctx.run("reference")
    if run.grader_passed:
        return Check.ok(f"reference solution passes the grader ({run.summary})")
    return Check.fail(run_failure(run, "the solution run", ctx.manifest.timeout_sec))


@gate(
    "TG402",
    "baseline-fails",
    severity=Severity.ERROR,
    summary="the grader fails when no solution has run (untouched workspace)",
    fix_hint=(
        "Make the grader assert on the output the instruction asks for, so a "
        "workspace where nothing was done cannot pass (no skips or early returns)."
    ),
    requires=("TG101",),
)
def baseline_fails(ctx: TaskContext) -> Check:
    if not ctx.build().ok:
        return Check.skip(NOT_BUILT)
    run = ctx.run("none")
    if run.error is not None or run.timed_out or run.grader_exit == PYTEST_NO_TESTS:
        return Check.fail(run_failure(run, "the baseline run", ctx.manifest.timeout_sec))
    if run.grader_passed:
        return Check.fail(f"the grader passes an untouched workspace ({run.summary})")
    return Check.ok(f"an untouched workspace fails the grader ({run.summary})")


STUB_LISTED = 3


def _stub_text(files: tuple[str, ...]) -> str:
    if not files:
        return "a no-op stub"
    if len(files) == 1:
        return f"a stub that writes {files[0]} empty"
    shown = ", ".join(files[:STUB_LISTED])
    more = f" and {len(files) - STUB_LISTED} more" if len(files) > STUB_LISTED else ""
    return f"a stub that writes {len(files)} files empty ({shown}{more})"


@gate(
    "TG403",
    "stub-solution-fails",
    severity=Severity.ERROR,
    summary="a stub that writes the reference solution's new files, empty, fails the grader",
    fix_hint=(
        "Make the grader check what each output contains, not only that it exists: "
        "compare exact content or line counts (zip(..., strict=True)), so empty "
        "placeholder files cannot pass."
    ),
    requires=("TG401",),
)
def stub_solution_fails(ctx: TaskContext) -> Check:
    created = ctx.run("reference").created
    run = ctx.run(Stub(created))
    stub = _stub_text(created)
    if run.error is None and not run.timed_out and run.solution_exit not in (None, 0):
        return Check.fail(f"the stub solution exited {run.solution_exit}: {run.summary}")
    if run.error is not None or run.timed_out or run.grader_exit == PYTEST_NO_TESTS:
        return Check.fail(run_failure(run, "the stub run", ctx.manifest.timeout_sec))
    if run.grader_passed:
        return Check.fail(f"the grader passes {stub} ({run.summary})")
    return Check.ok(f"{stub} fails the grader ({run.summary})")


CORE_GATES = (
    layout_complete,
    manifest_valid,
    solution_passes,
    baseline_fails,
    stub_solution_fails,
)
