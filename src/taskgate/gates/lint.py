"""Manifest and instruction lint: unknown manifest keys (TG103), a timeout outside
the recommended range (TG104) and an instruction with no actual text (TG105).

These gates look only at files, so they are cheap and never need a runner. They
skip themselves (rather than fail a second time) when the file they lint is
missing or does not parse; TG101 and TG102 already report that.
"""

from __future__ import annotations

import re

from taskgate.gates.base import Check, TaskContext, gate
from taskgate.manifest import unknown_keys
from taskgate.results import Severity

INSTRUCTION = "instruction.md"
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_HEADING = re.compile(r"^\s{0,3}#{1,6}(?:\s|$)")


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
    visible = _COMMENT.sub("", text)
    if not visible.strip():
        return Check.fail("instruction.md is empty (or holds only comments)")
    body = [line for line in visible.splitlines() if line.strip() and not _HEADING.match(line)]
    if not body:
        return Check.fail("instruction.md has only headings")
    words = sum(len(line.split()) for line in body)
    return Check.ok(f"instruction.md has {words} word{'' if words == 1 else 's'}")


LINT_GATES = (manifest_known_keys, timeout_in_range, instruction_not_empty)
