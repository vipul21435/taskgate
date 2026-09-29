"""End to end on the sample repository that `make demo` builds."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from taskgate.cli import app

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
BUILDER = EXAMPLES / "build_sample_repo.py"
runner = CliRunner()


def build(dest: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(BUILDER), str(dest)], capture_output=True, text=True, check=False
    )


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def check_branch(repo: Path, branch: str) -> tuple[int, dict[str, Any]]:
    git(repo, "checkout", "-q", branch)
    result = runner.invoke(app, ["check", str(repo), "--base", "main", "--format", "json"])
    return result.exit_code, json.loads(result.stdout)


@pytest.fixture(scope="module")
def sample_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    dest = tmp_path_factory.mktemp("demo") / "repo"
    assert build(dest).returncode == 0
    return dest


def test_builder_creates_main_and_three_pull_request_branches(sample_repo: Path) -> None:
    branches = git(sample_repo, "branch", "--format=%(refname:short)").split()
    assert branches == [
        "main",
        "pr/1-integer-determinant",
        "pr/2-word-count",
        "pr/3-gcd-pairs",
    ]
    assert git(sample_repo, "status", "--porcelain") == ""


def test_good_pull_request_passes_every_gate(sample_repo: Path) -> None:
    code, report = check_branch(sample_repo, "pr/1-integer-determinant")
    assert code == 0
    assert report["result"] == "pass"
    assert report["other_files"] == ["README.md"]
    (task,) = report["tasks"]
    assert task["path"] == "tasks/integer-determinant"
    assert task["change"] == "added"
    assert {gate["status"] for gate in task["gates"]} == {"pass"}


def test_bad_pull_request_is_blocked_by_manifest_secret_and_baseline_gates(
    sample_repo: Path,
) -> None:
    code, report = check_branch(sample_repo, "pr/2-word-count")
    assert code == 1
    assert report["blocking_failures"] == 3
    (task,) = report["tasks"]
    failed = {gate["code"]: gate["message"] for gate in task["gates"] if gate["status"] == "fail"}
    assert failed == {
        "TG102": "task.difficulty 'trivial' is not one of easy, medium, hard",
        "TG201": ("1 likely secret: solution/solve.sh:7 high-entropy string 'huG8...' (32 chars)"),
        "TG402": "the grader passes an untouched workspace (2 skipped)",
    }
    assert "huG8GP38" not in json.dumps(report)


def test_existence_only_grader_and_unpinned_base_are_blocked(sample_repo: Path) -> None:
    code, report = check_branch(sample_repo, "pr/3-gcd-pairs")
    assert code == 1
    assert report["blocking_failures"] == 2
    (task,) = report["tasks"]
    status = {gate["code"]: gate["status"] for gate in task["gates"]}
    assert status["TG402"] == "pass"
    failed = {gate["code"]: gate["message"] for gate in task["gates"] if gate["status"] == "fail"}
    assert failed == {
        "TG302": "1 image is not pinned by digest: line 3: FROM python:3.12-slim",
        "TG403": "the grader passes a stub that writes output/gcds.txt empty (2 passed)",
    }


def test_reports_are_byte_identical_across_builds(tmp_path: Path) -> None:
    outputs = []
    for name in ("first", "second"):
        dest = tmp_path / name
        assert build(dest).returncode == 0
        git(dest, "checkout", "-q", "pr/2-word-count")
        outputs.append(runner.invoke(app, ["check", str(dest), "--base", "main"]).stdout)
    assert outputs[0] == outputs[1]


def test_builder_rebuilds_its_own_output_but_refuses_other_directories(tmp_path: Path) -> None:
    dest = tmp_path / "repo"
    assert build(dest).returncode == 0
    assert build(dest).returncode == 0
    other = tmp_path / "precious"
    other.mkdir()
    refused = build(other)
    assert refused.returncode != 0
    assert "refusing to delete" in refused.stderr
    usage = subprocess.run([sys.executable, str(BUILDER)], capture_output=True, text=True)
    assert usage.returncode != 0
    assert "usage:" in usage.stderr
