import json
import runpy
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

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
