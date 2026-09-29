"""DockerRunner against the fake Docker CLI (tests/fake_docker_cli.py) on PATH."""

import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from fakedocker import FakeDocker
from taskfactory import ORDER_DEPENDENT_GRADER, make_task
from taskgate.config import RunnerOptions
from taskgate.determinism import parse_junit
from taskgate.docker_runner import (
    DRIVER,
    NOBODY,
    DockerRunner,
    RunnerChoice,
    RunnerUnavailableError,
    build_archive,
    build_error,
    context_digest,
    docker_status,
    image_tag,
    interpret,
    interpret_regrade,
    run_user,
    select_runner,
)
from taskgate.dockerfile import locate
from taskgate.runner import Completed, GraderRun, LocalRunner, Regrade, Stub, plugin_source

EXISTS_ONLY = (
    "from pathlib import Path\n\n"
    "def test_exists() -> None:\n"
    "    assert Path('output/greeting.txt').is_file()\n"
)


def flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def test_build_tags_by_context_digest_with_the_project_label(
    tmp_path: Path, fake_docker: FakeDocker
) -> None:
    task = make_task(tmp_path / "echo")
    built = DockerRunner().build(task)
    digest = context_digest(task / "environment", "Dockerfile")
    assert built.ok
    assert built.image == image_tag(digest) == f"taskgate-env:{digest[:16]}"
    assert built.message == f"image {built.image}"
    assert built.user == "agent"
    (argv,) = fake_docker.calls("build")
    assert argv[argv.index("--tag") + 1] == built.image
    labels = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--label"]
    assert labels == ["project=taskgate", f"taskgate.context={digest}"]
    assert flag(argv, "--file") == str(task / "environment" / "Dockerfile")
    assert argv[-1] == str(task / "environment")


def test_an_unchanged_context_is_built_once(tmp_path: Path, fake_docker: FakeDocker) -> None:
    task = make_task(tmp_path / "echo")
    runner = DockerRunner()
    assert runner.build(task) is runner.build(task)
    assert DockerRunner().build(make_task(tmp_path / "copy" / "echo")).ok
    assert len(fake_docker.calls("build")) == 1


def test_a_stale_tag_with_another_label_is_rebuilt(tmp_path: Path, fake_docker: FakeDocker) -> None:
    task = make_task(tmp_path / "echo")
    tag = DockerRunner().build(task).image
    images = fake_docker.images()
    images[tag]["labels"]["taskgate.context"] = "something else"
    (fake_docker.state / "images.json").write_text(json.dumps(images))
    assert DockerRunner().build(task).ok
    assert len(fake_docker.calls("build")) == 2


def test_digest_follows_content_and_modes_not_mtimes_or_caches(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo")
    env = task / "environment"
    first = context_digest(env, "Dockerfile")
    (env / "workspace" / "__pycache__").mkdir()
    (env / "workspace" / "__pycache__" / "x.pyc").write_bytes(b"\0")
    (env / ".DS_Store").write_bytes(b"\0")
    (env / "Dockerfile").touch()
    (task / "tests" / "test_more.py").write_text("", encoding="utf-8")
    assert context_digest(env, "Dockerfile") == first
    assert context_digest(env, "Other.Dockerfile") != first
    seed = env / "workspace" / "input" / "name.txt"
    seed.chmod(0o755)
    executable = context_digest(env, "Dockerfile")
    assert executable != first
    seed.chmod(0o644)
    seed.write_text("world!\n", encoding="utf-8")
    assert context_digest(env, "Dockerfile") not in (first, executable)
    seed.write_text("world\n", encoding="utf-8")
    assert context_digest(env, "Dockerfile") == first
    (env / "link").symlink_to("workspace")
    linked = context_digest(env, "Dockerfile")
    assert linked != first
    (env / "link").unlink()
    (env / "link").symlink_to("Dockerfile")
    assert context_digest(env, "Dockerfile") != linked


def test_build_failures_are_reported_with_the_last_log_line(
    tmp_path: Path, fake_docker: FakeDocker
) -> None:
    task = make_task(tmp_path / "echo", dockerfile="FROM scratch\nRUN FAKE_BUILD_FAIL\n")
    built = DockerRunner().build(task)
    assert not built.ok
    assert built.message == (
        "docker build failed (exit 1): ERROR: failed to build: failed to solve: exit code: 3"
    )
    assert "did not complete successfully" in built.output
    run = DockerRunner().run(task, solution="reference", timeout_sec=30)
    assert run.error == f"the environment did not build: {built.message}"
    assert fake_docker.calls("run") == []


def test_a_hanging_build_hits_the_build_timeout(
    tmp_path: Path, fake_docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DOCKER_BUILD", "hang")
    runner = DockerRunner(RunnerOptions(build_timeout_sec=1))
    built = runner.build(make_task(tmp_path / "echo"))
    assert not built.ok
    assert built.message == "docker build exceeded 1s (runner.build_timeout_sec)"


def test_dockerfile_must_exist_inside_the_build_context(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo")
    manifest = task / "task.toml"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(text.replace('"Dockerfile"', '"../task.toml"'), encoding="utf-8")
    built = DockerRunner().build(task)
    assert (built.ok, built.message) == (
        False,
        "environment.dockerfile '../task.toml' points outside environment/",
    )
    manifest.write_text(text.replace('"Dockerfile"', '"Missing.Dockerfile"'), encoding="utf-8")
    assert DockerRunner().build(task).message == "environment/Missing.Dockerfile not found"


def test_a_missing_docker_binary_is_a_build_failure(tmp_path: Path) -> None:
    built = DockerRunner(docker=str(tmp_path / "no-such-docker")).build(make_task(tmp_path / "e"))
    assert not built.ok
    assert built.message.startswith(f"cannot run {tmp_path / 'no-such-docker'}:")


def test_reference_run_is_locked_down_and_passes(tmp_path: Path, fake_docker: FakeDocker) -> None:
    task = make_task(tmp_path / "echo")
    result = DockerRunner().run(task, solution="reference", timeout_sec=60)
    assert (result.solution_exit, result.grader_exit, result.error) == (0, 0, None)
    assert result.summary == "1 passed"
    assert result.created == ("output/greeting.txt",)
    (argv,) = fake_docker.calls("run")
    assert flag(argv, "--network") == "none"
    assert flag(argv, "--cpus") == "1"
    assert flag(argv, "--memory") == flag(argv, "--memory-swap") == "1024m"
    assert flag(argv, "--pids-limit") == "256"
    assert flag(argv, "--user") == "agent"
    assert flag(argv, "--cap-drop") == "ALL"
    assert flag(argv, "--security-opt") == "no-new-privileges"
    assert flag(argv, "--tmpfs") == "/taskgate:rw,exec,nosuid,size=256m,mode=1777"
    assert flag(argv, "--workdir") == "/workspace"
    assert flag(argv, "--label") == "project=taskgate"
    assert "--rm" in argv
    assert argv[argv.index("--entrypoint") + 2 :][:3] == [
        DockerRunner().build(task).image,
        "-c",
        DRIVER,
    ]
    assert argv[-2:] == ["solve", "/taskgate"]


def test_limits_come_from_the_runner_options(tmp_path: Path, fake_docker: FakeDocker) -> None:
    options = RunnerOptions(cpus=0.5, memory_mb=256, pids_limit=64)
    DockerRunner(options).run(make_task(tmp_path / "echo"), solution="none", timeout_sec=60)
    (argv,) = fake_docker.calls("run")
    assert (flag(argv, "--cpus"), flag(argv, "--memory"), flag(argv, "--pids-limit")) == (
        "0.5",
        "256m",
        "64",
    )
    assert argv[-2] == "none"


def test_baseline_and_stub_runs(tmp_path: Path, fake_docker: FakeDocker) -> None:
    runner = DockerRunner()
    strict = make_task(tmp_path / "strict")
    baseline = runner.run(strict, solution="none", timeout_sec=60)
    assert (baseline.solution_exit, baseline.grader_exit) == (None, 1)
    assert baseline.summary == "1 failed"
    stub = Stub(("output/greeting.txt",))
    assert runner.run(strict, solution=stub, timeout_sec=60).summary == "1 failed"
    lenient = make_task(tmp_path / "lenient", grader=EXISTS_ONLY)
    result = runner.run(lenient, solution=stub, timeout_sec=60)
    assert result.grader_passed
    assert result.created == ("output/greeting.txt",)


def test_a_root_image_runs_as_nobody_and_the_driver_refuses_uid_0(
    tmp_path: Path, fake_docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = make_task(tmp_path / "echo", dockerfile="FROM scratch\nCOPY workspace/ /workspace/\n")
    assert DockerRunner().run(task, solution="reference", timeout_sec=60).grader_passed
    assert flag(fake_docker.calls("run")[0], "--user") == NOBODY
    monkeypatch.setenv("FAKE_DOCKER_AS_ROOT", "1")
    refused = DockerRunner().run(task, solution="reference", timeout_sec=60)
    assert refused.error == "the container user is root; TaskGate never runs task code as root"
    assert refused.grader_exit is None


@pytest.mark.parametrize(
    ("user", "expected"),
    [
        ("", NOBODY),
        ("root", NOBODY),
        ("0", NOBODY),
        ("0:0", NOBODY),
        ("root:staff", NOBODY),
        ("agent", "agent"),
        ("1000:1000", "1000:1000"),
    ],
)
def test_run_user(user: str, expected: str) -> None:
    assert run_user(user) == expected


def test_a_failing_solution_stops_before_the_grader(
    tmp_path: Path, fake_docker: FakeDocker
) -> None:
    task = make_task(tmp_path / "echo", solve="echo broken\nexit 3\n")
    result = DockerRunner().run(task, solution="reference", timeout_sec=60)
    assert (result.solution_exit, result.grader_exit) == (3, None)
    assert result.summary == "broken"


def test_a_slow_solution_is_killed_with_its_container(
    tmp_path: Path, fake_docker: FakeDocker
) -> None:
    task = make_task(tmp_path / "echo", solve="sleep 30\n")
    runner = DockerRunner()
    runner.build(task)
    result = runner.run(task, solution="reference", timeout_sec=1.5)
    assert result.timed_out
    assert (result.solution_exit, result.grader_exit) == (None, None)
    (run_argv,) = fake_docker.calls("run")
    assert fake_docker.calls("kill") == [["kill", flag(run_argv, "--name")]]


def test_a_run_that_exits_while_its_container_is_killed_is_still_a_timeout(
    tmp_path: Path, fake_docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``docker run`` exits during ``docker kill``, macOS answers ``killpg`` with EPERM;
    the run is reported as a timeout, not as 'cannot run docker'."""
    task = make_task(tmp_path / "echo", solve="sleep 30\n")
    runner = DockerRunner()
    runner.build(task)
    real_killpg = os.killpg

    def killpg(pgid: int, sig: int) -> None:
        real_killpg(pgid, sig)
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "killpg", killpg)
    result = runner.run(task, solution="reference", timeout_sec=1.5)
    assert (result.timed_out, result.error) == (True, None)


def test_the_task_workdir_is_the_containers_workdir(
    tmp_path: Path, fake_docker: FakeDocker
) -> None:
    task = make_task(tmp_path / "echo")
    manifest = task / "task.toml"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(text.replace('"/workspace"', '"/home/agent/task"'), encoding="utf-8")
    DockerRunner().run(task, solution="none", timeout_sec=60)
    manifest.write_text(text.replace('"/workspace"', '"relative/dir"'), encoding="utf-8")
    DockerRunner().run(task, solution="none", timeout_sec=60)
    assert [flag(argv, "--workdir") for argv in fake_docker.calls("run")] == [
        "/home/agent/task",
        "/workspace",
    ]


def test_created_files_in_a_container_leave_out_seeded_changed_and_cache_files(
    tmp_path: Path, fake_docker: FakeDocker
) -> None:
    solve = (
        "echo changed > input/name.txt\n"
        "mkdir -p out/deep __pycache__ pkg/.pytest_cache\n"
        "echo x > out/deep/a.txt\n"
        "echo y > b.txt\n"
        "echo z > __pycache__/c.pyc\n"
        "echo z > mod.pyc\n"
        "echo z > .DS_Store\n"
        "echo z > pkg/.pytest_cache/v\n"
    )
    grader = "def test_ok() -> None:\n    pass\n"
    task = make_task(tmp_path / "echo", solve=solve, grader=grader)
    result = DockerRunner().run(task, solution="reference", timeout_sec=60)
    assert result.created == ("b.txt", "out/deep/a.txt")


def test_a_container_killed_before_any_output_is_an_error(
    tmp_path: Path, fake_docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = make_task(tmp_path / "echo")
    runner = DockerRunner()
    runner.build(task)
    monkeypatch.setenv("FAKE_DOCKER_RUN_EXIT", "137")
    result = runner.run(task, solution="reference", timeout_sec=60)
    assert (
        result.error == "the container exited 137 before the run finished (killed: out of memory?)"
    )
    assert result.output == "fake: the container was killed"


def test_interpret_edge_cases() -> None:
    nonce = "abc"
    mark = f"@@taskgate-{nonce}"
    grader_died = Completed(
        1, f"{mark} listing before\n{mark} listing end\n{mark} solution 0\n", ""
    )
    result = interpret(grader_died, nonce)
    assert result.error == "the container exited 1 before the grader finished"
    assert result.solution_exit == 0
    setup = interpret(Completed(0, f"tar: bad archive\n{mark} setup-failed\n", ""), nonce)
    assert setup.error == "could not unpack the solution and tests into the container"
    assert setup.output == "tar: bad archive"
    forged = interpret(Completed(0, "@@taskgate-other grader 0\n", ""), nonce)
    assert forged.grader_exit is None
    garbled = interpret(Completed(0, f"{mark} grader x\n", ""), nonce)
    assert garbled.grader_exit is None
    grader_timeout = interpret(Completed(None, f"{mark} solution 0\nstill testing\n", ""), nonce)
    assert grader_timeout.timed_out
    assert (grader_timeout.solution_exit, grader_timeout.output) == (0, "still testing")


def test_archive_holds_tests_config_and_the_chosen_solution(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo")
    (task / "solution" / "helper.py").write_text("print(1)\n", encoding="utf-8")
    (task / "solution" / "solve.sh").chmod(0o755)
    (task / "tests" / "__pycache__").mkdir()
    (task / "tests" / "__pycache__" / "x.pyc").write_bytes(b"\0")

    def members(solution: object) -> dict[str, tuple[int, int]]:
        data = build_archive(task, solution)  # type: ignore[arg-type]
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            return {m.name: (m.mode, m.mtime) for m in tar.getmembers()}

    reference = members("reference")
    assert sorted(reference) == [
        "pytest.ini",
        "solution",
        "solution/helper.py",
        "solution/solve.sh",
        "tests",
        "tests/test_outputs.py",
    ]
    assert reference["solution/solve.sh"] == (0o755, 0)
    assert reference["solution/helper.py"] == (0o644, 0)
    assert sorted(members("none")) == ["pytest.ini", "tests", "tests/test_outputs.py"]
    stub = members(Stub(("out.txt",)))
    assert stub["solution/solve.sh"][0] == 0o755
    assert "solution/helper.py" not in stub
    assert build_archive(task, "reference") == build_archive(task, "reference")


def test_docker_status(
    tmp_path: Path, fake_docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert docker_status() is None
    monkeypatch.setenv("FAKE_DOCKER_DAEMON", "down")
    assert docker_status() == (
        "the Docker daemon did not answer: "
        "Cannot connect to the Docker daemon. Is the docker daemon running?"
    )
    assert docker_status("no-such-docker-cli") == "no-such-docker-cli is not on PATH"


def test_select_runner(
    tmp_path: Path, fake_docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = RunnerOptions(cpus=2.0)
    runner, note = select_runner(RunnerChoice.AUTO, options)
    assert isinstance(runner, DockerRunner)
    assert (runner.name, runner.options, note) == ("docker", options, None)
    assert isinstance(select_runner(RunnerChoice.DOCKER)[0], DockerRunner)
    local, note = select_runner(RunnerChoice.LOCAL)
    assert isinstance(local, LocalRunner)
    assert note is None
    monkeypatch.setenv("FAKE_DOCKER_DAEMON", "down")
    fallback, note = select_runner(RunnerChoice.AUTO)
    assert isinstance(fallback, LocalRunner)
    assert note is not None
    assert note.startswith("Docker is not available (the Docker daemon did not answer: ")
    assert note.endswith("); using the local runner")
    with pytest.raises(RunnerUnavailableError, match=r"^--runner docker: the Docker daemon"):
        select_runner(RunnerChoice.DOCKER)


def test_an_image_that_vanishes_after_its_build_is_a_failure(
    tmp_path: Path, fake_docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DOCKER_BUILD", "forget")
    built = DockerRunner().build(make_task(tmp_path / "echo"))
    assert not built.ok
    assert built.message.startswith("built taskgate-env:")
    assert built.message.endswith(" but cannot inspect it")


def test_a_cli_that_disappears_before_the_run_is_an_error(
    tmp_path: Path, fake_docker: FakeDocker
) -> None:
    task = make_task(tmp_path / "echo")
    runner = DockerRunner()
    assert runner.build(task).ok
    runner.docker = str(tmp_path / "gone")
    result = runner.run(task, solution="reference", timeout_sec=30)
    assert result.error is not None
    assert result.error.startswith("cannot run docker: ")


def test_docker_status_on_a_broken_or_silent_cli(
    tmp_path: Path, fake_docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = tmp_path / "broken-bin" / "docker"
    broken.parent.mkdir()
    broken.write_bytes(b"\0\1\2\3")
    broken.chmod(0o755)
    assert (docker_status(str(broken)) or "").startswith(f"cannot run {broken}: ")
    monkeypatch.setattr("taskgate.docker_runner.DOCKER_CHECK_TIMEOUT", 0.5)
    monkeypatch.setenv("FAKE_DOCKER_DAEMON", "hang")
    assert docker_status() == "docker version did not answer within 0.5s"


def test_digest_skips_special_files_and_archives_keep_symlinks(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo")
    env = task / "environment"
    before = context_digest(env, "Dockerfile")
    os.mkfifo(env / "pipe")
    assert context_digest(env, "Dockerfile") == before
    (task / "tests" / "link.py").symlink_to("test_outputs.py")
    with tarfile.open(fileobj=io.BytesIO(build_archive(task, "none"))) as tar:
        link = tar.getmember("tests/link.py")
    assert link.issym()
    assert link.linkname == "test_outputs.py"


def test_a_symlink_loop_is_not_a_dockerfile(tmp_path: Path) -> None:
    env = tmp_path / "environment"
    env.mkdir()
    (env / "loop").symlink_to("loop")
    assert locate(env, "loop") == "environment/loop is a symlink loop"


def test_build_errors_drop_buildkit_cache_ids() -> None:
    log = (
        "#7 ERROR: failed to calculate checksum\n"
        "ERROR: failed to build: failed to solve: failed to compute cache key: failed to "
        'calculate checksum of ref i7ud3grebs6bn3egvnjgwsgyz::8wpgzy0uhmmqctd1sskj8q87v: "/input": '
        "not found\n\n"
    )
    assert build_error(log) == (
        "ERROR: failed to build: failed to solve: failed to compute cache key: failed to "
        'calculate checksum of ref <id>: "/input": not found'
    )
    assert build_error("") == ""


def test_regrade_matches_the_local_runner(tmp_path: Path, fake_docker: FakeDocker) -> None:
    task = make_task(tmp_path / "echo", grader=ORDER_DEPENDENT_GRADER)
    seeds = (1, 2, 5)
    docker = DockerRunner().regrade(task, seeds=seeds, timeout_sec=60)
    local = LocalRunner().regrade(task, seeds=seeds, timeout_sec=60)
    assert docker.solution.solution_exit == 0
    assert docker.error is None
    assert [(r.seed, r.exit) for r in docker.runs] == [(1, 1), (2, 1), (5, 0)]
    assert [parse_junit(r.junit) for r in docker.runs] == [parse_junit(r.junit) for r in local.runs]
    assert "1 failed, 1 passed" in docker.runs[0].output
    (argv,) = fake_docker.calls("run")
    assert argv[-5:] == ["regrade", "/taskgate", "1", "2", "5"]


def test_regrade_restores_the_workspace_between_reruns(
    tmp_path: Path, fake_docker: FakeDocker
) -> None:
    grader = (
        "from pathlib import Path\n\n"
        "def test_fresh() -> None:\n"
        "    assert Path('output/greeting.txt').read_text() == 'world\\n'\n"
        "    assert not Path('graded.marker').exists()\n"
        "    Path('graded.marker').write_text('x')\n"
        "    Path('output/greeting.txt').write_text('changed by the grader')\n"
    )
    task = make_task(tmp_path / "echo", grader=grader)
    regraded = DockerRunner().regrade(task, seeds=(1, 2, 3), timeout_sec=60)
    assert [r.exit for r in regraded.runs] == [0, 0, 0]


def test_regrade_archive_carries_the_shuffle_plugin(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo")
    with tarfile.open(fileobj=io.BytesIO(build_archive(task, "reference", plugin=True))) as tar:
        plugin = tar.extractfile("plugin/taskgate_shuffle.py")
        assert plugin is not None
        assert plugin.read() == plugin_source()
    with tarfile.open(fileobj=io.BytesIO(build_archive(task, "reference"))) as tar:
        assert "plugin" not in tar.getnames()


def test_regrade_stops_at_a_failing_solution_or_a_missing_image(
    tmp_path: Path, fake_docker: FakeDocker
) -> None:
    task = make_task(tmp_path / "echo", solve="echo broken\nexit 3\n")
    regraded = DockerRunner().regrade(task, seeds=(1, 2), timeout_sec=60)
    assert (regraded.solution.solution_exit, regraded.solution.output, regraded.runs) == (
        3,
        "broken",
        (),
    )
    broken = make_task(tmp_path / "broken", dockerfile="FROM scratch\nRUN FAKE_BUILD_FAIL\n")
    unbuilt = DockerRunner().regrade(broken, seeds=(1,), timeout_sec=60)
    assert (unbuilt.solution.error or "").startswith("the environment did not build: ")


def test_regrade_kills_the_container_when_the_budget_runs_out(
    tmp_path: Path, fake_docker: FakeDocker
) -> None:
    grader = "import time\n\ndef test_slow() -> None:\n    time.sleep(30)\n"
    task = make_task(tmp_path / "echo", grader=grader)
    runner = DockerRunner()
    runner.build(task)
    regraded = runner.regrade(task, seeds=(1, 2), timeout_sec=1)
    assert [(r.seed, r.exit, r.junit) for r in regraded.runs] == [(1, None, "")]
    assert regraded.error is None
    assert len(fake_docker.calls("kill")) == 1


def test_interpret_regrade_edge_cases() -> None:
    nonce = "abc"
    mark = f"@@taskgate-{nonce}"
    solved = f"{mark} listing before\n{mark} listing end\n{mark} solution 0\n"

    def regrade(code: int, stdout: str, seeds: tuple[int, ...]) -> Regrade:
        return interpret_regrade(Completed(code, stdout, ""), nonce, seeds)

    snapshot = regrade(0, f"{solved}tar: full\n{mark} snapshot-failed\n", (1,))
    assert snapshot.runs == ()
    assert snapshot.error == "could not save the solved workspace for the reruns"
    one = (
        f"{solved}1 passed\n{mark} regrade 1 0\n{mark} listing junit-1\n<t/>\n{mark} listing end\n"
    )
    restore = regrade(0, f"{one}{mark} restore-failed 2\n", (1, 2))
    assert restore.runs == (GraderRun(1, 0, "<t/>", "1 passed"),)
    assert restore.error == "could not restore the workspace before rerun 2"
    oom = regrade(137, one, (1, 2))
    assert oom.error == "the container exited 137 after 1 of 2 reruns (killed: out of memory?)"
    assert regrade(1, one, (1, 2, 3)).error == "the container exited 1 after 1 of 3 reruns"
    assert regrade(0, f"{solved}{mark} regrade 4 x\n", (4,)).runs == (GraderRun(4, None, "", ""),)
    unsolved = regrade(0, f"{mark} setup-failed\n", (1,))
    assert unsolved.solution.error == "could not unpack the solution and tests into the container"
