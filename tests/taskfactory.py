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

BASE_IMAGE = (
    "python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f"
)
DOCKERFILE = f"""\
FROM {BASE_IMAGE}
RUN pip install --no-cache-dir pytest==9.1.1 \\
 && useradd --create-home --uid 1000 agent \\
 && mkdir /workspace && chown agent /workspace
WORKDIR /workspace
{{copy}}USER agent
"""
COPY_WORKSPACE = "COPY --chown=agent workspace/ /workspace/\n"

LENIENT_GRADER = """\
from pathlib import Path

import pytest


def test_greeting() -> None:
    output = Path("output/greeting.txt")
    if not output.exists():
        pytest.skip("no output yet")
    assert output.read_text(encoding="utf-8") == "world\\n"
"""


ORDER_DEPENDENT_GRADER = """\
from pathlib import Path

CACHE: dict[str, str] = {}


def test_output_is_read() -> None:
    CACHE["greeting"] = Path("output/greeting.txt").read_text(encoding="utf-8")


def test_greeting_matches() -> None:
    assert CACHE["greeting"] == "world\\n"
"""
"""Passes in file order only: the second test reuses what the first one cached."""

WORDS = '["alpha", "bravo", "charlie", "delta", "echo"]'
HASH_ORDER_SOLVE = f"""\
#!/bin/sh
set -eu
mkdir -p output
python3 -c 'print("\\n".join(set({WORDS})))' > output/words.txt
"""
HASH_ORDER_GRADER = f"""\
from pathlib import Path


def test_words_in_order() -> None:
    expected = "".join(word + "\\n" for word in set({WORDS}))
    assert Path("output/words.txt").read_text(encoding="utf-8") == expected


def test_one_word_per_line() -> None:
    assert len(Path("output/words.txt").read_text(encoding="utf-8").splitlines()) == 5
"""
"""The solution and the grader both iterate a set, so they agree only under the same
``PYTHONHASHSEED`` (TG401 runs both with 0; TG501's reruns change the grader's)."""


def make_task(
    task_dir: Path,
    *,
    task_id: str | None = None,
    timeout: int = 60,
    solve: str | None = SOLVE,
    grader: str | None = GRADER,
    workspace: bool = True,
    dockerfile: str | None = None,
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
    if dockerfile is None:
        dockerfile = DOCKERFILE.format(copy=COPY_WORKSPACE if workspace else "")
    (env / "Dockerfile").write_text(dockerfile, encoding="utf-8")
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
