"""Find credentials in text: known token formats, private keys and random-looking strings.

Three detectors run on every line:

1. **Known formats** with a fixed prefix and shape (cloud access key ids, source
   host tokens, chat and payment API keys, ``sk-`` style API keys, JSON web tokens).
2. **Private keys**: any ``-----BEGIN ... PRIVATE KEY-----`` header.
3. **High-entropy strings**: runs of at least ``min_length`` base64 or URL-safe
   characters that mix upper case, lower case and digits, have a Shannon entropy
   of at least ``entropy_threshold`` bits per character, switch character class
   often (at least 40% of neighbouring pairs) and do not read like words (fewer
   than 15% of neighbouring letter pairs are common English bigrams such as
   ``th`` or ``re``). The word rule is what separates random tokens from long
   identifiers such as ``PyUnicode_AsLatin1String``, which pass the first three.
   Hex digests never qualify (no upper case with lower case), and URLs and
   ``sha256=``/``sha512-`` digests are blanked out before this detector runs.

A line containing ``taskgate: allow-secret`` is skipped, and a finding whose text
matches an allowlist regex is dropped. Findings never carry more than the first
four characters of the matched text, so reports cannot leak the secret.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise

PRAGMA = "taskgate: allow-secret"
MIN_TRANSITIONS = 0.4
MAX_WORD_BIGRAMS = 0.15
_BIGRAMS = (
    "thheineranreonatenndtiesorteofedisitalarsttontngse"
    "haasouiolevecomedehiriroicneearacelichllbemasiomur"
)
COMMON_BIGRAMS: frozenset[str] = frozenset(_BIGRAMS[i : i + 2] for i in range(0, 100, 2))
"""The 50 most frequent letter pairs in English text; identifiers are full of them."""
SHOWN_CHARS = 4
PRIVATE_KEY = "private key"

KNOWN_FORMATS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,255}\b")),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("Stripe key", re.compile(r"\b[rs]k_live_[0-9A-Za-z]{20,}")),
    ("Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9]{34,}\b")),
    ("npm token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("sk- API key", re.compile(r"\bsk-[A-Za-z0-9_-]{32,}")),
    (
        "JSON web token",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ),
    (PRIVATE_KEY, re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")),
)
_BLANKED = re.compile(
    r"[A-Za-z][A-Za-z0-9+.-]*://\S+"  # URLs
    r"|\bsha(?:1|224|256|384|512)[-=:][A-Za-z0-9+/_-]+={0,2}"  # lockfile and RECORD digests
)


@dataclass(frozen=True, slots=True)
class Finding:
    """One likely credential."""

    path: str
    line: int
    kind: str
    text: str

    def describe(self) -> str:
        """``path:line kind 'abcd...' (N chars)``; never the full text."""
        if self.kind == PRIVATE_KEY:
            return f"{self.path}:{self.line} {PRIVATE_KEY}"
        shown = self.text[:SHOWN_CHARS]
        return f"{self.path}:{self.line} {self.kind} '{shown}...' ({len(self.text)} chars)"


def shannon_entropy(text: str) -> float:
    """Bits per character of ``text``'s own character distribution."""
    counts = Counter(text)
    total = len(text)
    return -sum(n / total * math.log2(n / total) for n in counts.values()) if total else 0.0


def _char_class(char: str) -> int:
    if char.isupper():
        return 0
    if char.islower():
        return 1
    return 2 if char.isdigit() else 3


def class_transitions(text: str) -> float:
    """Share of neighbouring character pairs whose class (upper, lower, digit, other) differs."""
    if len(text) < 2:
        return 0.0
    changes = sum(_char_class(a) != _char_class(b) for a, b in pairwise(text))
    return changes / (len(text) - 1)


def word_bigrams(text: str) -> float:
    """Share of neighbouring character pairs that are common English bigrams (any case)."""
    if len(text) < 2:
        return 0.0
    lower = text.lower()
    return sum(a + b in COMMON_BIGRAMS for a, b in pairwise(lower)) / (len(text) - 1)


def looks_random(token: str, entropy_threshold: float) -> bool:
    """The high-entropy rule, for a token already known to be long enough."""
    return (
        any(c.isupper() for c in token)
        and any(c.islower() for c in token)
        and any(c.isdigit() for c in token)
        and shannon_entropy(token) >= entropy_threshold
        and class_transitions(token) >= MIN_TRANSITIONS
        and word_bigrams(token) < MAX_WORD_BIGRAMS
    )


class Scanner:
    """A configured scan; build one per task and reuse it for every file."""

    def __init__(
        self,
        *,
        min_length: int = 24,
        entropy_threshold: float = 4.0,
        allow: Iterable[str] = (),
    ) -> None:
        self.entropy_threshold = entropy_threshold
        self._candidate = re.compile(rf"[A-Za-z0-9+/_-]{{{min_length},}}={{0,2}}")
        self._allow = tuple(re.compile(pattern) for pattern in allow)

    def _allowed(self, text: str) -> bool:
        return any(pattern.search(text) for pattern in self._allow)

    def scan_line(self, line: str, path: str, number: int) -> list[Finding]:
        if PRAGMA in line:
            return []
        findings: list[Finding] = []
        taken: list[tuple[int, int]] = []
        for kind, pattern in KNOWN_FORMATS:
            for match in pattern.finditer(line):
                taken.append(match.span())
                findings.append(Finding(path, number, kind, match.group()))
        blanked = _BLANKED.sub(lambda m: " " * len(m.group()), line)
        for match in self._candidate.finditer(blanked):
            start, end = match.span()
            if any(start < t_end and t_start < end for t_start, t_end in taken):
                continue
            if looks_random(match.group(), self.entropy_threshold):
                findings.append(Finding(path, number, "high-entropy string", match.group()))
        return [finding for finding in findings if not self._allowed(finding.text)]

    def scan_lines(self, lines: Iterable[str], path: str) -> list[Finding]:
        """Scan ``lines`` (without line endings) one at a time, numbering them from 1."""
        return [
            finding
            for number, line in enumerate(lines, start=1)
            for finding in self.scan_line(line, path, number)
        ]

    def scan_text(self, text: str, path: str) -> list[Finding]:
        return self.scan_lines(text.splitlines(), path)


def describe_all(findings: Iterable[Finding], limit: int = 5) -> str:
    """A one-line list of the first ``limit`` findings and how many more there are."""
    items = list(findings)
    shown = "; ".join(finding.describe() for finding in items[:limit])
    more = f"; and {len(items) - limit} more" if len(items) > limit else ""
    return f"{shown}{more}"
