"""Grader: recompute every determinant by cofactor expansion (independent of the
elimination the reference solution uses) and compare the output byte for byte."""

from pathlib import Path

MATRICES = Path("input/matrices.txt")
OUTPUT = Path("output/determinants.txt")


def cofactor_det(matrix: list[list[int]]) -> int:
    if len(matrix) == 1:
        return matrix[0][0]
    total = 0
    for col, entry in enumerate(matrix[0]):
        if entry:
            minor = [row[:col] + row[col + 1 :] for row in matrix[1:]]
            total += (-1) ** col * entry * cofactor_det(minor)
    return total


def matrices() -> list[list[list[int]]]:
    blocks = MATRICES.read_text(encoding="utf-8").split("\n\n")
    return [[[int(x) for x in row.split(" ")] for row in b.splitlines()] for b in blocks]


def expected_bytes() -> bytes:
    return "".join(f"{cofactor_det(m)}\n" for m in matrices()).encode("ascii")


def test_output_file_exists() -> None:
    assert OUTPUT.is_file(), "output/determinants.txt was not written"


def test_one_line_per_matrix() -> None:
    assert len(OUTPUT.read_text(encoding="ascii").splitlines()) == len(matrices())


def test_output_is_byte_exact() -> None:
    assert OUTPUT.read_bytes() == expected_bytes()
