"""Content-hash result cache: skip the gates on a task nothing has changed.

A task's gate results are stored under a key that covers everything they
depend on:

- the task's content (:func:`task_digest`): every directory, regular file and
  symlink under the task directory, by sorted relative POSIX path, with each
  file's bytes and permission bits (the executable bit among them) and each
  symlink's target. Modification times, owners and the order the directory
  walk returns entries in never enter it, and the tool caches that
  :mod:`taskgate.files` ignores (``__pycache__/``, ``*.pyc``, ``.DS_Store``,
  ``.taskgate/``, ...) are left out. In diff mode the paths git tracks in the
  task are hashed too, with the content of every tracked file whatever its
  name, since that list is what the hygiene gates read.
- the TaskGate build: its version plus a digest of its own source files, so an
  edited checkout never reuses results computed by older code;
- the gates: each gate's code, name, severity, summary, fix hint and
  ``requires``, where its check lives (with a digest of that module's source),
  the gate object's own attributes (a parameter set where a plugin builds its
  gate), and for a plugin gate its entry point, the distribution's version, the
  entry-point module's source, the module-level constants of those modules and
  the source of every module, function and class they import at top level. An
  edited or upgraded plugin so invalidates its results; a helper that a
  plugin's *helper* imports is not covered (README, Known issues);
- the effective ``taskgate.toml``: disabled gates, severities and every option;
- the runner (its name, and the Python, pytest and platform TaskGate runs on),
  and the task's label and directory name, which reports and TG501's
  reproduce command show.

Only results without a blocking failure are stored. A failing task is checked
again on every run, so a transient failure (a Docker hiccup, a timeout under
load) is never replayed from the cache. A task holding a symlink that leads
outside it is never cached, since what it points at is not in the hash.

An entry is replayed only when its results carry exactly the enabled gates'
codes in order; the entry is not authenticated beyond that, so the cache must
not be under anyone else's control: :func:`committed_paths` tells the CLI when
the cache directory is tracked by the repository it checks (a pull request
could commit entries), and it then runs uncached.

Entries are JSON files named ``<key>.json`` (64 hex digits) under
``<cache>/entries/``, written atomically (a temp file in the same directory,
``fsync``, ``os.replace``) while holding an exclusive ``flock`` on
``<cache>/lock``; readers need no lock, since a file is either the old or the
new one. A hit refreshes the entry's mtime, which ``taskgate cache prune``
reads as "last used". The directory is marked with a ``CACHEDIR.TAG`` when the
first entry is stored; ``stats`` and ``prune`` list and remove only key-named
files in a real (not symlinked) ``entries`` directory of a tagged cache, so a
wrong ``--cache-dir`` never deletes anything else. Cache problems never fail a
check: :class:`CachedChecks` reports them through its ``note`` callback and
carries on without the cache.
"""

from __future__ import annotations

import contextlib
import dataclasses
import enum
import fcntl
import functools
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import taskgate
from taskgate import __version__
from taskgate.config import Config
from taskgate.files import IGNORED_DIRS, is_ignored
from taskgate.gates import Gate
from taskgate.registry import PluginSource, Registered
from taskgate.results import GateResult, Severity, Status
from taskgate.runner import Runner

CACHE_SCHEMA = 2
"""Bumped whenever the entry format or the key's makeup changes."""

DEFAULT_CACHE_DIR = Path(".taskgate") / "cache"
"""Where the cache lives, relative to the repository root or the ``--all`` directory."""

ENTRIES_DIR = "entries"
LOCK_FILE = "lock"
STATS_FILE = "stats.json"
TMP_PREFIX = ".tmp-"
LOCK_TIMEOUT_SEC = 10.0
LOCK_POLL_SEC = 0.05
GITIGNORE = "# Created by taskgate: the result cache is local state, never committed.\n*\n"
CACHEDIR_TAG = (
    "Signature: 8a477f597d28d172789f06886806bc55\n"
    "# This file is a cache directory tag created by taskgate.\n"
    "# For information about cache directory tags see https://bford.info/cachedir/\n"
)
KEY = re.compile(r"^[0-9a-f]{64}$")
"""What an entry's file stem must look like for ``stats`` and ``prune`` to touch it."""

OWN_MODULES = frozenset({"taskgate", "pytest", "_pytest", "pluggy"})
"""Top-level modules the build id or the runner fingerprint already cover."""


class CacheError(RuntimeError):
    """The cache could not be read or written (the message says where and why)."""


class UncacheableError(ValueError):
    """The task's results must not be cached (the message says why)."""


def _file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _mode(st: os.stat_result) -> str:
    return format(stat.S_IMODE(st.st_mode), "04o")


def _entry(path: Path, relative: str, root: Path) -> list[str] | None:
    """One file-like path as hash material; ``None`` for fifos, sockets and devices."""
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode):
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError) as exc:  # RuntimeError on Python 3.12, OSError later
            raise UncacheableError(f"{relative} is a symlink loop") from exc
        if not resolved.is_relative_to(root):
            raise UncacheableError(f"the symlink {relative} leads outside the task")
        return ["link", relative, str(path.readlink())]
    if stat.S_ISREG(st.st_mode):
        return ["file", relative, _mode(st), _file_sha256(path)]
    return None


def task_digest(task_dir: Path, tracked: Sequence[str] | None = None) -> str:
    """SHA-256 over a task's content (see the module docstring for what counts).

    ``tracked`` is the task's file list as git tracks it in diff mode, relative to
    the task (``None`` for a plain directory). Raises :class:`UncacheableError`
    when a symlink leads outside the task or loops, and ``OSError`` when a path
    cannot be read.
    """
    root = task_dir.resolve()
    material: dict[tuple[str, str], list[str]] = {}
    for dirpath, dirnames, filenames in task_dir.walk():
        dirnames[:] = [name for name in dirnames if name not in IGNORED_DIRS]
        for name in dirnames:
            relative = (dirpath / name).relative_to(task_dir).as_posix()
            material[(relative, "dir")] = ["dir", relative, _mode((dirpath / name).lstat())]
        for name in filenames:
            if is_ignored((name,)):
                continue
            path = dirpath / name
            relative = path.relative_to(task_dir).as_posix()
            found = _entry(path, relative, root)
            if found is not None:
                material[(relative, "file")] = found
    if tracked is not None:
        for relative in tracked:
            material[(relative, "tracked")] = ["tracked", relative]
            path = task_dir / relative
            if (relative, "file") not in material and path.exists(follow_symlinks=False):
                found = _entry(path, relative, root)
                if found is not None:
                    material[(relative, "file")] = found
    digest = hashlib.sha256(b"taskgate-task-v1\n")
    digest.update(b"tracked\n" if tracked is not None else b"disk\n")
    for key in sorted(material):
        digest.update(json.dumps(material[key]).encode() + b"\n")
    return digest.hexdigest()


@functools.cache
def _source_digest(path: str) -> str | None:
    try:
        return _file_sha256(Path(path))
    except OSError:
        return None


@functools.cache
def build_id() -> str:
    """This TaskGate build: its version plus 12 hex of a digest of its own ``*.py`` files."""
    package = Path(taskgate.__file__).parent
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        digest.update(path.relative_to(package).as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return f"{__version__}+{digest.hexdigest()[:12]}"


def _plain(value: object, depth: int = 0) -> object:
    """``value`` as stable JSON material: data as itself, code by name, the rest by type."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, enum.Enum):
        return _plain(value.value, depth)
    if isinstance(value, re.Pattern):
        return {"pattern": value.pattern, "flags": value.flags}
    if isinstance(value, Path | os.PathLike):
        return os.fspath(value)
    if isinstance(value, bytes):
        return hashlib.sha256(value).hexdigest()
    if isinstance(value, ModuleType):
        return f"module {value.__name__}"
    if isinstance(value, type) or callable(value):
        return f"{getattr(value, '__module__', '?')}:{getattr(value, '__qualname__', '?')}"
    if depth >= 8:
        return f"{type(value).__module__}:{type(value).__qualname__}"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda kv: str(kv[0]))
        return {str(k): _plain(v, depth + 1) for k, v in items}
    if isinstance(value, list | tuple):
        return [_plain(v, depth + 1) for v in value]
    if isinstance(value, set | frozenset):
        return sorted(json.dumps(_plain(v, depth + 1), sort_keys=True) for v in value)
    return f"{type(value).__module__}:{type(value).__qualname__}"


def _state(gate: Gate) -> object:
    """A gate object's own attributes: parameters set where the gate was built."""
    if dataclasses.is_dataclass(gate) and not isinstance(gate, type):
        return _plain({f.name: getattr(gate, f.name) for f in dataclasses.fields(gate)})
    attributes = getattr(gate, "__dict__", None)
    if isinstance(attributes, dict):
        return _plain(attributes)
    names = [n for cls in type(gate).__mro__ for n in getattr(cls, "__slots__", ())]
    return _plain({n: getattr(gate, n) for n in names if hasattr(gate, n)})


def _is_data(value: object) -> bool:
    if value is None or isinstance(value, str | int | float | bool | bytes | re.Pattern):
        return True
    if isinstance(value, enum.Enum | Path):
        return True
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_data(v) for k, v in value.items())
    if isinstance(value, list | tuple | set | frozenset):
        return all(_is_data(v) for v in value)
    return False


def _module_material(name: str) -> dict[str, Any]:
    """A plugin module as key material: its source, its constants and what it imports.

    ``imports`` maps every module, function and class the module holds at top level
    (one hop) to a digest of the module it comes from, leaving out the standard
    library and TaskGate itself; ``constants`` are its public data values.
    """
    module = sys.modules.get(name)
    if module is None:
        return {"source": None, "constants": {}, "imports": {}}
    imports: dict[str, str | None] = {}
    constants: dict[str, object] = {}
    for attr, value in sorted(vars(module).items()):
        if attr.startswith("_"):
            continue
        origin: object
        if isinstance(value, ModuleType):
            origin = value.__name__
        elif isinstance(value, type) or callable(value):
            origin = getattr(value, "__module__", None)
        elif _is_data(value):
            constants[attr] = _plain(value)
            continue
        else:
            continue
        if not isinstance(origin, str) or _covered(origin):
            continue
        file = getattr(sys.modules.get(origin), "__file__", None)
        imports[origin] = _source_digest(file) if isinstance(file, str) else None
    file = getattr(module, "__file__", None)
    return {
        "source": _source_digest(file) if isinstance(file, str) else None,
        "constants": constants,
        "imports": imports,
    }


def _covered(module: str) -> bool:
    top = module.partition(".")[0]
    return top in sys.stdlib_module_names or top in OWN_MODULES


def _gate_origin(gate: Gate, plugin: PluginSource | None = None) -> dict[str, Any]:
    """Where a gate's check lives (with a digest of that module's source), the gate
    object's attributes, and for a plugin gate the entry point and its module."""
    target = getattr(gate, "func", gate)
    owner = target if callable(target) and hasattr(target, "__qualname__") else type(target)
    module = getattr(owner, "__module__", "?")
    source = getattr(sys.modules.get(module), "__file__", None)
    origin: dict[str, Any] = {
        "module": module,
        "qualname": getattr(owner, "__qualname__", "?"),
        "source": _source_digest(source) if isinstance(source, str) else None,
        "state": _state(gate),
    }
    if plugin is not None:
        entry_module = plugin.entry_point.partition(":")[0]
        origin["plugin"] = {
            "entry_point": plugin.entry_point,
            "distribution": plugin.distribution,
            "modules": {name: _module_material(name) for name in sorted({module, entry_module})},
        }
    return origin


def gate_fingerprint(gates: Sequence[Gate | Registered]) -> list[dict[str, Any]]:
    """Every gate as key material: its contract fields and where its code lives.

    A :class:`~taskgate.registry.Registered` entry adds how its plugin was loaded.
    """
    material: list[dict[str, Any]] = []
    for item in gates:
        gate, plugin = (item.gate, item.plugin) if isinstance(item, Registered) else (item, None)
        material.append(
            {
                "code": gate.code,
                "name": gate.name,
                "severity": gate.severity.value,
                "summary": gate.summary,
                "fix_hint": gate.fix_hint,
                "requires": list(gate.requires),
                "origin": _gate_origin(gate, plugin),
            }
        )
    return material


def plain_gates(gates: Sequence[Gate | Registered]) -> tuple[Gate, ...]:
    """The gates themselves, whether given directly or as registry entries."""
    return tuple(item.gate if isinstance(item, Registered) else item for item in gates)


def config_fingerprint(config: Config) -> dict[str, Any]:
    """The effective configuration as key material (its ``source`` is left out)."""
    return {
        "disabled": sorted(config.disabled),
        "severity": {code: config.severity[code].value for code in sorted(config.severity)},
        "manifest": dataclasses.asdict(config.manifest),
        "secrets": dataclasses.asdict(config.secrets),
        "files": dataclasses.asdict(config.files),
        "runner": dataclasses.asdict(config.runner),
        "determinism": dataclasses.asdict(config.determinism),
    }


def runner_fingerprint(runner: Runner) -> dict[str, str]:
    """The runner and the interpreter, pytest and platform TaskGate runs on."""
    return {
        "name": runner.name,
        "python": sys.version,
        "pytest": pytest.__version__,
        "platform": f"{sys.platform} {platform.machine()}",
    }


def cache_key(
    content: str,
    *,
    label: str,
    name: str,
    gates: Sequence[Gate | Registered],
    config: Config,
    runner: Runner,
) -> str:
    """The key a task's results are stored under (see the module docstring)."""
    material = {
        "schema": CACHE_SCHEMA,
        "taskgate": build_id(),
        "gates": gate_fingerprint(gates),
        "config": config_fingerprint(config),
        "runner": runner_fingerprint(runner),
        "task": {"label": label, "name": name, "content": content},
    }
    text = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def result_to_dict(result: GateResult) -> dict[str, Any]:
    return {
        "code": result.code,
        "name": result.name,
        "severity": result.severity.value,
        "status": result.status.value,
        "message": result.message,
        "fix_hint": result.fix_hint,
        "details": list(result.details),
    }


def result_from_dict(data: object) -> GateResult:
    """Rebuild a stored result; ``ValueError``/``KeyError``/``TypeError`` when it is malformed."""
    if not isinstance(data, dict):
        raise TypeError("a stored result is not an object")
    hint = data["fix_hint"]
    details = data["details"]
    texts = (data["code"], data["name"], data["message"])
    if not all(isinstance(text, str) for text in texts) or not isinstance(details, list):
        raise TypeError("a stored result has fields of the wrong type")
    if not (hint is None or isinstance(hint, str)) or not all(isinstance(d, str) for d in details):
        raise TypeError("a stored result has fields of the wrong type")
    return GateResult(
        code=data["code"],
        name=data["name"],
        severity=Severity(data["severity"]),
        status=Status(data["status"]),
        message=data["message"],
        fix_hint=hint,
        details=tuple(details),
    )


def _write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` so a reader sees the old file or the new one, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=TMP_PREFIX)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


@dataclass(frozen=True, slots=True)
class Counts:
    """What ``taskgate check`` runs did with the cache (summed in ``stats.json``)."""

    hits: int = 0
    misses: int = 0
    stored: int = 0
    not_stored: int = 0
    """Results that were not stored because a blocking gate failed."""

    def __add__(self, other: Counts) -> Counts:
        return Counts(*(a + b for a, b in zip(self.astuple(), other.astuple(), strict=True)))

    def astuple(self) -> tuple[int, int, int, int]:
        return (self.hits, self.misses, self.stored, self.not_stored)

    @classmethod
    def from_json(cls, data: object) -> Counts:
        if not isinstance(data, dict):
            return cls()
        values = [data.get(f.name, 0) for f in dataclasses.fields(cls)]
        if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in values):
            return cls()
        return cls(*values)


@dataclass(frozen=True, slots=True)
class Entry:
    """One stored result set, as ``stats`` and ``prune`` see it."""

    path: Path
    size: int
    last_used: float
    """The file's mtime: when it was stored or last hit."""

    task: str | None = None
    runner: str | None = None
    build: str | None = None
    schema: int | None = None

    @property
    def readable(self) -> bool:
        return self.schema is not None

    @property
    def current(self) -> bool:
        """Written by this TaskGate build in this cache format, so a lookup can hit it."""
        return self.schema == CACHE_SCHEMA and self.build == build_id()


@dataclass(frozen=True, slots=True)
class CacheStats:
    directory: Path
    entries: tuple[Entry, ...]
    counts: Counts

    @property
    def size(self) -> int:
        return sum(entry.size for entry in self.entries)

    @property
    def current(self) -> int:
        return sum(entry.current for entry in self.entries)

    @property
    def tasks(self) -> int:
        """Distinct tasks with a usable entry (a task cached on two runners counts once)."""
        return len({entry.task for entry in self.entries if entry.current})

    @property
    def runners(self) -> dict[str, int]:
        counted = Counter(entry.runner for entry in self.entries if entry.current)
        return {name: counted[name] for name in sorted(n for n in counted if n)}


@dataclass(frozen=True, slots=True)
class Pruned:
    """What ``prune`` removed (or would remove), by reason, and what it kept."""

    removed: dict[str, tuple[Entry, ...]]
    kept: tuple[Entry, ...]

    @property
    def count(self) -> int:
        return sum(len(entries) for entries in self.removed.values())

    @property
    def size(self) -> int:
        return sum(entry.size for entries in self.removed.values() for entry in entries)


PRUNE_REASONS: tuple[str, ...] = ("all", "stale", "unreadable", "superseded", "unused")
"""Why ``prune`` removes an entry: ``--all``; written by another TaskGate build or
cache format; not a readable entry; an older entry for the same task and runner;
not used within ``--older-than``."""


@dataclass(frozen=True, slots=True)
class ResultCache:
    """Gate results on disk under ``directory`` (see the module docstring)."""

    directory: Path
    lock_timeout: float = LOCK_TIMEOUT_SEC

    @property
    def entries_dir(self) -> Path:
        return self.directory / ENTRIES_DIR

    def entry_path(self, key: str) -> Path:
        return self.entries_dir / f"{key}.json"

    @property
    def tagged(self) -> bool:
        """Whether the directory carries the ``CACHEDIR.TAG`` TaskGate writes when it
        creates a cache (``stats`` and ``prune`` act on nothing else)."""
        signature = CACHEDIR_TAG.partition("\n")[0].encode()
        try:
            with (self.directory / "CACHEDIR.TAG").open("rb") as handle:
                return handle.read(len(signature)) == signature
        except OSError:
            return False

    def lookup(
        self, key: str, *, codes: Sequence[str] | None = None
    ) -> tuple[GateResult, ...] | None:
        """The results stored under ``key``; ``None`` for a miss or an unusable entry.

        With ``codes``, an entry whose results are not exactly those gates, in that
        order, is unusable too: a replayed entry can never report fewer gates than
        a fresh run would.
        """
        path = self.entry_path(key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["schema"] != CACHE_SCHEMA or data["key"] != key:
                return None
            results = tuple(result_from_dict(item) for item in data["results"])
        except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError):
            return None
        if codes is not None and [result.code for result in results] != list(codes):
            return None
        with contextlib.suppress(OSError):
            os.utime(path)
        return results

    def _prepare(self) -> None:
        """Create the cache directory with its ``.gitignore`` and ``CACHEDIR.TAG``."""
        if not self.directory.is_dir():
            self.directory.mkdir(parents=True, exist_ok=True)
        for name, text in ((".gitignore", GITIGNORE), ("CACHEDIR.TAG", CACHEDIR_TAG)):
            if not (self.directory / name).exists():
                _write_atomic(self.directory / name, text)

    @contextlib.contextmanager
    def locked(self, *, create: bool = True) -> Iterator[None]:
        """Hold the exclusive lock on ``<cache>/lock``; ``CacheError`` after ``lock_timeout``.

        With ``create`` the cache directory and its marker files are made first.
        """
        try:
            if create:
                self._prepare()
            fd = os.open(self.directory / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as exc:
            raise CacheError(f"cannot use the cache at {self.directory}: {exc}") from exc
        try:
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise CacheError(
                            f"{self.directory / LOCK_FILE} is still locked by another taskgate "
                            f"process after {self.lock_timeout:g}s"
                        ) from None
                    time.sleep(LOCK_POLL_SEC)
            yield
        finally:
            os.close(fd)

    def store(self, key: str, *, task: str, runner: str, results: Sequence[GateResult]) -> None:
        """Write ``results`` under ``key`` atomically, holding the lock."""
        payload = {
            "schema": CACHE_SCHEMA,
            "key": key,
            "taskgate": build_id(),
            "task": task,
            "runner": runner,
            "results": [result_to_dict(result) for result in results],
        }
        text = json.dumps(payload, indent=1) + "\n"
        with self.locked():
            try:
                _write_atomic(self.entry_path(key), text)
            except OSError as exc:
                raise CacheError(f"cannot write the cache at {self.directory}: {exc}") from exc

    def counts(self) -> Counts:
        try:
            return Counts.from_json(json.loads((self.directory / STATS_FILE).read_text("utf-8")))
        except (OSError, UnicodeDecodeError, ValueError):
            return Counts()

    def record(self, counts: Counts) -> None:
        """Add one run's hits, misses and stores to ``stats.json``, holding the lock."""
        with self.locked():
            total = self.counts() + counts
            data = dict(
                zip(("hits", "misses", "stored", "not_stored"), total.astuple(), strict=True)
            )
            try:
                _write_atomic(self.directory / STATS_FILE, json.dumps(data) + "\n")
            except OSError as exc:
                raise CacheError(f"cannot write the cache at {self.directory}: {exc}") from exc

    def entries(self) -> tuple[Entry, ...]:
        """Every entry file (a key-named ``*.json``), readable or not, sorted by path.

        Nothing is listed unless the cache is tagged and ``entries`` is a real
        directory, so a directory TaskGate did not create is left alone.
        """
        if not self.tagged or self.entries_dir.is_symlink():
            return ()
        found: list[Entry] = []
        for path in sorted(p for p in self.entries_dir.glob("*.json") if KEY.match(p.stem)):
            try:
                st = path.stat()
            except OSError:  # a dangling symlink, or removed since the listing
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            base = Entry(path, st.st_size, st.st_mtime)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                found.append(
                    dataclasses.replace(
                        base,
                        task=str(data["task"]),
                        runner=str(data["runner"]),
                        build=str(data["taskgate"]),
                        schema=int(data["schema"]),
                    )
                )
            except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError):
                found.append(base)
        return tuple(found)

    def stats(self) -> CacheStats:
        return CacheStats(self.directory, self.entries(), self.counts())

    def prune(
        self,
        *,
        everything: bool = False,
        older_than_sec: float | None = None,
        now: float | None = None,
        dry_run: bool = False,
    ) -> Pruned:
        """Remove entries no lookup can hit, and older duplicates (see :data:`PRUNE_REASONS`).

        By default that is entries from another TaskGate build or cache format,
        unreadable ones, and all but the most recently used entry per task and
        runner; ``older_than_sec`` also removes entries unused for that long, and
        ``everything`` removes them all. Leftover temp files of a writer that died
        are removed too. With ``dry_run`` nothing is deleted and nothing is written,
        not even the lock file. A directory without TaskGate's ``CACHEDIR.TAG`` is
        a :class:`CacheError` (a missing one is just empty).
        """
        if not self.directory.is_dir():
            return Pruned({}, ())
        if not self.tagged:
            raise CacheError(
                f"{self.directory} is not a taskgate cache (no CACHEDIR.TAG written by "
                "taskgate); nothing removed"
            )
        with contextlib.nullcontext() if dry_run else self.locked(create=False):
            entries = self.entries()
            removed: dict[str, list[Entry]] = {reason: [] for reason in PRUNE_REASONS}
            latest: dict[tuple[str | None, str | None], Entry] = {}
            clock = time.time() if now is None else now
            cutoff = None if older_than_sec is None else clock - older_than_sec
            for entry in entries:
                if everything:
                    removed["all"].append(entry)
                elif not entry.readable:
                    removed["unreadable"].append(entry)
                elif not entry.current:
                    removed["stale"].append(entry)
                elif cutoff is not None and entry.last_used < cutoff:
                    removed["unused"].append(entry)
                else:
                    group = (entry.task, entry.runner)
                    older = latest.get(group)
                    if older is None or entry.last_used > older.last_used:
                        if older is not None:
                            removed["superseded"].append(older)
                        latest[group] = entry
                    else:
                        removed["superseded"].append(entry)
            kept = sorted(latest.values(), key=lambda entry: entry.path)
            if not dry_run:
                for entries_removed in removed.values():
                    for entry in entries_removed:
                        with contextlib.suppress(FileNotFoundError):
                            entry.path.unlink()
                with contextlib.suppress(OSError):
                    for leftover in self.entries_dir.glob(f"{TMP_PREFIX}*"):
                        if leftover.is_file() and not leftover.is_symlink():
                            leftover.unlink()
            return Pruned(
                {reason: tuple(found) for reason, found in removed.items() if found},
                tuple(kept),
            )


def _git_stdout(*args: str) -> bytes | None:
    """What ``git ARGS`` prints, or ``None`` when git is missing or fails."""
    try:
        proc = subprocess.run(["git", *args], capture_output=True, check=False)
    except OSError:
        return None
    return proc.stdout if proc.returncode == 0 else None


def committed_paths(directory: Path) -> list[str]:
    """Paths git tracks at or under ``directory``, or a tracked ancestor of it
    (a symlink or file standing where the directory would be), relative to the
    repository root; ``[]`` when ``directory`` is not inside a git work tree or
    is the work tree's root itself.

    A cache directory the repository commits could hold entries a pull request
    wrote, so :class:`CachedChecks` refuses to use one.
    """
    probe = directory.absolute()
    while not probe.is_dir() and probe.parent != probe:
        probe = probe.parent
    toplevel = _git_stdout("-C", str(probe), "rev-parse", "--show-toplevel")
    if toplevel is None:
        return []
    root = Path(os.fsdecode(toplevel.strip()))
    candidates = {directory.absolute()}
    with contextlib.suppress(OSError, RuntimeError):
        candidates.add(directory.resolve())
    with contextlib.suppress(OSError, RuntimeError, ValueError):
        candidates.add(root.resolve() / directory.absolute().relative_to(root))
    relatives: set[str] = set()
    for candidate in candidates:
        for base in (root, root.resolve()):
            if candidate.is_relative_to(base) and candidate != base:
                relatives.add(candidate.relative_to(base).as_posix())
    if not relatives:
        return []
    ancestors: set[str] = set()
    for relative in relatives:
        parts = relative.split("/")
        ancestors.update("/".join(parts[:n]) for n in range(1, len(parts) + 1))
    listed = _git_stdout("-C", str(root), "ls-files", "-z", "--full-name", "--", *sorted(ancestors))
    if listed is None:
        return []
    found = [
        path
        for path in os.fsdecode(listed).split("\0")
        if path and (path in ancestors or any(path.startswith(f"{r}/") for r in relatives))
    ]
    return sorted(found)


@dataclass
class CachedChecks:
    """Look each task up in the cache before running its gates; store what passes.

    With ``cache`` set to ``None`` every task runs (``--no-cache``). A cache
    problem is passed to ``note`` once and turns the cache off for the rest of
    the run; the check itself carries on.
    """

    cache: ResultCache | None
    gates: Sequence[Gate | Registered]
    """The gates in run order; registry entries carry their plugin's origin into the key."""

    config: Config
    runner: Runner
    note: Callable[[str], None] = lambda message: None
    counts: Counts = field(default_factory=Counts)

    def __post_init__(self) -> None:
        if self.cache is not None:
            committed = committed_paths(self.cache.directory)
            if committed:
                self._disable(
                    f"{self.cache.directory} is tracked by the repository ({len(committed)} "
                    f"committed path{'s' if len(committed) != 1 else ''}, e.g. {committed[0]}); "
                    "a cache that a pull request can commit is not trusted"
                )

    @property
    def checked(self) -> tuple[Gate, ...]:
        """The gates to run (without registry wrapping)."""
        return plain_gates(self.gates)

    @property
    def expected_codes(self) -> list[str]:
        """The codes a fresh run reports, in order: every enabled gate."""
        return [gate.code for gate in self.checked if gate.code not in self.config.disabled]

    def _disable(self, problem: str) -> None:
        self.note(f"result cache off for this run: {problem}")
        self.cache = None

    def results(
        self,
        task_dir: Path,
        *,
        label: str,
        tracked: Sequence[str] | None,
        compute: Callable[[], tuple[GateResult, ...]],
    ) -> tuple[tuple[GateResult, ...], bool]:
        """The task's results and whether they came from the cache."""
        cache = self.cache
        if cache is None:
            return compute(), False
        try:
            content = task_digest(task_dir, tracked)
        except UncacheableError as exc:
            self.note(f"{label} is not cached: {exc}")
            return compute(), False
        except OSError as exc:
            self.note(f"{label} is not cached: cannot read it ({exc})")
            return compute(), False
        key = cache_key(
            content,
            label=label,
            name=task_dir.name,
            gates=self.gates,
            config=self.config,
            runner=self.runner,
        )
        found = cache.lookup(key, codes=self.expected_codes)
        if found is not None:
            self.counts += Counts(hits=1)
            return found, True
        self.counts += Counts(misses=1)
        results = compute()
        if any(result.blocking for result in results):
            self.counts += Counts(not_stored=1)
            return results, False
        try:
            cache.store(key, task=label, runner=self.runner.name, results=results)
        except CacheError as exc:
            self._disable(str(exc))
        else:
            self.counts += Counts(stored=1)
        return results, False

    def close(self) -> None:
        """Add this run's counts to the cache's ``stats.json``."""
        if self.cache is None or self.counts == Counts():
            return
        try:
            self.cache.record(self.counts)
        except CacheError as exc:
            self._disable(str(exc))
