"""The result cache through the CLI: taskgate check skips unchanged tasks; cache stats/prune."""

from __future__ import annotations

import fcntl
import json
import os
from functools import partial
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from fakedocker import FakeDocker
from gitrepo import GitRepo
from taskfactory import LENIENT_GRADER, SOLVE, make_task
from taskgate.cache import LOCK_FILE, ResultCache
from taskgate.cli import app

runner = CliRunner()


def counting_task(root: Path, log: Path, *, grader: str | None = None) -> Path:
    """A passing task whose solve.sh appends a line to ``log`` every time it runs."""
    solve = SOLVE + f'echo run >> "{log}"\n'
    if grader is None:
        return make_task(root / "tasks" / "echo", solve=solve)
    return make_task(root / "tasks" / "echo", solve=solve, grader=grader)


def runs(log: Path) -> int:
    return len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0


def check(*args: str) -> Result:
    return runner.invoke(app, ["check", *args])


def test_an_unchanged_task_is_skipped_and_reported_as_cached(tmp_path: Path) -> None:
    root, log = tmp_path / "root", tmp_path / "solve.log"
    task = counting_task(root, log)
    first = check("--all", str(root))
    assert first.exit_code == 0
    assert first.stdout.splitlines()[:2] == [
        "taskgate 0.1.0: all tasks, 1 task, local runner",
        "tasks/echo  PASS",
    ]
    assert runs(log) == 2  # TG401's run and TG501's solution run

    second = check("--all", str(root))
    assert second.exit_code == 0
    assert second.stdout.splitlines()[:2] == [
        "taskgate 0.1.0: all tasks, 1 task, local runner, 1 cached",
        "tasks/echo  PASS  (cached)",
    ]
    assert second.stdout.splitlines()[2:] == first.stdout.splitlines()[2:]
    assert runs(log) == 2

    for number, path in enumerate(sorted(task.rglob("*"))):
        os.utime(path, (1_000_000 + number, 1_000_000 + number))
    assert "(cached)" in check("--all", str(root)).stdout
    assert runs(log) == 2

    fresh = check("--all", str(root), "--no-cache")
    assert "cached" not in fresh.stdout
    assert runs(log) == 4

    (task / "instruction.md").write_text("Copy input/name.txt to output/greeting.txt!\n")
    changed = check("--all", str(root))
    assert "cached" not in changed.stdout
    assert runs(log) == 6
    (task / "solution" / "solve.sh").chmod(0o755)
    assert "cached" not in check("--all", str(root)).stdout
    assert runs(log) == 8


def test_json_and_markdown_mark_cached_tasks(tmp_path: Path) -> None:
    root = tmp_path / "root"
    counting_task(root, tmp_path / "solve.log")
    first = json.loads(check("--all", str(root), "--format", "json").stdout)
    assert first["tasks"][0]["cached"] is False
    second = json.loads(check("--all", str(root), "--format", "json").stdout)
    assert second["tasks"][0]["cached"] is True
    assert second["tasks"][0]["gates"] == first["tasks"][0]["gates"]
    markdown = check("--all", str(root), "--format", "markdown").stdout
    assert "1 blocking failure" not in markdown
    assert "0 blocking failures; all tasks, 1 task, local runner, 1 cached." in markdown
    assert "| `tasks/echo` | - | pass (cached) | - |" in markdown
    assert "Cached result: the task's content, the TaskGate build, the gates" in markdown


def test_a_task_with_a_blocking_failure_is_checked_every_time(tmp_path: Path) -> None:
    root, log = tmp_path / "root", tmp_path / "solve.log"
    counting_task(root, log, grader=LENIENT_GRADER)
    for _ in range(2):
        result = check("--all", str(root))
        assert result.exit_code == 1
        assert "cached" not in result.stdout
    assert runs(log) == 4
    stats = runner.invoke(app, ["cache", "stats", "--json"])
    data = json.loads(stats.stdout)
    assert (data["entries"], data["misses"], data["not_stored"]) == (0, 2, 2)


def test_the_default_cache_lives_in_the_repository_and_git_ignores_it(
    repo: GitRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TASKGATE_CACHE_DIR")
    log = tmp_path / "solve.log"
    repo.write("README.md")
    repo.commit("base")
    repo.branch("pr")
    counting_task(repo.root, log)
    repo.commit("add a task")
    first = check(str(repo.root), "--base", "main")
    assert first.exit_code == 0
    assert "tasks/echo  added  PASS\n" in first.stdout
    second = check(str(repo.root), "--base", "main")
    assert "tasks/echo  added  PASS  (cached)\n" in second.stdout
    assert runs(log) == 2
    cache = repo.root / ".taskgate" / "cache"
    assert len(list((cache / "entries").glob("*.json"))) == 1
    assert repo.git("status", "--porcelain") == ""
    stats = runner.invoke(app, ["cache", "stats", str(repo.root)])
    assert stats.exit_code == 0
    assert stats.stdout.splitlines()[0] == f"cache      {cache}"


def test_diff_and_all_mode_do_not_share_entries(
    repo: GitRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Diff mode hashes git's file list too, so the same task checked with --all misses."""
    log = tmp_path / "solve.log"
    repo.write("README.md")
    repo.commit("base")
    repo.branch("pr")
    counting_task(repo.root, log)
    repo.commit("add a task")
    check(str(repo.root), "--base", "main")
    all_mode = check("--all", str(repo.root))
    assert "cached" not in all_mode.stdout
    assert runs(log) == 4
    assert "(cached)" in check(str(repo.root), "--base", "main").stdout
    assert "(cached)" in check("--all", str(repo.root)).stdout
    assert runs(log) == 4


def test_the_runner_and_config_are_part_of_the_key(tmp_path: Path, fake_docker: FakeDocker) -> None:
    root = tmp_path / "root"
    counting_task(root, tmp_path / "solve.log")
    check("--all", str(root))
    docker = check("--all", str(root), "--runner", "docker")
    assert docker.exit_code == 0
    assert "docker runner\n" in docker.stdout
    assert "cached" not in docker.stdout
    assert "(cached)" in check("--all", str(root), "--runner", "docker").stdout
    stats = json.loads(runner.invoke(app, ["cache", "stats", "--json"]).stdout)
    assert (stats["entries"], stats["tasks"], stats["runners"]) == (
        2,
        1,
        {"docker": 1, "local": 1},
    )
    (root / "taskgate.toml").write_text('[gates]\ndisable = ["TG104"]\n', encoding="utf-8")
    assert "cached" not in check("--all", str(root)).stdout
    assert "(cached)" in check("--all", str(root)).stdout


def test_an_unusable_cache_does_not_fail_the_check(tmp_path: Path) -> None:
    root = tmp_path / "root"
    counting_task(root, tmp_path / "solve.log")
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    result = check("--all", str(root), "--cache-dir", str(blocker / "cache"))
    assert result.exit_code == 0
    assert "note: result cache off for this run: cannot use the cache at " in result.stderr


def test_cache_stats_and_prune(tmp_path: Path, private_result_cache: Path) -> None:
    root, log = tmp_path / "root", tmp_path / "solve.log"
    task = counting_task(root, log)
    empty = runner.invoke(app, ["cache", "stats"])
    assert empty.exit_code == 0
    assert empty.stdout.splitlines()[1:] == [
        "entries    0 (0 B): 0 usable by this taskgate build, 0 stale "
        "(taskgate cache prune removes them)",
        "tasks      0 (runners: -)",
        "lookups    0 hits, 0 misses",
        "stored     0 result sets; 0 not stored because a blocking gate failed",
    ]
    check("--all", str(root))
    check("--all", str(root))
    (task / "instruction.md").write_text("Copy input/name.txt to output/greeting.txt.\n\n")
    check("--all", str(root))
    stats = runner.invoke(app, ["cache", "stats"])
    lines = stats.stdout.splitlines()
    assert lines[0] == f"cache      {private_result_cache}"
    assert lines[1].startswith("entries    2 (")
    assert lines[1].endswith("): 2 usable by this taskgate build, 0 stale (taskgate cache prune "
                             "removes them)")  # fmt: skip
    assert lines[2:] == [
        "tasks      1 (runners: local 2)",
        "lookups    1 hit, 2 misses (33% hits)",
        "stored     2 result sets; 0 not stored because a blocking gate failed",
    ]
    data = json.loads(runner.invoke(app, ["cache", "stats", "--json"]).stdout)
    assert {k: data[k] for k in ("entries", "current", "stale", "tasks", "hits")} == {
        "entries": 2,
        "current": 2,
        "stale": 0,
        "tasks": 1,
        "hits": 1,
    }
    assert data["runners"] == {"local": 2}

    preview = runner.invoke(app, ["cache", "prune", "--dry-run"])
    assert preview.exit_code == 0
    assert preview.stdout.startswith("would remove 1 of 2 entries (")
    assert preview.stdout.rstrip().endswith(
        f"1 superseded by a newer entry for the same task and runner; 1 kept in "
        f"{private_result_cache}"
    )
    assert len(list((private_result_cache / "entries").iterdir())) == 2
    pruned = runner.invoke(app, ["cache", "prune"])
    assert pruned.stdout.startswith("removed 1 of 2 entries (")
    assert "(cached)" in check("--all", str(root)).stdout
    assert runner.invoke(app, ["cache", "prune", "--older-than", "1"]).stdout.startswith(
        "removed 0 of 1 entries (0 B); 1 kept in "
    )
    everything = runner.invoke(app, ["cache", "prune", "--all"])
    assert everything.stdout.startswith("removed 1 of 1 entries (")
    assert ": 1 removed by --all; 0 kept in " in everything.stdout
    assert "cached" not in check("--all", str(root)).stdout


def test_prune_while_another_process_holds_the_lock_is_a_usage_error(
    tmp_path: Path, private_result_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("taskgate.cli.ResultCache", partial(ResultCache, lock_timeout=0.1))
    private_result_cache.mkdir()
    fd = os.open(private_result_cache / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        result = runner.invoke(app, ["cache", "prune"])
    finally:
        os.close(fd)
    assert result.exit_code == 2
    assert "is still locked by another taskgate process after 0.1s" in result.stderr


def test_cache_without_a_subcommand_prints_help() -> None:
    result = runner.invoke(app, ["cache"])
    assert "stats" in result.output
    assert "prune" in result.output
