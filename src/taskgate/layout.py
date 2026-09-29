"""Task layout: find benchmark task directories and report missing parts.

A task directory is any directory that holds a ``task.toml`` manifest. A complete
task also has ``instruction.md`` and the ``environment/``, ``solution/`` and
``tests/`` directories. The full contract is in ``docs/task-layout.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

TASK_MANIFEST = "task.toml"
REQUIRED_FILES: tuple[str, ...] = (TASK_MANIFEST, "instruction.md")
REQUIRED_DIRS: tuple[str, ...] = ("environment", "solution", "tests")
SKIP_DIRS: frozenset[str] = frozenset({"node_modules", "__pycache__", "venv"})


@dataclass(frozen=True, slots=True)
class TaskDir:
    """A task directory found under a scanned root."""

    path: PurePosixPath
    """Location relative to the scanned root, in POSIX form (``.`` for the root itself)."""

    missing: tuple[str, ...]
    """Required parts that are absent; directories carry a trailing slash."""

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def name(self) -> str:
        return self.path.name or "."


def missing_parts(task_dir: Path) -> tuple[str, ...]:
    """Return the required layout parts that ``task_dir`` lacks, files first."""
    missing = [name for name in REQUIRED_FILES if not (task_dir / name).is_file()]
    missing.extend(f"{name}/" for name in REQUIRED_DIRS if not (task_dir / name).is_dir())
    return tuple(missing)


def is_skipped_dir(dirname: str) -> bool:
    """True for directories that are never scanned for tasks (hidden or tool-owned)."""
    return dirname.startswith(".") or dirname in SKIP_DIRS


def find_tasks(root: Path) -> list[TaskDir]:
    """Return every task directory under ``root``, sorted by relative path.

    Hidden directories and common tool directories are not scanned, symlinks
    are not followed, and a ``task.toml`` nested inside another task (a test
    fixture, for example) does not start a new task.
    """
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {root}")
    found: list[TaskDir] = []
    for dirpath, dirnames, filenames in root.walk():
        if TASK_MANIFEST in filenames:
            relative = PurePosixPath(dirpath.relative_to(root).as_posix())
            found.append(TaskDir(path=relative, missing=missing_parts(dirpath)))
            dirnames.clear()
            continue
        dirnames[:] = [d for d in dirnames if not is_skipped_dir(d)]
    return sorted(found, key=lambda task: task.path.as_posix())
