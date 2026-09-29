"""Grader: parse output/counts.txt once, then check it against a recount of the log.

This sample task is deliberately flawed so the demo can show TaskGate blocking
it: the first test fills a module-level cache that the other two read, so the
grader passes only when pytest runs the tests in file order. Every other gate
passes. The determinism gate (TG501) reruns the grader with the test order
shuffled and reports the tests whose outcome flips, with the seeds to rerun.
"""

from collections import Counter
from pathlib import Path

LOG = Path("input/app.log")
OUTPUT = Path("output/counts.txt")
parsed: dict[str, int] = {}


def expected() -> dict[str, int]:
    levels = Counter(line.split(" ")[1] for line in LOG.read_text(encoding="ascii").splitlines())
    return dict(sorted(levels.items()))


def test_output_parses() -> None:
    for line in OUTPUT.read_text(encoding="ascii").splitlines():
        level, count = line.split(" ")
        parsed[level] = int(count)
    assert list(parsed) == sorted(parsed), "levels are not sorted"


def test_counts_match() -> None:
    assert parsed == expected()


def test_one_line_per_level() -> None:
    assert len(parsed) == len(expected())
