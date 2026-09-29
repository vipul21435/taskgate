"""The ``task.toml`` manifest: parsing and schema validation.

The schema is documented in ``docs/task-layout.md``. Validation collects every
problem instead of stopping at the first one, so a single run tells the task
author everything that needs fixing.
"""

from __future__ import annotations

import difflib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DIFFICULTIES: tuple[str, ...] = ("easy", "medium", "hard")
TIMEOUT_RANGE: tuple[int, int] = (1, 3600)
DEFAULT_TIMEOUT_SEC = 120
DEFAULT_DOCKERFILE = "Dockerfile"
DEFAULT_WORKDIR = "/workspace"
TASK_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SCHEMA: dict[str, tuple[str, ...]] = {
    "task": ("id", "title", "difficulty", "timeout_sec"),
    "environment": ("dockerfile", "workdir"),
}
"""Every table and key a manifest may hold; anything else is flagged by TG103."""


@dataclass(frozen=True, slots=True)
class Manifest:
    """The validated fields of a ``task.toml``."""

    id: str
    title: str
    difficulty: str
    timeout_sec: int
    dockerfile: str
    workdir: str


@dataclass(frozen=True, slots=True)
class ManifestCheck:
    """Validation outcome: the manifest when valid, the problems otherwise."""

    manifest: Manifest | None
    problems: tuple[str, ...]
    raw: dict[str, Any]
    """The parsed TOML document (empty when it did not parse)."""

    parsed: bool = True
    """False when ``task.toml`` is missing or is not valid TOML."""

    @property
    def declared_timeout_sec(self) -> int | None:
        """``task.timeout_sec`` when it is an integer in the allowed range, else ``None``."""
        timeout = _as_int(_table(self.raw, "task").get("timeout_sec"))
        if timeout is not None and TIMEOUT_RANGE[0] <= timeout <= TIMEOUT_RANGE[1]:
            return timeout
        return None

    @property
    def timeout_sec(self) -> int:
        """The task's time budget, or the default when the manifest does not give a valid one."""
        declared = self.declared_timeout_sec
        return DEFAULT_TIMEOUT_SEC if declared is None else declared

    @property
    def dockerfile(self) -> str:
        """``environment.dockerfile`` when it is a non-empty string, else ``Dockerfile``."""
        value = _table(self.raw, "environment").get("dockerfile")
        return value if isinstance(value, str) and value.strip() else DEFAULT_DOCKERFILE

    @property
    def workdir(self) -> str:
        """``environment.workdir`` when it is an absolute path, else ``/workspace``."""
        value = _table(self.raw, "environment").get("workdir")
        return value if isinstance(value, str) and value.startswith("/") else DEFAULT_WORKDIR


def _table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


def _as_int(value: object) -> int | None:
    """``value`` when it is a TOML integer (booleans excluded), else ``None``."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _non_empty_str(table: dict[str, Any], key: str, where: str, problems: list[str]) -> str:
    value = table.get(key)
    if value is None:
        problems.append(f"missing {where}.{key}")
        return ""
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{where}.{key} must be a non-empty string")
        return ""
    return value


def validate(raw: dict[str, Any], task_dir_name: str) -> ManifestCheck:
    """Validate a parsed manifest for the task directory named ``task_dir_name``."""
    problems: list[str] = []
    for key in ("task", "environment"):
        if key not in raw:
            problems.append(f"missing [{key}] table")
        elif not isinstance(raw[key], dict):
            problems.append(f"{key} must be a table")
    task = _table(raw, "task")
    env = _table(raw, "environment")

    task_id = _non_empty_str(task, "id", "task", problems)
    if task_id and not TASK_ID.match(task_id):
        problems.append(f"task.id {task_id!r} is not kebab-case")
    elif task_id and task_id != task_dir_name:
        problems.append(f"task.id {task_id!r} does not match the directory name {task_dir_name!r}")
    title = _non_empty_str(task, "title", "task", problems)
    difficulty = _non_empty_str(task, "difficulty", "task", problems)
    if difficulty and difficulty not in DIFFICULTIES:
        problems.append(f"task.difficulty {difficulty!r} is not one of {', '.join(DIFFICULTIES)}")

    timeout = _as_int(task.get("timeout_sec"))
    low, high = TIMEOUT_RANGE
    if "timeout_sec" not in task:
        problems.append("missing task.timeout_sec")
    elif timeout is None:
        problems.append("task.timeout_sec must be an integer")
    elif not low <= timeout <= high:
        problems.append(f"task.timeout_sec {timeout} is outside {low}..{high}")

    dockerfile = _non_empty_str(env, "dockerfile", "environment", problems)
    workdir = _non_empty_str(env, "workdir", "environment", problems)
    if workdir and not workdir.startswith("/"):
        problems.append(f"environment.workdir {workdir!r} must be an absolute path")

    manifest = None
    if not problems and timeout is not None:
        manifest = Manifest(
            id=task_id,
            title=title,
            difficulty=difficulty,
            timeout_sec=timeout,
            dockerfile=dockerfile,
            workdir=workdir,
        )
    return ManifestCheck(manifest=manifest, problems=tuple(problems), raw=raw)


def _hint(name: str, candidates: dict[str, str]) -> str:
    """`` (did you mean X?)`` for the closest candidate, or nothing when none is close."""
    match = difflib.get_close_matches(name, list(candidates), n=1, cutoff=0.6)
    return f" (did you mean {candidates[match[0]]}?)" if match else ""


def _key_candidates(table: str | None) -> dict[str, str]:
    """Every known key, shown qualified; keys of ``table`` win over same-named ones elsewhere."""
    candidates: dict[str, str] = {}
    for owner in sorted(SCHEMA, key=lambda name: name != table):
        for key in SCHEMA[owner]:
            candidates.setdefault(key, f"{owner}.{key}")
    return candidates


def unknown_keys(raw: dict[str, Any]) -> list[str]:
    """Tables and keys outside :data:`SCHEMA`, in file order, each with a did-you-mean hint.

    Hints are qualified names: a misspelt table suggests ``[task]``, a misspelt
    key suggests ``task.timeout_sec``, and a known key in the wrong place
    (``timeout_sec`` above ``[task]``, or inside ``[environment]``) suggests
    where it belongs.
    """
    tables = {name: f"[{name}]" for name in SCHEMA}
    found: list[str] = []
    for name, value in raw.items():
        if name in SCHEMA and isinstance(value, dict):
            candidates = _key_candidates(name)
            found.extend(
                f"{name}.{key}{_hint(key, candidates)}" for key in value if key not in SCHEMA[name]
            )
        elif name not in SCHEMA:
            if isinstance(value, dict):
                found.append(f"[{name}]{_hint(name, tables)}")
            else:
                found.append(f"{name}{_hint(name, _key_candidates(None))}")
    return found


def load(task_dir: Path) -> ManifestCheck:
    """Read and validate ``task_dir/task.toml``."""
    path = task_dir / "task.toml"
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ManifestCheck(manifest=None, problems=("task.toml not found",), raw={}, parsed=False)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return ManifestCheck(
            manifest=None, problems=(f"task.toml does not parse: {exc}",), raw={}, parsed=False
        )
    return validate(raw, task_dir.name)
