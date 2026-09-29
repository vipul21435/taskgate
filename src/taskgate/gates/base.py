"""The gate contract: what a gate is, what it may look at and what it returns.

A gate is any object with a stable ``code``, a kebab-case ``name``, a default
``severity``, a one-line ``summary``, a ``fix_hint`` shown when it fails, the
codes it ``requires`` to have passed, and a ``check(ctx)`` method. Built-in and
third-party gates satisfy the same :class:`Gate` protocol; :func:`gate` turns a
plain function into one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol, runtime_checkable

from taskgate import manifest
from taskgate.config import Config
from taskgate.files import regular_files, tracked_regular_files
from taskgate.results import Severity, Status
from taskgate.runner import BuildResult, LocalRunner, Runner, RunResult, Solution

BINARY_SNIFF_BYTES = 8000
"""Like git, a file is binary when a NUL byte appears in its first 8000 bytes."""


def is_binary(data: bytes) -> bool:
    """True when ``data`` looks binary by git's rule (a NUL byte near the start)."""
    return b"\0" in data[:BINARY_SNIFF_BYTES]


@dataclass(frozen=True, slots=True)
class Check:
    """What a gate's check returns: a status and a one-line explanation."""

    status: Status
    message: str

    @classmethod
    def ok(cls, message: str) -> Check:
        return cls(Status.PASS, message)

    @classmethod
    def fail(cls, message: str) -> Check:
        return cls(Status.FAIL, message)

    @classmethod
    def skip(cls, message: str) -> Check:
        """The gate does not apply (for example the file it lints is missing)."""
        return cls(Status.SKIP, message)

    @property
    def passed(self) -> bool:
        return self.status is Status.PASS


@dataclass
class TaskContext:
    """Everything a gate may look at for one task. Derived data is computed once."""

    task_dir: Path
    runner: Runner = field(default_factory=LocalRunner)
    config: Config = field(default_factory=Config)
    tracked: tuple[str, ...] | None = None
    """In diff mode, every path git tracks in the task at the checked commit
    (relative to the task directory); ``None`` when a plain directory is checked."""

    _manifest: manifest.ManifestCheck | None = field(default=None, repr=False)
    _files: tuple[PurePosixPath, ...] | None = field(default=None, repr=False)
    _build: BuildResult | None = field(default=None, repr=False)
    _runs: dict[Solution, RunResult] = field(default_factory=dict, repr=False)

    @property
    def manifest(self) -> manifest.ManifestCheck:
        if self._manifest is None:
            self._manifest = manifest.load(self.task_dir)
        return self._manifest

    @property
    def files(self) -> tuple[PurePosixPath, ...]:
        """The task's regular files, relative and sorted; symlinks are left out.

        In diff mode these are the files git tracks, whatever their names, so a
        committed ``__pycache__/`` file or ``.DS_Store`` is checked like any other.
        Otherwise the directory is walked on disk, leaving out the tool caches a
        working tree collects (see :mod:`taskgate.files`).
        """
        if self._files is None:
            self._files = (
                regular_files(self.task_dir)
                if self.tracked is None
                else tracked_regular_files(self.task_dir, self.tracked)
            )
        return self._files

    def read_bytes(self, relative: PurePosixPath) -> bytes:
        return (self.task_dir / relative).read_bytes()

    def build(self) -> BuildResult:
        """The runner's build of this task's environment (built at most once per context)."""
        if self._build is None:
            self._build = self.runner.build(self.task_dir)
        return self._build

    def run(self, solution: Solution = "reference") -> RunResult:
        """Run ``solution`` and then the grader, once per distinct solution."""
        if solution not in self._runs:
            self._runs[solution] = self.runner.run(
                self.task_dir, solution=solution, timeout_sec=self.manifest.timeout_sec
            )
        return self._runs[solution]


@runtime_checkable
class Gate(Protocol):
    """A review gate. Codes are ``TG`` plus three digits and never change meaning."""

    @property
    def code(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def severity(self) -> Severity:
        """Default severity; ``taskgate.toml`` may override it."""
        ...

    @property
    def summary(self) -> str:
        """What the gate checks, in one line (shown by ``taskgate gates``)."""
        ...

    @property
    def fix_hint(self) -> str:
        """How to fix a failing task (shown next to every failure)."""
        ...

    @property
    def requires(self) -> tuple[str, ...]:
        """Codes that must pass first; otherwise this gate is reported as skipped."""
        ...

    def check(self, ctx: TaskContext) -> Check: ...


CheckFunction = Callable[[TaskContext], Check]


@dataclass(frozen=True, slots=True)
class FunctionGate:
    """A :class:`Gate` whose check is a plain function."""

    code: str
    name: str
    severity: Severity
    summary: str
    fix_hint: str
    func: CheckFunction
    requires: tuple[str, ...] = ()

    def check(self, ctx: TaskContext) -> Check:
        return self.func(ctx)


def gate(
    code: str,
    name: str,
    *,
    severity: Severity,
    summary: str,
    fix_hint: str,
    requires: tuple[str, ...] = (),
) -> Callable[[CheckFunction], FunctionGate]:
    """Decorator that turns a check function into a :class:`FunctionGate`.

    Example::

        @gate("TG701", "no-todo-markers", severity=Severity.WARNING,
              summary="no TODO markers", fix_hint="Resolve the TODOs.")
        def no_todo_markers(ctx: TaskContext) -> Check: ...
    """

    def wrap(func: CheckFunction) -> FunctionGate:
        return FunctionGate(code, name, severity, summary, fix_hint, func, requires)

    return wrap
