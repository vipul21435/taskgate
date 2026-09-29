"""The determinism gate (TG501): the grader must give the same answer every time.

It runs the reference solution once more, then the grader ``[determinism] runs``
times (default 5) on identical copies of that output. Rerun ``i`` uses seed
``seed + i - 1``: the bundled pytest plugin shuffles the test order with it, and
it is also ``PYTHONHASHSEED`` (set ordering, ``dict`` of ``set`` output) and
``TASKGATE_SEED`` (for graders that draw random cases). Per-test outcomes come
from pytest's JUnit XML. The gate fails when any rerun fails, when any test's
outcome differs between reruns, or when a test fails in every rerun (TG401's
run, in file order with ``PYTHONHASHSEED=0``, passed), and it lists each flipped
test with the runs and seeds where it flipped and a ``taskgate grade`` command
that reruns the first failing one.
"""

from __future__ import annotations

import shlex

from taskgate.determinism import ERROR, FAILED, Flip, Rerun, count_tests, flips, rerun
from taskgate.gates.base import Check, TaskContext, gate
from taskgate.results import Severity

LISTED = 3
"""How many flipped tests the one-line message names before it says "and N more"."""


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def reproduce(ctx: TaskContext, run: Rerun) -> str:
    """The command that repeats ``run``: the reference solution, then the grader with its seed."""
    return (
        f"taskgate grade {shlex.quote(ctx.path_label)} --seed {run.seed} --runner {ctx.runner.name}"
    )


def _flip_names(found: tuple[Flip, ...]) -> str:
    names = ", ".join(flip.test for flip in found[:LISTED])
    more = f" and {len(found) - LISTED} more" if len(found) > LISTED else ""
    return f"{_plural(len(found), 'test')} flipped: {names}{more}"


@gate(
    "TG501",
    "grader-deterministic",
    severity=Severity.ERROR,
    summary=(
        "the grader gives the same verdict and per-test outcomes on N reruns "
        "with shuffled test order and new seeds"
    ),
    fix_hint=(
        "Make each test independent of test order, hash order and chance: no state "
        "shared between tests, sort sets and dict keys before comparing or printing, "
        "and seed any random generator from TASKGATE_SEED. Run the reproduce command "
        "from the directory the task paths are relative to."
    ),
    requires=("TG401",),
)
def grader_deterministic(ctx: TaskContext) -> Check:
    seeds = ctx.config.determinism.seeds
    timeout = ctx.manifest.timeout_sec
    budget = f"{len(seeds) + 1} x task.timeout_sec = {timeout * (len(seeds) + 1)} s"
    regraded = ctx.regrade(seeds)
    solution = regraded.solution
    again = "the reference solution, run again for the reruns,"
    if solution.timed_out:
        return Check.fail(f"{again} did not finish ({budget})")
    if solution.error is not None:
        return Check.fail(f"{again} could not run: {solution.error}")
    if solution.solution_exit != 0:
        exited = f"solution/solve.sh exited {solution.solution_exit}: {solution.summary}"
        return Check.fail(f"{again} failed: {exited}")

    reruns = [rerun(number, run) for number, run in enumerate(regraded.runs, start=1)]
    found = flips(reruns)
    problems: list[str] = []
    details = [f"{flip.describe()}; reproduce: {reproduce(ctx, flip.reproduce)}" for flip in found]

    failing = [run for run in reruns if run.failing]
    if failing:
        problems.append(f"{len(failing)} of {len(seeds)} reruns failed")
    unexplained = [
        run
        for run in failing
        if run.problem is None and not {FAILED, ERROR} & set(run.outcomes.values())
    ]
    details += [
        f"{run.label}: pytest exited {run.exit} with no failing test; "
        f"reproduce: {reproduce(ctx, run)}"
        for run in unexplained
    ]
    for run in reruns:
        if run.problem is not None:
            if run.exit is None:
                problems.append(f"{run.label} did not finish within the budget")
                detail = f"{run.problem} ({budget})"
            else:
                problems.append(f"{run.label} left no readable JUnit XML")
                detail = run.problem
            details.append(f"{run.label}: {detail}; reproduce: {reproduce(ctx, run)}")
    missing = len(seeds) - len(reruns)
    if missing and regraded.error is None:
        problems.append(f"{_plural(missing, 'rerun')} did not start")
    if regraded.error is not None:
        stopped = _plural(len(reruns), "rerun")
        problems.append(f"the runner stopped after {stopped}: {regraded.error}")
    if found:
        problems.append(_flip_names(found))
    if problems:
        return Check.fail("; ".join(problems), details)
    return Check.ok(
        f"the grader passed all {len(reruns)} reruns with identical per-test outcomes "
        f"({_plural(count_tests(reruns), 'test')}, seeds {seeds[0]}-{seeds[-1]}, shuffled order)"
    )


DETERMINISM_GATES = (grader_deterministic,)
