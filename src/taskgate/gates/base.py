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
from taskgate.results import Severity, Status
from taskgate.runner import LocalRunner, Runner, RunResult

IGNORED_DIRS: frozenset[str] = frozenset(
    {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)
"""Directories that hold tool caches, never task content; gates do not see them."""

IGNORED_FILES: frozenset[str] = frozenset({".DS_Store"})
IGNORED_SUFFIXES: tuple[str, ...] = (".pyc",)
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
    _manifest: manifest.ManifestCheck | None = field(default=None, repr=False)
    _files: tuple[PurePosixPath, ...] | None = field(default=None, repr=False)

    @property
    def manifest(self) -> manifest.ManifestCheck:
        if self._manifest is None:
            self._manifest = manifest.load(self.task_dir)
        return self._manifest

    @property
    def files(self) -> tuple[PurePosixPath, ...]:
        """Regular files in the task, relative and sorted; caches and symlinks are left out."""
        if self._files is None:
            found: list[PurePosixPath] = []
            for dirpath, dirnames, filenames in self.task_dir.walk():
                dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
                for name in filenames:
                    path = dirpath / name
                    if (
                        name in IGNORED_FILES
                        or name.endswith(IGNORED_SUFFIXES)
                        or path.is_symlink()
                        or not path.is_file()
                    ):
                        continue
                    found.append(PurePosixPath(path.relative_to(self.task_dir).as_posix()))
            self._files = tuple(sorted(found))
        return self._files

    def read_bytes(self, relative: PurePosixPath) -> bytes:
        return (self.task_dir / relative).read_bytes()

    def run(self, *, with_solution: bool) -> RunResult:
        return self.runner.run(
            self.task_dir, with_solution=with_solution, timeout_sec=self.manifest.timeout_sec
        )


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
