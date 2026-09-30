"""The composite action's steps, run locally from action.yml against the fake API.

GitHub's runner substitutes ``${{ ... }}`` expressions and runs each ``run:``
block in bash. This harness does the same for the small subset action.yml
uses (inputs with defaults, ``github.*`` values, ``steps.check.outputs.*``,
``&&``, ``||``, ``==`` and ``!=``), skips the two setup steps (uv is already
here) and points ``TASKGATE`` at this environment's console script.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from gitrepo import GitRepo
from taskfactory import make_task
from taskgate.fakegithub import DEFAULT_TOKEN, FakeGitHub
from taskgate.github import MARKER

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / "action.yml"
SAMPLE = ROOT / "examples" / "sample-repo"
EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
REPO = "sample/tasks"


@dataclass
class Step:
    name: str
    id: str = ""
    condition: str = ""
    env: dict[str, str] = field(default_factory=dict)
    run: str = ""
    uses: str = ""


def parse_action(text: str) -> tuple[dict[str, str], list[Step]]:
    """The inputs' defaults and the steps of action.yml (enough YAML for this file)."""
    defaults: dict[str, str] = {}
    steps: list[Step] = []
    section = ""
    current_input = ""
    block: str | None = None
    for line in text.splitlines():
        if block is not None:
            if line.startswith("        ") or not line.strip():
                steps[-1].run += line[8:] + "\n"
                continue
            block = None
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            section = line.rstrip(":")
            continue
        if section == "inputs":
            if line.startswith("  ") and not line.startswith("    "):
                current_input = line.strip().rstrip(":")
                defaults[current_input] = ""
            elif line.strip().startswith("default:"):
                value = line.split("default:", 1)[1].strip()
                defaults[current_input] = value.strip('"')
        elif section == "runs" and line.startswith("    "):
            stripped = line.strip()
            if line.startswith("    - name:"):
                steps.append(Step(stripped.removeprefix("- name:").strip()))
            elif line.startswith("      id:"):
                steps[-1].id = stripped.removeprefix("id:").strip()
            elif line.startswith("      if:"):
                steps[-1].condition = stripped.removeprefix("if:").strip()
            elif line.startswith("      uses:"):
                steps[-1].uses = stripped.removeprefix("uses:").strip()
            elif line.startswith("      run: |"):
                block = steps[-1].name
            elif line.startswith("      run:"):
                steps[-1].run = stripped.removeprefix("run:").strip()
            elif (
                line.startswith("        ")
                and ":" in stripped
                and not line.startswith("          ")
            ):
                key, _, value = stripped.partition(":")
                steps[-1].env[key.strip()] = value.strip()
    return defaults, steps


class Runner:
    """Substitutes expressions like GitHub's runner and runs steps in bash."""

    def __init__(
        self, inputs: dict[str, str], github: dict[str, str], environ: dict[str, str]
    ) -> None:
        self.defaults, self.steps = parse_action(ACTION.read_text(encoding="utf-8"))
        unknown = set(inputs) - set(self.defaults)
        assert not unknown, unknown
        self.inputs = {**self.defaults, **inputs}
        self.github = github
        self.outputs: dict[str, dict[str, str]] = {}
        self.environ = environ

    def value(self, expression: str) -> str:
        """Evaluate one ``${{ }}`` expression."""

        def lookup(match: re.Match[str]) -> str:
            path = match.group(0)
            if path.startswith("inputs."):
                return repr(self.inputs[path.removeprefix("inputs.")])
            if path.startswith("steps."):
                _, step, _, name = path.split(".", 3)
                return repr(self.outputs.get(step, {}).get(name, ""))
            if path.startswith("github."):
                return repr(self.github.get(path.removeprefix("github."), ""))
            raise AssertionError(f"unknown context in action.yml: {path}")

        python = re.sub(r"[a-z_]+(?:\.[a-z_-]+)+", lookup, expression)
        python = python.replace("&&", " and ").replace("||", " or ")
        result = eval(python, {"__builtins__": {}}, {})
        return "true" if result is True else "false" if result is False else str(result)

    def substitute(self, text: str) -> str:
        return EXPRESSION.sub(lambda m: self.value(m.group(1)), text)

    def resolve_inputs(self) -> None:
        self.inputs = {key: self.substitute(value) for key, value in self.inputs.items()}

    def enabled(self, step: Step) -> bool:
        return self.substitute(step.condition) == "true" if step.condition else True

    def run(self, step: Step, cwd: Path) -> subprocess.CompletedProcess[str]:
        env = {**self.environ, **{k: self.substitute(v) for k, v in step.env.items()}}
        env["TASKGATE"] = str(Path(sys.executable).with_name("taskgate"))
        env["PYTHON"] = sys.executable
        return subprocess.run(
            ["bash", "-c", step.run], cwd=cwd, env=env, capture_output=True, text=True, check=False
        )

    def read_outputs(self, step: Step, output_file: Path) -> None:
        if step.id and output_file.exists():
            for line in output_file.read_text(encoding="utf-8").splitlines():
                key, _, value = line.partition("=")
                self.outputs.setdefault(step.id, {})[key] = value
            output_file.write_text("", encoding="utf-8")


@pytest.fixture
def fake() -> Iterator[FakeGitHub]:
    with FakeGitHub(REPO) as server:
        yield server


def harness(tmp_path: Path, fake: FakeGitHub, inputs: dict[str, str], **extra: str) -> Runner:
    github = {
        "action_path": str(ROOT),
        "token": DEFAULT_TOKEN,
        "api_url": fake.url,
        "sha": "f" * 40,
        "event.pull_request.number": "",
        "event.pull_request.head.sha": "",
    }
    (tmp_path / "output").write_text("", encoding="utf-8")
    (tmp_path / "summary.md").write_text("", encoding="utf-8")
    environ = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "TASKGATE_RUNNER": "local",
        "TASKGATE_CACHE_DIR": str(tmp_path / "cache"),
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        **extra,
    }
    runner = Runner(inputs, github, environ)
    runner.resolve_inputs()
    return runner


def run_action(
    runner: Runner, cwd: Path, tmp_path: Path
) -> dict[str, subprocess.CompletedProcess[str]]:
    """Run every non-setup step that is enabled; stop at the first failing one."""
    done: dict[str, subprocess.CompletedProcess[str]] = {}
    for step in runner.steps:
        if step.uses or step.run.lstrip().startswith("uv sync") or not runner.enabled(step):
            continue
        result = runner.run(step, cwd)
        done[step.name] = result
        runner.read_outputs(step, tmp_path / "output")
        if result.returncode != 0:
            break
    return done


def test_the_action_checks_every_sample_task_and_reports_to_the_pull_request(
    tmp_path: Path, fake: FakeGitHub
) -> None:
    workspace = tmp_path / "workspace"
    shutil.copytree(SAMPLE, workspace / "examples" / "sample-repo")
    inputs = {
        "path": "examples/sample-repo",
        "all": "true",
        "fail-on-blocking": "false",
        "pr": "1",
        "head-sha": "a" * 40,
        "runner": "local",
        "out": str(tmp_path / "out"),
    }
    runner = harness(tmp_path, fake, inputs)
    assert runner.inputs["github-api"] == fake.url
    assert runner.inputs["github-token"] == DEFAULT_TOKEN
    done = run_action(runner, workspace, tmp_path)
    assert list(done) == ["Run the gates", "Report to the pull request"]
    check = done["Run the gates"]
    assert check.returncode == 0, check.stderr
    assert runner.outputs["check"] == {
        "exit-code": "1",
        "result": "fail",
        "blocking-failures": "1",
        "report-dir": str(tmp_path / "out"),
        "path-prefix": "examples/sample-repo",
    }
    assert "tasks/word-count-draft  FAIL" in check.stdout
    assert (
        "::error file=examples/sample-repo/tasks/word-count-draft/task.toml,line=1,"
        "title=TG101 layout-complete::"
    ) in check.stdout
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert summary.startswith("## TaskGate: FAIL\n")
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [
        "junit.xml",
        "report.json",
        "report.md",
    ]
    report = done["Report to the pull request"]
    assert report.returncode == 0, report.stderr
    assert report.stdout.splitlines() == [
        f"comment created: https://github.example/{REPO}/pull/1#issuecomment-1",
        f"check run failure: 1 annotation in 1 request: https://github.example/{REPO}/runs/2",
    ]
    (comment,) = fake.comments(1)
    assert comment["body"].startswith(f"{MARKER}\n## TaskGate: FAIL\n")
    (run,) = fake.check_runs()
    assert run["head_sha"] == "a" * 40
    assert run["output"]["annotations"][0]["path"] == (
        "examples/sample-repo/tasks/word-count-draft/task.toml"
    )
    assert all(r.authorized for r in fake.requests)


def test_the_action_fails_the_step_on_blocking_gates_when_asked(
    tmp_path: Path, fake: FakeGitHub
) -> None:
    workspace = tmp_path / "workspace"
    shutil.copytree(SAMPLE, workspace / "sample")
    inputs = {"path": "sample", "all": "true", "comment": "false", "check-run": "false"}
    runner = harness(tmp_path, fake, inputs)
    done = run_action(runner, workspace, tmp_path)
    assert list(done) == ["Run the gates", "Fail on blocking gates"]
    failed = done["Fail on blocking gates"]
    assert failed.returncode == 1
    assert failed.stderr.strip() == (
        "TaskGate: 1 blocking gate failure(s); see the job summary and the annotations"
    )
    assert fake.requests == []


def test_the_action_diffs_against_the_pull_request_base_with_its_file_list(
    tmp_path: Path, fake: FakeGitHub, repo: GitRepo
) -> None:
    repo.write("README.md", "# Tasks\n")
    repo.commit("base")
    repo.git("update-ref", "refs/remotes/origin/main", "main")
    repo.branch("feature")
    task = make_task(repo.root / "tasks" / "echo")
    repo.commit("add echo")
    files = sorted(p.relative_to(repo.root).as_posix() for p in task.rglob("*") if p.is_file())
    fake.add_pull(12, [(path, "added") for path in files])
    inputs = {
        "pr": "12",
        "pr-files": "true",
        "head-sha": "b" * 40,
        "out": str(tmp_path / "out"),
    }
    runner = harness(tmp_path, fake, inputs, GITHUB_BASE_REF="main")
    done = run_action(runner, repo.root, tmp_path)
    assert list(done) == ["Run the gates", "Report to the pull request"]
    check = done["Run the gates"]
    assert check.returncode == 0, check.stderr
    assert runner.outputs["check"]["result"] == "pass"
    assert runner.outputs["check"]["path-prefix"] == ""
    assert "files of pull request #12, 1 changed task" in check.stdout
    assert "::error" not in check.stdout
    assert (
        done["Report to the pull request"]
        .stdout.splitlines()[1]
        .startswith("check run success: 0 annotations in 1 request: ")
    )
    assert [(r.method, r.path) for r in fake.requests[:2]] == [
        ("GET", f"/repos/{REPO}/pulls/12"),
        ("GET", f"/repos/{REPO}/pulls/12/files"),
    ]


def test_a_usage_error_fails_the_check_step(tmp_path: Path, fake: FakeGitHub) -> None:
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    runner = harness(tmp_path, fake, {"path": str(not_a_repo)})
    done = run_action(runner, not_a_repo, tmp_path)
    assert list(done) == ["Run the gates"]
    assert done["Run the gates"].returncode == 2
    assert "error:" in done["Run the gates"].stderr
