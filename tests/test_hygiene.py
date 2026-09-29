"""Hygiene gates (TG201, TG202, TG203) on passing and failing fixtures."""

import random
import string
import time
from pathlib import Path

import pytest

from taskfactory import make_task
from taskgate.config import Config, FileOptions, SecretOptions
from taskgate.engine import run_gates
from taskgate.gates.hygiene import HYGIENE_GATES, human_size, text_lines
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


def small_task(path: Path) -> Path:
    """The factory task with a one-line Dockerfile, so the size numbers below stay small."""
    return make_task(path, dockerfile="FROM scratch\n")


def hygiene(
    task: Path, config: Config | None = None, tracked: list[str] | None = None
) -> dict[str, GateResult]:
    results = run_gates(task, gates=HYGIENE_GATES, config=config, tracked=tracked)
    return {r.code: r for r in results}


def write(task: Path, relative: str, content: str | bytes) -> None:
    path = task / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def test_a_clean_task_passes_every_hygiene_gate(tmp_path: Path) -> None:
    results = hygiene(small_task(tmp_path / "echo"))
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
    task = small_task(tmp_path / "echo")
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
    task = small_task(tmp_path / "echo")
    write(task, "tests/data/fixture.txt", token(seed=2) + "\n")
    write(task, "instruction.md", f"Use the sample key {token(seed=3)}.\n")
    assert hygiene(task)["TG201"].message.startswith("2 likely secrets")
    options = SecretOptions(exclude=("tests/data/*",), allow=(f"^{token(seed=3)[:8]}",))
    result = hygiene(task, Config(secrets=options))["TG201"]
    assert (result.status, result.message) == (Status.PASS, "no secrets in 6 text files")


def test_secret_scan_skips_binary_files_and_tolerates_bad_utf8(tmp_path: Path) -> None:
    task = small_task(tmp_path / "echo")
    write(task, "environment/workspace/input/blob.bin", b"\0" + token().encode())
    write(task, "environment/workspace/input/latin1.txt", b"caf\xe9 " + token(seed=4).encode())
    result = hygiene(task)["TG201"]
    assert result.message.startswith("1 likely secret: environment/workspace/input/latin1.txt:1")


def test_secret_findings_are_capped_in_the_message(tmp_path: Path) -> None:
    task = small_task(tmp_path / "echo")
    write(task, "notes.txt", "".join(f"{token(seed=n)}\n" for n in range(7)))
    message = hygiene(task)["TG201"].message
    assert message.startswith("7 likely secrets: notes.txt:1 ")
    assert message.endswith("; and 2 more")


def test_file_size_limits(tmp_path: Path) -> None:
    task = small_task(tmp_path / "echo")
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
    task = small_task(tmp_path / "echo")
    result = hygiene(task, Config(files=FileOptions(max_task_bytes=100)))["TG202"]
    assert result.message == "the task holds 417 B, over the 100 B task limit"


def test_many_big_files_are_capped_in_the_message(tmp_path: Path) -> None:
    task = small_task(tmp_path / "echo")
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
    task = small_task(tmp_path / "echo")
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


def test_a_one_megabyte_input_line_is_scanned_in_seconds(tmp_path: Path) -> None:
    """Regression: a 1,000,000-character one-line input took 673 s in TG201."""
    task = small_task(tmp_path / "longest-palindrome")
    rng = random.Random(7)
    text = "".join(rng.choice("ab") for _ in range(1_000_000)) + "\n"
    write(task, "environment/workspace/input/s.txt", text)
    started = time.perf_counter()
    results = hygiene(task)
    assert time.perf_counter() - started < 10.0
    assert {code: r.status for code, r in results.items()} == {
        "TG201": Status.PASS,
        "TG202": Status.PASS,
        "TG203": Status.PASS,
    }


def test_the_secret_scan_reads_at_most_the_file_size_limit(tmp_path: Path) -> None:
    task = small_task(tmp_path / "echo")
    early, late = token(seed=5), token(seed=6)
    write(task, "data/big.txt", f"{early}\n" + "x" * 3000 + f"\n{late}\n")
    write(task, "data/one-line.txt", "y " * 2500 + late + "\n")
    result = hygiene(task, Config(files=FileOptions(max_file_bytes=2048)))["TG201"]
    assert result.message == (
        f"1 likely secret: data/big.txt:1 high-entropy string '{early[:4]}...' (32 chars); "
        "only the first 2.0 KiB of 2 larger files was scanned (see TG202)"
    )
    write(task, "data/big.txt", "z\n")
    result = hygiene(task, Config(files=FileOptions(max_file_bytes=2048)))["TG201"]
    assert (result.status, result.message) == (
        Status.PASS,
        "no secrets in 8 text files; only the first 2.0 KiB of 1 larger file was scanned "
        "(see TG202)",
    )
    assert "late" not in result.message
    assert hygiene(task)["TG201"].message.startswith("1 likely secret: data/one-line.txt:1 ")


def test_text_lines_stop_at_the_budget(tmp_path: Path) -> None:
    path = tmp_path / "f.txt"
    path.write_bytes(b"ab\r\ncdef\nghij")
    with path.open("rb") as handle:
        assert list(text_lines(handle, 100)) == ["ab", "cdef", "ghij"]
    with path.open("rb") as handle:
        assert list(text_lines(handle, 6)) == ["ab", "cd"]
    with path.open("rb") as handle:
        assert list(text_lines(handle, 0)) == []


def test_committed_files_named_like_caches_are_checked_in_diff_mode(tmp_path: Path) -> None:
    """Regression: TG201-TG203 skipped committed __pycache__/, *.pyc, .DS_Store and
    .mypy_cache/ files, so a secret, a 5 MiB .pyc and a hidden binary input passed."""
    task = small_task(tmp_path / "echo")
    key = "AKIA" + "".join(random.Random(3).choice(string.ascii_uppercase) for _ in range(16))
    write(task, "solution/__pycache__/notes.txt", f"AWS_ACCESS_KEY_ID={key}\n")
    write(task, "solution/.DS_Store", "-----BEGIN " + "RSA PRIVATE KEY-----\nabc\n")
    write(task, "environment/workspace/model.pyc", b"\0" * (5 << 20))
    write(task, "environment/workspace/input/.mypy_cache/blob.bin", b"\0\1" * (3 << 19))
    on_disk = hygiene(task)
    assert on_disk["TG201"].message == "no secrets in 6 text files"
    tracked = [
        "task.toml",
        "instruction.md",
        "environment/Dockerfile",
        "environment/workspace/input/name.txt",
        "environment/workspace/input/.mypy_cache/blob.bin",
        "environment/workspace/model.pyc",
        "solution/solve.sh",
        "solution/.DS_Store",
        "solution/__pycache__/notes.txt",
        "tests/test_outputs.py",
    ]
    results = hygiene(task, tracked=tracked)
    assert results["TG201"].message == (
        "2 likely secrets: solution/.DS_Store:1 private key; "
        "solution/__pycache__/notes.txt:1 AWS access key id 'AKIA...' (20 chars)"
    )
    assert results["TG202"].message == (
        "2 files over the 1.0 MiB file limit: environment/workspace/model.pyc (5.0 MiB), "
        "environment/workspace/input/.mypy_cache/blob.bin (3.0 MiB)"
    )
    assert results["TG203"].message == (
        "2 binary files not in [files] binary_allow: "
        "environment/workspace/input/.mypy_cache/blob.bin, environment/workspace/model.pyc"
    )
    assert all(result.blocking for result in results.values())


def test_tracked_paths_that_are_not_regular_files_are_left_out(tmp_path: Path) -> None:
    task = small_task(tmp_path / "echo")
    (task / "link.txt").symlink_to(task / "instruction.md")
    (task / "submodule").mkdir()
    tracked = ["instruction.md", "link.txt", "submodule", "deleted-locally.txt", "task.toml"]
    assert hygiene(task, tracked=tracked)["TG202"].message == "2 files, 190 B in total"
