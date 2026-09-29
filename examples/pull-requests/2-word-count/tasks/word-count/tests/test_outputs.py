"""Grader: compare the counts with str.split, line by line.

This sample task is deliberately flawed so the demo can show TaskGate blocking
it: the tests skip when the output file is missing, so a workspace where the
agent did nothing passes (gate TG402), task.toml uses a difficulty value the
schema does not allow (gate TG102), and solution/solve.sh exports a leftover
API key (gate TG201; the key is a random string, not a real credential).
"""

from pathlib import Path

import pytest

TEXT = Path("input/text.txt")
OUTPUT = Path("output/counts.txt")


def expected() -> str:
    lines = TEXT.read_text(encoding="ascii").splitlines()
    return "".join(f"{sum(1 for word in line.split(' ') if word)}\n" for line in lines)


def test_counts_match() -> None:
    if not OUTPUT.exists():
        pytest.skip("output/counts.txt not written yet")
    assert OUTPUT.read_text(encoding="ascii") == expected()


def test_one_count_per_line() -> None:
    if not OUTPUT.exists():
        pytest.skip("output/counts.txt not written yet")
    assert len(OUTPUT.read_text(encoding="ascii").splitlines()) == len(
        TEXT.read_text(encoding="ascii").splitlines()
    )
