"""Gates TG301 (environment builds), TG302 (images pinned) and TG403 (stub fails)."""

from dataclasses import dataclass, field
from pathlib import Path

from fakedocker import FakeDocker
from taskfactory import BASE_IMAGE, make_task
from taskgate.docker_runner import DockerRunner
from taskgate.engine import run_gates
from taskgate.gates import BUILTIN_GATES
from taskgate.results import GateResult, Status
from taskgate.runner import BuildResult, LocalRunner, Runner, RunResult, Solution, Stub

EXISTS_ONLY = (
    "from pathlib import Path\n\n"
    "def test_exists() -> None:\n"
    "    assert Path('output/greeting.txt').is_file()\n"
)
PASSED = RunResult(0, 0, timed_out=False, output="1 passed")


def gates(task: Path, *codes: str, runner: Runner | None = None) -> dict[str, GateResult]:
    chosen = [g for g in BUILTIN_GATES if g.code in codes or g.code in ("TG101", "TG401")]
    return {r.code: r for r in run_gates(task, gates=chosen, runner=runner)}


def test_local_runner_checks_the_dockerfile_statically(tmp_path: Path) -> None:
    result = gates(make_task(tmp_path / "echo"), "TG301")["TG301"]
    assert (result.status, result.message) == (
        Status.PASS,
        "Dockerfile passes the static checks; not built: the local runner does not use Docker",
    )


def test_docker_runner_builds_the_environment(tmp_path: Path, fake_docker: FakeDocker) -> None:
    task = make_task(tmp_path / "echo")
    runner = DockerRunner()
    result = gates(task, "TG301", runner=runner)["TG301"]
    assert result.status is Status.PASS
    assert result.message == f"environment builds: image {runner.build(task).image}"
    assert len(fake_docker.calls("build")) == 1


def test_static_problems_and_build_failures_are_reported_together(
    tmp_path: Path, fake_docker: FakeDocker
) -> None:
    task = make_task(tmp_path / "echo", dockerfile="FROM alpine\nRUN FAKE_BUILD_FAIL\n")
    result = gates(task, "TG301", runner=DockerRunner())["TG301"]
    assert result.status is Status.FAIL
    assert result.message == (
        "the final stage has no USER instruction, so the image runs as root; "
        "environment/workspace/ is never copied into the image; "
        "docker build failed (exit 1): ERROR: failed to build: failed to solve: exit code: 3"
    )
    assert result.fix_hint is not None


def test_a_missing_dockerfile_is_reported_once(tmp_path: Path, fake_docker: FakeDocker) -> None:
    task = make_task(tmp_path / "echo")
    (task / "environment" / "Dockerfile").unlink()
    results = gates(task, "TG301", "TG302", "TG402", runner=DockerRunner())
    assert results["TG301"].message == "environment/Dockerfile not found"
    assert results["TG302"].status is Status.SKIP
    assert results["TG302"].message == "skipped: environment/Dockerfile not found (see TG301)"
    assert results["TG401"].message == "skipped: the environment did not build (see TG301)"
    assert results["TG402"].status is Status.SKIP


def test_environment_gates_skip_without_an_environment(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo")
    for path in sorted((task / "environment").rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    (task / "environment").rmdir()
    results = gates(task, "TG301", "TG302")
    assert results["TG301"].message == "skipped: environment/ is missing (see TG101)"
    assert results["TG302"].status is Status.SKIP


def test_pinned_unpinned_and_stage_only_images(tmp_path: Path) -> None:
    pinned = gates(make_task(tmp_path / "pinned"), "TG302")["TG302"]
    assert (pinned.status, pinned.message) == (Status.PASS, "1 image pinned by sha256 digest")
    two = make_task(
        tmp_path / "two",
        dockerfile=f"FROM {BASE_IMAGE} AS build\nFROM {BASE_IMAGE}\nUSER 1\n",
    )
    assert gates(two, "TG302")["TG302"].message == "2 images pinned by sha256 digest"
    scratch = make_task(tmp_path / "scratch", dockerfile="FROM scratch\n")
    assert gates(scratch, "TG302")["TG302"].message == (
        "no external images (scratch or build stages only)"
    )
    unpinned = make_task(
        tmp_path / "unpinned",
        dockerfile="ARG V\nFROM python:3.12-slim\nFROM alpine:$V\nCOPY --from=busybox /a /a\n",
    )
    result = gates(unpinned, "TG302")["TG302"]
    assert result.status is Status.FAIL
    assert result.blocking
    assert result.message == (
        "3 images are not pinned by digest: line 2: FROM python:3.12-slim; "
        "line 3: FROM alpine:$V (a variable in it has no default); "
        "line 4: COPY --from busybox"
    )


def test_stub_gate_passes_a_grader_that_checks_content(tmp_path: Path) -> None:
    result = gates(make_task(tmp_path / "echo"), "TG403")["TG403"]
    assert (result.status, result.message) == (
        Status.PASS,
        "a stub that writes output/greeting.txt empty fails the grader (1 failed)",
    )


def test_stub_gate_blocks_a_grader_that_only_checks_existence(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo", grader=EXISTS_ONLY)
    results = {r.code: r for r in run_gates(task)}
    assert results["TG402"].status is Status.PASS
    failed = results["TG403"]
    assert failed.blocking
    assert (
        failed.message
        == "the grader passes a stub that writes output/greeting.txt empty (1 passed)"
    )
    assert failed.fix_hint is not None
    assert "strict=True" in failed.fix_hint


def test_stub_gate_in_docker(tmp_path: Path, fake_docker: FakeDocker) -> None:
    task = make_task(tmp_path / "echo", grader=EXISTS_ONLY)
    result = gates(task, "TG403", runner=DockerRunner())["TG403"]
    assert result.status is Status.FAIL
    runs = fake_docker.calls("run")
    assert [argv[-2] for argv in runs] == ["solve", "solve"]


@dataclass
class CannedRunner:
    """A runner whose reference run creates ``created`` and whose stub run returns ``stub``."""

    created: tuple[str, ...]
    stub: RunResult
    seen: list[Solution] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "canned"

    def build(self, task_dir: Path) -> BuildResult:
        return BuildResult(ok=True, message="canned")

    def run(self, task_dir: Path, *, solution: Solution, timeout_sec: float) -> RunResult:
        self.seen.append(solution)
        if solution == "reference":
            return RunResult(0, 0, timed_out=False, output="1 passed", created=self.created)
        return self.stub


def test_stub_messages_name_the_files(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo")
    failed = RunResult(0, 1, timed_out=False, output="1 failed")
    no_op = CannedRunner((), failed)
    assert gates(task, "TG403", runner=no_op)["TG403"].message == (
        "a no-op stub fails the grader (1 failed)"
    )
    assert no_op.seen == ["reference", Stub()]
    many = CannedRunner(("a", "b", "c", "d", "e"), PASSED)
    assert gates(task, "TG403", runner=many)["TG403"].message == (
        "the grader passes a stub that writes 5 files empty (a, b, c and 2 more) (1 passed)"
    )
    two = CannedRunner(("a", "b"), failed)
    assert gates(task, "TG403", runner=two)["TG403"].message == (
        "a stub that writes 2 files empty (a, b) fails the grader (1 failed)"
    )


def test_stub_run_failures_are_described(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo")
    cases = {
        RunResult(None, None, timed_out=True, output=""): (
            "the stub run exceeded the 60s budget (task.timeout_sec)"
        ),
        RunResult(0, 5, timed_out=False, output="no tests ran"): "the grader collected no tests",
        RunResult(2, None, timed_out=False, output="mkdir: denied"): (
            "the stub solution exited 2: mkdir: denied"
        ),
        RunResult(None, None, timed_out=False, output="", error="container exited 137"): (
            "the stub run could not run: container exited 137"
        ),
    }
    for stub, message in cases.items():
        result = gates(task, "TG403", runner=CannedRunner(("out",), stub))["TG403"]
        assert (result.status, result.message) == (Status.FAIL, message)


def test_every_new_gate_passes_the_factory_task_on_the_local_runner(tmp_path: Path) -> None:
    results = run_gates(make_task(tmp_path / "echo"), runner=LocalRunner())
    assert {r.code: r.status for r in results if r.code[2] in "34"} == {
        "TG301": Status.PASS,
        "TG302": Status.PASS,
        "TG401": Status.PASS,
        "TG402": Status.PASS,
        "TG403": Status.PASS,
    }
