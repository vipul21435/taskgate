"""``taskgate.toml``: turn gates off, change their severity, tune their options.

::

    [gates]
    disable = ["TG104"]          # these gates do not run at all

    [gates.severity]
    TG203 = "warning"            # error | warning | info

Validation collects every problem before failing, and unknown sections, keys and
gate codes are errors rather than silently ignored. In diff mode the file is read
from the base ref, not from the pull request, so a pull request cannot relax the
gates it is checked by.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from taskgate.results import ConfigSummary, Severity

CONFIG_FILE = "taskgate.toml"
CODE = re.compile(r"^TG[1-9][0-9]{2}$")
SEVERITIES = ", ".join(s.value for s in Severity)


class ConfigError(ValueError):
    """``taskgate.toml`` does not parse or does not match the schema."""


@dataclass(frozen=True, slots=True)
class Config:
    """The effective configuration; the defaults apply when no file exists."""

    source: str | None = None
    """Where the configuration came from, for reports (``None`` for the defaults)."""

    disabled: frozenset[str] = frozenset()
    severity: Mapping[str, Severity] = field(default_factory=dict)

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


def from_dict(raw: Mapping[str, Any], source: str) -> Config:
    """Validate a parsed ``taskgate.toml``; raise :class:`ConfigError` listing every problem."""
    problems = _Problems()
    problems.unknown(raw, ("gates",), "")
    gates = problems.table(raw, "gates", "[gates]")
    problems.unknown(gates, ("disable", "severity"), "gates.")
    disabled = _codes(gates.get("disable", []), "gates.disable", problems)
    severity = _severities(problems.table(gates, "severity", "gates.severity"), problems)
    if problems:
        raise ConfigError(f"invalid {source}:\n  " + "\n  ".join(problems))
    return Config(source=source, disabled=disabled, severity=severity)


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
