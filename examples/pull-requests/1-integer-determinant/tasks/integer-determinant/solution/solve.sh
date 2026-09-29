#!/bin/sh
# Reference solution: fraction-free Gaussian elimination (Bareiss), which keeps
# every intermediate value an exact integer.
set -eu
mkdir -p output
python3 - <<'PY'
from pathlib import Path


def bareiss(matrix: list[list[int]]) -> int:
    a = [row[:] for row in matrix]
    n = len(a)
    sign, previous = 1, 1
    for k in range(n - 1):
        if a[k][k] == 0:
            swap = next((i for i in range(k + 1, n) if a[i][k] != 0), None)
            if swap is None:
                return 0
            a[k], a[swap] = a[swap], a[k]
            sign = -sign
        for i in range(k + 1, n):
            for j in range(k + 1, n):
                a[i][j] = (a[i][j] * a[k][k] - a[i][k] * a[k][j]) // previous
        previous = a[k][k]
    return sign * a[n - 1][n - 1]


text = Path("input/matrices.txt").read_text(encoding="utf-8")
blocks = [block for block in text.split("\n\n") if block.strip()]
matrices = [[[int(x) for x in row.split(" ")] for row in block.splitlines()] for block in blocks]
lines = "".join(f"{bareiss(m)}\n" for m in matrices)
Path("output/determinants.txt").write_text(lines, encoding="utf-8")
PY
