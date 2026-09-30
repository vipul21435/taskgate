"""The content-hash result cache: the task hash, the key, the store, prune and stats."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import shutil
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from gitrepo import GitRepo
from taskfactory import make_task
from taskgate import cache as cache_module
from taskgate.cache import (
    CACHE_SCHEMA,
    LOCK_FILE,
    CachedChecks,
    CacheError,
    Counts,
    ResultCache,
    UncacheableError,
    build_id,
    cache_key,
    task_digest,
)
from taskgate.config import Config, DeterminismOptions, FileOptions
from taskgate.gates import BUILTIN_GATES, Check, Gate, gate
from taskgate.registry import PluginSource, Registered
from taskgate.results import GateResult, Severity, Status
from taskgate.runner import LocalRunner

PASS = GateResult("TG101", "layout-complete", Severity.ERROR, Status.PASS, "layout complete")
WARN = GateResult(
    "TG104",
    "timeout-in-range",
    Severity.WARNING,
    Status.FAIL,
    "task.timeout_sec 5 is outside 10..1800",
    fix_hint="Pick a timeout inside the range.",
    details=("a detail",),
)
BLOCK = GateResult("TG401", "solution-passes", Severity.ERROR, Status.FAIL, "the grader failed")


def task_files(task: Path) -> list[Path]:
    return sorted(path for path in task.rglob("*") if path.is_file())


@pytest.fixture
def task(tmp_path: Path) -> Path:
    made = make_task(tmp_path / "echo")
    (made / "solution" / "solve.sh").chmod(0o755)
    return made


# --- the task hash -------------------------------------------------------------------


def test_the_hash_ignores_mtimes(task: Path) -> None:
    before = task_digest(task)
    for number, path in enumerate(sorted(task.rglob("*"))):
        os.utime(path, (1_000_000 + number * 7919, 2_000_000 + number * 104729))
    assert task_digest(task) == before


def test_the_hash_ignores_walk_order(task: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before = task_digest(task)
    real_walk = Path.walk

    def reversed_walk(self: Path, *args: object, **kwargs: object) -> Iterator[object]:
        for dirpath, dirnames, filenames in real_walk(self, *args, **kwargs):  # type: ignore[arg-type]
            dirnames.reverse()
            filenames.reverse()
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(Path, "walk", reversed_walk)
    assert task_digest(task) == before


def test_the_hash_ignores_the_order_files_were_created_in(tmp_path: Path) -> None:
    names = [f"d{n % 3}/f{n}.txt" for n in range(12)]
    digests = []
    for label, order in (("forward", names), ("backward", names[::-1])):
        root = tmp_path / label / "task"
        for name in order:
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(name, encoding="utf-8")
        digests.append(task_digest(root))
    assert digests[0] == digests[1]


def test_the_hash_changes_on_any_byte(task: Path) -> None:
    before = task_digest(task)
    seen = {before}
    for path in task_files(task):
        original = path.read_bytes()
        for index in {0, len(original) // 2, len(original) - 1}:
            changed = bytearray(original)
            changed[index] ^= 0x01
            path.write_bytes(bytes(changed))
            digest = task_digest(task)
            assert digest not in seen, (path, index)
            seen.add(digest)
        path.write_bytes(original + b"\n")
        assert task_digest(task) not in seen
        path.write_bytes(original)
        assert task_digest(task) == before


def test_the_hash_changes_on_any_mode_change(task: Path) -> None:
    before = task_digest(task)
    solve = task / "solution" / "solve.sh"
    seen = {before}
    for mode in (0o644, 0o700, 0o600, 0o444):
        solve.chmod(mode)
        digest = task_digest(task)
        assert digest not in seen, oct(mode)
        seen.add(digest)
    solve.chmod(0o755)
    assert task_digest(task) == before
    (task / "tests").chmod(0o700)
    assert task_digest(task) not in seen
    (task / "tests").chmod(0o755)
    assert task_digest(task) == before


def test_the_hash_covers_names_empty_directories_and_symlinks(task: Path) -> None:
    before = task_digest(task)
    grader = task / "tests" / "test_outputs.py"
    grader.rename(task / "tests" / "test_output.py")
    renamed = task_digest(task)
    assert renamed != before
    (task / "tests" / "test_output.py").rename(grader)
    (task / "environment" / "workspace" / "output").mkdir()
    assert task_digest(task) not in (before, renamed)
    (task / "environment" / "workspace" / "output").rmdir()
    assert task_digest(task) == before
    (task / "tests" / "link.py").symlink_to("test_outputs.py")
    linked = task_digest(task)
    assert linked != before
    (task / "tests" / "link.py").unlink()
    (task / "tests" / "link.py").symlink_to("../instruction.md")
    assert task_digest(task) not in (before, linked)


def test_ignored_files_are_left_out(task: Path) -> None:
    before = task_digest(task)
    for relative in (
        "tests/__pycache__/test_outputs.cpython-312.pyc",
        "solution/helper.pyc",
        ".DS_Store",
        "environment/workspace/.pytest_cache/v/cache/lastfailed",
        ".taskgate/cache/entries/x.json",
        ".mypy_cache/3.12/x.json",
    ):
        (task / relative).parent.mkdir(parents=True, exist_ok=True)
        (task / relative).write_bytes(b"\0cache")
    assert task_digest(task) == before


def test_tracked_paths_and_committed_cache_files_count_in_diff_mode(task: Path) -> None:
    files = [path.relative_to(task).as_posix() for path in task_files(task)]
    disk = task_digest(task)
    tracked = task_digest(task, files)
    assert tracked != disk
    assert task_digest(task, list(reversed(files))) == tracked
    assert task_digest(task, files[:-1]) != tracked
    committed = task / "tests" / "__pycache__" / "x.pyc"
    committed.parent.mkdir()
    committed.write_bytes(b"one")
    with_pyc = task_digest(task, [*files, "tests/__pycache__/x.pyc"])
    committed.write_bytes(b"two")
    assert task_digest(task, [*files, "tests/__pycache__/x.pyc"]) != with_pyc
    assert task_digest(task) == disk


def test_symlinks_that_leave_the_task_or_loop_are_uncacheable(task: Path, tmp_path: Path) -> None:
    outside = tmp_path / "shared.toml"
    outside.write_text("x", encoding="utf-8")
    (task / "extra.toml").symlink_to(outside)
    with pytest.raises(UncacheableError, match=r"the symlink extra\.toml leads outside the task"):
        task_digest(task)
    (task / "extra.toml").unlink()
    (task / "loop").symlink_to("loop")
    with pytest.raises(UncacheableError, match="loop is a symlink loop"):
        task_digest(task)
    (task / "loop").unlink()
    (task / ".DS_Store").symlink_to(outside)
    assert task_digest(task)
    with pytest.raises(UncacheableError, match=r"the symlink \.DS_Store leads outside"):
        task_digest(task, [".DS_Store"])


def test_special_files_are_left_out(task: Path) -> None:
    before = task_digest(task)
    os.mkfifo(task / "pipe")
    assert task_digest(task) == before
    os.mkfifo(task / ".DS_Store")
    assert task_digest(task, ["pipe", ".DS_Store"]) == task_digest(task, ["pipe", ".DS_Store"])


# --- the key ---------------------------------------------------------------------------


def key(**changes: object) -> str:
    args: dict[str, object] = {
        "label": "tasks/echo",
        "name": "echo",
        "gates": BUILTIN_GATES,
        "config": Config(),
        "runner": LocalRunner(),
    }
    args.update(changes)
    return cache_key("content", **args)  # type: ignore[arg-type]


@gate(
    "TG701", "no-todo", severity=Severity.WARNING, summary="no TODO", fix_hint="Resolve the TODOs."
)
def no_todo(ctx: object) -> Check:
    return Check.ok("no TODO")


class DockerNamed(LocalRunner):
    @property
    def name(self) -> str:
        return "docker"


def test_the_key_covers_config_gates_runner_label_and_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = key()
    assert key() == base
    assert cache_key("other", label="tasks/echo", name="echo", gates=BUILTIN_GATES,
                     config=Config(), runner=LocalRunner()) != base  # fmt: skip
    variants = [
        key(config=Config(disabled=frozenset({"TG501"}))),
        key(config=Config(severity={"TG402": Severity.WARNING})),
        key(config=Config(files=FileOptions(max_file_bytes=10))),
        key(config=Config(determinism=DeterminismOptions(runs=3))),
        key(gates=BUILTIN_GATES[:-1]),
        key(gates=(*BUILTIN_GATES, no_todo)),
        key(runner=DockerNamed()),
        key(label="other/echo"),
        key(name="renamed"),
    ]
    assert len({base, *variants}) == len(variants) + 1
    assert key(config=Config(source="somewhere else")) == base
    monkeypatch.setattr(cache_module, "build_id", lambda: "9.9.9+000000000000")
    assert key() != base


def test_a_gate_whose_source_cannot_be_read_still_has_an_origin(tmp_path: Path) -> None:
    assert cache_module._source_digest(str(tmp_path / "missing.py")) is None
    assert cache_module._gate_origin(no_todo)["qualname"] == "no_todo"


def test_the_build_id_is_the_version_plus_a_source_digest() -> None:
    version, _, digest = build_id().partition("+")
    assert version == cache_module.__version__
    assert len(digest) == 12
    assert int(digest, 16) >= 0


# --- the store -------------------------------------------------------------------------


def test_store_and_lookup_round_trip(tmp_path: Path) -> None:
    store = ResultCache(tmp_path / "cache")
    assert store.lookup("k" * 64) is None
    store.store("k" * 64, task="tasks/echo", runner="local", results=(PASS, WARN))
    assert store.lookup("k" * 64) == (PASS, WARN)
    assert (tmp_path / "cache" / ".gitignore").read_text(encoding="utf-8").endswith("*\n")
    assert (
        (tmp_path / "cache" / "CACHEDIR.TAG")
        .read_text(encoding="utf-8")
        .startswith("Signature: 8a477f597d28d172789f06886806bc55")
    )
    entry = json.loads(store.entry_path("k" * 64).read_text(encoding="utf-8"))
    assert (entry["schema"], entry["task"], entry["runner"], entry["taskgate"]) == (
        CACHE_SCHEMA,
        "tasks/echo",
        "local",
        build_id(),
    )
    assert sorted(p.name for p in store.entries_dir.iterdir()) == [f"{'k' * 64}.json"]


def test_a_hit_refreshes_the_entry_mtime(tmp_path: Path) -> None:
    store = ResultCache(tmp_path / "cache")
    store.store("a", task="t", runner="local", results=(PASS,))
    os.utime(store.entry_path("a"), (1000, 1000))
    assert store.lookup("a") == (PASS,)
    assert store.entry_path("a").stat().st_mtime > 1000


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        b"\xff\xfe",
        "[]",
        json.dumps({"schema": CACHE_SCHEMA + 1, "key": "a", "results": []}),
        json.dumps({"schema": CACHE_SCHEMA, "key": "b", "results": []}),
        json.dumps({"schema": CACHE_SCHEMA, "key": "a", "results": [1]}),
        json.dumps({"schema": CACHE_SCHEMA, "key": "a", "results": [{"code": "TG101"}]}),
        json.dumps({"schema": CACHE_SCHEMA, "key": "a", "results": {"x": 1}}),
    ],
)
def test_unusable_entries_are_misses(tmp_path: Path, content: str | bytes) -> None:
    store = ResultCache(tmp_path / "cache")
    store.entries_dir.mkdir(parents=True)
    path = store.entry_path("a")
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    assert store.lookup("a") is None


@pytest.mark.parametrize(
    "change",
    [
        {"code": 1},
        {"details": "not a list"},
        {"details": [1]},
        {"fix_hint": 3},
        {"severity": "fatal"},
        {"status": "maybe"},
    ],
)
def test_malformed_results_are_misses(tmp_path: Path, change: dict[str, object]) -> None:
    store = ResultCache(tmp_path / "cache")
    store.store("a", task="t", runner="local", results=(WARN,))
    data = json.loads(store.entry_path("a").read_text(encoding="utf-8"))
    data["results"][0].update(change)
    store.entry_path("a").write_text(json.dumps(data), encoding="utf-8")
    assert store.lookup("a") is None


def test_a_failed_write_leaves_no_temp_file_and_keeps_the_old_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ResultCache(tmp_path / "cache")
    store.store("a", task="t", runner="local", results=(PASS,))

    def refuse(self: Path, target: Path) -> Path:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "replace", refuse)
    with pytest.raises(CacheError, match="No space left on device"):
        store.store("a", task="t", runner="local", results=(WARN,))
    monkeypatch.undo()
    assert store.lookup("a") == (PASS,)
    assert [p.name for p in store.entries_dir.iterdir()] == ["a.json"]


def test_an_unwritable_cache_is_a_cache_error(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(CacheError, match=r"^cannot use the cache at "):
        ResultCache(blocker / "cache").store("a", task="t", runner="local", results=(PASS,))
    readonly = tmp_path / "readonly"
    store = ResultCache(readonly)
    store.store("a", task="t", runner="local", results=(PASS,))
    store.entries_dir.chmod(0o555)
    try:
        with pytest.raises(CacheError, match=r"^cannot write the cache at "):
            store.store("b", task="t", runner="local", results=(PASS,))
        (readonly / "stats.json").write_text("{}", encoding="utf-8")
        (readonly / "stats.json").chmod(0o444)
        readonly.chmod(0o555)
        with pytest.raises(CacheError, match=r"^cannot write the cache at "):
            store.record(Counts(hits=1))
    finally:
        readonly.chmod(0o755)
        store.entries_dir.chmod(0o755)


@pytest.fixture
def held_lock(tmp_path: Path) -> Iterator[Path]:
    """Another process's view: the cache lock held through a separate open file."""
    directory = tmp_path / "cache"
    directory.mkdir()
    fd = os.open(directory / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        yield directory
    finally:
        os.close(fd)


def test_a_writer_waits_for_the_lock_and_gives_up_after_the_timeout(held_lock: Path) -> None:
    store = ResultCache(held_lock, lock_timeout=0.2)
    started = time.monotonic()
    with pytest.raises(CacheError, match=r"lock is still locked by another taskgate process"):
        store.store("a", task="t", runner="local", results=(PASS,))
    assert time.monotonic() - started >= 0.2
    assert not store.entry_path("a").exists()


def test_a_writer_proceeds_once_the_lock_is_released(tmp_path: Path) -> None:
    directory = tmp_path / "cache"
    directory.mkdir()
    fd = os.open(directory / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    store = ResultCache(directory, lock_timeout=10)
    writer = threading.Thread(
        target=store.store, args=("a",), kwargs={"task": "t", "runner": "l", "results": (PASS,)}
    )
    writer.start()
    time.sleep(0.3)
    assert writer.is_alive()
    assert not store.entry_path("a").exists()
    os.close(fd)
    writer.join(timeout=10)
    assert store.lookup("a") == (PASS,)


def test_concurrent_writers_never_leave_a_torn_entry(tmp_path: Path) -> None:
    store = ResultCache(tmp_path / "cache")
    results = [tuple([WARN] * n) for n in range(1, 9)]
    seen: list[object] = []
    stop = threading.Event()

    def read() -> None:
        while not stop.is_set():
            seen.append(store.lookup("a"))

    def write(found: tuple[GateResult, ...]) -> None:
        for _ in range(5):
            store.store("a", task="t", runner="local", results=found)

    reader = threading.Thread(target=read)
    reader.start()
    writers = [threading.Thread(target=write, args=(r,)) for r in results]
    for thread in writers:
        thread.start()
    for thread in writers:
        thread.join()
    stop.set()
    reader.join()
    assert all(found is None or found in results for found in seen)
    assert store.lookup("a") in results
    assert [p.name for p in store.entries_dir.iterdir()] == ["a.json"]


def test_counts_add_up_across_runs(tmp_path: Path) -> None:
    store = ResultCache(tmp_path / "cache")
    assert store.counts() == Counts()
    store.record(Counts(hits=1, misses=2, stored=1, not_stored=1))
    store.record(Counts(hits=3))
    assert store.counts() == Counts(hits=4, misses=2, stored=1, not_stored=1)
    for broken in ("[1]", '{"hits": -1}', '{"hits": true}', "{oops"):
        (tmp_path / "cache" / "stats.json").write_text(broken, encoding="utf-8")
        assert store.counts() == Counts()


# --- prune and stats -------------------------------------------------------------------


def hexkey(name: str) -> str:
    """A key-shaped (64 hex digit) file stem for a short test name."""
    return hashlib.sha256(name.encode()).hexdigest()


def aged(store: ResultCache, name: str, task: str, when: float, runner: str = "local") -> Path:
    key = hexkey(name)
    store.store(key, task=task, runner=runner, results=(PASS,))
    os.utime(store.entry_path(key), (when, when))
    return store.entry_path(key)


def names(entries: tuple[cache_module.Entry, ...]) -> list[str]:
    known = {hexkey(n): n for n in ("old", "new", "mid", "docker", "b", "stale", "broken")}
    return sorted(known[entry.path.stem] for entry in entries)


def test_prune_keeps_the_latest_entry_per_task_and_runner(tmp_path: Path) -> None:
    store = ResultCache(tmp_path / "cache")
    now = 10_000_000.0
    aged(store, "old", "tasks/a", now - 300)
    aged(store, "new", "tasks/a", now - 100)
    aged(store, "mid", "tasks/a", now - 200)
    aged(store, "docker", "tasks/a", now - 400, runner="docker")
    aged(store, "b", "tasks/b", now - 50)
    stale = aged(store, "stale", "tasks/b", now)
    data = json.loads(stale.read_text(encoding="utf-8"))
    stale.write_text(json.dumps({**data, "taskgate": "0.0.1+000000000000"}), encoding="utf-8")
    store.entry_path(hexkey("broken")).write_text("{", encoding="utf-8")
    (store.entries_dir / ".tmp-leftover").write_text("half", encoding="utf-8")
    foreign = store.entries_dir / "settings.json"
    foreign.write_text('{"theme": "dark"}', encoding="utf-8")

    stats = store.stats()
    assert (len(stats.entries), stats.current, stats.tasks) == (7, 5, 2)
    assert stats.runners == {"docker": 1, "local": 4}

    preview = store.prune(now=now, dry_run=True)
    assert {reason: names(found) for reason, found in preview.removed.items()} == {
        "stale": ["stale"],
        "unreadable": ["broken"],
        "superseded": ["mid", "old"],
    }
    assert names(preview.kept) == ["b", "docker", "new"]
    assert len(list(store.entries_dir.iterdir())) == 9

    pruned = store.prune(now=now)
    assert pruned.count == 4
    assert pruned.size > 0
    assert sorted(p.name for p in store.entries_dir.iterdir()) == sorted(
        [f"{hexkey(n)}.json" for n in ("b", "docker", "new")] + ["settings.json"]
    )
    assert foreign.read_text(encoding="utf-8") == '{"theme": "dark"}'  # not key-named: untouched
    assert store.prune(now=now).count == 0

    unused = store.prune(now=now, older_than_sec=200)
    assert {reason: names(found) for reason, found in unused.removed.items()} == {
        "unused": ["docker"]
    }
    everything = store.prune(everything=True)
    assert names(everything.removed["all"]) == ["b", "new"]
    assert everything.kept == ()
    assert [p.name for p in store.entries_dir.iterdir()] == ["settings.json"]


def test_prune_and_stats_on_a_missing_cache(tmp_path: Path) -> None:
    store = ResultCache(tmp_path / "nothing")
    assert store.prune().count == 0
    stats = store.stats()
    assert (stats.entries, stats.size, stats.counts) == ((), 0, Counts())
    assert not (tmp_path / "nothing").exists()


def test_entries_skip_files_that_vanish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ResultCache(tmp_path / "cache")
    store.store("a", task="t", runner="local", results=(PASS,))
    real_stat = Path.stat
    calls: list[Path] = []

    def vanished(self: Path, **kwargs: object) -> os.stat_result:
        """The entry exists when it is listed, and is gone (pruned) when it is stat'ed."""
        if self.suffix == ".json" and self.parent == store.entries_dir:
            calls.append(self)
            if len(calls) > 1:
                raise FileNotFoundError(errno.ENOENT, "No such file or directory", str(self))
        return real_stat(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "stat", vanished)
    assert store.entries() == ()


def test_entries_of_an_unreadable_directory_are_empty(tmp_path: Path) -> None:
    blocker = tmp_path / "cache"
    blocker.mkdir()
    (blocker / "entries").write_text("not a directory", encoding="utf-8")
    assert ResultCache(blocker).entries() == ()


# --- CachedChecks ----------------------------------------------------------------------


class Recorder:
    def __init__(self, *results: GateResult) -> None:
        self.calls = 0
        self.results = results

    def __call__(self) -> tuple[GateResult, ...]:
        self.calls += 1
        return self.results


def gates_of(*results: GateResult) -> tuple[Gate, ...]:
    """The built-in gates that report ``results``, so a replay carries exactly those codes."""
    codes = {result.code for result in results}
    return tuple(g for g in BUILTIN_GATES if g.code in codes)


def checks(store: ResultCache | None, notes: list[str], *results: GateResult) -> CachedChecks:
    gates = gates_of(*(results or (PASS, WARN)))
    return CachedChecks(store, gates, Config(), LocalRunner(), note=notes.append)


def test_a_passing_task_is_stored_and_then_skipped(task: Path, tmp_path: Path) -> None:
    store = ResultCache(tmp_path / "cache")
    notes: list[str] = []
    compute = Recorder(PASS, WARN)
    first = checks(store, notes)
    assert first.results(task, label="tasks/echo", tracked=None, compute=compute) == (
        (PASS, WARN),
        False,
    )
    first.close()
    second = checks(store, notes)
    assert second.results(task, label="tasks/echo", tracked=None, compute=compute) == (
        (PASS, WARN),
        True,
    )
    second.close()
    assert compute.calls == 1
    assert store.counts() == Counts(hits=1, misses=1, stored=1)
    assert notes == []
    (task / "instruction.md").write_text("Something else.\n", encoding="utf-8")
    third = checks(store, notes)
    assert third.results(task, label="tasks/echo", tracked=None, compute=compute)[1] is False
    assert third.results(task, label="elsewhere/echo", tracked=None, compute=compute)[1] is False
    assert compute.calls == 3


def test_a_blocking_failure_is_never_stored(task: Path, tmp_path: Path) -> None:
    store = ResultCache(tmp_path / "cache")
    compute = Recorder(PASS, BLOCK)
    for _ in range(2):
        run = checks(store, [], PASS, BLOCK)
        assert run.results(task, label="t", tracked=None, compute=compute)[1] is False
        run.close()
    assert compute.calls == 2
    assert store.counts() == Counts(misses=2, not_stored=2)
    assert store.entries() == ()


def test_without_a_cache_every_task_runs(task: Path, tmp_path: Path) -> None:
    compute = Recorder(PASS)
    run = checks(None, [], PASS)
    for _ in range(2):
        assert run.results(task, label="t", tracked=None, compute=compute) == ((PASS,), False)
    run.close()
    assert compute.calls == 2
    assert not (tmp_path / "cache").exists()
    empty = checks(ResultCache(tmp_path / "cache"), [])
    empty.close()
    assert not (tmp_path / "cache").exists()


def test_uncacheable_and_unreadable_tasks_run_with_a_note(
    task: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ResultCache(tmp_path / "cache")
    notes: list[str] = []
    (task / "link").symlink_to(tmp_path)
    compute = Recorder(PASS)
    run = checks(store, notes, PASS)
    assert run.results(task, label="tasks/echo", tracked=None, compute=compute)[1] is False
    (task / "link").unlink()

    def unreadable(path: Path) -> str:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(cache_module, "_file_sha256", unreadable)
    assert run.results(task, label="tasks/echo", tracked=None, compute=compute)[1] is False
    assert notes == [
        "tasks/echo is not cached: the symlink link leads outside the task",
        "tasks/echo is not cached: cannot read it ([Errno 13] Permission denied)",
    ]
    assert compute.calls == 2
    assert run.counts == Counts()


def test_a_cache_that_cannot_be_written_is_turned_off_for_the_run(
    task: Path, tmp_path: Path
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    notes: list[str] = []
    compute = Recorder(PASS)
    run = checks(ResultCache(blocker / "cache"), notes, PASS)
    assert run.results(task, label="t", tracked=None, compute=compute) == ((PASS,), False)
    assert run.cache is None
    assert run.results(task, label="t", tracked=None, compute=compute) == ((PASS,), False)
    run.close()
    assert len(notes) == 1
    assert notes[0].startswith("result cache off for this run: cannot use the cache at ")


def test_counts_that_cannot_be_recorded_are_noted(task: Path, held_lock: Path) -> None:
    notes: list[str] = []
    run = checks(ResultCache(held_lock, lock_timeout=0.1), notes)
    run.counts = Counts(hits=1)
    run.close()
    assert notes == [
        f"result cache off for this run: {held_lock / LOCK_FILE} is still locked by "
        "another taskgate process after 0.1s"
    ]


def test_a_copied_task_hits_only_under_its_own_label(task: Path, tmp_path: Path) -> None:
    store = ResultCache(tmp_path / "cache")
    compute = Recorder(PASS)
    checks(store, [], PASS).results(task, label="tasks/echo", tracked=None, compute=compute)
    copy = tmp_path / "elsewhere" / "echo"
    shutil.copytree(task, copy)
    found = checks(store, [], PASS).results(copy, label="tasks/echo", tracked=None, compute=compute)
    assert found == ((PASS,), True)
    assert compute.calls == 1


# --- what a replayed entry must satisfy, and what a cache must not be ------------------


def test_an_entry_is_replayed_only_with_exactly_the_enabled_gates(tmp_path: Path) -> None:
    """A forged or stale entry that reports fewer, more or other gates is a miss: a
    replay can never show zero gates as a pass."""
    store = ResultCache(tmp_path / "cache")
    store.store("k", task="t", runner="local", results=(PASS, WARN))
    store.store("empty", task="t", runner="local", results=())
    assert store.lookup("k") == (PASS, WARN)
    assert store.lookup("k", codes=["TG101", "TG104"]) == (PASS, WARN)
    assert store.lookup("k", codes=["TG104", "TG101"]) is None
    assert store.lookup("k", codes=["TG101"]) is None
    assert store.lookup("k", codes=["TG101", "TG104", "TG401"]) is None
    assert store.lookup("empty") == ()
    assert store.lookup("empty", codes=["TG101"]) is None
    assert store.lookup("empty", codes=[]) == ()


def test_expected_codes_leave_out_disabled_gates(tmp_path: Path) -> None:
    run = CachedChecks(None, BUILTIN_GATES, Config(disabled=frozenset({"TG104"})), LocalRunner())
    assert "TG104" not in run.expected_codes
    assert run.expected_codes == [g.code for g in BUILTIN_GATES if g.code != "TG104"]
    assert run.checked == tuple(BUILTIN_GATES)


def test_committed_paths_names_what_git_tracks_at_the_cache(repo: GitRepo) -> None:
    repo.write("README.md", "# Tasks\n")
    repo.commit("base")
    cache = repo.root / ".taskgate" / "cache"
    assert cache_module.committed_paths(cache) == []  # not there at all
    cache.mkdir(parents=True)
    assert cache_module.committed_paths(cache) == []  # untracked
    repo.write(".taskgate/cache/entries/" + "a" * 64 + ".json", "{}")
    repo.write(".taskgate/cache/CACHEDIR.TAG", "Signature: 8a477f597d28d172789f06886806bc55\n")
    repo.git("add", "-f", ".taskgate")
    repo.commit("forged")
    assert cache_module.committed_paths(cache) == [
        ".taskgate/cache/CACHEDIR.TAG",
        ".taskgate/cache/entries/" + "a" * 64 + ".json",
    ]
    assert cache_module.committed_paths(cache / "entries") == [
        ".taskgate/cache/entries/" + "a" * 64 + ".json"
    ]
    assert cache_module.committed_paths(repo.root / "elsewhere") == []


def test_a_committed_symlink_standing_for_the_cache_counts_as_tracked(repo: GitRepo) -> None:
    """A pull request can commit ``.taskgate/cache`` as a symlink to a directory of
    entries it also commits (or to one outside the checkout)."""
    repo.write("README.md", "# Tasks\n")
    repo.write("fixtures/" + "b" * 64 + ".json", "{}")
    (repo.root / ".taskgate").mkdir()
    (repo.root / ".taskgate" / "cache").symlink_to(Path("..") / "fixtures")
    repo.git("add", "-f", ".taskgate", "fixtures")
    repo.commit("link")
    found = cache_module.committed_paths(repo.root / ".taskgate" / "cache")
    assert ".taskgate/cache" in found
    assert "fixtures/" + "b" * 64 + ".json" in found


def test_committed_paths_is_empty_outside_a_repository_or_without_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = tmp_path / "plain" / "cache"
    plain.mkdir(parents=True)
    assert cache_module.committed_paths(plain) == []
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    assert cache_module.committed_paths(plain) == []


def test_a_cache_the_repository_tracks_is_not_used(task: Path, repo: GitRepo) -> None:
    """The trust model: a pull request must not be able to supply its own passes."""
    repo.write("README.md", "# Tasks\n")
    repo.write(".taskgate/cache/entries/" + "c" * 64 + ".json", "{}")
    repo.git("add", "-f", ".taskgate")
    repo.commit("with a cache")
    notes: list[str] = []
    compute = Recorder(PASS)
    run = CachedChecks(
        ResultCache(repo.root / ".taskgate" / "cache"),
        gates_of(PASS),
        Config(),
        LocalRunner(),
        note=notes.append,
    )
    assert run.cache is None
    assert run.results(task, label="t", tracked=None, compute=compute) == ((PASS,), False)
    run.close()
    assert notes == [
        f"result cache off for this run: {repo.root / '.taskgate' / 'cache'} is tracked by the "
        "repository (1 committed path, e.g. .taskgate/cache/entries/" + "c" * 64 + ".json); "
        "a cache that a pull request can commit is not trusted"
    ]


# --- plugin edits invalidate their results ----------------------------------------------


PLUGIN_GATE = """
from taskgate.gates import Check, gate
from taskgate.results import Severity

import tgtest_patterns
from tgtest_helper import scan


@gate("TG702", "no-banned-phrase", severity=Severity.ERROR, summary="no banned phrase",
      fix_hint="Reword it.")
def no_banned_phrase(ctx):
    return scan(ctx, tgtest_patterns.BANNED)
"""

PLUGIN_RULES = """
from tgtest_base import WordGate

RULE = WordGate("TG703", "no-word", word={word!r})
"""

PLUGIN_BASE = """
from dataclasses import dataclass

from taskgate.gates import Check
from taskgate.results import Severity


@dataclass(frozen=True)
class WordGate:
    code: str
    name: str
    word: str
    severity: Severity = Severity.ERROR
    summary: str = "no word"
    fix_hint: str = "Reword it."
    requires: tuple[str, ...] = ()

    def check(self, ctx):
        return Check.ok("fine")
"""


def write_plugin(
    where: Path, *, banned: str, word: str, helper: str = "return Check.ok('x')"
) -> None:
    (where / "tgtest_patterns.py").write_text(f"BANNED = {banned!r}\n", encoding="utf-8")
    (where / "tgtest_helper.py").write_text(
        f"from taskgate.gates import Check\n\n\ndef scan(ctx, banned):\n    {helper}\n",
        encoding="utf-8",
    )
    (where / "tgtest_gate.py").write_text(PLUGIN_GATE, encoding="utf-8")
    (where / "tgtest_base.py").write_text(PLUGIN_BASE, encoding="utf-8")
    (where / "tgtest_rules.py").write_text(PLUGIN_RULES.format(word=word), encoding="utf-8")


def plugin_key(where: Path, *, version: str = "0.1") -> str:
    """The cache key with the plugin at ``where`` freshly imported."""
    import importlib
    import sys

    for name in [n for n in sys.modules if n.startswith("tgtest_")]:
        del sys.modules[name]
    importlib.invalidate_caches()
    cache_module._source_digest.cache_clear()  # one process, several plugin versions
    sys.path.insert(0, str(where))
    try:
        gate_module = importlib.import_module("tgtest_gate")
        rules = importlib.import_module("tgtest_rules")
    finally:
        sys.path.remove(str(where))
    entries = [
        Registered(
            gate_module.no_banned_phrase,
            "plugin fg",
            PluginSource("tgtest_gate:no_banned_phrase", f"fg {version}"),
        ),
        Registered(rules.RULE, "plugin wg", PluginSource("tgtest_rules:RULE", f"wg {version}")),
    ]
    return cache_key(
        "content",
        label="tasks/echo",
        name="echo",
        gates=[*BUILTIN_GATES, *entries],
        config=Config(),
        runner=LocalRunner(),
    )


def test_editing_a_plugin_or_its_helpers_or_parameters_changes_the_key(tmp_path: Path) -> None:
    """A tightened rule must not replay the verdict of the old one: the key covers the
    plugin's helper modules, the constants and parameters it reads, its entry-point
    module and its distribution version."""
    where = tmp_path / "plugin"
    where.mkdir()
    write_plugin(where, banned="zzz", word="zzz")
    base = plugin_key(where)
    assert plugin_key(where) == base  # a re-import alone changes nothing
    variants: list[str] = []
    write_plugin(where, banned="Copy", word="zzz")  # a constant in an imported module
    variants.append(plugin_key(where))
    write_plugin(where, banned="zzz", word="Copy")  # a parameter set in the entry-point module
    variants.append(plugin_key(where))
    write_plugin(where, banned="zzz", word="zzz", helper="return Check.fail('banned')")
    variants.append(plugin_key(where))  # the imported helper function's module
    write_plugin(where, banned="zzz", word="zzz")
    variants.append(plugin_key(where, version="0.2"))  # the distribution's version
    assert len({base, *variants}) == len(variants) + 1
    assert plugin_key(where) == base


def test_gate_material_covers_every_kind_of_value(tmp_path: Path) -> None:
    import re
    import sys

    plain = cache_module._plain
    assert plain({"b": 1, "a": (2, 3.5, None, True)}) == {"a": [2, 3.5, None, True], "b": 1}
    assert plain(Severity.ERROR) == "error"
    pattern = re.compile("x", re.I)
    assert plain(pattern) == {"pattern": "x", "flags": pattern.flags}
    assert plain(Path("/a")) == "/a"
    assert plain(b"raw") == hashlib.sha256(b"raw").hexdigest()
    assert plain(sys) == "module sys"
    assert plain(Path) == "pathlib._local:Path" or plain(Path) == "pathlib:Path"
    assert plain(frozenset({2, 1})) == ["1", "2"]
    assert plain(threading.Lock()).startswith("_thread:")
    nested: list[object] = []
    for _ in range(10):
        nested = [nested]
    assert "list" in json.dumps(plain(nested))
    assert cache_module._is_data({"k": [1, "x", re.compile("y")]})
    assert not cache_module._is_data({1: "not a string key"})
    assert not cache_module._is_data([sys])
    assert cache_module._module_material("tgtest_not_imported") == {
        "source": None,
        "constants": {},
        "imports": {},
    }

    class Slotted:
        __slots__ = ("code", "name")

        def __init__(self) -> None:
            self.code = "TG799"
            self.name = "slotted"

    assert cache_module._state(Slotted()) == {"code": "TG799", "name": "slotted"}  # type: ignore[arg-type]
    assert cache_module._gate_origin(no_todo)["state"]["code"] == "TG701"


def test_gate_state_reads_enums_and_plain_objects() -> None:
    import enum

    class Level(enum.Enum):
        LOW = 1

    class Params:
        def __init__(self) -> None:
            self.code = "TG798"
            self.level = Level.LOW
            self.limit = 3

    assert cache_module._plain(Level.LOW) == 1
    assert cache_module._state(Params()) == {"code": "TG798", "level": 1, "limit": 3}  # type: ignore[arg-type]
    assert cache_module._is_data(Level.LOW)
    assert cache_module._is_data(Path("patterns.txt"))


# --- stats and prune touch only what TaskGate wrote -----------------------------------


def test_entries_are_key_named_regular_files_only(tmp_path: Path) -> None:
    store = ResultCache(tmp_path / "cache")
    real = hexkey("real")
    store.store(real, task="t", runner="local", results=(PASS,))
    odd_dir = store.entries_dir / f"{hexkey('dir')}.json"
    odd_dir.mkdir()
    dangling = store.entries_dir / f"{hexkey('dangling')}.json"
    dangling.symlink_to(tmp_path / "missing")
    leftover_dir = store.entries_dir / f"{cache_module.TMP_PREFIX}dir"
    leftover_dir.mkdir()
    assert [entry.path.name for entry in store.entries()] == [f"{real}.json"]
    assert store.prune(everything=True).count == 1
    assert odd_dir.is_dir()
    assert dangling.is_symlink()
    assert leftover_dir.is_dir()


def test_prune_refuses_a_directory_taskgate_did_not_tag(tmp_path: Path) -> None:
    """A wrong ``--cache-dir`` must never delete files: without TaskGate's
    CACHEDIR.TAG nothing is listed and prune is an error, even as a dry run."""
    other = tmp_path / "project"
    (other / "entries").mkdir(parents=True)
    victim = other / "entries" / f"{hexkey('x')}.json"
    victim.write_text("{}", encoding="utf-8")
    store = ResultCache(other)
    assert store.entries() == ()
    for dry_run in (False, True):
        with pytest.raises(CacheError, match="not a taskgate cache"):
            store.prune(dry_run=dry_run)
    assert victim.read_text(encoding="utf-8") == "{}"
    assert not (other / LOCK_FILE).exists()


def test_a_symlinked_entries_directory_is_left_alone(tmp_path: Path) -> None:
    store = ResultCache(tmp_path / "cache")
    store.store(hexkey("a"), task="t", runner="local", results=(PASS,))
    elsewhere = tmp_path / "elsewhere"
    store.entries_dir.rename(elsewhere)
    store.entries_dir.symlink_to(elsewhere)
    assert store.entries() == ()
    assert store.prune(everything=True).count == 0
    assert (elsewhere / f"{hexkey('a')}.json").is_file()


def test_a_dry_run_writes_nothing(tmp_path: Path) -> None:
    store = ResultCache(tmp_path / "cache")
    store.store(hexkey("a"), task="t", runner="local", results=(PASS,))
    (store.directory / LOCK_FILE).unlink()
    assert store.prune(everything=True, dry_run=True).count == 1
    assert not (store.directory / LOCK_FILE).exists()
    assert store.entry_path(hexkey("a")).is_file()


def test_an_older_entry_listed_first_is_superseded(tmp_path: Path) -> None:
    store = ResultCache(tmp_path / "cache")
    first, second = sorted([hexkey("p"), hexkey("q")])
    now = time.time()
    for key, when in ((first, now - 100), (second, now)):
        store.store(key, task="tasks/a", runner="local", results=(PASS,))
        os.utime(store.entry_path(key), (when, when))
    pruned = store.prune(now=now, dry_run=True)
    assert [entry.path.stem for entry in pruned.removed["superseded"]] == [first]
    assert [entry.path.stem for entry in pruned.kept] == [second]


# --- committed_paths edge cases ---------------------------------------------------------


def test_committed_paths_through_a_symlinked_path_to_the_repository(
    repo: GitRepo, tmp_path: Path
) -> None:
    """The cache path may reach the work tree through a symlink (``/var`` versus
    ``/private/var`` on macOS); the tracked entries are still found."""
    repo.write("README.md", "# Tasks\n")
    repo.write(".taskgate/cache/entries/" + "d" * 64 + ".json", "{}")
    repo.git("add", "-f", ".taskgate")
    repo.commit("with a cache")
    alias = tmp_path / "alias"
    alias.symlink_to(repo.root)
    assert cache_module.committed_paths(alias / ".taskgate" / "cache") == [
        ".taskgate/cache/entries/" + "d" * 64 + ".json"
    ]


def test_committed_paths_of_the_work_tree_root_or_a_failed_listing_is_empty(
    repo: GitRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.write("README.md", "# Tasks\n")
    repo.commit("base")
    assert cache_module.committed_paths(repo.root) == []
    real = cache_module._git_stdout
    monkeypatch.setattr(
        cache_module, "_git_stdout", lambda *args: None if "ls-files" in args else real(*args)
    )
    assert cache_module.committed_paths(repo.root / "README.md") == []
