"""Grader: recompute every gcd with Euclid's algorithm and compare line by line.

This sample task is deliberately flawed so the demo can show TaskGate blocking
it: zip() stops at the shorter of its inputs, so an empty output/gcds.txt passes
every comparison (gate TG403 runs exactly that stub), and
environment/Dockerfile names its base image by tag only (gate TG302). An
untouched workspace does fail, because the file must exist, so the baseline
gate (TG402) alone would let this grader through.
"""

from pathlib import Path

PAIRS = Path("input/pairs.txt")
OUTPUT = Path("output/gcds.txt")


def euclid(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a


def pairs() -> list[tuple[int, int]]:
    rows = PAIRS.read_text(encoding="ascii").splitlines()
    return [(int(a), int(b)) for a, b in (row.split(" ") for row in rows)]


def test_output_file_exists() -> None:
    assert OUTPUT.is_file(), "output/gcds.txt was not written"


def test_every_gcd_matches() -> None:
    lines = OUTPUT.read_text(encoding="ascii").splitlines()
    for (a, b), line in zip(pairs(), lines):  # noqa: B905 - the planted flaw
        assert int(line) == euclid(a, b)
