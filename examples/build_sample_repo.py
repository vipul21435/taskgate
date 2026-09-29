"""Build the throwaway git repository that ``make demo`` and the tests check.

Usage: python examples/build_sample_repo.py DEST

The ``main`` branch holds one accepted task (``modular-inverse``, copied from
``examples/sample-repo``) and a README. Each directory under
``examples/pull-requests/`` becomes a branch off ``main`` whose single commit
overlays that directory's files onto the repository:

- ``pr/1-integer-determinant`` adds a well-formed task (every gate passes);
- ``pr/2-word-count`` adds a task whose grader skips when the output is missing,
  so an untouched workspace passes, whose manifest has an invalid difficulty,
  and whose solution script exports a leftover (fake) API key;
- ``pr/3-gcd-pairs`` adds a task whose grader compares lines with a non-strict
  ``zip()``, so an empty output file passes, and whose Dockerfile names its base
  image by tag only.

Author, committer and dates are fixed, so the commit hashes (and therefore the
reports TaskGate writes) are the same on every machine. DEST is deleted and
rebuilt only when its ``.git/`` holds the marker file of an earlier build.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parent
PULL_REQUESTS = EXAMPLES / "pull-requests"
BASE_TASKS = ("modular-inverse",)
MARKER = "taskgate-demo-repo"
"""Marker file inside ``.git/`` that proves DEST was built here (so it is safe to delete)."""
BASE_README = """\
# Sample task repository

Benchmark tasks for AI coding agents, one directory per task under `tasks/`.

| Task | Difficulty |
| --- | --- |
| modular-inverse | easy |
"""
GIT_ENV = {
    "GIT_AUTHOR_NAME": "TaskGate Demo",
    "GIT_AUTHOR_EMAIL": "demo@example.invalid",
    "GIT_COMMITTER_NAME": "TaskGate Demo",
    "GIT_COMMITTER_EMAIL": "demo@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
}
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")


def git(repo: Path, *args: str, date: str = "2026-01-01T00:00:00Z") -> None:
    env = {**os.environ, **GIT_ENV, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    subprocess.run(["git", "-C", str(repo), *args], check=True, env=env)


def prepare(dest: Path) -> None:
    if dest.exists():
        if not (dest / ".git" / MARKER).is_file():
            sys.exit(f"refusing to delete {dest}: it was not built by this script")
        shutil.rmtree(dest)
    dest.mkdir(parents=True)


def build(dest: Path) -> list[str]:
    """Build the repository at ``dest`` and return the pull-request branch names."""
    prepare(dest)
    git(dest, "init", "-q", "-b", "main")
    (dest / ".git" / MARKER).write_text(
        "built by examples/build_sample_repo.py\n", encoding="utf-8"
    )
    (dest / "README.md").write_text(BASE_README, encoding="utf-8")
    for name in BASE_TASKS:
        shutil.copytree(
            EXAMPLES / "sample-repo" / "tasks" / name, dest / "tasks" / name, ignore=_IGNORE
        )
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", "Add modular-inverse task")

    overlays = sorted(path for path in PULL_REQUESTS.iterdir() if path.is_dir())
    branches = []
    for number, overlay in enumerate(overlays, start=1):
        branch = f"pr/{overlay.name}"
        git(dest, "checkout", "-q", "-b", branch, "main")
        shutil.copytree(overlay, dest, ignore=_IGNORE, dirs_exist_ok=True)
        git(dest, "add", "-A")
        git(
            dest,
            "commit",
            "-q",
            "-m",
            f"Add {overlay.name.split('-', 1)[1]} task",
            date=f"2026-01-0{number + 1}T00:00:00Z",
        )
        branches.append(branch)
    git(dest, "checkout", "-q", "main")
    return branches


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: python examples/build_sample_repo.py DEST")
    dest = Path(sys.argv[1])
    for branch in build(dest):
        print(f"built {dest} branch {branch}")


if __name__ == "__main__":
    main()
