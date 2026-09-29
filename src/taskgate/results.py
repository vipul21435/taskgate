"""Result model shared by gates and reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Severity(StrEnum):
    """How much a failing gate matters. Only ``error`` blocks a pull request."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Status(StrEnum):
    """Outcome of one gate on one task."""

    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class GateResult:
    """One gate's verdict on one task."""

    code: str
    """Stable gate code, for example ``TG401``."""

    name: str
    """Short kebab-case gate name, for example ``solution-passes``."""

    severity: Severity
    status: Status
    message: str
    fix_hint: str | None = None
    """How to fix the task; set only when the gate failed."""

    details: tuple[str, ...] = ()
    """Extra lines under the message, e.g. each flaky test and how to reproduce it."""

    @property
    def blocking(self) -> bool:
        """True when this result alone makes the pull request fail."""
        return self.status is Status.FAIL and self.severity is Severity.ERROR


@dataclass(frozen=True, slots=True)
class TaskReport:
    """Every gate result for one task directory."""

    path: str
    """Task directory relative to the repository root, in POSIX form."""

    change: str | None
    """``added``, ``modified`` or ``removed`` in diff mode; ``None`` when checking all tasks."""

    results: tuple[GateResult, ...] = ()
    changed_files: tuple[str, ...] = ()

    @property
    def checked(self) -> bool:
        return self.change != "removed"

    @property
    def blocking_failures(self) -> int:
        return sum(result.blocking for result in self.results)

    @property
    def verdict(self) -> str:
        """``pass``, ``fail`` (a blocking gate failed) or ``skip`` (removed task)."""
        if not self.checked:
            return "skip"
        return "fail" if self.blocking_failures else "pass"


OptionValue = int | float | tuple[str, ...]
"""The value of one option in a ``taskgate.toml`` section such as ``[manifest]``."""


@dataclass(frozen=True, slots=True)
class ConfigSummary:
    """What ``taskgate.toml`` changed, as reports show it."""

    source: str | None = None
    """``taskgate.toml at main``, a ``--config`` path, or ``None`` for the defaults."""

    disabled: tuple[str, ...] = ()
    severity: tuple[tuple[str, Severity], ...] = ()
    """``(code, severity)`` pairs that override a gate's default severity."""

    options: tuple[tuple[str, OptionValue], ...] = ()
    """``(section.key, value)`` for every gate option that differs from its default."""


@dataclass(frozen=True, slots=True)
class CheckReport:
    """The outcome of one ``taskgate check`` run."""

    version: str
    mode: str
    """``diff`` (tasks changed against a base) or ``all`` (every task under the root)."""

    base: str | None = None
    merge_base: str | None = None
    tasks: tuple[TaskReport, ...] = ()
    other_files: tuple[str, ...] = field(default=())
    config: ConfigSummary = field(default_factory=ConfigSummary)
    runner: str = "local"
    """Where solutions and graders ran: ``local`` or ``docker``."""

    @property
    def blocking_failures(self) -> int:
        return sum(task.blocking_failures for task in self.tasks)

    @property
    def passed(self) -> bool:
        return self.blocking_failures == 0
