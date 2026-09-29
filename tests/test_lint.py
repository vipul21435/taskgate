"""Manifest and instruction lint gates (TG103, TG104, TG105) on passing and failing fixtures."""

from pathlib import Path

import pytest

from taskfactory import make_task
from taskgate.config import Config, ManifestOptions
from taskgate.engine import run_gates
from taskgate.gates.lint import LINT_GATES
from taskgate.results import GateResult, Severity, Status

GOOD_MANIFEST_TAIL = '\n[environment]\ndockerfile = "Dockerfile"\nworkdir = "/workspace"\n'


def lint(task: Path, config: Config | None = None) -> dict[str, GateResult]:
    return {r.code: r for r in run_gates(task, gates=LINT_GATES, config=config)}


def write_manifest(task: Path, text: str) -> None:
    (task / "task.toml").write_text(text, encoding="utf-8")


def test_a_good_task_passes_every_lint_gate(tmp_path: Path) -> None:
    results = lint(make_task(tmp_path / "echo"))
    assert {code: (r.status, r.message) for code, r in results.items()} == {
        "TG103": (Status.PASS, "no unknown keys"),
        "TG104": (Status.PASS, "task.timeout_sec 60 is within 10..1800"),
        "TG105": (Status.PASS, "instruction.md has 4 words"),
    }


def test_default_severities() -> None:
    assert [(g.code, g.name, g.severity) for g in LINT_GATES] == [
        ("TG103", "manifest-known-keys", Severity.WARNING),
        ("TG104", "timeout-in-range", Severity.WARNING),
        ("TG105", "instruction-not-empty", Severity.ERROR),
    ]


def test_unknown_keys_fail_with_did_you_mean_hints(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo")
    write_manifest(
        task,
        'timeout_sec = 60\n[task]\nid = "echo"\ntitle = "Echo"\ndifficulty = "easy"\n'
        'timout_sec = 60\ntags = ["text"]\n[enviroment]\ndockerfile = "Dockerfile"\n',
    )
    result = lint(task)["TG103"]
    assert result.status is Status.FAIL
    assert not result.blocking
    assert result.message == (
        "unknown in task.toml: timeout_sec (did you mean task.timeout_sec?), "
        "task.timout_sec (did you mean task.timeout_sec?), task.tags, "
        "[enviroment] (did you mean [environment]?)"
    )
    assert result.fix_hint is not None
    assert "docs/task-layout.md" in result.fix_hint


def test_unknown_keys_skip_when_the_manifest_does_not_parse(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo")
    write_manifest(task, "[task\n")
    result = lint(task)["TG103"]
    assert (result.status, result.message) == (
        Status.SKIP,
        "task.toml is missing or does not parse (see TG102)",
    )
    (task / "task.toml").unlink()
    assert lint(task)["TG103"].status is Status.SKIP


@pytest.mark.parametrize(
    ("timeout", "status", "message"),
    [
        (10, Status.PASS, "task.timeout_sec 10 is within 10..1800"),
        (1800, Status.PASS, "task.timeout_sec 1800 is within 10..1800"),
        (5, Status.FAIL, "task.timeout_sec 5 is below the recommended minimum 10"),
        (3600, Status.FAIL, "task.timeout_sec 3600 is above the recommended maximum 1800"),
    ],
)
def test_timeout_range(tmp_path: Path, timeout: int, status: Status, message: str) -> None:
    result = lint(make_task(tmp_path / "echo", timeout=timeout))["TG104"]
    assert (result.status, result.message) == (status, message)


def test_timeout_range_comes_from_the_config(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo", timeout=60)
    strict = Config(manifest=ManifestOptions(min_timeout_sec=90, max_timeout_sec=300))
    result = lint(task, strict)["TG104"]
    assert result.message == "task.timeout_sec 60 is below the recommended minimum 90"


@pytest.mark.parametrize("value", ['"60"', "0", "99999"])
def test_timeout_range_skips_an_invalid_timeout(tmp_path: Path, value: str) -> None:
    task = make_task(tmp_path / "echo")
    write_manifest(
        task,
        f'[task]\nid = "echo"\ntitle = "E"\ndifficulty = "easy"\ntimeout_sec = {value}\n'
        + GOOD_MANIFEST_TAIL,
    )
    result = lint(task)["TG104"]
    assert (result.status, result.message) == (
        Status.SKIP,
        "task.timeout_sec is missing or invalid (see TG102)",
    )


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("", "instruction.md is empty (or holds only comments)"),
        ("  \n\n\t\n", "instruction.md is empty (or holds only comments)"),
        ("<!-- write the task here -->\n", "instruction.md is empty (or holds only comments)"),
        ("# Title\n\n## Details\n", "instruction.md has only headings"),
        ("# Title\n<!--\nTODO\n-->\n", "instruction.md has only headings"),
    ],
)
def test_empty_instructions_fail(tmp_path: Path, content: str, message: str) -> None:
    task = make_task(tmp_path / "echo")
    (task / "instruction.md").write_text(content, encoding="utf-8")
    result = lint(task)["TG105"]
    assert (result.status, result.message, result.blocking) == (Status.FAIL, message, True)


def test_instruction_text_is_counted_after_a_heading_and_a_bom(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo")
    (task / "instruction.md").write_bytes(b"\xef\xbb\xbf# Echo\n\nCopy the file.\n#hashtag\n")
    assert lint(task)["TG105"].message == "instruction.md has 4 words"
    (task / "instruction.md").write_text("Go.\n", encoding="utf-8")
    assert lint(task)["TG105"].message == "instruction.md has 1 word"


def test_instruction_must_be_utf8_and_present(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo")
    (task / "instruction.md").write_bytes(b"caf\xe9\n")
    assert lint(task)["TG105"].message == "instruction.md is not valid UTF-8"
    (task / "instruction.md").unlink()
    result = lint(task)["TG105"]
    assert (result.status, result.message) == (Status.SKIP, "instruction.md not found (see TG101)")
