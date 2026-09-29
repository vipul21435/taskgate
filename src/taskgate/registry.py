"""The gate registry: built-in gates plus third-party gates from entry points.

A package adds gates by declaring entry points in the ``taskgate.gates`` group::

    [project.entry-points."taskgate.gates"]
    todo = "my_package.gates:no_todo_markers"

An entry point may name a gate, a list or tuple of gates, or a callable that
returns either. Third-party codes must use TG7xx to TG9xx so they can never
collide with a code TaskGate adds later. The registry refuses (with every
problem listed at once) duplicate codes or names, malformed codes, names or
severities, empty summaries or fix hints, and ``requires`` that do not name an
earlier gate.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points

from taskgate.gates import BUILTIN_GATES, Gate
from taskgate.results import Severity

ENTRY_POINT_GROUP = "taskgate.gates"
CODE = re.compile(r"^TG[1-9][0-9]{2}$")
NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
GROUPS: dict[int, str] = {
    1: "layout and manifest",
    2: "hygiene",
    3: "environment",
    4: "solution and baselines",
    5: "determinism",
    6: "reserved",
    7: "third-party",
    8: "third-party",
    9: "third-party",
}
FIRST_PLUGIN_GROUP = 7
BUILTIN = "built-in"


class RegistryError(ValueError):
    """A gate could not be loaded or does not follow the gate contract."""


@dataclass(frozen=True, slots=True)
class Registered:
    """A gate and where it came from (``built-in`` or ``plugin NAME``)."""

    gate: Gate
    source: str

    @property
    def builtin(self) -> bool:
        return self.source == BUILTIN


def group_of(code: str) -> int:
    """The hundreds digit of a well-formed code (``TG401`` -> 4)."""
    return int(code[2])


def group_label(group: int) -> str:
    return f"TG{group}xx  {GROUPS[group]}"


@dataclass(frozen=True, slots=True)
class Registry:
    """Every known gate, sorted by code (the order they run in)."""

    entries: tuple[Registered, ...]

    @property
    def gates(self) -> tuple[Gate, ...]:
        return tuple(entry.gate for entry in self.entries)

    @property
    def codes(self) -> frozenset[str]:
        return frozenset(entry.gate.code for entry in self.entries)

    @property
    def plugin_count(self) -> int:
        return sum(not entry.builtin for entry in self.entries)


def _as_gates(obj: object, origin: str, *, call: bool = True) -> list[Gate]:
    """Coerce what an entry point loaded into a list of gates (calling it once if needed)."""
    if isinstance(obj, Gate) and not isinstance(obj, type):
        return [obj]
    if isinstance(obj, list | tuple):
        bad = [type(item).__name__ for item in obj if not isinstance(item, Gate)]
        if bad:
            raise RegistryError(f"{origin}: not a gate: {', '.join(bad)}")
        return list(obj)
    if call and callable(obj):
        return _as_gates(obj(), origin, call=False)
    raise RegistryError(
        f"{origin}: expected a gate, a list of gates or a callable returning one, "
        f"got {type(obj).__name__}"
    )


def load_plugins(eps: Iterable[EntryPoint] | None = None) -> list[Registered]:
    """Load the gates behind every ``taskgate.gates`` entry point, sorted by entry-point name."""
    found = entry_points(group=ENTRY_POINT_GROUP) if eps is None else eps
    loaded: list[Registered] = []
    for ep in sorted(found, key=lambda ep: ep.name):
        origin = f"entry point {ep.name!r} ({ep.value})"
        try:
            obj = ep.load()
            gates = _as_gates(obj, origin)
        except RegistryError:
            raise
        except Exception as exc:
            raise RegistryError(f"{origin}: {type(exc).__name__}: {exc}") from exc
        loaded.extend(Registered(gate, f"plugin {ep.name}") for gate in gates)
    return loaded


def _problems(entry: Registered) -> list[str]:
    gate = entry.gate
    where = f"{gate.code} ({entry.source})"
    problems: list[str] = []
    if not isinstance(gate.code, str) or not CODE.match(gate.code):
        return [f"{gate.code!r} ({entry.source}): code must be TG followed by three digits"]
    if not entry.builtin and group_of(gate.code) < FIRST_PLUGIN_GROUP:
        problems.append(f"{where}: TG1xx-TG6xx are reserved for built-in gates; use TG7xx-TG9xx")
    if not isinstance(gate.name, str) or not NAME.match(gate.name):
        problems.append(f"{where}: name {gate.name!r} must be kebab-case")
    if not isinstance(gate.severity, Severity):
        problems.append(f"{where}: severity must be a taskgate.results.Severity")
    for attr in ("summary", "fix_hint"):
        value = getattr(gate, attr)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{where}: {attr} must be a non-empty string")
    return problems


def build(entries: Iterable[Registered]) -> Registry:
    """Validate ``entries`` and return them as a registry sorted by code."""
    ordered = sorted(entries, key=lambda entry: str(entry.gate.code))
    problems: list[str] = []
    seen_codes: dict[str, str] = {}
    seen_names: dict[str, str] = {}
    for entry in ordered:
        gate = entry.gate
        if gate.code in seen_codes:
            problems.append(
                f"{gate.code}: registered twice ({seen_codes[gate.code]} and {entry.source})"
            )
            continue
        problems.extend(_problems(entry))
        if gate.name in seen_names:
            problems.append(
                f"{gate.code}: name {gate.name!r} is already used by {seen_names[gate.name]}"
            )
        for required in gate.requires:
            if required not in seen_codes:
                problems.append(
                    f"{gate.code}: requires {required}, which is not a gate with a lower code"
                )
        seen_codes.setdefault(gate.code, entry.source)
        seen_names.setdefault(gate.name, gate.code)
    if problems:
        raise RegistryError("invalid gate registry:\n  " + "\n  ".join(problems))
    return Registry(tuple(ordered))


def load_registry(*, plugins: bool = True, eps: Iterable[EntryPoint] | None = None) -> Registry:
    """The built-in gates plus, unless ``plugins`` is false, every entry-point gate."""
    entries = [Registered(gate, BUILTIN) for gate in BUILTIN_GATES]
    if plugins:
        entries.extend(load_plugins(eps))
    return build(entries)
