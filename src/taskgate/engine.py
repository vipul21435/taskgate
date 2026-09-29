"""Run a sequence of gates on one task directory and collect their results.

Gates run in the order given (the registry sorts them by code). A gate whose
``requires`` did not all pass is reported as skipped instead of producing a
second, misleading failure. A gate that raises is reported as a failure with
the exception, so one broken third-party gate cannot hide the other results.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from taskgate.gates import BUILTIN_GATES, Check, Gate, TaskContext
from taskgate.results import GateResult, Status
from taskgate.runner import LocalRunner, Runner


def _check(gate: Gate, ctx: TaskContext) -> Check:
    try:
        outcome = gate.check(ctx)
    except Exception as exc:  # a gate bug must not abort the other gates
        return Check.fail(f"gate raised {type(exc).__name__}: {exc}")
    if not isinstance(outcome, Check):
        return Check.fail(f"gate returned {type(outcome).__name__}, not a Check")
    return outcome


def run_gates(
    task_dir: Path,
    *,
    gates: Sequence[Gate] = BUILTIN_GATES,
    runner: Runner | None = None,
) -> tuple[GateResult, ...]:
    """Run ``gates`` in order on ``task_dir`` and return one result per gate."""
    ctx = TaskContext(task_dir=task_dir, runner=runner or LocalRunner())
    status: dict[str, Status] = {}
    results: list[GateResult] = []
    for gate in gates:
        unmet = [code for code in gate.requires if status.get(code) is not Status.PASS]
        outcome = (
            Check.skip(f"skipped: requires {', '.join(unmet)} to pass")
            if unmet
            else _check(gate, ctx)
        )
        result = GateResult(
            code=gate.code,
            name=gate.name,
            severity=gate.severity,
            status=outcome.status,
            message=outcome.message,
            fix_hint=gate.fix_hint if outcome.status is Status.FAIL else None,
        )
        status[gate.code] = result.status
        results.append(result)
    return tuple(results)
