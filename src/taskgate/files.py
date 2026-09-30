"""Which paths in a task are content, and a deterministic walk over them.

On disk, tool caches (``__pycache__/``, ``.pytest_cache/``, ...), ``.DS_Store``
and ``*.pyc`` files are not task content: a working tree collects them from
local runs. The disk walk leaves them out, both runners leave them out of what
they copy or archive, and a solution that leaves them behind has not "created"
them. What git tracks is different: a committed file is part of the pull
request whatever its name, so :func:`tracked_regular_files` keeps it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath

IGNORED_DIRS: frozenset[str] = frozenset(
    {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".taskgate"}
)
"""Directories that hold tool caches (TaskGate's own result cache among them), never
task content."""

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


def ignore_caches(directory: str, names: list[str]) -> set[str]:
    """A :func:`shutil.copytree` ``ignore`` callable: leave out what :func:`is_ignored` names."""
    return {name for name in names if is_ignored((name,))}


def tracked_regular_files(root: Path, tracked: Iterable[str]) -> tuple[PurePosixPath, ...]:
    """The ``tracked`` paths (relative to ``root``) that are regular files on disk, sorted.

    Nothing is dropped for its name: a committed ``__pycache__/`` file or
    ``.DS_Store`` is listed like any other. Symlinks, submodules and files missing
    from the working tree are left out.
    """
    found: set[PurePosixPath] = set()
    for relative in tracked:
        path = root / relative
        if not path.is_symlink() and path.is_file():
            found.add(PurePosixPath(relative))
    return tuple(sorted(found))
