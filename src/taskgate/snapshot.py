"""Save a solved workspace and put it back before each TG501 rerun.

Rerun 1 grades the workspace ``solve.sh`` left behind. Every later rerun has to
see that same workspace, even when an earlier rerun's grader wrote into it.

:func:`save` records every entry under the workspace: its type, permission
bits, nanosecond atime and mtime, owner, device, inode and change time, a
symlink's target and which names share an inode (hard links). It copies the
content of each regular file (once per inode) into a store outside the
workspace.

:func:`restore` walks the workspace and puts back only what changed. Any
write, ``chmod``, rename or link-count change moves an inode's change time
(ctime), and nobody can set a ctime by hand. So an entry whose inode and ctime
are the recorded ones was not touched, and restore leaves it exactly as it is,
owner and inode included. An entry that changed, appeared or disappeared is
removed and recreated from the store with its recorded mode and times, and
its hard links are linked again. A directory is kept while its inode is the
same, and its mode and mtime are reset after its children. The workspace
directory itself is never replaced. A metadata change the run user is not
allowed to make is skipped for entries that user does not own, so a workdir
the user may write but does not own (``mkdir -m 777``) is fine.

The local runner imports this module. The Docker runner copies the file into
the container as ``taskgate_snapshot.py`` and runs ``python taskgate_snapshot.py
save|restore WORKDIR STORE``. The module uses only the standard library and
keeps to Python 3.8 syntax and APIs, because in a task image it runs under
whatever Python the image provides. Error messages name workspace-relative
paths only.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

MANIFEST = "manifest.json"
BLOBS = "blobs"
FORMAT = 1
USAGE = "usage: taskgate_snapshot.py save|restore WORKDIR STORE"
OWNER_ALL = 0o700
OWNER_READ_LIST = 0o500


class SnapshotError(Exception):
    """Saving or restoring failed; the message starts with a workspace-relative path."""


@dataclass
class Entry:
    """One workspace entry as :func:`save` found it."""

    name: str
    kind: str
    """``dir``, ``file``, ``link``, ``fifo`` or ``other`` (a socket or device file)."""

    mode: int
    atime: int
    mtime: int
    uid: int
    dev: int
    ino: int
    ctime: int
    blob: int | None = None
    """For a file, the store's copy of its content; ``None`` when it could not be read."""

    target: str | None = None
    children: list[Entry] = field(default_factory=list)


def _kind(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "link"
    return "fifo" if stat.S_ISFIFO(mode) else "other"


def _mine(st: os.stat_result) -> bool:
    return st.st_uid == os.geteuid()


def _join(rel: str, name: str) -> str:
    return f"{rel}/{name}" if rel else name


def _fail(rel: str, exc: OSError) -> SnapshotError:
    return SnapshotError(f"{rel or '.'}: {exc.strerror or exc}")


def _copy(src: str, dest: str) -> None:
    """Copy a regular file's bytes into a new file ``dest`` (mode 0600 until restored)."""
    with open(src, "rb") as reader:
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with open(fd, "wb") as writer:
            shutil.copyfileobj(reader, writer, 1 << 20)


def _entry(name: str, st: os.stat_result, **extra: Any) -> Entry:
    return Entry(
        name=name,
        kind=_kind(st.st_mode),
        mode=stat.S_IMODE(st.st_mode),
        atime=st.st_atime_ns,
        mtime=st.st_mtime_ns,
        uid=st.st_uid,
        dev=st.st_dev,
        ino=st.st_ino,
        ctime=st.st_ctime_ns,
        **extra,
    )


class _Saver:
    def __init__(self, store: str) -> None:
        self.blobs = os.path.join(store, BLOBS)
        self.inodes: dict[tuple[int, int], int | None] = {}
        self.count = 0

    def entry(self, path: str, rel: str, name: str) -> Entry:
        try:
            st = os.lstat(path)
            kind = _kind(st.st_mode)
            if kind == "dir":
                children = self.children(path, rel, st)
                return _entry(name, os.lstat(path), children=children)
            if kind == "file":
                blob = self.content(path, st)
                return _entry(name, os.lstat(path), blob=blob)
            if kind == "link":
                return _entry(name, st, target=os.readlink(path))
            return _entry(name, st)
        except OSError as exc:
            raise _fail(rel, exc) from exc

    def children(self, path: str, rel: str, st: os.stat_result) -> list[Entry]:
        """Every entry in a directory, sorted; an own directory is opened up meanwhile."""
        mode = stat.S_IMODE(st.st_mode)
        locked = _mine(st) and mode & OWNER_READ_LIST != OWNER_READ_LIST
        if locked:
            os.chmod(path, mode | OWNER_READ_LIST)
        try:
            return [
                self.entry(os.path.join(path, name), _join(rel, name), name)
                for name in sorted(os.listdir(path))
            ]
        finally:
            if locked:
                os.chmod(path, mode)

    def content(self, path: str, st: os.stat_result) -> int | None:
        """Store the file's bytes once per inode; ``None`` when the run user cannot read it."""
        key = (st.st_dev, st.st_ino)
        if key in self.inodes:
            return self.inodes[key]
        blob: int | None = self.count
        self.count += 1
        dest = os.path.join(self.blobs, str(blob))
        try:
            _copy(path, dest)
        except PermissionError:
            if not _mine(st):
                blob = None
            else:
                mode = stat.S_IMODE(st.st_mode)
                os.chmod(path, mode | stat.S_IRUSR)
                try:
                    _copy(path, dest)
                finally:
                    os.chmod(path, mode)
        self.inodes[key] = blob
        return blob


def save(workdir: str | os.PathLike[str], store: str | os.PathLike[str]) -> None:
    """Record ``workdir`` in ``store`` (a new directory outside ``workdir``)."""
    root, where = os.fspath(workdir), os.fspath(store)
    try:
        os.makedirs(os.path.join(where, BLOBS))
    except OSError as exc:
        raise SnapshotError(f"cannot create the snapshot store: {exc.strerror or exc}") from exc
    tree = _Saver(where).entry(root, "", "")
    data = {"format": FORMAT, "root": asdict(tree)}
    try:
        with open(os.path.join(where, MANIFEST), "w", encoding="utf-8") as handle:
            json.dump(data, handle)
    except OSError as exc:
        raise SnapshotError(f"cannot write the snapshot manifest: {exc.strerror or exc}") from exc


def _load_entry(data: dict[str, Any]) -> Entry:
    fields = dict(data)
    fields["children"] = [_load_entry(child) for child in data.get("children", [])]
    return Entry(**fields)


def load(store: str | os.PathLike[str]) -> Entry:
    """The workspace tree :func:`save` recorded in ``store``."""
    try:
        with open(os.path.join(os.fspath(store), MANIFEST), encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise SnapshotError(f"cannot read the snapshot manifest: {exc.strerror or exc}") from exc
    except ValueError as exc:
        raise SnapshotError("the snapshot manifest is not JSON") from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise SnapshotError("the snapshot manifest has an unknown format")
    try:
        return _load_entry(data["root"])
    except (KeyError, TypeError, AttributeError) as exc:
        raise SnapshotError("the snapshot manifest has an unknown format") from exc


def _unchanged(entry: Entry, st: os.stat_result) -> bool:
    """True when the entry at this name is the recorded inode and (unless a
    directory) nothing changed it since."""
    if _kind(st.st_mode) != entry.kind or (st.st_dev, st.st_ino) != (entry.dev, entry.ino):
        return False
    return entry.kind == "dir" or st.st_ctime_ns == entry.ctime


class _Restorer:
    def __init__(self, store: str) -> None:
        self.blobs = os.path.join(store, BLOBS)
        self.linked: dict[int, str] = {}
        """Blob -> a path already holding that inode in this pass (for hard links)."""

    def sync_dir(self, path: str, rel: str, entry: Entry) -> None:
        """Make an existing directory hold exactly the recorded children, then reset it."""
        try:
            st = os.lstat(path)
            mode = stat.S_IMODE(st.st_mode)
            if _mine(st) and mode & OWNER_ALL != OWNER_ALL:
                os.chmod(path, mode | OWNER_ALL)
            expected = {child.name for child in entry.children}
            extra = sorted(set(os.listdir(path)) - expected)
        except OSError as exc:
            raise _fail(rel, exc) from exc
        for name in extra:
            self.remove(os.path.join(path, name), _join(rel, name))
        for child in entry.children:
            self.sync(os.path.join(path, child.name), _join(rel, child.name), child)
        self.reset(path, rel, entry)

    def sync(self, path: str, rel: str, entry: Entry) -> None:
        try:
            try:
                st: os.stat_result | None = os.lstat(path)
            except FileNotFoundError:
                st = None
            if st is not None and _unchanged(entry, st):
                if entry.kind == "dir":
                    self.sync_dir(path, rel, entry)
                elif entry.blob is not None:
                    self.linked.setdefault(entry.blob, path)
                return
            if st is not None:
                self.remove(path, rel)
            self.create(path, rel, entry)
        except OSError as exc:
            raise _fail(rel, exc) from exc

    def create(self, path: str, rel: str, entry: Entry) -> None:
        if entry.kind == "dir":
            os.mkdir(path, OWNER_ALL)
            self.sync_dir(path, rel, entry)
            return
        if entry.kind == "file":
            if entry.blob is None:
                raise SnapshotError(f"{rel}: not saved, since the run user could not read it")
            first = self.linked.get(entry.blob)
            if first is not None:
                os.link(first, path)
                return
            _copy(os.path.join(self.blobs, str(entry.blob)), path)
            self.linked[entry.blob] = path
        elif entry.kind == "link":
            os.symlink(entry.target or "", path)
        elif entry.kind == "fifo":
            os.mkfifo(path, 0o600)
        else:
            raise SnapshotError(f"{rel}: a socket or device file cannot be restored")
        self.reset(path, rel, entry)

    def reset(self, path: str, rel: str, entry: Entry) -> None:
        """Give an entry its recorded mode and times, skipping what a non-owner cannot set."""
        try:
            st = os.lstat(path)
            link = entry.kind == "link"
            try:
                if not link and stat.S_IMODE(st.st_mode) != entry.mode:
                    os.chmod(path, entry.mode)
                if st.st_mtime_ns != entry.mtime and (
                    not link or os.utime in os.supports_follow_symlinks
                ):
                    os.utime(path, ns=(entry.atime, entry.mtime), follow_symlinks=not link)
            except PermissionError:
                if _mine(st):
                    raise
        except OSError as exc:
            raise _fail(rel, exc) from exc

    def remove(self, path: str, rel: str) -> None:
        try:
            st = os.lstat(path)
            if not stat.S_ISDIR(st.st_mode):
                os.unlink(path)
                return
            mode = stat.S_IMODE(st.st_mode)
            if _mine(st) and mode & OWNER_ALL != OWNER_ALL:
                os.chmod(path, mode | OWNER_ALL)
            names = sorted(os.listdir(path))
        except OSError as exc:
            raise _fail(rel, exc) from exc
        for name in names:
            self.remove(os.path.join(path, name), _join(rel, name))
        try:
            os.rmdir(path)
        except OSError as exc:
            raise _fail(rel, exc) from exc


def restore(workdir: str | os.PathLike[str], store: str | os.PathLike[str]) -> None:
    """Put ``workdir`` back to what :func:`save` recorded in ``store``."""
    tree = load(store)
    _Restorer(os.fspath(store)).sync_dir(os.fspath(workdir), "", tree)


def main(argv: list[str]) -> int:
    """``save|restore WORKDIR STORE``: exit 0, or print the problem and exit 1 (2: usage)."""
    if len(argv) != 3 or argv[0] not in ("save", "restore"):
        print(USAGE)
        return 2
    command, workdir, store = argv
    try:
        (save if command == "save" else restore)(workdir, store)
    except SnapshotError as exc:
        print(exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
