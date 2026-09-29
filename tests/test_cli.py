import json
import runpy
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gitrepo import GitRepo
from taskfactory import GRADER, LENIENT_GRADER
from taskfactory import make_task as make_runnable_task
from taskgate import __version__
from taskgate.cli import app

SAMPLE_REPO = Path(__file__).resolve().parents[1] / "examples" / "sample-repo"
runner = CliRunner()


def make_task(task_dir: Path, *, complete: bool = True) -> None:
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text("[task]\n", encoding="utf-8")
    if complete:
        (task_dir / "instruction.md").write_text("Do it.\n", encoding="utf-8")
        for name in ("environment", "solution", "tests"):
            (task_dir / name).mkdir()


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output == f"taskgate {__version__}\n"


def test_no_arguments_prints_help() -> None:
    result = runner.invoke(app, [])
    assert "Usage" in result.output
    assert "tasks" in result.output


def test_tasks_lists_the_bundled_sample_repo() -> None:
    result = runner.invoke(app, ["tasks", str(SAMPLE_REPO)])
    assert result.exit_code == 0
    assert result.output.splitlines() == [
        "ok       tasks/modular-inverse",
        "missing  tasks/word-count-draft  (environment/, solution/, tests/)",
        "2 task(s), 1 complete",
    ]


def test_tasks_strict_fails_on_an_incomplete_task() -> None:
    result = runner.invoke(app, ["tasks", "--strict", str(SAMPLE_REPO)])
    assert result.exit_code == 1


def test_tasks_strict_passes_when_every_task_is_complete(tmp_path: Path) -> None:
    make_task(tmp_path / "tasks" / "one")
    result = runner.invoke(app, ["tasks", "--strict", str(tmp_path)])
    assert result.exit_code == 0
    assert result.output.endswith("1 task(s), 1 complete\n")


def test_tasks_json_output(tmp_path: Path) -> None:
    make_task(tmp_path / "b")
    make_task(tmp_path / "a", complete=False)
    result = runner.invoke(app, ["tasks", "--json", str(tmp_path)])
    assert result.exit_code == 0
    assert json.loads(result.output) == [
        {
            "path": "a",
            "complete": False,
            "missing": ["instruction.md", "environment/", "solution/", "tests/"],
        },
        {"path": "b", "complete": True, "missing": []},
    ]


def test_tasks_rejects_a_missing_root(tmp_path: Path) -> None:
    result = runner.invoke(app, ["tasks", str(tmp_path / "nope")])
    assert result.exit_code == 2
    assert "not a directory" in result.output


def test_python_dash_m_entry_point(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["taskgate", "version"])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("taskgate", run_name="__main__")
    assert exit_info.value.code == 0
    assert capsys.readouterr().out == f"taskgate {__version__}\n"


def pr_repo(repo: GitRepo, grader: str = GRADER) -> GitRepo:
    """main holds one accepted task; the checked-out branch adds tasks/echo."""
    make_runnable_task(repo.root / "tasks" / "accepted")
    repo.commit("base")
    repo.branch("pr")
    make_runnable_task(repo.root / "tasks" / "echo", grader=grader)
    repo.write("README.md", "# Tasks\n")
    repo.commit("add echo")
    return repo


def test_check_passes_a_good_pull_request(repo: GitRepo, tmp_path: Path) -> None:
    pr_repo(repo)
    out = tmp_path / "out"
    result = runner.invoke(app, ["check", str(repo.root), "--base", "main", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "tasks/echo  added  PASS" in result.stdout
    assert "tasks/accepted" not in result.stdout
    assert "1 changed task, 1 other file" in result.stdout
    assert result.stdout.endswith("result: PASS, 0 blocking failures\n")
    data = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert data["result"] == "pass"
    assert [task["path"] for task in data["tasks"]] == ["tasks/echo"]
    assert (out / "report.md").read_text(encoding="utf-8").startswith("## TaskGate: PASS")


def test_check_blocks_a_lenient_grader(repo: GitRepo) -> None:
    pr_repo(repo, grader=LENIENT_GRADER)
    result = runner.invoke(app, ["check", str(repo.root), "--base", "main", "--format", "json"])
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["blocking_failures"] == 1
    (task,) = data["tasks"]
    failed = [gate["code"] for gate in task["gates"] if gate["status"] == "fail"]
    assert failed == ["TG402"]


def test_check_markdown_on_stdout(repo: GitRepo) -> None:
    pr_repo(repo)
    result = runner.invoke(app, ["check", str(repo.root), "--base", "main", "--format", "markdown"])
    assert result.exit_code == 0
    assert result.stdout.startswith("## TaskGate: PASS\n")


def test_check_skips_removed_tasks(repo: GitRepo) -> None:
    make_runnable_task(repo.root / "tasks" / "old")
    repo.commit("base")
    repo.branch("pr")
    repo.remove("tasks/old")
    repo.commit("drop old")
    result = runner.invoke(app, ["check", str(repo.root), "--base", "main"])
    assert result.exit_code == 0
    assert "tasks/old  removed  SKIP" in result.stdout


def test_check_rejects_a_bad_base_and_a_non_repository(repo: GitRepo, tmp_path: Path) -> None:
    repo.commit("base")
    bad_base = runner.invoke(app, ["check", str(repo.root), "--base", "nope"])
    assert bad_base.exit_code == 2
    assert "base ref not found: nope" in bad_base.output
    not_repo = tmp_path / "plain"
    not_repo.mkdir()
    assert runner.invoke(app, ["check", str(not_repo)]).exit_code == 2


def test_check_all_on_the_bundled_sample_repo() -> None:
    result = runner.invoke(app, ["check", "--all", str(SAMPLE_REPO)])
    assert result.exit_code == 1
    assert "tasks/modular-inverse  PASS" in result.stdout
    assert "tasks/word-count-draft  FAIL" in result.stdout
    assert "missing environment/, solution/, tests/" in result.stdout


def test_check_all_rejects_a_missing_directory(tmp_path: Path) -> None:
    result = runner.invoke(app, ["check", "--all", str(tmp_path / "nope")])
    assert result.exit_code == 2
