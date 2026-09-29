"""Which paths in a task are content, and a deterministic walk over them.

Tool caches (``__pycache__/``, ``.pytest_cache/``, ...), ``.DS_Store`` and
``*.pyc`` files are never task content: gates do not see them, the Docker
runner leaves them out of the image tag and archives, and a solution that
leaves them behind has not "created" them.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path, PurePosixPath

IGNORED_DIRS: frozenset[str] = frozenset(
    {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)
"""Directories that hold tool caches, never task content."""

IGNORED_FILES: frozenset[str] = frozenset({".DS_Store"})
IGNORED_SUFFIXES: tuple[str, ...] = (".pyc",)


def is_ignored(parts: Sequence[str]) -> bool:
    """True when a relative path (given as its parts) is a cache or OS clutter."""
    if not parts:
        return False
    *dirs, name = parts
    return (
        any(part in IGNORED_DIRS for part in dirs)
        or name in IGNORED_DIRS
        or name in IGNORED_FILES
        or name.endswith(IGNORED_SUFFIXES)
    )


def regular_files(root: Path) -> tuple[PurePosixPath, ...]:
    """Regular files under ``root``, relative and sorted; ignored paths and symlinks left out."""
    found: list[PurePosixPath] = []
    for dirpath, dirnames, filenames in root.walk():
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        for name in filenames:
            path = dirpath / name
            if is_ignored((name,)) or path.is_symlink() or not path.is_file():
                continue
            found.append(PurePosixPath(path.relative_to(root).as_posix()))
    return tuple(sorted(found))
