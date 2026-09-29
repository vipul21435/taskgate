"""Build a task's environment and run a solution and the grader in it.

A :class:`Runner` does two things: ``build`` the task's environment and ``run``
one :data:`Solution` followed by the grader. There are three kinds of solution:

- ``"reference"``: the task's own ``solution/`` directory (gate TG401);
- ``"none"``: nothing runs before the grader, the workspace is untouched (TG402);
- a :class:`Stub`: a generated ``solve.sh`` that creates the files the reference
  solution created, empty, and does nothing else (TG403).

``regrade`` runs the reference solution once and then the grader once per seed
on an identical copy of its output (TG501). Each rerun loads the bundled
:mod:`taskgate.shuffle_plugin` to shuffle the test order with the seed, sets
``PYTHONHASHSEED`` and ``TASKGATE_SEED`` to the seed, and writes pytest's JUnit
XML, so per-test outcomes can be compared across reruns.

:class:`LocalRunner` follows the runtime contract in ``docs/task-layout.md``
without Docker: it copies ``environment/workspace/`` into a fresh temporary
workspace, runs ``solve.sh`` there with ``sh``, then runs ``python -m pytest`` on
a copy of ``tests/`` from the same working directory. The copies keep the
task's source tree clean and keep pytest from picking up configuration from the
surrounding repository. The Docker runner lives in :mod:`taskgate.docker_runner`.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from taskgate import shuffle_plugin
from taskgate.files import ignore_caches, regular_files

OUTPUT_TAIL_LINES = 20
PYTEST_NO_TESTS = 5
PYTEST_ARGS: tuple[str, ...] = ("-m", "pytest", "-q", "-p", "no:cacheprovider")
"""Grader arguments after the interpreter; ``--rootdir``, ``-c`` and the tests dir follow."""
SOLUTION_ENTRY = "solve.sh"
PLUGIN_MODULE = "taskgate_shuffle"
"""The name :mod:`taskgate.shuffle_plugin` is copied under for a rerun (``-p`` loads it)."""
PLUGIN_DIR = "plugin"
JUNIT_FILE = "junit.xml"
MAX_JUNIT_BYTES = 8 << 20
"""JUnit XML beyond this size is not read (a rerun with it reports no outcomes)."""
_DURATION = re.compile(r"\s+in\s+\d+(?:\.\d+)?s\b.*$")


@dataclass(frozen=True, slots=True)
class Stub:
    """A stand-in solution: creates ``files`` (workspace-relative) empty, nothing else.

    With no files it is a pure no-op that exits 0.
    """

    files: tuple[str, ...] = ()

    def script(self) -> str:
        """The ``solve.sh`` text of this stub."""
        lines = [
            "#!/bin/sh",
            "# TaskGate stub solution: the reference solution's new files, left empty.",
            "set -eu",
        ]
        parents = sorted({str(PurePosixPath(path).parent) for path in self.files} - {"."})
        if parents:
            lines.append("mkdir -p -- " + " ".join(shlex.quote(p) for p in parents))
        lines += [f": > {shlex.quote(path)}" for path in self.files]
        return "\n".join(lines) + "\n"


Solution = Literal["reference", "none"] | Stub
"""What runs before the grader: the task's solution, nothing, or a stub."""


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

    created: tuple[str, ...] = ()
    """Workspace files that did not exist before ``solve.sh`` ran and did after it."""

    error: str | None = None
    """Set when the runner itself failed (container would not start, refused root, ...)."""

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


@dataclass(frozen=True, slots=True)
class BuildResult:
    """Whether a task's environment is ready to run in, and a one-line account."""

    ok: bool
    message: str
    image: str | None = None
    """The image tag when the runner built (or reused) one."""

    user: str = ""
    """The image's configured ``USER`` (empty when unset or not built)."""

    output: str = ""
    """The last lines of the build log (on failure)."""


@dataclass(frozen=True, slots=True)
class GraderRun:
    """One rerun of the grader with a seed (see :meth:`Runner.regrade`)."""

    seed: int
    exit: int | None
    """pytest's exit code; ``None`` when the run did not finish within the budget."""

    junit: str
    """The JUnit XML pytest wrote (``""`` when it wrote none or it was too big)."""

    output: str
    """pytest's combined stdout and stderr, in full."""


@dataclass(frozen=True, slots=True)
class Regrade:
    """The reference solution run once, then the grader once per seed on its output."""

    solution: RunResult
    """The solution step (its grader fields are unset); reruns happen only after exit 0."""

    runs: tuple[GraderRun, ...] = ()
    """One per seed, in order; shorter than the seeds when the budget ran out."""

    error: str | None = None
    """Set when the runner failed between reruns (could not restore the workspace, ...)."""


class Runner(Protocol):
    """Anything that can prepare a task's environment and run a solution plus the grader."""

    @property
    def name(self) -> str:
        """Short name shown in reports: ``local`` or ``docker``."""
        ...

    def build(self, task_dir: Path) -> BuildResult: ...

    def run(self, task_dir: Path, *, solution: Solution, timeout_sec: float) -> RunResult: ...

    def regrade(self, task_dir: Path, *, seeds: Sequence[int], timeout_sec: float) -> Regrade:
        """Run the reference solution, then the grader once per seed on a fresh copy of
        its output; the whole call has ``(len(seeds) + 1) * timeout_sec`` seconds."""
        ...


def tail(text: str) -> str:
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


@dataclass(frozen=True, slots=True)
class Completed:
    """A finished (or killed) subprocess; ``code`` is ``None`` when it hit its deadline."""

    code: int | None
    stdout: str
    stderr: str


def execute(
    cmd: list[str],
    *,
    timeout: float,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    stdin: bytes | None = None,
    merge_stderr: bool = True,
    on_timeout: Callable[[], None] | None = None,
) -> Completed:
    """Run ``cmd`` in its own process group; on timeout call ``on_timeout``, then kill the group.

    With ``merge_stderr`` stderr is folded into stdout (in order); otherwise it is
    returned separately. Output is decoded as UTF-8 with replacement.
    """
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
        start_new_session=True,
    )
    code: int | None
    try:
        out, err = proc.communicate(input=stdin, timeout=max(timeout, 0.001))
        code = proc.returncode
    except subprocess.TimeoutExpired:
        if on_timeout is not None:
            on_timeout()
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        code = None
    return Completed(
        code,
        out.decode("utf-8", errors="replace"),
        (err or b"").decode("utf-8", errors="replace"),
    )


def grader_argv(python: str, root: str) -> list[str]:
    """``python -m pytest`` on ``root/tests`` with an empty ``root/pytest.ini`` as config."""
    return [python, *PYTEST_ARGS, "--rootdir", root, "-c", f"{root}/pytest.ini", f"{root}/tests"]


def regrade_args(seed: str, junit: str) -> list[str]:
    """Extra grader arguments for a rerun: the shuffle plugin with ``seed``, JUnit XML.

    ``xunit1`` keeps each test's ``file`` attribute, from which the report rebuilds
    pytest's own test ids (``tests/test_x.py::test_y``).
    """
    return [
        "-p",
        PLUGIN_MODULE,
        shuffle_plugin.OPTION,
        seed,
        "--junitxml",
        junit,
        "-o",
        "junit_family=xunit1",
    ]


def plugin_source() -> bytes:
    """The shuffle plugin's source, as it is copied into every rerun."""
    return Path(shuffle_plugin.__file__).read_bytes()


def seeded_env(env: dict[str, str], seed: int, plugin_dir: Path) -> dict[str, str]:
    """``env`` with the rerun's seeds set and the plugin directory first on ``PYTHONPATH``."""
    path = os.pathsep.join(part for part in (str(plugin_dir), env.get("PYTHONPATH", "")) if part)
    return {**env, "PYTHONHASHSEED": str(seed), "TASKGATE_SEED": str(seed), "PYTHONPATH": path}


def read_junit(path: Path) -> str:
    """The JUnit XML at ``path``; ``""`` when it is missing or larger than the cap."""
    try:
        if path.stat().st_size > MAX_JUNIT_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def stage_solution(task_dir: Path, solution: Stub | Literal["reference"], dest: Path) -> None:
    """Write what runs as ``dest/solve.sh`` (and its siblings) for ``solution``."""
    if isinstance(solution, Stub):
        dest.mkdir()
        (dest / SOLUTION_ENTRY).write_text(solution.script(), encoding="utf-8")
    else:
        shutil.copytree(task_dir / "solution", dest, ignore=ignore_caches, symlinks=True)


def _files(root: Path) -> set[str]:
    return {path.as_posix() for path in regular_files(root)}


@dataclass(frozen=True, slots=True)
class LocalRunner:
    """Run tasks as local subprocesses in throwaway directories (the no-Docker fallback)."""

    python: str = sys.executable
    """Interpreter that runs the grader; it must have pytest installed."""

    @property
    def name(self) -> str:
        return "local"

    def build(self, task_dir: Path) -> BuildResult:
        return BuildResult(ok=True, message="not built: the local runner does not use Docker")

    @staticmethod
    def _stage(task_dir: Path, root: Path) -> Path:
        """Copy the workspace seed and tests under ``root``; return the workspace."""
        workspace = root / "workspace"
        seed = task_dir / "environment" / "workspace"
        if seed.is_dir():
            shutil.copytree(seed, workspace, ignore=ignore_caches, symlinks=True)
        else:
            workspace.mkdir()
        shutil.copytree(task_dir / "tests", root / "tests", ignore=ignore_caches, symlinks=True)
        (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        return workspace

    def run(self, task_dir: Path, *, solution: Solution, timeout_sec: float) -> RunResult:
        deadline = time.monotonic() + timeout_sec
        env = _child_env()
        with tempfile.TemporaryDirectory(prefix="taskgate-") as tmp:
            root = Path(tmp)
            workspace = self._stage(task_dir, root)
            solution_exit: int | None = None
            created: tuple[str, ...] = ()
            if solution != "none":
                stage_solution(task_dir, solution, root / "solution")
                before = _files(workspace)
                done = execute(
                    ["sh", str(root / "solution" / SOLUTION_ENTRY)],
                    cwd=workspace,
                    env=env,
                    timeout=deadline - time.monotonic(),
                )
                solution_exit = done.code
                if done.code is None:
                    return RunResult(None, None, timed_out=True, output=tail(done.stdout))
                if done.code != 0:
                    return RunResult(done.code, None, timed_out=False, output=tail(done.stdout))
                created = tuple(sorted(_files(workspace) - before))

            graded = execute(
                grader_argv(self.python, str(root)),
                cwd=workspace,
                env=env,
                timeout=deadline - time.monotonic(),
            )
            return RunResult(
                solution_exit,
                graded.code,
                timed_out=graded.code is None,
                output=tail(graded.stdout),
                created=created,
            )

    def regrade(self, task_dir: Path, *, seeds: Sequence[int], timeout_sec: float) -> Regrade:
        deadline = time.monotonic() + timeout_sec * (len(seeds) + 1)
        env = _child_env()
        with tempfile.TemporaryDirectory(prefix="taskgate-") as tmp:
            root = Path(tmp)
            workspace = self._stage(task_dir, root)
            stage_solution(task_dir, "reference", root / "solution")
            done = execute(
                ["sh", str(root / "solution" / SOLUTION_ENTRY)],
                cwd=workspace,
                env=env,
                timeout=deadline - time.monotonic(),
            )
            if done.code != 0:
                timed_out = done.code is None
                return Regrade(RunResult(done.code, None, timed_out, output=tail(done.stdout)))
            solved = RunResult(0, None, timed_out=False, output="")
            snapshot = root / "solved"
            shutil.copytree(workspace, snapshot, symlinks=True)
            plugin = root / PLUGIN_DIR
            plugin.mkdir()
            (plugin / f"{PLUGIN_MODULE}.py").write_bytes(plugin_source())
            junit = root / JUNIT_FILE
            runs: list[GraderRun] = []
            for index, seed in enumerate(seeds):
                if index:
                    try:
                        shutil.rmtree(workspace)
                        shutil.copytree(snapshot, workspace, symlinks=True)
                    except OSError as exc:
                        error = f"could not restore the workspace before rerun {index + 1}: {exc}"
                        return Regrade(solved, tuple(runs), error=error)
                junit.unlink(missing_ok=True)
                graded = execute(
                    [*grader_argv(self.python, str(root)), *regrade_args(str(seed), str(junit))],
                    cwd=workspace,
                    env=seeded_env(env, seed, plugin),
                    timeout=deadline - time.monotonic(),
                )
                runs.append(GraderRun(seed, graded.code, read_junit(junit), graded.stdout))
                if graded.code is None:
                    break
            return Regrade(solved, tuple(runs))
