"""``taskgate.toml``: turn gates off, change their severity, tune their options.

::

    [gates]
    disable = ["TG104"]          # these gates do not run at all

    [gates.severity]
    TG203 = "warning"            # error | warning | info

    [manifest]                   # TG104
    min_timeout_sec = 10
    max_timeout_sec = 1800

    [secrets]                    # TG201
    allow = ["^EXAMPLE"]         # regexes; a finding whose text matches is dropped
    exclude = ["tests/data/*"]   # task-relative path globs that are not scanned
    min_length = 24              # shortest string the entropy detector looks at
    entropy_threshold = 4.0      # bits per character

    [files]                      # TG202, TG203
    max_file_bytes = 1048576
    max_task_bytes = 10485760
    binary_allow = ["environment/workspace/*.png"]

    [runner]                     # the Docker runner (TG301, TG401-TG403)
    cpus = 1.0                   # docker run --cpus
    memory_mb = 1024             # docker run --memory (swap is not allowed on top)
    pids_limit = 256             # docker run --pids-limit
    build_timeout_sec = 900      # docker build wall-clock limit

Globs use :func:`fnmatch.fnmatchcase` on task-relative POSIX paths, so ``*``
also matches ``/``. Validation collects every problem before failing, and
unknown sections, keys and gate codes are errors rather than silently ignored.
In diff mode the file is read from the base ref, not from the pull request, so a
pull request cannot relax the gates it is checked by.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from taskgate.manifest import TIMEOUT_RANGE
from taskgate.results import ConfigSummary, OptionValue, Severity

CONFIG_FILE = "taskgate.toml"
CODE = re.compile(r"^TG[1-9][0-9]{2}$")
SEVERITIES = ", ".join(s.value for s in Severity)
MAX_BYTES = 1 << 40


class ConfigError(ValueError):
    """``taskgate.toml`` does not parse or does not match the schema."""


@dataclass(frozen=True, slots=True)
class ManifestOptions:
    """TG104: the recommended ``task.timeout_sec`` range (TG102 enforces 1..3600)."""

    min_timeout_sec: int = 10
    max_timeout_sec: int = 1800


@dataclass(frozen=True, slots=True)
class SecretOptions:
    """TG201: what the secret scan ignores and how random a string must look."""

    allow: tuple[str, ...] = ()
    """Regexes; a finding whose matched text matches one is dropped."""

    exclude: tuple[str, ...] = ()
    """Task-relative path globs that are not scanned."""

    min_length: int = 24
    entropy_threshold: float = 4.0


@dataclass(frozen=True, slots=True)
class FileOptions:
    """TG202 and TG203: size limits and the binary files a task may hold."""

    max_file_bytes: int = 1 << 20
    max_task_bytes: int = 10 << 20
    binary_allow: tuple[str, ...] = ()
    """Task-relative path globs of binary files that TG203 accepts."""


@dataclass(frozen=True, slots=True)
class RunnerOptions:
    """Limits for the Docker runner; the run time limit is the task's ``timeout_sec``."""

    cpus: float = 1.0
    memory_mb: int = 1024
    pids_limit: int = 256
    build_timeout_sec: int = 900


OptionSection = ManifestOptions | SecretOptions | FileOptions | RunnerOptions
"""The dataclasses behind the option sections (``[manifest]``, ``[secrets]``, ...)."""


def _overrides(name: str, options: OptionSection) -> list[tuple[str, OptionValue]]:
    """``(section.key, value)`` for every option that differs from its default."""
    default = type(options)()
    return [
        (f"{name}.{f.name}", getattr(options, f.name))
        for f in fields(options)
        if getattr(options, f.name) != getattr(default, f.name)
    ]


@dataclass(frozen=True, slots=True)
class Config:
    """The effective configuration; the defaults apply when no file exists."""

    source: str | None = None
    """Where the configuration came from, for reports (``None`` for the defaults)."""

    disabled: frozenset[str] = frozenset()
    severity: Mapping[str, Severity] = field(default_factory=dict)
    manifest: ManifestOptions = ManifestOptions()
    secrets: SecretOptions = SecretOptions()
    files: FileOptions = FileOptions()
    runner: RunnerOptions = RunnerOptions()

    def severity_for(self, code: str, default: Severity) -> Severity:
        return self.severity.get(code, default)

    def validate_codes(self, known: Collection[str]) -> None:
        """Raise :class:`ConfigError` when the file names a gate that is not registered."""
        unknown = sorted((set(self.disabled) | set(self.severity)) - set(known))
        if unknown:
            raise ConfigError(
                f"invalid {self.source}:\n  unknown gate code(s): {', '.join(unknown)} "
                "(run taskgate gates for the list)"
            )

    def summary(self) -> ConfigSummary:
        return ConfigSummary(
            source=self.source,
            disabled=tuple(sorted(self.disabled)),
            severity=tuple(sorted(self.severity.items())),
            options=(
                *_overrides("manifest", self.manifest),
                *_overrides("secrets", self.secrets),
                *_overrides("files", self.files),
                *_overrides("runner", self.runner),
            ),
        )


class _Problems(list[str]):
    def table(self, raw: Mapping[str, Any], key: str, where: str) -> dict[str, Any]:
        value = raw.get(key, {})
        if not isinstance(value, dict):
            self.append(f"{where} must be a table")
            return {}
        return value

    def unknown(self, table: Mapping[str, Any], allowed: Collection[str], where: str) -> None:
        for key in sorted(set(table) - set(allowed)):
            self.append(f"unknown key {where}{key}" if where else f"unknown section [{key}]")

    def section(self, raw: Mapping[str, Any], name: str, keys: Collection[str]) -> _Section:
        """The ``[name]`` table, with its unknown keys reported."""
        table = self.table(raw, name, f"[{name}]")
        self.unknown(table, keys, f"{name}.")
        return _Section(name, table, self)


@dataclass(frozen=True, slots=True)
class _Section:
    """Typed reads from one table; a bad value is reported and replaced by its default."""

    name: str
    table: dict[str, Any]
    problems: _Problems

    def integer(self, key: str, default: int, low: int, high: int) -> int:
        value = self.table.get(key, default)
        if not isinstance(value, int) or isinstance(value, bool):
            self.problems.append(f"{self.name}.{key} must be an integer")
        elif not low <= value <= high:
            self.problems.append(f"{self.name}.{key} {value} is outside {low}..{high}")
        else:
            return value
        return default

    def number(self, key: str, default: float, low: float, high: float) -> float:
        value = self.table.get(key, default)
        if not isinstance(value, int | float) or isinstance(value, bool):
            self.problems.append(f"{self.name}.{key} must be a number")
        elif not low <= value <= high:
            self.problems.append(f"{self.name}.{key} {value} is outside {low}..{high}")
        else:
            return float(value)
        return default

    def strings(self, key: str) -> tuple[str, ...]:
        value = self.table.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            self.problems.append(f"{self.name}.{key} must be a list of strings")
            return ()
        return tuple(value)


def _codes(value: object, where: str, problems: _Problems) -> frozenset[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        problems.append(f"{where} must be a list of gate codes")
        return frozenset()
    for code in value:
        if not CODE.match(code):
            problems.append(f"{where}: {code!r} is not a gate code like TG101")
    return frozenset(value)


def _severities(table: Mapping[str, Any], problems: _Problems) -> dict[str, Severity]:
    result: dict[str, Severity] = {}
    for code, value in table.items():
        if not CODE.match(code):
            problems.append(f"gates.severity: {code!r} is not a gate code like TG101")
        elif not isinstance(value, str) or value not in {s.value for s in Severity}:
            problems.append(f"gates.severity.{code} must be one of {SEVERITIES}, not {value!r}")
        else:
            result[code] = Severity(value)
    return result


def _manifest(raw: Mapping[str, Any], problems: _Problems) -> ManifestOptions:
    section = problems.section(raw, "manifest", ("min_timeout_sec", "max_timeout_sec"))
    default = ManifestOptions()
    before = len(problems)
    low = section.integer("min_timeout_sec", default.min_timeout_sec, *TIMEOUT_RANGE)
    high = section.integer("max_timeout_sec", default.max_timeout_sec, *TIMEOUT_RANGE)
    if len(problems) == before and low > high:
        problems.append(f"manifest.min_timeout_sec {low} is above max_timeout_sec {high}")
    return ManifestOptions(min_timeout_sec=low, max_timeout_sec=high)


def _regexes(sources: tuple[str, ...], where: str, problems: _Problems) -> tuple[str, ...]:
    for source in sources:
        try:
            re.compile(source)
        except re.error as exc:
            problems.append(f"{where}: {source!r} is not a valid regex ({exc})")
    return sources


def _secrets(raw: Mapping[str, Any], problems: _Problems) -> SecretOptions:
    keys = ("allow", "exclude", "min_length", "entropy_threshold")
    section = problems.section(raw, "secrets", keys)
    default = SecretOptions()
    return SecretOptions(
        allow=_regexes(section.strings("allow"), "secrets.allow", problems),
        exclude=section.strings("exclude"),
        min_length=section.integer("min_length", default.min_length, 8, 1024),
        entropy_threshold=section.number("entropy_threshold", default.entropy_threshold, 1.0, 8.0),
    )


def _files(raw: Mapping[str, Any], problems: _Problems) -> FileOptions:
    keys = ("max_file_bytes", "max_task_bytes", "binary_allow")
    section = problems.section(raw, "files", keys)
    default = FileOptions()
    return FileOptions(
        max_file_bytes=section.integer("max_file_bytes", default.max_file_bytes, 1, MAX_BYTES),
        max_task_bytes=section.integer("max_task_bytes", default.max_task_bytes, 1, MAX_BYTES),
        binary_allow=section.strings("binary_allow"),
    )


def _runner(raw: Mapping[str, Any], problems: _Problems) -> RunnerOptions:
    keys = ("cpus", "memory_mb", "pids_limit", "build_timeout_sec")
    section = problems.section(raw, "runner", keys)
    default = RunnerOptions()
    return RunnerOptions(
        cpus=section.number("cpus", default.cpus, 0.1, 64.0),
        memory_mb=section.integer("memory_mb", default.memory_mb, 64, 65536),
        pids_limit=section.integer("pids_limit", default.pids_limit, 16, 65536),
        build_timeout_sec=section.integer("build_timeout_sec", default.build_timeout_sec, 10, 7200),
    )


def from_dict(raw: Mapping[str, Any], source: str) -> Config:
    """Validate a parsed ``taskgate.toml``; raise :class:`ConfigError` listing every problem."""
    problems = _Problems()
    problems.unknown(raw, ("gates", "manifest", "secrets", "files", "runner"), "")
    gates = problems.table(raw, "gates", "[gates]")
    problems.unknown(gates, ("disable", "severity"), "gates.")
    disabled = _codes(gates.get("disable", []), "gates.disable", problems)
    severity = _severities(problems.table(gates, "severity", "gates.severity"), problems)
    manifest = _manifest(raw, problems)
    secrets = _secrets(raw, problems)
    files = _files(raw, problems)
    runner = _runner(raw, problems)
    if problems:
        raise ConfigError(f"invalid {source}:\n  " + "\n  ".join(problems))
    return Config(
        source=source,
        disabled=disabled,
        severity=severity,
        manifest=manifest,
        secrets=secrets,
        files=files,
        runner=runner,
    )


def parse(text: str, source: str) -> Config:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid {source}:\n  does not parse: {exc}") from exc
    return from_dict(raw, source)


def load_file(path: Path, source: str | None = None) -> Config:
    """Read ``path``; ``source`` (default: the path as given) is what reports show."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    return parse(text, source or str(path))


def discover(root: Path) -> Config:
    """``root/taskgate.toml`` when it exists, else the defaults."""
    path = root / CONFIG_FILE
    return load_file(path, CONFIG_FILE) if path.is_file() else Config()
