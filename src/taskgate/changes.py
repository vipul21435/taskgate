"""Changed-task discovery: map a pull request's git diff to task directories.

The diff is taken between the merge base of ``base`` and ``head`` and ``head``
itself (what ``git diff base...head`` shows), so commits that landed on the base
branch after the pull request forked are not attributed to it. Every changed
path is mapped to the task directory that contains it, using the same rules as
:func:`taskgate.layout.find_tasks`: a task is a directory holding ``task.toml``,
the outermost one wins, and hidden or tool directories never hold tasks.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from taskgate.layout import TASK_MANIFEST, is_skipped_dir

DEFAULT_BASES: tuple[str, ...] = ("origin/main", "main")
"""Refs tried in order when no base is given."""

ChangeKind = Literal["added", "modified", "removed"]


class GitError(RuntimeError):
    """A git command failed or a ref could not be resolved."""


@dataclass(frozen=True, slots=True)
class ChangedTask:
    """A task directory touched by the diff."""

    path: PurePosixPath
    """Task directory relative to the repository root, in POSIX form."""

    change: ChangeKind
    """``added`` (no manifest at the base), ``removed`` (no manifest at head) or ``modified``."""

    files: tuple[str, ...]
    """Changed paths inside the task, relative to the repository root, sorted."""

    tracked: tuple[str, ...] = ()
    """Every path tracked at ``head`` inside the task, relative to the task, sorted
    (empty for a removed task)."""


@dataclass(frozen=True, slots=True)
class ChangeSet:
    """The result of diffing ``head`` against ``base``."""

    base: str
    merge_base: str
    head: str
    tasks: tuple[ChangedTask, ...]
    other_files: tuple[str, ...]
    """Changed paths that belong to no task (READMEs, CI config, ...)."""


def git(repo: Path, *args: str) -> str:
    """Run ``git -C repo args...`` and return its stdout; raise :class:`GitError` on failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitError("git is not installed or not on PATH") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or f"exit code {proc.returncode}"
        raise GitError(f"git {' '.join(args)}: {detail}")
    return proc.stdout


def repo_root(path: Path) -> Path:
    """Return the top-level directory of the git work tree containing ``path``."""
    return Path(git(path, "rev-parse", "--show-toplevel").strip())


def _ref_exists(repo: Path, ref: str) -> bool:
    try:
        git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    except GitError:
        return False
    return True


def resolve_base(repo: Path, base: str | None) -> str:
    """Return ``base`` if it names a commit, else the first of :data:`DEFAULT_BASES` that does."""
    candidates = (base,) if base is not None else DEFAULT_BASES
    for ref in candidates:
        if _ref_exists(repo, ref):
            return ref
    raise GitError(f"base ref not found: {' or '.join(candidates)}")


def _split_z(output: str) -> list[str]:
    return [item for item in output.split("\0") if item]


def task_roots(paths: Iterable[str]) -> frozenset[PurePosixPath]:
    """Return the task directories implied by a list of tracked file paths.

    A directory is a task when it holds ``task.toml``, no component of its path
    is hidden or tool-owned, and no ancestor directory is itself a task.
    """
    candidates: set[PurePosixPath] = set()
    for raw in paths:
        path = PurePosixPath(raw)
        if path.name != TASK_MANIFEST:
            continue
        directory = path.parent
        if any(is_skipped_dir(part) for part in directory.parts):
            continue
        candidates.add(directory)
    return frozenset(
        root for root in candidates if not any(parent in candidates for parent in root.parents)
    )


def tracked_files(repo: Path, ref: str) -> list[str]:
    """Return every file path tracked at ``ref``."""
    return _split_z(git(repo, "ls-tree", "-r", "-z", "--name-only", ref))


def read_file_at(repo: Path, ref: str, path: str) -> str | None:
    """Return the text of ``path`` at ``ref``, or ``None`` when it is not tracked there."""
    if not git(repo, "ls-tree", "--name-only", ref, "--", path).strip():
        return None
    return git(repo, "show", f"{ref}:{path}")


def owning_task(path: str, roots: frozenset[PurePosixPath]) -> PurePosixPath | None:
    """Return the outermost task directory in ``roots`` that contains ``path``."""
    for ancestor in reversed(PurePosixPath(path).parents):
        if ancestor in roots:
            return ancestor
    return None


def changed_tasks(
    repo: Path,
    base: str | None = None,
    head: str = "HEAD",
    paths: Iterable[str] | None = None,
) -> ChangeSet:
    """Diff ``head`` against its merge base with ``base`` and group the paths by task.

    ``paths``, when given, replaces the ``git diff`` (a pull request's file list
    from the GitHub API); task roots still come from git at ``head`` and at the
    merge base.
    """
    base_ref = resolve_base(repo, base)
    if not _ref_exists(repo, head):
        raise GitError(f"head ref not found: {head}")
    merge_base = git(repo, "merge-base", base_ref, head).strip()
    if paths is None:
        changed = _split_z(git(repo, "diff", "--name-only", "-z", "--no-renames", merge_base, head))
    else:
        changed = sorted(set(paths))
    head_files = tracked_files(repo, head)
    head_roots = task_roots(head_files)
    base_roots = task_roots(tracked_files(repo, merge_base))

    grouped: dict[tuple[PurePosixPath, ChangeKind], list[str]] = {}
    other: list[str] = []
    for path in changed:
        root = owning_task(path, head_roots)
        kind: ChangeKind
        if root is not None:
            kind = "modified" if root in base_roots else "added"
        else:
            root = owning_task(path, base_roots)
            if root is None:
                other.append(path)
                continue
            kind = "removed"
        grouped.setdefault((root, kind), []).append(path)

    touched = {root for root, kind in grouped if kind != "removed"}
    contents: dict[PurePosixPath, list[str]] = {root: [] for root in touched}
    for path in head_files:
        owner = owning_task(path, head_roots)
        if owner is not None and owner in touched:
            contents[owner].append(PurePosixPath(path).relative_to(owner).as_posix())
    tasks = tuple(
        ChangedTask(
            path=root,
            change=kind,
            files=tuple(sorted(files)),
            tracked=tuple(sorted(contents.get(root, ()))),
        )
        for (root, kind), files in sorted(grouped.items(), key=lambda item: item[0][0].as_posix())
    )
    return ChangeSet(
        base=base_ref,
        merge_base=merge_base,
        head=head,
        tasks=tasks,
        other_files=tuple(sorted(other)),
    )
