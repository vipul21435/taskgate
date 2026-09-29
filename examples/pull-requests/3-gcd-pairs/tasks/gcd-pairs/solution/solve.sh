#!/bin/sh
# Reference solution: math.gcd from the standard library.
set -eu
mkdir -p output
python3 - <<'PY'
import math
from pathlib import Path

rows = Path("input/pairs.txt").read_text(encoding="ascii").splitlines()
gcds = [math.gcd(*(int(part) for part in row.split(" "))) for row in rows]
Path("output/gcds.txt").write_text("".join(f"{g}\n" for g in gcds), encoding="ascii")
PY
