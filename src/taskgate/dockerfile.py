"""Read a task's Dockerfile: locate it, parse it, and check it without Docker.

The parser follows the Dockerfile rules that matter for review: a leading
UTF-8 byte order mark is dropped, lines end only at ``\n`` (a ``\r`` before it
is dropped too), parser directives (``# escape=``), comment lines, line
continuations (also across comment and blank lines), heredocs (``RUN <<EOF``
... ``EOF``, so a heredoc body is never taken for an instruction), and
case-insensitive keywords.

:func:`external_images` lists the images a build pulls (``FROM``,
``COPY --from=``/``ADD --from=`` and the ``from=`` of a ``RUN --mount``), with
global ``ARG`` defaults substituted (a default may use earlier ones), leaving
out ``scratch`` and earlier build stages; gate TG302 requires each to be pinned
by digest. :func:`static_problems` is the part of gate TG301 that needs no Docker.
"""

from __future__ import annotations

import csv
import json
import posixpath
import re
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

KNOWN_INSTRUCTIONS: frozenset[str] = frozenset(
    {
        "ADD",
        "ARG",
        "CMD",
        "COPY",
        "ENTRYPOINT",
        "ENV",
        "EXPOSE",
        "FROM",
        "HEALTHCHECK",
        "LABEL",
        "MAINTAINER",
        "ONBUILD",
        "RUN",
        "SHELL",
        "STOPSIGNAL",
        "USER",
        "VOLUME",
        "WORKDIR",
    }
)
HEREDOC_INSTRUCTIONS = frozenset({"RUN", "COPY", "ADD"})
DIRECTIVE = re.compile(r"^#\s*([A-Za-z][A-Za-z0-9]*)\s*=\s*(\S+)\s*$")
HEREDOC = re.compile(r"<<(-?)(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
VARIABLE = re.compile(
    r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)(?::([-+])([^}]*))?\}|([A-Za-z_][A-Za-z0-9_]*))"
)
FLAG = re.compile(r"--(\S*)\s*")
KEYWORD_SPLIT = re.compile(r"[\t\v\f\r ]+")
"""What separates an instruction's keyword from its arguments (as in Docker's parser)."""
GLOB_CHARS = frozenset("*?[")
ROOT_USERS = frozenset({"root", "0"})
LISTED = 5


@dataclass(frozen=True, slots=True)
class Instruction:
    line: int
    """1-based line on which the instruction starts."""

    keyword: str
    """Upper-cased, e.g. ``FROM``."""

    args: str
    """Everything after the keyword, continuation lines joined, heredoc bodies left out."""


@dataclass(frozen=True, slots=True)
class ImageRef:
    """An image the build pulls."""

    line: int
    instruction: str
    """``FROM``, ``COPY --from``, ``ADD --from`` or ``RUN --mount from``."""

    written: str
    resolved: str | None
    """``written`` with ``ARG`` defaults substituted; ``None`` if a variable has no value."""

    @property
    def pinned(self) -> bool:
        return self.resolved is not None and DIGEST.search(self.resolved) is not None

    def describe(self) -> str:
        text = f"line {self.line}: {self.instruction} {self.written}"
        if self.resolved is None:
            return f"{text} (a variable in it has no default)"
        if self.resolved != self.written:
            return f"{text} (= {self.resolved})"
        return text


def locate(env_dir: Path, name: str) -> Path | str:
    """The Dockerfile that ``environment.dockerfile`` names, or why it cannot be used.

    ``name`` is relative to ``env_dir`` and must stay inside it, since Docker only
    sees the build context.
    """
    path = env_dir / name
    try:
        inside = path.resolve().is_relative_to(env_dir.resolve())
    except (OSError, RuntimeError):  # RuntimeError on Python 3.12, OSError later
        return f"environment/{name} is a symlink loop"
    if PurePosixPath(name).is_absolute() or not inside:
        return f"environment.dockerfile {name!r} points outside environment/"
    if not path.is_file():
        return f"environment/{name} not found"
    return path


def read(env_dir: Path, name: str) -> tuple[Instruction, ...] | str:
    """Locate and parse the Dockerfile, or say why it cannot be read."""
    located = locate(env_dir, name)
    if isinstance(located, str):
        return located
    try:
        return parse(located.read_bytes().decode("utf-8"))
    except UnicodeDecodeError:
        return f"environment/{name} is not UTF-8"


def _is_comment(line: str) -> bool:
    return line.lstrip().startswith("#")


def split_lines(text: str) -> list[str]:
    """``text`` split into lines the way Docker reads a Dockerfile.

    A leading byte order mark is dropped and lines end at ``\n`` only, with one
    ``\r`` before it removed; form feeds, ``\x85``, U+2028 and the other
    characters :meth:`str.splitlines` also breaks at stay inside their line.
    """
    return [line.removesuffix("\r") for line in text.removeprefix("\ufeff").split("\n")]


def parse(text: str) -> tuple[Instruction, ...]:
    """Split a Dockerfile into instructions (see the module docstring for the rules).

    A logical line that holds nothing but line continuations becomes an
    instruction with an empty keyword (Docker rejects it; TG301 reports it).
    """
    lines = split_lines(text)
    escape = "\\"
    index = 0
    while index < len(lines) and (directive := DIRECTIVE.match(lines[index])):
        if directive.group(1).lower() == "escape" and directive.group(2) in ("\\", "`"):
            escape = directive.group(2)
        index += 1
    found: list[Instruction] = []
    while index < len(lines):
        start = index + 1
        current = lines[index].rstrip()
        index += 1
        if not current.strip() or _is_comment(current):
            continue
        parts: list[str] = []
        while current.endswith(escape):
            parts.append(current[: -len(escape)])
            while index < len(lines) and (not lines[index].strip() or _is_comment(lines[index])):
                index += 1
            current = lines[index].rstrip() if index < len(lines) else ""
            index += 1
        parts.append(current)
        keyword, *rest = KEYWORD_SPLIT.split("".join(parts).strip(), maxsplit=1)
        keyword = keyword.upper()
        args = rest[0] if rest else ""
        if keyword in HEREDOC_INSTRUCTIONS:
            for strip_tabs, _, word in HEREDOC.findall(args):
                while index < len(lines):
                    body = lines[index].lstrip("\t") if strip_tabs else lines[index]
                    index += 1
                    if body == word:
                        break
        found.append(Instruction(start, keyword, args.strip()))
    return tuple(found)


def _substitute(text: str, values: dict[str, str | None]) -> str | None:
    """``text`` with ``$VAR``, ``${VAR}``, ``${VAR:-word}`` and ``${VAR:+word}`` expanded."""
    missing = False

    def expand(match: re.Match[str]) -> str:
        nonlocal missing
        name = match.group(1) or match.group(4)
        operator, word = match.group(2), match.group(3)
        value = values.get(name)
        if operator == "-":
            return value or word
        if operator == "+":
            return word if value else ""
        if value is None:
            missing = True
            return ""
        return value

    result = VARIABLE.sub(expand, text)
    return None if missing else result


def _words(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _declare_args(args: str, values: dict[str, str | None]) -> None:
    """Add ``ARG A=1 B=${A}x C`` to ``values`` in order: ``A=1``, ``B=1x``, ``C`` unset.

    A default may use the ``ARG``s declared before it, as in Docker; a default
    that uses a variable with no value is itself treated as having no value.
    """
    for word in _words(args):
        name, equals, value = word.partition("=")
        values[name] = _substitute(value, values) if equals else None


def _flag_items(args: str) -> tuple[list[tuple[str, str]], str]:
    """Leading ``--name=value`` flags in order (names lower-cased) and the rest."""
    items: list[tuple[str, str]] = []
    rest = args
    while match := FLAG.match(rest):
        name, _, value = match.group(1).partition("=")
        items.append((name.lower(), value))
        rest = rest[match.end() :]
    return items, rest


def _flags(args: str) -> tuple[dict[str, str], str]:
    """Leading ``--name=value`` flags (the last one of a name wins) and the rest."""
    items, rest = _flag_items(args)
    return dict(items), rest


def _mount_sources(args: str) -> list[str]:
    """The ``from=`` value of each ``RUN --mount`` (a CSV list of ``key=value`` fields)."""
    items, _ = _flag_items(args)
    sources: list[str] = []
    for name, value in items:
        if name != "mount":
            continue
        for field in next(csv.reader([value]), []):
            key, equals, source = field.strip().partition("=")
            if equals and key.strip().lower() == "from" and source:
                sources.append(source)
    return sources


def external_images(instructions: tuple[Instruction, ...]) -> list[ImageRef]:
    """Images pulled by ``FROM``, ``COPY/ADD --from`` and ``RUN --mount=from=``, minus
    ``scratch`` and build stages."""
    global_args: dict[str, str | None] = {}
    stages: list[str] = []
    refs: list[ImageRef] = []
    seen_from = False

    def external(line: int, instruction: str, written: str) -> None:
        resolved = _substitute(written, global_args)
        name = (resolved or written).lower()
        if name == "scratch" or name in stages or (instruction != "FROM" and name.isdigit()):
            return
        refs.append(ImageRef(line, instruction, written, resolved))

    for ins in instructions:
        if ins.keyword == "ARG" and not seen_from:
            _declare_args(ins.args, global_args)
        elif ins.keyword == "FROM":
            seen_from = True
            _, rest = _flags(ins.args)
            words = rest.split()
            if not words:
                continue
            external(ins.line, "FROM", words[0])
            if len(words) >= 3 and words[1].lower() == "as":
                stages.append(words[2].lower())
        elif ins.keyword in ("COPY", "ADD"):
            flags, _ = _flags(ins.args)
            if flags.get("from"):
                external(ins.line, f"{ins.keyword} --from", flags["from"])
        elif ins.keyword == "RUN":
            for source in _mount_sources(ins.args):
                external(ins.line, "RUN --mount from", source)
    return refs


def _copy_sources(args: str) -> list[str] | None:
    """The sources of a ``COPY``/``ADD`` without ``--from`` (``None`` for ``--from``)."""
    flags, rest = _flags(args)
    if "from" in flags:
        return None
    items: list[str]
    if rest.startswith("["):
        try:
            loaded = json.loads(rest)
        except json.JSONDecodeError:
            loaded = None
        items = [str(item) for item in loaded] if isinstance(loaded, list) else _words(rest)
    else:
        items = _words(rest)
    return items[:-1]


def _skip_source(source: str) -> bool:
    """Heredocs, variables and remote URLs cannot be checked against the context."""
    return source.startswith("<<") or "$" in source or "://" in source or source.startswith("git@")


def _source_problem(env_dir: Path, ins: Instruction, source: str) -> str | None:
    normalized = posixpath.normpath(source.lstrip("/"))
    where = f"line {ins.line}: {ins.keyword} source {source!r}"
    if normalized == ".." or normalized.startswith("../"):
        return f"{where} is outside the build context"
    if GLOB_CHARS & set(normalized):
        if not any(env_dir.glob(normalized)):
            return f"{where} matches nothing in environment/"
    elif not (env_dir / normalized).exists():
        return f"{where} not found in environment/"
    return None


def _copies_workspace(source: str) -> bool:
    normalized = posixpath.normpath(source.lstrip("/"))
    return normalized in (".", "workspace") or normalized.startswith("workspace/")


def _user_problem(final_stage: list[Instruction]) -> str | None:
    users = [ins for ins in final_stage if ins.keyword == "USER"]
    if not users:
        return "the final stage has no USER instruction, so the image runs as root"
    words = users[-1].args.split()
    if not words:
        return f"line {users[-1].line}: USER has no value"
    user = words[0]
    if user.split(":", 1)[0] in ROOT_USERS:
        return f"line {users[-1].line}: the final stage runs as root (USER {user})"
    return None


def listed(problems: list[str]) -> str:
    """Problems joined with ``; ``, the first five of them and a count of the rest."""
    more = f"; and {len(problems) - LISTED} more" if len(problems) > LISTED else ""
    return "; ".join(problems[:LISTED]) + more


def static_problems(env_dir: Path, name: str) -> list[str]:
    """What is wrong with the Dockerfile, found without Docker (empty when nothing is).

    The Dockerfile must exist inside ``environment/`` and parse into known
    instructions with ``FROM`` first (only ``ARG`` may precede it); every local
    ``COPY``/``ADD`` source must exist in the build context; the final stage must
    set a ``USER`` that is not root; and when ``environment/workspace/`` exists,
    some ``COPY``/``ADD`` must copy it into the image.
    """
    instructions = read(env_dir, name)
    if isinstance(instructions, str):
        return [instructions]
    problems = [
        f"line {ins.line}: unknown instruction {ins.keyword}"
        if ins.keyword
        else f"line {ins.line}: a line continuation with no instruction after it"
        for ins in instructions
        if ins.keyword not in KNOWN_INSTRUCTIONS
    ]
    if not any(ins.keyword == "FROM" for ins in instructions):
        return [*problems, f"environment/{name} has no FROM instruction"]
    first = next(ins for ins in instructions if ins.keyword not in ("ARG", ""))
    if first.keyword != "FROM":
        problems.append(f"line {first.line}: {first.keyword} comes before the first FROM")
    copies_workspace = False
    final_stage: list[Instruction] = []
    for ins in instructions:
        if ins.keyword == "FROM":
            final_stage = []
        final_stage.append(ins)
        sources = _copy_sources(ins.args) if ins.keyword in ("COPY", "ADD") else None
        for source in sources or []:
            copies_workspace = copies_workspace or _copies_workspace(source)
            if not _skip_source(source):
                problem = _source_problem(env_dir, ins, source)
                if problem:
                    problems.append(problem)
    user = _user_problem(final_stage)
    if user:
        problems.append(user)
    if (env_dir / "workspace").is_dir() and not copies_workspace:
        problems.append("environment/workspace/ is never copied into the image")
    return problems
