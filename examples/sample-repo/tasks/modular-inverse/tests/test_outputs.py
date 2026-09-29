"""Grader: recompute every inverse with the extended Euclidean algorithm and
compare the agent's output file byte for byte."""

from pathlib import Path

PAIRS = Path("input/pairs.txt")
OUTPUT = Path("output/inverses.txt")


def extended_gcd(a: int, b: int) -> tuple[int, int, int]:
    old_r, r = a, b
    old_s, s = 1, 0
    old_t, t = 0, 1
    while r:
        q = old_r // r
        old_r, r = r, old_r - q * r
        old_s, s = s, old_s - q * s
        old_t, t = t, old_t - q * t
    return old_r, old_s, old_t


def expected_line(a: int, m: int) -> str:
    g, x, _ = extended_gcd(a % m, m)
    return str(x % m) if g == 1 else "none"


def pairs() -> list[tuple[int, int]]:
    rows = PAIRS.read_text(encoding="utf-8").splitlines()
    return [(int(a), int(m)) for a, m in (row.split(" ") for row in rows)]


def test_output_file_exists() -> None:
    assert OUTPUT.is_file(), "output/inverses.txt was not written"


def test_output_is_byte_exact() -> None:
    expected = "".join(f"{expected_line(a, m)}\n" for a, m in pairs())
    assert OUTPUT.read_bytes() == expected.encode("ascii")


def test_every_inverse_checks_out() -> None:
    for (a, m), line in zip(pairs(), OUTPUT.read_text(encoding="ascii").splitlines(), strict=True):
        if line != "none":
            assert (a * int(line)) % m == 1
