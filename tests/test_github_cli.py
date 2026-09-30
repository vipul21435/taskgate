"""The GitHub side of the CLI: check --pr, check --out's JUnit XML, report and publish."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gitrepo import GitRepo
from taskfactory import make_task
from taskgate.cli import app
from taskgate.fakegithub import DEFAULT_TOKEN, FakeGitHub
from taskgate.github import MARKER
from taskgate.report import to_annotations, to_json, to_markdown
from test_report_formats import RICH

REPO = "sample/tasks"
cli = CliRunner()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGitHub]:
    with FakeGitHub(REPO) as server:
        monkeypatch.setenv("TASKGATE_GITHUB_API", server.url)
        monkeypatch.setenv("GITHUB_TOKEN", DEFAULT_TOKEN)
        monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
        yield server


@pytest.fixture
def pull_request(repo: GitRepo) -> GitRepo:
    """``main`` with a README and a doc; ``pr`` adds a runnable task, edits the README
    and renames the doc."""
    repo.write("README.md", "# Tasks\n")
    repo.write("docs/old.md", "notes\n")
    repo.commit("base")
    repo.branch("pr")
    make_task(repo.root / "tasks" / "echo")
    repo.write("README.md", "# Tasks\n\n- echo\n")
    repo.git("mv", "docs/old.md", "docs/new.md")
    repo.commit("add echo")
    return repo


def check_json(*args: str) -> tuple[int, dict[str, object]]:
    result = cli.invoke(app, ["check", *args, "--format", "json"])
    return result.exit_code, json.loads(result.stdout) if result.stdout else {}


def test_check_takes_the_changed_files_from_the_pull_request(
    pull_request: GitRepo, fake: FakeGitHub
) -> None:
    task_files = sorted(
        path.relative_to(pull_request.root).as_posix()
        for path in (pull_request.root / "tasks" / "echo").rglob("*")
        if path.is_file()
    )
    fake.add_pull(
        5,
        [
            *[(path, "added") for path in task_files],
            ("README.md", "modified"),
            ("docs/new.md", "renamed", "docs/old.md"),
        ],
    )
    root = str(pull_request.root)
    code, from_api = check_json(root, "--base", "main", "--pr", "5")
    git_code, from_git = check_json(root, "--base", "main", "--no-cache")
    assert (code, git_code) == (0, 0)
    assert from_api["pull_request"] == 5
    assert from_git["pull_request"] is None
    assert from_api["other_files"] == ["README.md", "docs/new.md", "docs/old.md"]
    assert from_api["tasks"] == from_git["tasks"]
    assert [(r.method, r.path) for r in fake.requests] == [
        ("GET", f"/repos/{REPO}/pulls/5"),
        ("GET", f"/repos/{REPO}/pulls/5/files"),
    ]
    text = cli.invoke(app, ["check", root, "--base", "main", "--pr", "5"]).stdout
    assert text.splitlines()[0].startswith("taskgate 0.1.0: diff against main (merge base ")
    assert ", files of pull request #5, 1 changed task, 3 other files, local runner" in text


def test_check_pr_problems_are_usage_errors(
    pull_request: GitRepo, fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = str(pull_request.root)
    missing = cli.invoke(app, ["check", root, "--base", "main", "--pr", "9"])
    assert missing.exit_code == 2
    assert missing.stderr == f"error: GET /repos/{REPO}/pulls/9: HTTP 404: Not Found\n"
    both = cli.invoke(app, ["check", root, "--all", "--pr", "9"])
    assert both.exit_code == 2
    assert "cannot be used with --all" in both.stderr
    monkeypatch.delenv("GITHUB_REPOSITORY")
    no_repo = cli.invoke(app, ["check", root, "--base", "main", "--pr", "9"])
    assert no_repo.exit_code == 2
    assert "--pr needs --repo OWNER/NAME (or GITHUB_REPOSITORY)" in no_repo.stderr


def test_check_out_writes_junit_next_to_the_other_reports(tmp_path: Path) -> None:
    task = make_task(tmp_path / "tasks" / "echo")
    out = tmp_path / "out"
    result = cli.invoke(app, ["check", "--all", str(task.parent), "--out", str(out)])
    assert result.exit_code == 0
    assert sorted(path.name for path in out.iterdir()) == ["junit.xml", "report.json", "report.md"]
    assert result.stderr == (
        f"wrote {out / 'report.md'}, {out / 'report.json'} and {out / 'junit.xml'}\n"
    )
    junit = (out / "junit.xml").read_text(encoding="utf-8")
    assert '<testsuite name="echo" tests="14" failures="0" errors="0" skipped="0">' in junit
    rendered = cli.invoke(app, ["report", str(out / "report.json"), "--format", "junit"])
    assert rendered.stdout == junit


@pytest.fixture
def saved(tmp_path: Path) -> Path:
    path = tmp_path / "report.json"
    path.write_text(to_json(RICH), encoding="utf-8")
    return path


def test_report_renders_a_saved_report(saved: Path) -> None:
    annotated = cli.invoke(
        app, ["report", str(saved), "--format", "annotations", "--path-prefix", "sub"]
    )
    assert annotated.exit_code == 0
    assert annotated.stdout == to_annotations(RICH, "sub")
    assert annotated.stdout.startswith("::error file=sub/tasks/bad/task.toml,line=1,")
    markdown = cli.invoke(app, ["report", str(saved)])
    assert markdown.stdout == to_markdown(RICH)


def test_report_rejects_what_it_cannot_read(tmp_path: Path) -> None:
    missing = cli.invoke(app, ["report", str(tmp_path / "nope.json")])
    assert missing.exit_code == 2
    assert (
        missing.stderr
        == f"error: cannot read {tmp_path / 'nope.json'}: No such file or directory\n"
    )
    (tmp_path / "bad.json").write_text("[]", encoding="utf-8")
    bad = cli.invoke(app, ["report", str(tmp_path / "bad.json")])
    assert bad.exit_code == 2
    assert "not a TaskGate report.json" in bad.stderr


def publish(saved: Path, *args: str) -> tuple[int, str, str]:
    result = cli.invoke(app, ["publish", str(saved), "--pr", "3", *args])
    return result.exit_code, result.stdout, result.stderr


def test_publish_keeps_one_comment_and_creates_a_check_run(saved: Path, fake: FakeGitHub) -> None:
    code, out, _ = publish(saved, "--sha", "abc123", "--path-prefix", "examples")
    assert code == 0
    assert out.splitlines() == [
        f"comment created: https://github.example/{REPO}/pull/3#issuecomment-1",
        f"check run failure: 3 annotations in 1 request: https://github.example/{REPO}/runs/2",
    ]
    (comment,) = fake.comments(3)
    assert comment["body"] == f"{MARKER}\n{to_markdown(RICH)}"
    (run,) = fake.check_runs()
    assert run["head_sha"] == "abc123"
    assert run["conclusion"] == "failure"
    assert run["output"]["title"] == "TaskGate: FAIL, 2 blocking failures"
    assert run["output"]["summary"] == to_markdown(RICH)
    assert [(a["path"], a["annotation_level"]) for a in run["output"]["annotations"]] == [
        ("examples/tasks/bad/task.toml", "failure"),
        ("examples/tasks/flaky/task.toml", "failure"),
        ("examples/tasks/ok/task.toml", "warning"),
    ]
    code, out, _ = publish(saved, "--no-check-run")
    assert (code, out) == (
        0,
        f"comment unchanged: https://github.example/{REPO}/pull/3#issuecomment-1\n",
    )
    assert len(fake.comments(3)) == 1
    assert len(fake.check_runs()) == 1


def test_publish_a_passing_report_without_a_comment(tmp_path: Path, fake: FakeGitHub) -> None:
    passing = tmp_path / "report.json"
    passing.write_text(to_json(RICH.__class__(version="1", mode="all")), encoding="utf-8")
    code, out, _ = publish(passing, "--sha", "abc", "--no-comment", "--check-name", "Gates")
    assert (code, out) == (
        0,
        f"check run success: 0 annotations in 1 request: https://github.example/{REPO}/runs/1\n",
    )
    assert fake.check_runs()[0]["name"] == "Gates"
    assert fake.comments(3) == []


def test_publish_usage_and_api_errors(
    saved: Path, fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert publish(saved)[0] == 2
    assert "--sha is required for a check run" in publish(saved)[2]
    monkeypatch.setenv("GITHUB_TOKEN", "wrong")
    code, _, err = publish(saved, "--no-check-run")
    assert (code, err) == (
        1,
        f"error: GET /repos/{REPO}/issues/3/comments?per_page=100: HTTP 401: Bad credentials\n",
    )
    monkeypatch.delenv("GITHUB_TOKEN")
    assert publish(saved, "--no-check-run")[2] == (
        "error: GITHUB_TOKEN is not set; publish needs a token\n"
    )
    monkeypatch.delenv("GITHUB_REPOSITORY")
    assert publish(saved, "--no-check-run")[2] == (
        "error: --repo OWNER/NAME (or GITHUB_REPOSITORY) is required\n"
    )
