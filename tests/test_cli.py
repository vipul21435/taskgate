import json
import runpy
import sys
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gitrepo import GitRepo
from taskfactory import GRADER, LENIENT_GRADER
from taskfactory import make_task as make_runnable_task
from taskgate import __version__, registry
from taskgate.cli import app
from taskgate.gates import BUILTIN_GATES
from taskgate.registry import ENTRY_POINT_GROUP

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


def fake_plugins(monkeypatch: pytest.MonkeyPatch, *attrs: str) -> None:
    eps = [
        EntryPoint(name=attr.lower(), value=f"plugin_fixtures:{attr}", group=ENTRY_POINT_GROUP)
        for attr in attrs
    ]
    monkeypatch.setattr(registry, "entry_points", lambda group: eps)


def test_gates_lists_every_code_grouped_by_hundreds() -> None:
    result = runner.invoke(app, ["gates"])
    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0] == "TG1xx  layout and manifest"
    assert lines[1].startswith("  TG101  error    layout-complete  ")
    assert "TG4xx  solution and baselines" in lines
    assert lines[-1] == f"{len(BUILTIN_GATES)} gates: {len(BUILTIN_GATES)} built-in, 0 from plugins"
    listed = [line.split()[0] for line in lines if line.startswith("  TG")]
    assert listed == [g.code for g in BUILTIN_GATES]


def test_gates_shows_plugin_gates_and_their_source(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_plugins(monkeypatch, "SINGLE")
    result = runner.invoke(app, ["gates"])
    assert result.exit_code == 0
    assert "TG8xx  third-party" in result.output
    assert "TG801  warning  single" in result.output
    assert result.output.rstrip().endswith("1 from plugins")
    assert "[plugin single]" in result.output


def test_gates_json(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_plugins(monkeypatch, "PAIR")
    data = json.loads(runner.invoke(app, ["gates", "--json"]).output)
    assert data[0]["code"] == "TG101"
    assert data[0]["group"] == "layout and manifest"
    assert data[0]["source"] == "built-in"
    assert data[-1] == {
        "code": "TG803",
        "name": "pair-two",
        "group": "third-party",
        "enabled": True,
        "severity": "warning",
        "default_severity": "warning",
        "summary": "a plugin gate",
        "fix_hint": "fix it",
        "requires": ["TG802"],
        "source": "plugin pair",
    }


def test_a_broken_plugin_is_a_usage_error(monkeypatch: pytest.MonkeyPatch, repo: GitRepo) -> None:
    fake_plugins(monkeypatch, "RESERVED")
    listed = runner.invoke(app, ["gates"])
    assert listed.exit_code == 2
    assert "TG1xx-TG6xx are reserved for built-in gates" in listed.output
    pr_repo(repo)
    checked = runner.invoke(app, ["check", str(repo.root), "--base", "main"])
    assert checked.exit_code == 2
    assert "invalid gate registry" in checked.output


def test_plugin_gates_run_in_check(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_plugins(monkeypatch, "ClassGate")
    make_runnable_task(tmp_path / "tasks" / "echo")
    result = runner.invoke(app, ["check", "--all", str(tmp_path), "--format", "json"])
    assert result.exit_code == 0, result.output
    (task,) = json.loads(result.stdout)["tasks"]
    assert task["gates"][-1]["code"] == "TG808"
    assert task["gates"][-1]["message"] == "checked echo"


LOOSE_CONFIG = """\
[gates]
disable = ["TG101"]

[gates.severity]
TG402 = "warning"
"""


def test_check_all_reads_taskgate_toml_from_the_checked_directory(tmp_path: Path) -> None:
    make_runnable_task(tmp_path / "tasks" / "echo", grader=LENIENT_GRADER)
    (tmp_path / "taskgate.toml").write_text(
        '[gates.severity]\nTG402 = "warning"\n', encoding="utf-8"
    )
    result = runner.invoke(app, ["check", "--all", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[1] == "config: taskgate.toml; severity TG402=warning"
    assert "  fail  TG402  baseline-fails" in result.stdout


def test_diff_mode_takes_the_config_from_the_base_not_the_pull_request(repo: GitRepo) -> None:
    make_runnable_task(repo.root / "tasks" / "accepted")
    repo.commit("base")
    repo.branch("pr")
    make_runnable_task(repo.root / "tasks" / "echo", grader=LENIENT_GRADER)
    repo.write("taskgate.toml", LOOSE_CONFIG)
    repo.commit("add echo and relax the gates")
    blocked = runner.invoke(app, ["check", str(repo.root), "--base", "main", "--format", "json"])
    assert blocked.exit_code == 1
    data = json.loads(blocked.stdout)
    assert data["config"] == {"source": None, "disabled": [], "severity": {}}
    assert data["other_files"] == ["taskgate.toml"]

    repo.checkout("main")
    repo.write("taskgate.toml", '[gates.severity]\nTG402 = "warning"\n')
    repo.commit("policy: baseline gate warns")
    repo.checkout("pr")
    relaxed = runner.invoke(app, ["check", str(repo.root), "--base", "main", "--format", "json"])
    assert relaxed.exit_code == 0, relaxed.output
    config = json.loads(relaxed.stdout)["config"]
    assert config == {
        "source": "taskgate.toml at main",
        "disabled": [],
        "severity": {"TG402": "warning"},
    }


def test_explicit_config_disables_gates_and_skips_their_dependents(
    repo: GitRepo, tmp_path: Path
) -> None:
    pr_repo(repo, grader=LENIENT_GRADER)
    config = tmp_path / "loose.toml"
    config.write_text(LOOSE_CONFIG, encoding="utf-8")
    result = runner.invoke(
        app, ["check", str(repo.root), "--base", "main", "--config", str(config)]
    )
    assert result.exit_code == 0, result.output
    assert f"config: {config}; disabled TG101; severity TG402=warning" in result.stdout
    assert "  TG101  " not in result.stdout
    assert "skip  TG401  solution-passes  skipped: requires TG101 (disabled) to pass" in (
        result.stdout
    )


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("[gates]\ndisable = ['TG999']\n", "unknown gate code(s): TG999"),
        ("[gates\n", "does not parse"),
        ("[gate]\n", "unknown section [gate]"),
    ],
)
def test_a_bad_config_is_a_usage_error(tmp_path: Path, content: str, message: str) -> None:
    (tmp_path / "taskgate.toml").write_text(content, encoding="utf-8")
    for args in (["check", "--all", str(tmp_path)], ["gates", str(tmp_path)]):
        result = runner.invoke(app, args)
        assert result.exit_code == 2
        assert message in result.output


def test_gates_shows_the_effective_severity(tmp_path: Path) -> None:
    config = tmp_path / "taskgate.toml"
    config.write_text(LOOSE_CONFIG, encoding="utf-8")
    result = runner.invoke(app, ["gates", str(tmp_path)])
    assert result.exit_code == 0
    assert "  TG101  off      layout-complete" in result.output
    assert "  TG402  warning  baseline-fails" in result.output
    assert result.output.splitlines()[-1] == (
        "config: taskgate.toml; disabled TG101; severity TG402=warning"
    )
    data = json.loads(runner.invoke(app, ["gates", "--json", "--config", str(config)]).output)
    by_code = {gate["code"]: gate for gate in data}
    assert by_code["TG101"]["enabled"] is False
    assert (by_code["TG402"]["severity"], by_code["TG402"]["default_severity"]) == (
        "warning",
        "error",
    )


def test_a_missing_config_file_is_a_usage_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["gates", "--config", str(tmp_path / "nope.toml")])
    assert result.exit_code == 2
    assert "cannot read" in result.output
