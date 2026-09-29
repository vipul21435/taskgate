#!/bin/sh
# Reference solution: Python's three-argument pow computes the inverse
# and raises ValueError when a and m are not coprime.
set -eu
mkdir -p output
python3 - <<'PY'
from pathlib import Path

lines = []
for raw in Path("input/pairs.txt").read_text(encoding="utf-8").splitlines():
    a, m = (int(part) for part in raw.split(" "))
    try:
        lines.append(str(pow(a, -1, m)))
    except ValueError:
        lines.append("none")
Path("output/inverses.txt").write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
PY
