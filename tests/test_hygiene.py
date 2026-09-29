"""Hygiene gates (TG201, TG202, TG203) on passing and failing fixtures."""

import random
import string
from pathlib import Path

import pytest

from taskfactory import make_task
from taskgate.config import Config, FileOptions, SecretOptions
from taskgate.engine import run_gates
from taskgate.gates.hygiene import HYGIENE_GATES, human_size
from taskgate.results import GateResult, Severity, Status
from taskgate.secretscan import looks_random

PNG_HEADER = b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR"


def token(length: int = 32, seed: int = 1) -> str:
    """A seeded random token that the entropy rule flags (distinct seeds, distinct tokens)."""
    for attempt in range(seed * 100, seed * 100 + 100):
        rng = random.Random(attempt)
        text = "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(length))
        if looks_random(text, 4.0):
            return text
    raise AssertionError("no flagged token")


def hygiene(task: Path, config: Config | None = None) -> dict[str, GateResult]:
    return {r.code: r for r in run_gates(task, gates=HYGIENE_GATES, config=config)}


def write(task: Path, relative: str, content: str | bytes) -> None:
    path = task / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def test_a_clean_task_passes_every_hygiene_gate(tmp_path: Path) -> None:
    results = hygiene(make_task(tmp_path / "echo"))
    assert {code: (r.status, r.message) for code, r in results.items()} == {
        "TG201": (Status.PASS, "no secrets in 6 text files"),
        "TG202": (Status.PASS, "6 files, 417 B in total"),
        "TG203": (Status.PASS, "no binary files"),
    }


def test_default_severities() -> None:
    assert [(g.code, g.name, g.severity) for g in HYGIENE_GATES] == [
        ("TG201", "no-secrets", Severity.ERROR),
        ("TG202", "file-size-limits", Severity.ERROR),
        ("TG203", "no-binary-files", Severity.ERROR),
    ]


def test_secrets_in_any_file_block_and_are_redacted(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo")
    secret = token()
    write(task, "solution/solve.sh", f"#!/bin/sh\nexport API_TOKEN={secret}\ncp a b\n")
    write(task, ".env", "KEY=" + "AKIA" + "Q" * 16 + "\n")
    result = hygiene(task)["TG201"]
    assert result.status is Status.FAIL
    assert result.blocking
    assert result.message == (
        "2 likely secrets: .env:1 AWS access key id 'AKIA...' (20 chars); "
        f"solution/solve.sh:2 high-entropy string '{secret[:4]}...' (32 chars)"
    )
    assert secret not in result.message
    assert result.fix_hint is not None
    assert "rotate" in result.fix_hint


def test_secret_scan_exclude_and_allow_lists(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo")
    write(task, "tests/data/fixture.txt", token(seed=2) + "\n")
    write(task, "instruction.md", f"Use the sample key {token(seed=3)}.\n")
    assert hygiene(task)["TG201"].message.startswith("2 likely secrets")
    options = SecretOptions(exclude=("tests/data/*",), allow=(f"^{token(seed=3)[:8]}",))
    result = hygiene(task, Config(secrets=options))["TG201"]
    assert (result.status, result.message) == (Status.PASS, "no secrets in 6 text files")


def test_secret_scan_skips_binary_files_and_tolerates_bad_utf8(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo")
    write(task, "environment/workspace/input/blob.bin", b"\0" + token().encode())
    write(task, "environment/workspace/input/latin1.txt", b"caf\xe9 " + token(seed=4).encode())
    result = hygiene(task)["TG201"]
    assert result.message.startswith("1 likely secret: environment/workspace/input/latin1.txt:1")


def test_secret_findings_are_capped_in_the_message(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo")
    write(task, "notes.txt", "".join(f"{token(seed=n)}\n" for n in range(7)))
    message = hygiene(task)["TG201"].message
    assert message.startswith("7 likely secrets: notes.txt:1 ")
    assert message.endswith("; and 2 more")


def test_file_size_limits(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo")
    write(task, "environment/workspace/input/big.txt", "x" * 3000)
    write(task, "environment/workspace/input/bigger.txt", "x" * 5000)
    options = FileOptions(max_file_bytes=2048, max_task_bytes=6000)
    result = hygiene(task, Config(files=options))["TG202"]
    assert result.status is Status.FAIL
    assert result.message == (
        "2 files over the 2.0 KiB file limit: environment/workspace/input/bigger.txt (4.9 KiB), "
        "environment/workspace/input/big.txt (2.9 KiB); "
        "the task holds 8.2 KiB, over the 5.9 KiB task limit"
    )
    assert hygiene(task)["TG202"].message == "8 files, 8.2 KiB in total"


def test_total_size_alone_can_fail(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo")
    result = hygiene(task, Config(files=FileOptions(max_task_bytes=100)))["TG202"]
    assert result.message == "the task holds 417 B, over the 100 B task limit"


def test_many_big_files_are_capped_in_the_message(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo")
    for n in range(7):
        write(task, f"data/part{n}.txt", "y" * 200)
    result = hygiene(task, Config(files=FileOptions(max_file_bytes=150)))["TG202"]
    assert result.message.startswith("7 files over the 150 B file limit: data/part0.txt (200 B)")
    assert result.message.endswith(", and 2 more")


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (0, "0 B"),
        (1023, "1023 B"),
        (1024, "1.0 KiB"),
        (1536, "1.5 KiB"),
        (10 << 20, "10.0 MiB"),
        (3 << 30, "3.0 GiB"),
        (5 << 40, "5120.0 GiB"),
    ],
)
def test_human_size(size: int, text: str) -> None:
    assert human_size(size) == text


def test_binary_files_need_to_be_allowed(tmp_path: Path) -> None:
    task = make_task(tmp_path / "echo")
    write(task, "environment/workspace/input/chart.png", PNG_HEADER)
    write(task, "tests/expected.bin", b"\0\1\2")
    result = hygiene(task)["TG203"]
    assert (result.status, result.blocking) == (Status.FAIL, True)
    assert result.message == (
        "2 binary files not in [files] binary_allow: "
        "environment/workspace/input/chart.png, tests/expected.bin"
    )
    partly = Config(files=FileOptions(binary_allow=("environment/workspace/*.png",)))
    assert hygiene(task, partly)["TG203"].message == (
        "1 binary file not in [files] binary_allow: tests/expected.bin"
    )
    allowed = Config(files=FileOptions(binary_allow=("*.png", "tests/*.bin")))
    result = hygiene(task, allowed)["TG203"]
    assert (result.status, result.message) == (
        Status.PASS,
        "2 binary files, all in [files] binary_allow",
    )
