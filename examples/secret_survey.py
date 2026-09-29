"""Measure the TG201 secret scanner: false positives on real code, recall on random tokens.

Usage:
    python examples/secret_survey.py scan DIR   # findings over every text file under DIR
    python examples/secret_survey.py recall     # share of seeded random tokens flagged
    python examples/secret_survey.py timing     # scan time of long single lines

``scan`` reads files the way the gate does (binary files skipped, invalid UTF-8
replaced) with the default options and prints the totals and the first findings,
redacted. ``recall`` generates 2000 seeded base64 tokens per length and prints the
share the high-entropy rule flags, so the numbers are the same on every machine.
``timing`` scans one line of 20,000 to 1,000,000 characters of each shape an input
file often has (one letter, two letters, hex, DNA) and prints the seconds taken,
best of three, so linear growth is easy to check.
"""

from __future__ import annotations

import random
import string
import sys
import time
from collections import Counter
from pathlib import Path

from taskgate.gates import is_binary
from taskgate.secretscan import Scanner, looks_random

BASE64 = string.ascii_letters + string.digits + "+/"
SHOWN = 10


def scan(root: Path) -> None:
    scanner = Scanner()
    files = lines = 0
    kinds: Counter[str] = Counter()
    shown: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        data = path.read_bytes()
        if is_binary(data):
            continue
        text = data.decode("utf-8", "replace")
        files += 1
        lines += len(text.splitlines())
        for finding in scanner.scan_text(text, path.relative_to(root).as_posix()):
            kinds[finding.kind] += 1
            if len(shown) < SHOWN:
                shown.append(finding.describe())
    print(f"{files} text files, {lines} lines, {sum(kinds.values())} findings")
    for kind, count in sorted(kinds.items()):
        print(f"  {count:>6}  {kind}")
    for line in shown:
        print(f"  {line}")


def recall(samples: int = 2000) -> None:
    for length in (24, 32, 40, 64):
        flagged = 0
        for seed in range(samples):
            rng = random.Random(seed)
            token = "".join(rng.choice(BASE64) for _ in range(length))
            flagged += looks_random(token, 4.0)
        print(f"{length} chars: {flagged}/{samples} flagged ({flagged / samples:.4f})")


def timing() -> None:
    rng = random.Random(0)
    shapes = {
        "a": lambda n: "a" * n,
        "ab": lambda n: "".join(rng.choice("ab") for _ in range(n)),
        "hex": lambda n: "".join(rng.choice("0123456789abcdef") for _ in range(n)),
        "ACGT": lambda n: "".join(rng.choice("ACGT") for _ in range(n)),
    }
    print("chars      " + "  ".join(f"{name:>8}" for name in shapes))
    for length in (20_000, 80_000, 1_000_000):
        cells = []
        for make in shapes.values():
            line = make(length)
            best = min(_seconds(line) for _ in range(3))
            cells.append(f"{best:>7.3f}s")
        print(f"{length:<9}  " + "  ".join(cells))


def _seconds(line: str) -> float:
    started = time.perf_counter()
    Scanner().scan_line(line, "input.txt", 1)
    return time.perf_counter() - started


def main() -> None:
    match sys.argv[1:]:
        case ["scan", directory]:
            scan(Path(directory))
        case ["recall"]:
            recall()
        case ["timing"]:
            timing()
        case _:
            sys.exit("usage: python examples/secret_survey.py scan DIR | recall | timing")


if __name__ == "__main__":
    main()
