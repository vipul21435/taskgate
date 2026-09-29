"""Objects that fake entry points load in the registry tests."""

from __future__ import annotations

from dataclasses import dataclass

from taskgate.gates import Check, FunctionGate, TaskContext
from taskgate.results import Severity


def ok(_ctx: TaskContext) -> Check:
    return Check.ok("fine")


def plugin_gate(code: str, name: str, **changes: object) -> FunctionGate:
    fields: dict[str, object] = {
        "code": code,
        "name": name,
        "severity": Severity.WARNING,
        "summary": "a plugin gate",
        "fix_hint": "fix it",
        "func": ok,
    }
    fields.update(changes)
    return FunctionGate(**fields)  # type: ignore[arg-type]


SINGLE = plugin_gate("TG801", "single")
PAIR = (plugin_gate("TG802", "pair-one"), plugin_gate("TG803", "pair-two", requires=("TG802",)))
NOT_A_GATE = 42
MIXED = [plugin_gate("TG804", "mixed"), "nope"]
RESERVED = plugin_gate("TG150", "reserved")
MALFORMED = plugin_gate("TG7", "malformed")
BAD_FIELDS = plugin_gate(
    "TG805", "Bad_Name", severity="fatal", summary=" ", fix_hint=None, requires=("TG999",)
)
DUPLICATE_NAME = plugin_gate("TG806", "single")


def factory() -> list[FunctionGate]:
    return [plugin_gate("TG807", "from-factory")]


def returns_callable() -> object:
    return factory


def broken_factory() -> list[FunctionGate]:
    raise RuntimeError("cannot build gates")


@dataclass(frozen=True)
class ClassGate:
    """A gate implemented as a class instead of a function."""

    code: str = "TG808"
    name: str = "class-gate"
    severity: Severity = Severity.INFO
    summary: str = "a class-based gate"
    fix_hint: str = "fix it"
    requires: tuple[str, ...] = ()

    def check(self, ctx: TaskContext) -> Check:
        return Check.ok(f"checked {ctx.task_dir.name}")
