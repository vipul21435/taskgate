"""Manifest and instruction lint: unknown manifest keys (TG103), a timeout outside
the recommended range (TG104) and an instruction with no actual text (TG105).

These gates look only at files, so they are cheap and never need a runner. They
skip themselves (rather than fail a second time) when the file they lint is
missing or does not parse; TG101 and TG102 already report that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from taskgate.gates.base import Check, TaskContext, gate
from taskgate.manifest import unknown_keys
from taskgate.results import Severity

INSTRUCTION = "instruction.md"
_ATX_HEADING = re.compile(r" {0,3}#{1,6}(?:[ \t]|$)")
_SETEXT_UNDERLINE = re.compile(r" {0,3}(?:=+|-+)[ \t]*$")
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
_BLOCK_MARKER = re.compile(r"[ \t]*(?:>|(?:[-*+]|\d{1,9}[.)])(?=[ \t]|$))")
_TASK_BOX = re.compile(r"[ \t]*\[[ xX]\](?=[ \t]|$)")


def strip_comments(text: str) -> str:
    """``text`` without HTML comments; an unclosed ``<!--`` hides everything after it.

    Linear time (``str.find``), unlike a lazy ``<!--.*?-->`` regex, which rescans to
    the end of the text from every unclosed ``<!--``. ``<!-->`` and ``<!--->`` are
    complete comments, as in CommonMark.
    """
    kept: list[str] = []
    position = 0
    while (start := text.find("<!--", position)) >= 0:
        kept.append(text[position:start])
        end = text.find("-->", start + 2)
        if end < 0:
            return "".join(kept)
        position = end + 3
    kept.append(text[position:])
    return "".join(kept)


def _words(line: str) -> int:
    """Words on a line: tokens with a letter or digit, after list, quote and task-box markers."""
    position = 0
    while marker := _BLOCK_MARKER.match(line, position):
        position = marker.end()
    if box := _TASK_BOX.match(line, position):
        position = box.end()
    return sum(any(char.isalnum() for char in token) for token in line[position:].split())


def _closes(line: str, fence: str) -> bool:
    """True when ``line`` closes a code block opened with ``fence`` (same character, as long)."""
    body = line.strip()
    indent = len(line) - len(line.lstrip(" "))
    return indent <= 3 and len(body) >= len(fence) and set(body) == {fence[0]}


@dataclass(frozen=True, slots=True)
class InstructionText:
    """What a reader of the rendered ``instruction.md`` would see."""

    blank: bool
    """Nothing but whitespace once HTML comments are removed."""

    headings: int
    words: int
    """Tokens with a letter or digit outside headings, markers and fence lines."""

    markup: int
    """Non-blank lines with no words that are not headings (``-``, ``---``, fences, ``>``)."""


def read_instruction(markdown: str) -> InstructionText:
    """Count the words a Markdown instruction shows once comments, headings and markup go.

    Headings are ATX (``# Title``) and setext (a paragraph underlined with ``===``
    or ``---``). Lines inside fenced code blocks count as text; the fence lines do
    not. List bullets, ordered-list numbers, ``>`` quote markers and ``[ ]`` task
    boxes are not words, so a template with an empty bullet, a thematic break or
    an empty code block has no text.
    """
    visible = strip_comments(markdown)
    words = headings = markup = 0
    paragraph: int | None = None  # words of the lines a setext underline would claim
    fence: str | None = None
    for line in visible.splitlines():
        if fence is not None:
            if _closes(line, fence):
                fence = None
                markup += 1
            else:
                words += _words(line)
            continue
        opened = _FENCE.match(line)
        if opened:
            fence, paragraph = opened.group(1), None
            markup += 1
        elif not line.strip():
            paragraph = None
        elif _ATX_HEADING.match(line):
            headings, paragraph = headings + 1, None
        elif paragraph is not None and _SETEXT_UNDERLINE.match(line):
            words -= paragraph
            headings, paragraph = headings + 1, None
        else:
            count = _words(line)
            words += count
            markup += count == 0
            if count == 0 or _BLOCK_MARKER.match(line):
                paragraph = None
            else:
                paragraph = (paragraph or 0) + count
    return InstructionText(blank=not visible.strip(), headings=headings, words=words, markup=markup)


@gate(
    "TG103",
    "manifest-known-keys",
    severity=Severity.WARNING,
    summary="task.toml has no unknown tables or keys (likely typos)",
    fix_hint=(
        "Rename or remove the unknown keys; every key task.toml may hold is listed in "
        "docs/task-layout.md."
    ),
)
def manifest_known_keys(ctx: TaskContext) -> Check:
    if not ctx.manifest.parsed:
        return Check.skip("task.toml is missing or does not parse (see TG102)")
    unknown = unknown_keys(ctx.manifest.raw)
    if unknown:
        return Check.fail(f"unknown in task.toml: {', '.join(unknown)}")
    return Check.ok("no unknown keys")


@gate(
    "TG104",
    "timeout-in-range",
    severity=Severity.WARNING,
    summary="task.timeout_sec is inside the recommended range (default 10..1800 s)",
    fix_hint=(
        "Give task.timeout_sec enough headroom for container start-up and a slow grader, "
        "without letting a stuck run hold a worker for long; the range is [manifest] "
        "min_timeout_sec and max_timeout_sec in taskgate.toml."
    ),
)
def timeout_in_range(ctx: TaskContext) -> Check:
    timeout = ctx.manifest.declared_timeout_sec
    if timeout is None:
        return Check.skip("task.timeout_sec is missing or invalid (see TG102)")
    low = ctx.config.manifest.min_timeout_sec
    high = ctx.config.manifest.max_timeout_sec
    if timeout < low:
        return Check.fail(f"task.timeout_sec {timeout} is below the recommended minimum {low}")
    if timeout > high:
        return Check.fail(f"task.timeout_sec {timeout} is above the recommended maximum {high}")
    return Check.ok(f"task.timeout_sec {timeout} is within {low}..{high}")


@gate(
    "TG105",
    "instruction-not-empty",
    severity=Severity.ERROR,
    summary="instruction.md is UTF-8 and has text beyond headings and comments",
    fix_hint=(
        "Write the task statement in instruction.md: what the agent must produce, "
        "where it goes and in what format."
    ),
)
def instruction_not_empty(ctx: TaskContext) -> Check:
    path = ctx.task_dir / INSTRUCTION
    if not path.is_file():
        return Check.skip("instruction.md not found (see TG101)")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return Check.fail("instruction.md is not valid UTF-8")
    found = read_instruction(text)
    if found.blank:
        return Check.fail("instruction.md is empty (or holds only comments)")
    if found.words == 0:
        if found.markup == 0:
            return Check.fail("instruction.md has only headings")
        return Check.fail("instruction.md has no text, only headings and Markdown markup")
    return Check.ok(f"instruction.md has {found.words} word{'' if found.words == 1 else 's'}")


LINT_GATES = (manifest_known_keys, timeout_in_range, instruction_not_empty)
