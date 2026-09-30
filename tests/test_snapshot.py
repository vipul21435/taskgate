"""The workspace snapshot behind TG501's reruns: put back exactly what a rerun changed."""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import shutil
import socket
import stat
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from taskgate import snapshot
from taskgate.snapshot import SnapshotError, load, main, restore, save

Fingerprint = dict[str, tuple[Any, ...]]


def fingerprint(root: Path) -> Fingerprint:
    """Everything a grader could observe about each entry, except inode numbers; hard
    links show up as the sorted names sharing an inode."""
    inodes: dict[tuple[int, int], list[str]] = {}
    found: dict[str, tuple[Any, ...]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in [*dirnames, *filenames]:
            path = Path(dirpath, name)
            rel = path.relative_to(root).as_posix()
            st = path.lstat()
            mode = stat.S_IMODE(st.st_mode)
            if stat.S_ISLNK(st.st_mode):
                found[rel] = ("link", str(path.readlink()), st.st_mtime_ns)
            elif stat.S_ISREG(st.st_mode):
                inodes.setdefault((st.st_dev, st.st_ino), []).append(rel)
                path.chmod(mode | stat.S_IRUSR)
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                path.chmod(mode)
                found[rel] = ("file", mode, st.st_mtime_ns, digest, (st.st_dev, st.st_ino))
            else:
                kind = "dir" if stat.S_ISDIR(st.st_mode) else "fifo"
                found[rel] = (kind, mode, st.st_mtime_ns)
    for rel, value in found.items():
        if value[0] == "file":
            found[rel] = (*value[:4], tuple(sorted(inodes[value[4]])))
    return found


def build_workspace(root: Path) -> None:
    """A workspace with every kind of entry a solution can leave behind."""
    (root / "output" / "shared").mkdir(parents=True)
    (root / "output" / "greeting.txt").write_text("world\n", encoding="utf-8")
    (root / "output" / "greeting.txt").chmod(0o664)
    os.link(root / "output" / "greeting.txt", root / "output" / "backup.txt")
    (root / "output" / "shared").chmod(0o1777)
    (root / "output" / "locked.txt").write_text("token\n", encoding="utf-8")
    (root / "output" / "locked.txt").chmod(0o000)
    os.mkfifo(root / "output" / "jobs")
    (root / "latest").symlink_to("output/greeting.txt")
    frozen = root / "frozen"
    frozen.mkdir()
    (frozen / "data.txt").write_text("fixed\n", encoding="utf-8")
    (frozen / "data.txt").chmod(0o666)
    frozen.chmod(0o555)
    # Sub-second mtimes a whole-second archive would lose.
    os.utime(root / "output" / "greeting.txt", ns=(1_700_000_000_300_000_000,) * 2)
    os.utime(frozen / "data.txt", ns=(1_700_000_000_700_000_000,) * 2)


def open_up(root: Path) -> None:
    """Let the test's own cleanup delete what the workspace locked."""
    for dirpath, dirnames, _ in os.walk(root):
        for name in dirnames:
            Path(dirpath, name).chmod(0o755)


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "workspace"
    root.mkdir()
    build_workspace(root)
    yield root
    root.chmod(0o755)
    open_up(root)


def mutate(root: Path) -> None:
    """What a careless grader might do to the workspace between reruns."""
    (root / "output" / "greeting.txt").write_text("changed\n", encoding="utf-8")
    (root / "output" / "locked.txt").chmod(0o644)
    (root / "output" / "jobs").unlink()
    (root / "latest").unlink()
    (root / "latest").symlink_to("elsewhere")
    (root / "frozen").chmod(0o755)
    (root / "frozen" / "data.txt").write_text("edited\n", encoding="utf-8")
    (root / "frozen").chmod(0o500)
    (root / "output" / "shared").chmod(0o700)
    extra = root / "output" / "extra"
    (extra / "deep").mkdir(parents=True)
    (extra / "deep" / "file.txt").write_text("x", encoding="utf-8")
    extra.chmod(0o000)
    (root / "graded.marker").write_text("x", encoding="utf-8")


def test_restore_puts_back_every_change_exactly(workspace: Path, tmp_path: Path) -> None:
    before = fingerprint(workspace)
    save(workspace, tmp_path / "store")
    mutate(workspace)
    assert fingerprint(workspace) != before
    restore(workspace, tmp_path / "store")
    assert fingerprint(workspace) == before
    assert before["output/greeting.txt"][1:4] == (
        0o664,
        1_700_000_000_300_000_000,
        hashlib.sha256(b"world\n").hexdigest(),
    )
    assert before["output/greeting.txt"][4] == ("output/backup.txt", "output/greeting.txt")
    assert before["output/shared"] == ("dir", 0o1777, before["output/shared"][2])
    assert before["output/jobs"][0] == "fifo"
    restore(workspace, tmp_path / "store")
    assert fingerprint(workspace) == before


def test_untouched_entries_keep_their_inode_and_ctime(workspace: Path, tmp_path: Path) -> None:
    def identities() -> dict[str, tuple[int, int]]:
        return {
            str(path.relative_to(workspace)): (path.lstat().st_ino, path.lstat().st_ctime_ns)
            for path in workspace.rglob("*")
            if not path.is_dir() or path.is_symlink()
        }

    save(workspace, tmp_path / "store")
    before = identities()
    restore(workspace, tmp_path / "store")
    assert identities() == before
    (workspace / "output" / "greeting.txt").write_text("changed\n", encoding="utf-8")
    restore(workspace, tmp_path / "store")
    after = identities()
    assert after["latest"] == before["latest"]
    assert after["frozen/data.txt"] == before["frozen/data.txt"]
    assert after["output/greeting.txt"] != before["output/greeting.txt"]
    assert after["output/greeting.txt"][0] == after["output/backup.txt"][0]


def test_a_workdir_the_user_cannot_retime_is_still_restored(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review's case: a ``mkdir -m 777`` workdir owned by root. Its own mode and
    times cannot be reset by the run user, and that must not stop the restore."""
    save(workspace, tmp_path / "store")
    before = fingerprint(workspace)
    (workspace / "graded.marker").write_text("x", encoding="utf-8")
    root_ino = workspace.lstat().st_ino
    real_mine, real_utime = snapshot._mine, os.utime

    def utime(path: Any, *args: Any, **kwargs: Any) -> None:
        if os.lstat(path).st_ino == root_ino:
            raise PermissionError(1, "Operation not permitted")
        real_utime(path, *args, **kwargs)

    monkeypatch.setattr(snapshot, "_mine", lambda st: st.st_ino != root_ino and real_mine(st))
    monkeypatch.setattr(os, "utime", utime)
    restore(workspace, tmp_path / "store")
    monkeypatch.undo()
    assert fingerprint(workspace) == before
    assert not (workspace / "graded.marker").exists()


def test_a_metadata_error_on_an_own_entry_is_reported(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save(workspace, tmp_path / "store")
    (workspace / "output" / "greeting.txt").write_text("changed\n", encoding="utf-8")

    def utime(*args: Any, **kwargs: Any) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "utime", utime)
    with pytest.raises(SnapshotError) as caught:
        restore(workspace, tmp_path / "store")
    monkeypatch.undo()
    assert str(caught.value) == "output/backup.txt: Operation not permitted"


@contextmanager
def failing(name: str, target: str, errno: int, text: str) -> Iterator[None]:
    """Make ``os.<name>`` fail for paths ending in ``target``, and only those."""
    real: Callable[..., Any] = getattr(os, name)

    def fake(*args: Any, **kwargs: Any) -> Any:
        if any(str(arg).endswith(target) for arg in args) and "dir_fd" not in kwargs:
            raise OSError(errno, text)
        return real(*args, **kwargs)

    setattr(os, name, fake)
    try:
        yield
    finally:
        setattr(os, name, real)


def test_errors_name_workspace_relative_paths(workspace: Path, tmp_path: Path) -> None:
    save(workspace, tmp_path / "store")
    (workspace / "output" / "new.txt").write_text("x", encoding="utf-8")
    with (
        failing("unlink", "new.txt", 1, "Operation not permitted"),
        pytest.raises(SnapshotError) as caught,
    ):
        restore(workspace, tmp_path / "store")
    assert str(caught.value) == "output/new.txt: Operation not permitted"
    (workspace / "output" / "extra").mkdir()
    with failing("rmdir", "extra", 13, "Permission denied"), pytest.raises(SnapshotError) as caught:
        restore(workspace, tmp_path / "store")
    assert str(caught.value) == "output/extra: Permission denied"
    with (
        failing("listdir", "output", 13, "Permission denied"),
        pytest.raises(SnapshotError) as caught,
    ):
        restore(workspace, tmp_path / "store")
    assert str(caught.value) == "output: Permission denied"
    (workspace / "output" / "extra").mkdir(exist_ok=True)
    (workspace / "output" / "extra" / "x").write_text("x", encoding="utf-8")
    with (
        failing("listdir", "extra", 13, "Permission denied"),
        pytest.raises(SnapshotError) as caught,
    ):
        restore(workspace, tmp_path / "store")
    assert str(caught.value) == "output/extra: Permission denied"
    (workspace / "latest").unlink()
    with (
        failing("symlink", "latest", 28, "No space left on device"),
        pytest.raises(SnapshotError) as caught,
    ):
        restore(workspace, tmp_path / "store")
    assert str(caught.value) == "latest: No space left on device"


def test_what_the_run_user_cannot_read_or_list(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Entries of another owner: an unreadable file is fine until it has to be restored;
    an unreadable directory stops the save."""
    real = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: real + 1)
    save(workspace, tmp_path / "store")
    (output,) = [entry for entry in load(tmp_path / "store").children if entry.name == "output"]
    (locked,) = [entry for entry in output.children if entry.name == "locked.txt"]
    assert locked.blob is None
    restore(workspace, tmp_path / "store")
    (workspace / "output" / "locked.txt").chmod(0o600)
    with pytest.raises(SnapshotError) as caught:
        restore(workspace, tmp_path / "store")
    assert str(caught.value) == (
        "output/locked.txt: not saved, since the run user could not read it"
    )
    (workspace / "output" / "hidden").mkdir(mode=0o000)
    with pytest.raises(SnapshotError) as caught:
        save(workspace, tmp_path / "store2")
    monkeypatch.undo()
    (workspace / "output" / "hidden").chmod(0o755)
    assert str(caught.value) == "output/hidden: Permission denied"


def test_sockets_are_kept_but_cannot_be_recreated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.chdir(root)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind("sock")
    listener.close()
    save(root, tmp_path / "store")
    restore(root, tmp_path / "store")
    assert stat.S_ISSOCK((root / "sock").lstat().st_mode)
    (root / "sock").unlink()
    with pytest.raises(SnapshotError) as caught:
        restore(root, tmp_path / "store")
    assert str(caught.value) == "sock: a socket or device file cannot be restored"


def test_store_and_manifest_problems(workspace: Path, tmp_path: Path) -> None:
    taken = tmp_path / "taken"
    (taken / "blobs").mkdir(parents=True)
    with pytest.raises(SnapshotError, match=r"^cannot create the snapshot store: File exists$"):
        save(workspace, taken)
    with pytest.raises(SnapshotError) as caught:
        restore(workspace, taken)
    assert str(caught.value) == "cannot read the snapshot manifest: No such file or directory"
    for text, problem in [
        ("{", "the snapshot manifest is not JSON"),
        ("[]", "the snapshot manifest has an unknown format"),
        (json.dumps({"format": 99}), "the snapshot manifest has an unknown format"),
        (json.dumps({"format": 1}), "the snapshot manifest has an unknown format"),
        (
            json.dumps({"format": 1, "root": {"name": ""}}),
            "the snapshot manifest has an unknown format",
        ),
    ]:
        (taken / "manifest.json").write_text(text, "utf-8")
        with pytest.raises(SnapshotError) as caught:
            load(taken)
        assert str(caught.value) == problem


def test_a_manifest_that_cannot_be_written(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full(*args: Any, **kwargs: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(json, "dump", full)
    with pytest.raises(SnapshotError) as caught:
        save(workspace, tmp_path / "store")
    assert str(caught.value) == "cannot write the snapshot manifest: No space left on device"


def test_names_that_are_not_utf8_round_trip(tmp_path: Path) -> None:
    if sys.platform == "darwin":
        pytest.skip("APFS rejects file names that are not valid UTF-8")
    root = tmp_path / "workspace"
    root.mkdir()
    name = os.fsdecode(b"caf\xe9.txt")
    (root / name).write_text("x", encoding="utf-8")
    save(root, tmp_path / "store")
    (root / name).write_text("changed", encoding="utf-8")
    restore(root, tmp_path / "store")
    assert (root / name).read_text(encoding="utf-8") == "x"


def test_the_command_line(
    workspace: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = str(tmp_path / "store")
    assert main(["save", str(workspace), store]) == 0
    (workspace / "graded.marker").write_text("x", encoding="utf-8")
    assert main(["restore", str(workspace), store]) == 0
    assert not (workspace / "graded.marker").exists()
    assert main(["save", str(workspace), store]) == 1
    assert main(["copy", "a", "b"]) == 2
    assert capsys.readouterr().out.splitlines() == [
        "cannot create the snapshot store: File exists",
        snapshot.USAGE,
    ]


def test_the_module_runs_as_a_script(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["taskgate_snapshot.py", "save", str(workspace), "x"])
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exited:
        runpy.run_path(snapshot.__file__, run_name="__main__")
    assert exited.value.code == 0
    assert (tmp_path / "x" / "manifest.json").is_file()


def test_an_own_locked_directory_is_saved_and_a_removed_one_recreated(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    (root / "private" / "sub").mkdir(parents=True)
    (root / "private" / "sub" / "notes.txt").write_text("kept\n", encoding="utf-8")
    (root / "private").chmod(0o000)
    save(root, tmp_path / "store")
    assert stat.S_IMODE((root / "private").stat().st_mode) == 0
    (root / "private").chmod(0o755)
    shutil.rmtree(root / "private")
    restore(root, tmp_path / "store")
    assert stat.S_IMODE((root / "private").stat().st_mode) == 0
    (root / "private").chmod(0o755)
    assert (root / "private" / "sub" / "notes.txt").read_text(encoding="utf-8") == "kept\n"
