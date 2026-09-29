"""Find a task's Dockerfile inside its build context (``environment/``)."""

from __future__ import annotations

from pathlib import Path, PurePosixPath


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
