"""Build small runnable task directories for tests."""

from __future__ import annotations

from pathlib import Path

MANIFEST = """\
[task]
id = "{task_id}"
title = "Echo a greeting"
difficulty = "easy"
timeout_sec = {timeout}

[environment]
dockerfile = "Dockerfile"
workdir = "/workspace"
"""

SOLVE = """\
#!/bin/sh
set -eu
mkdir -p output
cp input/name.txt output/greeting.txt
"""

GRADER = """\
from pathlib import Path


def test_greeting() -> None:
    assert Path("output/greeting.txt").read_text(encoding="utf-8") == "world\\n"
"""

LENIENT_GRADER = """\
from pathlib import Path

import pytest


def test_greeting() -> None:
    output = Path("output/greeting.txt")
    if not output.exists():
        pytest.skip("no output yet")
    assert output.read_text(encoding="utf-8") == "world\\n"
"""


def make_task(
    task_dir: Path,
    *,
    task_id: str | None = None,
    timeout: int = 60,
    solve: str | None = SOLVE,
    grader: str | None = GRADER,
    workspace: bool = True,
) -> Path:
    """Write a complete task that echoes ``input/name.txt``; pass ``None`` to omit a part."""
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.toml").write_text(
        MANIFEST.format(task_id=task_id or task_dir.name, timeout=timeout), encoding="utf-8"
    )
    (task_dir / "instruction.md").write_text(
        "Copy input/name.txt to output/greeting.txt.\n", encoding="utf-8"
    )
    env = task_dir / "environment"
    env.mkdir(exist_ok=True)
    (env / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    if workspace:
        (env / "workspace" / "input").mkdir(parents=True, exist_ok=True)
        (env / "workspace" / "input" / "name.txt").write_text("world\n", encoding="utf-8")
    (task_dir / "solution").mkdir(exist_ok=True)
    if solve is not None:
        (task_dir / "solution" / "solve.sh").write_text(solve, encoding="utf-8")
    (task_dir / "tests").mkdir(exist_ok=True)
    if grader is not None:
        (task_dir / "tests" / "test_outputs.py").write_text(grader, encoding="utf-8")
    return task_dir
