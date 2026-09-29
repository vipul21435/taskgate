from pathlib import Path
from typing import Any

import pytest

from taskgate.manifest import DEFAULT_TIMEOUT_SEC, Manifest, load, validate

VALID: dict[str, Any] = {
    "task": {"id": "demo", "title": "Demo", "difficulty": "easy", "timeout_sec": 30},
    "environment": {"dockerfile": "Dockerfile", "workdir": "/workspace"},
}


def with_task(**changes: object) -> dict[str, Any]:
    return {**VALID, "task": {**VALID["task"], **changes}}


def with_env(**changes: object) -> dict[str, Any]:
    return {**VALID, "environment": {**VALID["environment"], **changes}}


def without_task_key(key: str) -> dict[str, Any]:
    return {**VALID, "task": {k: v for k, v in VALID["task"].items() if k != key}}


def test_valid_manifest() -> None:
    check = validate(VALID, "demo")
    assert check.problems == ()
    assert check.manifest == Manifest(
        id="demo",
        title="Demo",
        difficulty="easy",
        timeout_sec=30,
        dockerfile="Dockerfile",
        workdir="/workspace",
    )
    assert check.timeout_sec == 30


@pytest.mark.parametrize(
    ("raw", "problem"),
    [
        ({}, "missing [task] table"),
        ({**VALID, "task": "nope"}, "task must be a table"),
        (without_task_key("id"), "missing task.id"),
        (with_task(id=""), "task.id must be a non-empty string"),
        (with_task(id=7), "task.id must be a non-empty string"),
        (with_task(id="Demo_Task"), "task.id 'Demo_Task' is not kebab-case"),
        (with_task(id="other"), "task.id 'other' does not match the directory name 'demo'"),
        (with_task(difficulty="trivial"), "task.difficulty 'trivial' is not one of easy"),
        (without_task_key("timeout_sec"), "missing task.timeout_sec"),
        (with_task(timeout_sec="60"), "task.timeout_sec must be an integer"),
        (with_task(timeout_sec=True), "task.timeout_sec must be an integer"),
        (with_task(timeout_sec=0), "task.timeout_sec 0 is outside 1..3600"),
        (with_env(workdir="workspace"), "environment.workdir 'workspace' must be an absolute"),
        ({"task": VALID["task"]}, "missing [environment] table"),
    ],
)
def test_invalid_manifests(raw: dict[str, Any], problem: str) -> None:
    check = validate(raw, "demo")
    assert check.manifest is None
    assert any(p.startswith(problem) for p in check.problems), check.problems


def test_every_problem_is_reported_at_once() -> None:
    check = validate({"task": {"id": "x"}, "environment": {}}, "x")
    assert check.problems == (
        "missing task.title",
        "missing task.difficulty",
        "missing task.timeout_sec",
        "missing environment.dockerfile",
        "missing environment.workdir",
    )


def test_invalid_timeout_falls_back_to_the_default() -> None:
    assert validate(with_task(timeout_sec=99999), "demo").timeout_sec == DEFAULT_TIMEOUT_SEC
    assert validate({}, "demo").timeout_sec == DEFAULT_TIMEOUT_SEC


def test_load_reads_the_task_directory(tmp_path: Path) -> None:
    task = tmp_path / "demo"
    task.mkdir()
    (task / "task.toml").write_text(
        '[task]\nid = "demo"\ntitle = "Demo"\ndifficulty = "hard"\ntimeout_sec = 5\n'
        '[environment]\ndockerfile = "Dockerfile"\nworkdir = "/w"\n',
        encoding="utf-8",
    )
    check = load(task)
    assert check.manifest is not None
    assert check.manifest.difficulty == "hard"


def test_load_reports_missing_and_unparsable_manifests(tmp_path: Path) -> None:
    assert load(tmp_path).problems == ("task.toml not found",)
    (tmp_path / "task.toml").write_text("[task\n", encoding="utf-8")
    (problem,) = load(tmp_path).problems
    assert problem.startswith("task.toml does not parse:")
