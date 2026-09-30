"""The GitHub reporting part of ``make demo``, against the in-process fake API.

Usage: python examples/github_demo.py REPO OUT_DIR

Starts :mod:`taskgate.fakegithub` in this process, registers the fourth demo
pull request's file list (taken from git, as GitHub would list it), and runs
the TaskGate CLI as subprocesses against the fake:

1. ``taskgate check REPO --base main --pr 4 --out OUT/github/pr-4``: the changed
   files come from the pull request's file list instead of ``git diff``;
2. ``taskgate publish OUT/github/pr-4/report.json --pr 4 --sha HEAD``: one
   summary comment and one check run with an annotation per failed gate;
3. ``publish`` again with ``--no-check-run``: the comment is found by its hidden
   marker and left alone, since the report did not change.

It then prints every request the fake received. Commit hashes, ids and URLs
are fixed, so the output is the same on every machine.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from taskgate.fakegithub import DEFAULT_TOKEN, FakeGitHub

REPO_NAME = "sample/tasks"
BRANCH = "pr/4-log-levels"
NUMBER = 4
STATUS = {"A": "added", "M": "modified", "D": "removed"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def taskgate(args: list[str], env: dict[str, str], expected: int) -> None:
    print(f"$ taskgate {' '.join(args)}", flush=True)
    done = subprocess.run([sys.executable, "-m", "taskgate", *args], env=env, check=False)
    if done.returncode != expected:
        sys.exit(f"demo: expected exit {expected}, got {done.returncode}")


def main(argv: list[str]) -> None:
    if len(argv) != 2:
        sys.exit("usage: python examples/github_demo.py REPO OUT_DIR")
    repo, out = Path(argv[0]), Path(argv[1]) / "github" / f"pr-{NUMBER}"
    files = [
        (path, STATUS[status])
        for line in git(repo, "diff", "--name-status", "--no-renames", f"main...{BRANCH}")
        .strip()
        .splitlines()
        for status, path in [line.split("\t", 1)]
    ]
    git(repo, "checkout", "-q", BRANCH)
    sha = git(repo, "rev-parse", "HEAD").strip()
    with FakeGitHub(REPO_NAME) as fake:
        fake.add_pull(NUMBER, files)
        env = {
            **os.environ,
            "TASKGATE_GITHUB_API": fake.url,
            "GITHUB_TOKEN": DEFAULT_TOKEN,
            "GITHUB_REPOSITORY": REPO_NAME,
        }
        report = out / "report.json"
        taskgate(
            ["check", str(repo), "--base", "main", "--pr", str(NUMBER), "--out", str(out)],
            env,
            expected=1,
        )
        publish = ["publish", str(report), "--pr", str(NUMBER), "--sha", sha]
        print()
        taskgate(publish, env, expected=0)
        print()
        taskgate([*publish, "--no-check-run"], env, expected=0)
        print()
        print("== requests the fake GitHub API received")
        for request in fake.requests:
            query = "&".join(f"{k}={v}" for k, v in request.query.items())
            line = (
                f"{request.method} {request.path}{'?' + query if query else ''} -> {request.status}"
            )
            if request.path.endswith("/check-runs") and request.method == "POST":
                count = len(request.body["output"]["annotations"])
                line += f" ({count} annotation{'' if count == 1 else 's'})"
            print(line)
        (comment,) = fake.comments(NUMBER)
        (run,) = fake.check_runs()
        print()
        print(f"comment {comment['id']} starts with: {comment['body'].splitlines()[0]}")
        print(f"check run {run['id']}: {run['conclusion']}, title {run['output']['title']!r}")
        for note in run["output"]["annotations"]:
            where = f"{note['path']}:{note['start_line']}"
            print(f"  {note['annotation_level']}  {where}  {note['title']}")
    git(repo, "checkout", "-q", "main")


if __name__ == "__main__":
    main(sys.argv[1:])
