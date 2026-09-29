"""The Docker runner, and the choice between it and the local runner.

:class:`DockerRunner` builds ``environment/Dockerfile`` (the path comes from
``environment.dockerfile``) with ``environment/`` as the build context, under a
tag derived from the context's content (``taskgate-env:<16 hex>``) and with the
labels ``project=taskgate`` and ``taskgate.context=<sha256>``. An image whose
label already matches is reused, so an unchanged environment is built once.

Each run is one ``docker run --rm`` of that image with no network, a CPU,
memory (no extra swap) and process limit, all capabilities dropped,
``no-new-privileges``, and the image's ``USER`` (or ``65534:65534`` when the image
would run as root). The task's ``solution/`` (or a generated stub) and
``tests/`` are streamed in as a tar archive on stdin and unpacked into a tmpfs
at ``/taskgate``, never into the workspace. A small POSIX shell driver then runs
``solve.sh`` from the manifest's ``workdir`` (which the image provides, seeded
by the Dockerfile), lists the workspace before and after it, and runs
``python -m pytest``; it refuses to run at all as uid 0. The driver marks each
step on stdout with a per-run nonce, so task output cannot forge a marker by
accident. When the task's ``timeout_sec`` runs out, the container is killed.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import secrets
import shutil
import tarfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath

from taskgate import manifest
from taskgate.config import RunnerOptions
from taskgate.dockerfile import locate
from taskgate.files import IGNORED_DIRS, is_ignored
from taskgate.runner import (
    SOLUTION_ENTRY,
    BuildResult,
    Completed,
    LocalRunner,
    Runner,
    RunResult,
    Solution,
    Stub,
    execute,
    grader_argv,
    tail,
)

IMAGE_REPOSITORY = "taskgate-env"
TAG_HEX = 16
PROJECT_LABEL = "project=taskgate"
CONTEXT_LABEL = "taskgate.context"
INSPECT_FORMAT = '{{index .Config.Labels "' + CONTEXT_LABEL + '"}}|{{.Config.User}}'
STAGE_DIR = "/taskgate"
STAGE_TMPFS_MB = 256
NOBODY = "65534:65534"
ROOT_USERS = frozenset({"", "root", "0"})
DOCKER_CHECK_TIMEOUT = 20.0
KILL_TIMEOUT = 30.0
OOM_EXIT = 137
MARK = "@@taskgate-"
DIGEST_VERSION = b"taskgate-env-v1"

DRIVER = f"""\
nonce=$1 mode=$2 stage=$3
exec 2>&1
mark() {{ printf '\\n{MARK}%s %s\\n' "$nonce" "$*"; }}
if [ "$(id -u)" = 0 ]; then mark refuse-root; exit 0; fi
if ! tar -xf - -C "$stage"; then mark setup-failed; exit 0; fi
if [ "$mode" = solve ]; then
    mark listing before
    find . -type f
    mark listing end
    sh "$stage/solution/{SOLUTION_ENTRY}" </dev/null
    code=$?
    mark solution "$code"
    [ "$code" -eq 0 ] || exit 0
    mark listing after
    find . -type f
    mark listing end
fi
if command -v python >/dev/null 2>&1; then py=python; else py=python3; fi
{" ".join(grader_argv('"$py"', '"$stage"'))} </dev/null
mark grader "$?"
"""
"""The in-container driver, run as ``sh -c DRIVER taskgate-driver NONCE MODE STAGE_DIR``."""


class RunnerChoice(StrEnum):
    """``--runner``: Docker when it answers (``auto``), Docker or nothing, or local only."""

    AUTO = "auto"
    DOCKER = "docker"
    LOCAL = "local"


class RunnerUnavailableError(RuntimeError):
    """``--runner docker`` was asked for and Docker does not answer."""


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _docker_env() -> dict[str, str]:
    return {**os.environ, "BUILDKIT_PROGRESS": "plain", "DOCKER_CLI_HINTS": "false"}


def context_digest(env_dir: Path, dockerfile: str) -> str:
    """SHA-256 of the build context: every path, file mode kind, content and symlink target.

    Paths are sorted and caches are left out, so mtimes and walk order never
    change the digest; any byte, an executable bit or a symlink target does.
    """
    entries: list[tuple[str, str, str]] = []
    for dirpath, dirnames, filenames in env_dir.walk():
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for name in filenames:
            path = dirpath / name
            relative = path.relative_to(env_dir).as_posix()
            if is_ignored(PurePosixPath(relative).parts):
                continue
            if path.is_symlink():
                entries.append((relative, "link", str(path.readlink())))
            elif path.is_file():
                kind = "exec" if path.stat().st_mode & 0o111 else "file"
                entries.append((relative, kind, hashlib.sha256(path.read_bytes()).hexdigest()))
    digest = hashlib.sha256(DIGEST_VERSION + b"\0" + dockerfile.encode() + b"\0")
    for relative, kind, content in sorted(entries):
        digest.update(f"{relative}\0{kind}\0{content}\n".encode())
    return digest.hexdigest()


def image_tag(digest: str) -> str:
    return f"{IMAGE_REPOSITORY}:{digest[:TAG_HEX]}"


def run_user(image_user: str) -> str:
    """The image's ``USER``, or ``nobody`` when that would be root (or is unset)."""
    name = image_user.split(":", 1)[0].strip()
    return NOBODY if name in ROOT_USERS else image_user


def _normalize(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """Drop caches; strip owner and time so archives depend on content and modes only."""
    if is_ignored(PurePosixPath(info.name).parts[1:]):
        return None
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    if info.isdir() or (info.isreg() and info.mode & 0o111):
        info.mode = 0o755
    elif info.isreg():
        info.mode = 0o644
    return info


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes, mode: int) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    tar.addfile(info, io.BytesIO(data))


def build_archive(task_dir: Path, solution: Solution) -> bytes:
    """``tests/``, ``pytest.ini`` and (unless ``none``) ``solution/`` as an uncompressed tar."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        tar.add(task_dir / "tests", arcname="tests", filter=_normalize)
        if isinstance(solution, Stub):
            directory = tarfile.TarInfo("solution")
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o755
            tar.addfile(directory)
            _add_bytes(tar, f"solution/{SOLUTION_ENTRY}", solution.script().encode(), 0o755)
        elif solution == "reference":
            tar.add(task_dir / "solution", arcname="solution", filter=_normalize)
        _add_bytes(tar, "pytest.ini", b"[pytest]\n", 0o644)
    return buffer.getvalue()


def _created(before: list[str], after: list[str]) -> tuple[str, ...]:
    def clean(lines: list[str]) -> set[str]:
        paths = set()
        for line in lines:
            path = line.removeprefix("./")
            if path and path != "." and not is_ignored(PurePosixPath(path).parts):
                paths.add(path)
        return paths

    return tuple(sorted(clean(after) - clean(before)))


@dataclass
class _Transcript:
    """The driver's stdout split into step outputs, step events and workspace listings."""

    events: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, list[str]] = field(default_factory=dict)
    listings: dict[str, list[str]] = field(default_factory=dict)
    pending: list[str] = field(default_factory=list)
    """Output after the last step marker (the step that was running when it ended)."""

    @classmethod
    def parse(cls, stdout: str, nonce: str) -> _Transcript:
        marker = f"{MARK}{nonce} "
        transcript = cls()
        listing: list[str] | None = None
        for line in stdout.splitlines():
            if line.startswith(marker):
                event, _, value = line.removeprefix(marker).partition(" ")
                if event == "listing":
                    listing = None if value == "end" else transcript.listings.setdefault(value, [])
                    continue
                transcript.events[event] = value
                transcript.outputs[event] = transcript.pending
                transcript.pending = []
            elif listing is not None:
                listing.append(line)
            else:
                transcript.pending.append(line)
        return transcript

    def exit_code(self, step: str) -> int | None:
        value = self.events.get(step)
        return int(value) if value is not None and value.lstrip("-").isdigit() else None

    def output(self, step: str) -> str:
        return tail("\n".join(self.outputs.get(step, [])))


def interpret(done: Completed, nonce: str) -> RunResult:
    """Turn one ``docker run`` of :data:`DRIVER` into a :class:`RunResult`."""
    transcript = _Transcript.parse(done.stdout, nonce)
    solution_exit = transcript.exit_code("solution")
    grader_exit = transcript.exit_code("grader")
    if done.code is None:
        output = tail("\n".join([*transcript.pending, *done.stderr.splitlines()]))
        return RunResult(solution_exit, None, timed_out=True, output=output)
    if "refuse-root" in transcript.events:
        return RunResult(
            None,
            None,
            timed_out=False,
            output="",
            error="the container user is root; TaskGate never runs task code as root",
        )
    if "setup-failed" in transcript.events:
        return RunResult(
            None,
            None,
            timed_out=False,
            output=transcript.output("setup-failed"),
            error="could not unpack the solution and tests into the container",
        )
    if solution_exit is not None and solution_exit != 0:
        return RunResult(solution_exit, None, timed_out=False, output=transcript.output("solution"))
    if grader_exit is not None:
        return RunResult(
            solution_exit,
            grader_exit,
            timed_out=False,
            output=transcript.output("grader"),
            created=_created(
                transcript.listings.get("before", []), transcript.listings.get("after", [])
            ),
        )
    step = "grader" if "solution" in transcript.events else "run"
    why = " (killed: out of memory?)" if done.code == OOM_EXIT else ""
    return RunResult(
        solution_exit,
        None,
        timed_out=False,
        output=tail("\n".join([*transcript.pending, *done.stderr.splitlines()])),
        error=f"the container exited {done.code} before the {step} finished{why}",
    )


@dataclass
class DockerRunner:
    """Build each task's environment image and run solutions and graders in it."""

    options: RunnerOptions = field(default_factory=RunnerOptions)
    docker: str = "docker"
    """The Docker CLI (looked up on PATH at call time)."""

    _builds: dict[str, BuildResult] = field(default_factory=dict, repr=False)

    @property
    def name(self) -> str:
        return "docker"

    def _cli(
        self,
        *args: str,
        timeout: float,
        stdin: bytes | None = None,
        merge_stderr: bool = True,
        on_timeout: Callable[[], None] | None = None,
    ) -> Completed:
        exe = shutil.which(self.docker) or self.docker
        return execute(
            [exe, *args],
            timeout=timeout,
            env=_docker_env(),
            stdin=stdin,
            merge_stderr=merge_stderr,
            on_timeout=on_timeout,
        )

    def _inspect(self, tag: str) -> tuple[str, str] | None:
        """``(context label, user)`` of ``tag``, or ``None`` when there is no such image."""
        done = self._cli(
            "image",
            "inspect",
            "--format",
            INSPECT_FORMAT,
            tag,
            timeout=DOCKER_CHECK_TIMEOUT,
            merge_stderr=False,
        )
        if done.code != 0:
            return None
        label, _, user = done.stdout.strip().partition("|")
        return label, user

    def build(self, task_dir: Path) -> BuildResult:
        name = manifest.load(task_dir).dockerfile
        env_dir = task_dir / "environment"
        dockerfile = locate(env_dir, name)
        if isinstance(dockerfile, str):
            return BuildResult(ok=False, message=dockerfile)
        digest = context_digest(env_dir, name)
        if digest not in self._builds:
            try:
                self._builds[digest] = self._build(env_dir, dockerfile, digest)
            except OSError as exc:
                return BuildResult(ok=False, message=f"cannot run {self.docker}: {exc}")
        return self._builds[digest]

    def _build(self, env_dir: Path, dockerfile: Path, digest: str) -> BuildResult:
        tag = image_tag(digest)
        found = self._inspect(tag)
        if found is None or found[0] != digest:
            timeout = self.options.build_timeout_sec
            done = self._cli(
                "build",
                "--label",
                PROJECT_LABEL,
                "--label",
                f"{CONTEXT_LABEL}={digest}",
                "--tag",
                tag,
                "--file",
                str(dockerfile),
                str(env_dir),
                timeout=timeout,
            )
            if done.code is None:
                return BuildResult(
                    ok=False,
                    message=f"docker build exceeded {timeout:g}s (runner.build_timeout_sec)",
                    output=tail(done.stdout),
                )
            if done.code != 0:
                return BuildResult(
                    ok=False,
                    message=f"docker build failed (exit {done.code}): {_last_line(done.stdout)}",
                    output=tail(done.stdout),
                )
            found = self._inspect(tag)
            if found is None:
                return BuildResult(ok=False, message=f"built {tag} but cannot inspect it")
        return BuildResult(ok=True, message=f"image {tag}", image=tag, user=found[1])

    def run_argv(
        self, image: str, user: str, workdir: str, name: str, nonce: str, mode: str
    ) -> list[str]:
        """``docker run`` arguments (after ``docker``) for one locked-down driver run."""
        limits = self.options
        return [
            "run",
            "--rm",
            "--interactive",
            "--name",
            name,
            "--label",
            PROJECT_LABEL,
            "--network",
            "none",
            "--cpus",
            f"{limits.cpus:g}",
            "--memory",
            f"{limits.memory_mb}m",
            "--memory-swap",
            f"{limits.memory_mb}m",
            "--pids-limit",
            str(limits.pids_limit),
            "--user",
            run_user(user),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            f"{STAGE_DIR}:rw,exec,nosuid,size={STAGE_TMPFS_MB}m,mode=1777",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "PYTHONHASHSEED=0",
            "--workdir",
            workdir,
            "--entrypoint",
            "sh",
            image,
            "-c",
            DRIVER,
            "taskgate-driver",
            nonce,
            mode,
            STAGE_DIR,
        ]

    def run(self, task_dir: Path, *, solution: Solution, timeout_sec: float) -> RunResult:
        built = self.build(task_dir)
        if not built.ok or built.image is None:
            return RunResult(
                None,
                None,
                timed_out=False,
                output=built.output,
                error=f"the environment did not build: {built.message}",
            )
        deadline = time.monotonic() + timeout_sec
        nonce = secrets.token_hex(8)
        name = f"taskgate-run-{nonce}"
        mode = "none" if solution == "none" else "solve"
        argv = self.run_argv(
            built.image, built.user, manifest.load(task_dir).workdir, name, nonce, mode
        )
        try:
            done = self._cli(
                *argv,
                timeout=deadline - time.monotonic(),
                stdin=build_archive(task_dir, solution),
                merge_stderr=False,
                on_timeout=lambda: self._kill(name),
            )
        except OSError as exc:
            return RunResult(
                None, None, timed_out=False, output="", error=f"cannot run docker: {exc}"
            )
        return interpret(done, nonce)

    def _kill(self, name: str) -> None:
        with contextlib.suppress(OSError):
            self._cli("kill", name, timeout=KILL_TIMEOUT)


def docker_status(docker: str = "docker") -> str | None:
    """``None`` when the Docker CLI is on PATH and its daemon answers, else why not."""
    exe = shutil.which(docker)
    if exe is None:
        return f"{docker} is not on PATH"
    try:
        done = execute(
            [exe, "version", "--format", "{{.Server.Version}}"],
            timeout=DOCKER_CHECK_TIMEOUT,
            env=_docker_env(),
            merge_stderr=False,
        )
    except OSError as exc:
        return f"cannot run {exe}: {exc}"
    if done.code is None:
        return f"docker version did not answer within {DOCKER_CHECK_TIMEOUT:g}s"
    if done.code != 0:
        return f"the Docker daemon did not answer: {_last_line(done.stderr) or f'exit {done.code}'}"
    return None


def select_runner(
    choice: RunnerChoice, options: RunnerOptions | None = None, docker: str = "docker"
) -> tuple[Runner, str | None]:
    """The runner for ``choice`` and, when ``auto`` fell back to local, a note saying why."""
    if choice is RunnerChoice.LOCAL:
        return LocalRunner(), None
    problem = docker_status(docker)
    if problem is None:
        return DockerRunner(options or RunnerOptions(), docker), None
    if choice is RunnerChoice.DOCKER:
        raise RunnerUnavailableError(f"--runner docker: {problem}")
    return LocalRunner(), f"Docker is not available ({problem}); using the local runner"
