"""The secret scanner's detectors, allowlists and redaction.

Every fake credential is assembled at runtime from a prefix and seeded random
characters, so no literal token appears in this repository.
"""

import random
import string

import pytest

from taskgate.secretscan import (
    PRAGMA,
    Finding,
    Scanner,
    class_transitions,
    describe_all,
    looks_random,
    shannon_entropy,
    word_bigrams,
)

ALNUM = string.ascii_letters + string.digits
BASE64 = ALNUM + "+/"


def random_token(length: int, seed: int = 1, alphabet: str = ALNUM) -> str:
    rng = random.Random(seed)
    return "".join(rng.choice(alphabet) for _ in range(length))


def flagged_token(length: int, seed: int = 1) -> str:
    """The first seeded random token from ``seed`` on that the entropy rule flags."""
    return next(t for s in range(seed, seed + 100) if looks_random(t := random_token(length, s), 4))


def upper_digits(length: int, seed: int = 2) -> str:
    return random_token(length, seed, string.ascii_uppercase + string.digits)


def scan(line: str, **options: object) -> list[tuple[str, str]]:
    scanner = Scanner(**options)  # type: ignore[arg-type]
    return [(f.kind, f.text) for f in scanner.scan_line(line, "file.txt", 1)]


KNOWN = {
    "AWS access key id": "AKIA" + upper_digits(16),
    "GitHub token": "gh" + "p_" + random_token(36),
    "GitHub fine-grained token": "github" + "_pat_" + random_token(40),
    "GitLab token": "gl" + "pat-" + random_token(20),
    "Slack token": "xo" + "xb-" + random_token(24),
    "Google API key": "AI" + "za" + random_token(35),
    "Stripe key": "sk" + "_live_" + random_token(24),
    "Hugging Face token": "hf" + "_" + random_token(34),
    "npm token": "npm" + "_" + random_token(36),
    "sk- API key": "sk" + "-" + random_token(40),
    "JSON web token": ".".join(("ey" + "J" + random_token(20, s)) for s in (1, 2))
    + "."
    + random_token(20, 3),
}


@pytest.mark.parametrize(("kind", "token"), sorted(KNOWN.items()))
def test_known_formats_are_found_in_context(kind: str, token: str) -> None:
    found = scan(f'API_TOKEN = "{token}"  # do not commit')
    assert found == [(kind, token)]


def test_private_key_headers_are_found_and_never_shown() -> None:
    for header in (
        "RSA PRIVATE KEY",
        "OPENSSH PRIVATE KEY",
        "PRIVATE KEY",
        "PGP PRIVATE KEY BLOCK",
    ):
        line = "-----BEGIN " + header + "-----"
        assert scan(line) == [("private key", line)]
    finding = Finding("keys/id", 3, "private key", "-----BEGIN " + "RSA PRIVATE KEY-----")
    assert finding.describe() == "keys/id:3 private key"
    assert scan("-----BEGIN " + "PUBLIC KEY-----") == []


def test_random_tokens_are_found_by_entropy() -> None:
    token = flagged_token(32)
    assert scan(f"password: {token}") == [("high-entropy string", token)]
    assert scan(f"password: {token[:23]}") == []
    assert scan(f"password: {token[:23]}", min_length=16) == [("high-entropy string", token[:23])]
    assert scan(f"password: {token}", entropy_threshold=5.5) == []


@pytest.mark.parametrize(("length", "floor"), [(24, 0.85), (32, 0.95), (40, 0.95), (64, 0.99)])
def test_the_entropy_rule_flags_most_random_tokens(length: int, floor: float) -> None:
    """Measured recall on 2000 seeded base64 tokens: 0.8905, 0.9665, 0.972, 0.9975."""
    tokens = [random_token(length, seed, BASE64) for seed in range(2000)]
    found = sum(looks_random(token, 4.0) for token in tokens) / len(tokens)
    assert found >= floor


@pytest.mark.parametrize(
    "text",
    [
        "PyUnicode_AsLatin1String",
        "CPyBytes_ReadF32BEUnsafe",
        "CPyList_GetItemInt64Borrow",
        "MYPYC_DECLARED_tuple_T2VU1U1",
        "BSD-3-Clause-No-Nuclear-License-2014",
        "test_output_is_byte_exact_for_every_matrix",
        "environment/workspace/input/matrices.txt",
        "3f786850e387550fdab836ed7e6dc881de23001b3f786850e387550fdab836ed",
        "https://example.com/commit/9fceb02d0ae598e95dc970b74767f19372d61af8?Token=Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7",
        '"integrity": "sha512-' + random_token(64, 4, BASE64) + '=="',
        "pkg/__init__.py,sha256=" + random_token(43, 5, ALNUM + "-_") + ",1234",
        "FROM python:3.12-slim@sha256:" + "f77ac9e44ae96ef2c90b8053ea08c31f" * 2,
    ],
)
def test_identifiers_digests_paths_and_urls_are_not_secrets(text: str) -> None:
    assert scan(text) == []


def test_the_word_rule_separates_identifiers_from_random_text() -> None:
    assert word_bigrams("PyUnicode_AsLatin1String") > 0.4
    assert word_bigrams(random_token(40)) < 0.15
    assert word_bigrams("") == 0.0
    assert class_transitions("aA1") == 1.0
    assert class_transitions("a") == 0.0
    assert shannon_entropy("") == 0.0
    assert shannon_entropy("abcd") == 2.0


def test_pragma_and_allowlist_suppress_findings() -> None:
    token = flagged_token(32)
    assert scan(f"key = {token}  # {PRAGMA}") == []
    assert scan(f"key = {token}", allow=(f"^{token[:6]}",)) == []
    assert scan(f"key = {token}", allow=("^nomatch",)) == [("high-entropy string", token)]


def test_a_known_format_is_not_reported_twice_by_the_entropy_detector() -> None:
    token = KNOWN["GitHub token"]
    assert scan(token) == [("GitHub token", token)]


def test_scan_text_numbers_lines_and_describe_redacts() -> None:
    token = flagged_token(30)
    findings = Scanner().scan_text(f"one\ntwo {token}\n", "solution/solve.sh")
    assert findings == [Finding("solution/solve.sh", 2, "high-entropy string", token)]
    assert findings[0].describe() == (
        f"solution/solve.sh:2 high-entropy string '{token[:4]}...' (30 chars)"
    )
    assert token not in describe_all(findings)


def test_describe_all_caps_the_list() -> None:
    findings = [Finding("f", n, "high-entropy string", "abcdefgh") for n in range(1, 8)]
    described = describe_all(findings, limit=2)
    assert described == (
        "f:1 high-entropy string 'abcd...' (8 chars); "
        "f:2 high-entropy string 'abcd...' (8 chars); and 5 more"
    )
    assert describe_all(findings[:1]) == "f:1 high-entropy string 'abcd...' (8 chars)"
