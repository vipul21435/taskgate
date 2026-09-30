"""Opt-in: the Docker runner against a real Docker daemon.

Run with ``make test-docker`` (``TASKGATE_DOCKER_TESTS=1 uv run pytest -m docker``).
The tests skip unless that variable is 1 and the daemon answers. The first run
builds the sample task's image (python:3.12-slim pinned by digest, plus pytest),
which needs network access for the build only; later runs reuse the image by its
content-derived tag. The containers themselves never get a network.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from taskfactory import (
    COPY_WORKSPACE,
    DOCKERFILE,
    FAITHFUL_GRADER,
    HARD_LINK_SOLVE,
    make_task,
)
from taskgate.config import RunnerOptions
from taskgate.docker_runner import DockerRunner, docker_status
from taskgate.engine import run_gates
from taskgate.gates import BUILTIN_GATES
from taskgate.results import Status
from taskgate.runner import MAX_JUNIT_BYTES, LocalRunner

pytestmark = pytest.mark.docker

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "sample-repo" / "tasks"
MEMORY_MB = 256
PROBE_SOLVE = """\
#!/bin/sh
set -eu
mkdir -p output
id -u > output/uid.txt
cat /sys/fs/cgroup/memory.max > output/memory.txt
cat /sys/fs/cgroup/pids.max > output/pids.txt
python3 - <<'PY'
import socket
try:
    socket.create_connection(("1.1.1.1", 53), timeout=3)
    state = "online"
except OSError:
    state = "offline"
open("output/network.txt", "w").write(state)
PY
"""
PROBE_GRADER = f"""\
from pathlib import Path


def read(name: str) -> str:
    return Path("output", name).read_text().strip()


def test_not_root() -> None:
    assert read("uid.txt") != "0"


def test_no_network() -> None:
    assert read("network.txt") == "offline"


def test_limits() -> None:
    assert read("memory.txt") == str({MEMORY_MB} * 1024 * 1024)
    assert read("pids.txt") == "256"
"""


FLAKY_GRADER = """\
import os
from pathlib import Path

SEEN: list[int] = []


def test_first() -> None:
    SEEN.append(1)


def test_second_needs_the_first() -> None:
    assert SEEN == [1]


def test_seeds_and_a_fresh_workspace() -> None:
    assert os.environ["PYTHONHASHSEED"] == os.environ.get("TASKGATE_SEED", "0")
    marker = Path("graded.marker")
    assert not marker.exists()
    marker.write_text("x")
"""


@pytest.fixture(scope="module")
def runner() -> DockerRunner:
    if os.environ.get("TASKGATE_DOCKER_TESTS") != "1":
        pytest.skip("set TASKGATE_DOCKER_TESTS=1 to run the tests against real Docker")
    problem = docker_status()
    if problem is not None:
        pytest.skip(problem)
    return DockerRunner(RunnerOptions(memory_mb=MEMORY_MB))


def sample(tmp_path: Path) -> Path:
    return Path(shutil.copytree(SAMPLE / "modular-inverse", tmp_path / "modular-inverse"))


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True).stdout


def test_the_sample_task_passes_every_gate_in_docker(tmp_path: Path, runner: DockerRunner) -> None:
    results = {r.code: r for r in run_gates(sample(tmp_path), runner=runner)}
    assert {code: r.status for code, r in results.items()} == dict.fromkeys(results, Status.PASS)
    image = runner.build(tmp_path / "modular-inverse").image
    assert image is not None
    assert results["TG301"].message == f"environment builds: image {image}"
    assert docker("image", "inspect", "--format", '{{index .Config.Labels "project"}}', image) == (
        "taskgate\n"
    )


def test_runs_have_no_network_no_root_and_the_configured_limits(
    tmp_path: Path, runner: DockerRunner
) -> None:
    task = sample(tmp_path)
    (task / "solution" / "solve.sh").write_text(PROBE_SOLVE, encoding="utf-8")
    (task / "tests" / "test_outputs.py").write_text(PROBE_GRADER, encoding="utf-8")
    result = runner.run(task, solution="reference", timeout_sec=120)
    assert result.grader_passed, result.output
    assert result.summary == "3 passed"


def test_a_memory_hog_is_killed(tmp_path: Path, runner: DockerRunner) -> None:
    task = sample(tmp_path)
    hog = "python3 -c \"data = b'x' * (512 * 1024 * 1024)\"\n"
    (task / "solution" / "solve.sh").write_text(hog, encoding="utf-8")
    result = runner.run(task, solution="reference", timeout_sec=120)
    assert result.solution_exit == 137
    assert result.grader_exit is None


def test_a_slow_solution_is_stopped_and_its_container_removed(
    tmp_path: Path, runner: DockerRunner
) -> None:
    task = sample(tmp_path)
    (task / "solution" / "solve.sh").write_text("sleep 60\n", encoding="utf-8")
    started = time.monotonic()
    result = runner.run(task, solution="reference", timeout_sec=5)
    assert result.timed_out
    assert time.monotonic() - started < 40
    for _ in range(20):
        left = docker(
            "ps", "-aq", "--filter", "label=project=taskgate", "--filter", "name=taskgate-run-"
        )
        if not left.strip():
            break
        time.sleep(0.5)
    assert left.strip() == ""


def test_the_determinism_gate_finds_the_same_flips_in_docker(
    tmp_path: Path, runner: DockerRunner
) -> None:
    task = sample(tmp_path)
    (task / "tests" / "test_outputs.py").write_text(FLAKY_GRADER, encoding="utf-8")
    gates = [g for g in BUILTIN_GATES if g.code in ("TG101", "TG401", "TG501")]
    in_docker = {r.code: r for r in run_gates(task, gates=gates, runner=runner)}["TG501"]
    local = {r.code: r for r in run_gates(task, gates=gates, runner=LocalRunner())}["TG501"]
    assert in_docker.status is Status.FAIL
    assert in_docker.message.endswith(
        "1 test flipped: tests/test_outputs.py::test_second_needs_the_first"
    )
    assert in_docker.message == local.message
    assert [d.replace("--runner docker", "--runner local") for d in in_docker.details] == list(
        local.details
    )


RUNTIME_GATES = tuple(g for g in BUILTIN_GATES if g.code in ("TG101", "TG401", "TG501"))
WORLD_WRITABLE_WORKDIR = DOCKERFILE.replace(
    "mkdir /workspace && chown agent /workspace", "mkdir -m 777 /workspace"
).format(copy=COPY_WORKSPACE)


def tg501(task: Path, runner: DockerRunner | LocalRunner) -> tuple[Status, str, tuple[str, ...]]:
    results = {r.code: r for r in run_gates(task, gates=RUNTIME_GATES, runner=runner)}
    assert results["TG401"].status is Status.PASS, results["TG401"].message
    result = results["TG501"]
    return result.status, result.message, result.details


def test_reruns_restore_a_root_owned_world_writable_workdir(
    tmp_path: Path, runner: DockerRunner
) -> None:
    """Review finding: tar could not reset the workdir's own times and mode, so every
    task in a ``mkdir -m 777`` workdir failed TG501."""
    task = make_task(tmp_path / "echo", dockerfile=WORLD_WRITABLE_WORKDIR)
    status, message, _ = tg501(task, runner)
    assert status is Status.PASS, message


def test_reruns_keep_modes_links_pipes_and_subsecond_mtimes(
    tmp_path: Path, runner: DockerRunner
) -> None:
    """Review finding: reruns 2-5 lost group-write bits and sub-second mtimes."""
    task = make_task(tmp_path / "echo", solve=HARD_LINK_SOLVE, grader=FAITHFUL_GRADER)
    status, message, _ = tg501(task, runner)
    assert status is Status.PASS, message


def test_junit_over_the_limit_gives_the_same_verdict_on_both_runners(
    tmp_path: Path, runner: DockerRunner
) -> None:
    grader = (
        "def test_big(record_property) -> None:\n"
        f"    record_property('output', 'x' * ({MAX_JUNIT_BYTES} + 1))\n"
    )
    task = make_task(tmp_path / "echo", grader=grader)
    status, message, details = tg501(task, runner)
    local = tg501(task, LocalRunner())
    assert status is Status.FAIL
    assert (status, message) == local[:2]
    assert [d.replace("--runner docker", "--runner local") for d in details] == list(local[2])
