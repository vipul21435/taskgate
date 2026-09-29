"""Read a task's Dockerfile: locate it, parse it, and check it without Docker.

The parser follows the Dockerfile rules that matter for review: parser
directives (``# escape=``), comment lines, line continuations (also across
comment and blank lines), heredocs (``RUN <<EOF`` ... ``EOF``, so a heredoc
body is never taken for an instruction), and case-insensitive keywords.

:func:`external_images` lists the images a build pulls (``FROM`` and
``COPY --from=``), with global ``ARG`` defaults substituted, leaving out
``scratch`` and earlier build stages; gate TG302 requires each to be pinned by
digest. :func:`static_problems` is the part of gate TG301 that needs no Docker.
"""

from __future__ import annotations

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
    """``FROM`` or ``COPY --from`` (or ``ADD --from``)."""

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
        return parse(located.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        return f"environment/{name} is not UTF-8"


def _is_comment(line: str) -> bool:
    return line.lstrip().startswith("#")


def parse(text: str) -> tuple[Instruction, ...]:
    """Split a Dockerfile into instructions (see the module docstring for the rules)."""
    lines = text.splitlines()
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
        keyword, *rest = "".join(parts).split(None, 1)
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


def _arg_defaults(args: str) -> dict[str, str | None]:
    """``ARG A=1 B`` -> ``{"A": "1", "B": None}``."""
    values: dict[str, str | None] = {}
    for word in _words(args):
        name, equals, value = word.partition("=")
        values[name] = value if equals else None
    return values


def _flags(args: str) -> tuple[dict[str, str], str]:
    """Leading ``--name=value`` flags and the rest of the arguments."""
    flags: dict[str, str] = {}
    rest = args
    while rest.startswith("--"):
        flag, _, rest = rest.partition(" ")
        name, _, value = flag[2:].partition("=")
        flags[name.lower()] = value
        rest = rest.lstrip()
    return flags, rest


def external_images(instructions: tuple[Instruction, ...]) -> list[ImageRef]:
    """Images pulled by ``FROM`` and ``COPY/ADD --from``, minus ``scratch`` and build stages."""
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
            global_args.update(_arg_defaults(ins.args))
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
        for ins in instructions
        if ins.keyword not in KNOWN_INSTRUCTIONS
    ]
    if not any(ins.keyword == "FROM" for ins in instructions):
        return [*problems, f"environment/{name} has no FROM instruction"]
    first = next(ins for ins in instructions if ins.keyword != "ARG")
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
