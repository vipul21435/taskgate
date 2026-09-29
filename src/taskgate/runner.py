"""Run a task's reference solution and grader.

:class:`LocalRunner` follows the runtime contract in ``docs/task-layout.md``
without Docker: it copies ``environment/workspace/`` into a fresh temporary
workspace, runs ``solution/solve.sh`` there with ``sh`` (unless asked for an
untouched baseline), then runs ``python -m pytest`` on a copy of ``tests/``
from the same working directory. The copies keep the task's source tree clean
and keep pytest from picking up configuration from the surrounding repository.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

OUTPUT_TAIL_LINES = 20
PYTEST_NO_TESTS = 5
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
_DURATION = re.compile(r"\s+in\s+\d+(?:\.\d+)?s\b.*$")


@dataclass(frozen=True, slots=True)
class RunResult:
    """What happened when a task was run once."""

    solution_exit: int | None
    """Exit code of ``solve.sh``; ``None`` when it was not run (baseline) or timed out."""

    grader_exit: int | None
    """Exit code of pytest; ``None`` when it did not run or timed out."""

    timed_out: bool
    output: str
    """The last lines of combined stdout and stderr of the step that ran last."""

    @property
    def grader_passed(self) -> bool:
        return self.grader_exit == 0

    @property
    def summary(self) -> str:
        """pytest's final summary line without its duration, e.g. ``1 failed, 2 passed``."""
        for line in reversed(self.output.splitlines()):
            text = line.strip().strip("=").strip()
            if text:
                return _DURATION.sub("", text)
        return "no output"


class Runner(Protocol):
    """Anything that can run a task with or without its reference solution."""

    def run(self, task_dir: Path, *, with_solution: bool, timeout_sec: float) -> RunResult: ...


def _tail(text: str) -> str:
    return "\n".join(text.splitlines()[-OUTPUT_TAIL_LINES:])


def _child_env() -> dict[str, str]:
    """The parent environment minus pytest and coverage hooks, with this interpreter first."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTEST_", "COV_CORE_"))
    }
    bin_dir = str(Path(sys.executable).parent)
    env["PATH"] = os.pathsep.join(part for part in (bin_dir, env.get("PATH", "")) if part)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONHASHSEED"] = "0"
    return env


def _run(cmd: list[str], cwd: Path, env: dict[str, str], timeout: float) -> tuple[int | None, str]:
    """Run ``cmd`` in its own process group; on timeout kill the whole group."""
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=max(timeout, 0.001))
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        return None, out
    return proc.returncode, out


@dataclass(frozen=True, slots=True)
class LocalRunner:
    """Run tasks as local subprocesses in throwaway directories (the no-Docker fallback)."""

    python: str = sys.executable
    """Interpreter that runs the grader; it must have pytest installed."""

    def run(self, task_dir: Path, *, with_solution: bool, timeout_sec: float) -> RunResult:
        deadline = time.monotonic() + timeout_sec
        env = _child_env()
        with tempfile.TemporaryDirectory(prefix="taskgate-") as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            seed = task_dir / "environment" / "workspace"
            if seed.is_dir():
                shutil.copytree(seed, workspace, ignore=_IGNORE, symlinks=True)
            else:
                workspace.mkdir()
            shutil.copytree(task_dir / "tests", root / "tests", ignore=_IGNORE, symlinks=True)
            (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")

            solution_exit: int | None = None
            if with_solution:
                shutil.copytree(
                    task_dir / "solution", root / "solution", ignore=_IGNORE, symlinks=True
                )
                solution_exit, out = _run(
                    ["sh", str(root / "solution" / "solve.sh")],
                    workspace,
                    env,
                    deadline - time.monotonic(),
                )
                if solution_exit is None:
                    return RunResult(None, None, timed_out=True, output=_tail(out))
                if solution_exit != 0:
                    return RunResult(solution_exit, None, timed_out=False, output=_tail(out))

            grader_exit, out = _run(
                [
                    self.python,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "--rootdir",
                    str(root),
                    "-c",
                    str(root / "pytest.ini"),
                    str(root / "tests"),
                ],
                workspace,
                env,
                deadline - time.monotonic(),
            )
            return RunResult(
                solution_exit, grader_exit, timed_out=grader_exit is None, output=_tail(out)
            )
