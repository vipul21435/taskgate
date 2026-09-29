"""Hygiene gates: no credentials (TG201), no oversized files (TG202) and no
unreviewed binary files (TG203) anywhere in the task directory.

They look at every file :attr:`TaskContext.files` lists (tool caches and
symlinks are left out) and never need a runner. Options come from the
``[secrets]`` and ``[files]`` sections of ``taskgate.toml``.
"""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatchcase
from pathlib import PurePosixPath

from taskgate.gates.base import BINARY_SNIFF_BYTES, Check, TaskContext, gate, is_binary
from taskgate.results import Severity
from taskgate.secretscan import Finding, Scanner, describe_all

LISTED = 5
"""How many paths a failure message names before it says "and N more"."""


def matches_any(path: PurePosixPath, globs: Iterable[str]) -> bool:
    return any(fnmatchcase(path.as_posix(), pattern) for pattern in globs)


def human_size(size: int) -> str:
    """``512 B``, ``1.5 KiB``, ``12.0 MiB``: one decimal above a kibibyte."""
    if size < 1024:
        return f"{size} B"
    value, unit = size / 1024, "KiB"
    for bigger in ("MiB", "GiB"):
        if value < 1024:
            break
        value, unit = value / 1024, bigger
    return f"{value:.1f} {unit}"


def _listed(items: list[str]) -> str:
    more = f", and {len(items) - LISTED} more" if len(items) > LISTED else ""
    return ", ".join(items[:LISTED]) + more


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _head(ctx: TaskContext, path: PurePosixPath) -> bytes:
    with (ctx.task_dir / path).open("rb") as handle:
        return handle.read(BINARY_SNIFF_BYTES)


@gate(
    "TG201",
    "no-secrets",
    severity=Severity.ERROR,
    summary="no credentials: known token formats, private keys or high-entropy strings",
    fix_hint=(
        "Remove the credential and rotate it, since it has been pushed. For a false "
        "positive, add 'taskgate: allow-secret' to the line, or an allow regex or "
        "exclude glob under [secrets] in taskgate.toml."
    ),
)
def no_secrets(ctx: TaskContext) -> Check:
    options = ctx.config.secrets
    scanner = Scanner(
        min_length=options.min_length,
        entropy_threshold=options.entropy_threshold,
        allow=options.allow,
    )
    findings: list[Finding] = []
    scanned = 0
    for path in ctx.files:
        if matches_any(path, options.exclude):
            continue
        with (ctx.task_dir / path).open("rb") as handle:
            if is_binary(handle.read(BINARY_SNIFF_BYTES)):
                continue
            handle.seek(0)
            scanned += 1
            lines = (raw.rstrip(b"\r\n").decode("utf-8", "replace") for raw in handle)
            findings.extend(scanner.scan_lines(lines, path.as_posix()))
    if findings:
        return Check.fail(
            f"{_plural(len(findings), 'likely secret')}: {describe_all(findings, LISTED)}"
        )
    return Check.ok(f"no secrets in {_plural(scanned, 'text file')}")


@gate(
    "TG202",
    "file-size-limits",
    severity=Severity.ERROR,
    summary="every file and the task as a whole stay under the size limits (1 MiB, 10 MiB)",
    fix_hint=(
        "Generate large inputs from a small script or a seed instead of committing them; "
        "the limits are [files] max_file_bytes and max_task_bytes in taskgate.toml."
    ),
)
def file_size_limits(ctx: TaskContext) -> Check:
    options = ctx.config.files
    sizes = {path: (ctx.task_dir / path).stat().st_size for path in ctx.files}
    total = sum(sizes.values())
    big = sorted(
        (path for path, size in sizes.items() if size > options.max_file_bytes),
        key=lambda path: (-sizes[path], path),
    )
    problems: list[str] = []
    if big:
        listed = [f"{path.as_posix()} ({human_size(sizes[path])})" for path in big]
        problems.append(
            f"{_plural(len(big), 'file')} over the {human_size(options.max_file_bytes)} "
            f"file limit: {_listed(listed)}"
        )
    if total > options.max_task_bytes:
        problems.append(
            f"the task holds {human_size(total)}, over the "
            f"{human_size(options.max_task_bytes)} task limit"
        )
    if problems:
        return Check.fail("; ".join(problems))
    return Check.ok(f"{_plural(len(sizes), 'file')}, {human_size(total)} in total")


@gate(
    "TG203",
    "no-binary-files",
    severity=Severity.ERROR,
    summary="no binary files unless [files] binary_allow lists them",
    fix_hint=(
        "Store inputs as text or generate them in solve.sh or the environment build, so "
        "reviewers can read them; a maintainer can accept a reviewed binary with "
        "[files] binary_allow in taskgate.toml."
    ),
)
def no_binary_files(ctx: TaskContext) -> Check:
    allow = ctx.config.files.binary_allow
    binary = [path for path in ctx.files if is_binary(_head(ctx, path))]
    unreviewed = [path.as_posix() for path in binary if not matches_any(path, allow)]
    if unreviewed:
        return Check.fail(
            f"{_plural(len(unreviewed), 'binary file')} not in [files] binary_allow: "
            f"{_listed(unreviewed)}"
        )
    if binary:
        return Check.ok(f"{_plural(len(binary), 'binary file')}, all in [files] binary_allow")
    return Check.ok("no binary files")


HYGIENE_GATES = (no_secrets, file_size_limits, no_binary_files)
