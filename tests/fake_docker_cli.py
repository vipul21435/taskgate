"""A stand-in for the Docker CLI, used by the unit tests through a ``docker`` shim on PATH.

It understands the handful of commands TaskGate sends and keeps its state in
``$FAKE_DOCKER_STATE``: ``images.json`` (tag -> build context, USER, labels) and
``calls.jsonl`` (every argv, one JSON list per line).

- ``version`` answers, fails like a stopped daemon when ``FAKE_DOCKER_DAEMON=down``,
  or hangs when it is ``hang``.
- ``build`` records the image (its ``USER`` is the last ``USER`` line of the
  Dockerfile); a Dockerfile containing ``FAKE_BUILD_FAIL`` fails the build,
  ``FAKE_DOCKER_BUILD=hang`` makes it hang and ``FAKE_DOCKER_BUILD=forget``
  reports success without keeping the image.
- ``image inspect`` prints ``<taskgate.context label>|<user>`` for a known image.
- ``run`` emulates the container on the host: the workdir is a temporary
  directory seeded from the build context's ``workspace/`` (as the Dockerfile's
  ``COPY workspace/`` would), the ``--tmpfs`` mount point becomes another
  temporary directory, and the entrypoint runs there with the ``--env`` values,
  this interpreter first on PATH and stdin passed through. ``FAKE_DOCKER_AS_ROOT=1``
  makes ``id -u`` print 0; ``FAKE_DOCKER_RUN_EXIT=N`` exits N at once, like a
  container that was killed before it printed anything.
- ``kill`` is only recorded.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

STATE = Path(os.environ["FAKE_DOCKER_STATE"])
IMAGES = STATE / "images.json"
CALLS = STATE / "calls.jsonl"
VALUE_FLAGS = frozenset(
    {
        "--name",
        "--label",
        "--network",
        "--cpus",
        "--memory",
        "--memory-swap",
        "--pids-limit",
        "--user",
        "--cap-drop",
        "--security-opt",
        "--tmpfs",
        "--env",
        "--workdir",
        "--entrypoint",
        "--tag",
        "--file",
        "--format",
    }
)
Flags = list[tuple[str, str | None]]


def load_images() -> dict[str, dict[str, object]]:
    return json.loads(IMAGES.read_text()) if IMAGES.exists() else {}


def parse(args: list[str]) -> tuple[Flags, list[str]]:
    """Leading flags (with their values) and the positional arguments after them."""
    flags: Flags = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in VALUE_FLAGS:
            flags.append((arg, args[index + 1]))
            index += 2
        elif arg.startswith("-"):
            flags.append((arg, None))
            index += 1
        else:
            break
    return flags, args[index:]


def values(flags: Flags, name: str) -> list[str]:
    return [value for flag, value in flags if flag == name and value is not None]


def version() -> int:
    if os.environ.get("FAKE_DOCKER_DAEMON") == "hang":
        time.sleep(60)
    if os.environ.get("FAKE_DOCKER_DAEMON") == "down":
        print("Cannot connect to the Docker daemon. Is the docker daemon running?", file=sys.stderr)
        return 1
    print("29.0.0-fake")
    return 0


def inspect(args: list[str]) -> int:
    _, (tag,) = parse(args)
    image = load_images().get(tag)
    if image is None:
        print(f"Error response from daemon: No such image: {tag}", file=sys.stderr)
        return 1
    labels = image["labels"]
    assert isinstance(labels, dict)
    print(f"{labels.get('taskgate.context', '')}|{image['user']}")
    return 0


def build(args: list[str]) -> int:
    flags, (context,) = parse(args)
    (tag,) = values(flags, "--tag")
    (dockerfile,) = values(flags, "--file")
    text = Path(dockerfile).read_text(encoding="utf-8")
    print('#0 building with "fake" instance')
    if os.environ.get("FAKE_DOCKER_BUILD") == "hang":
        time.sleep(60)
    if "FAKE_BUILD_FAIL" in text:
        print('#5 ERROR: process "/bin/sh -c exit 3" did not complete successfully: exit code: 3')
        print("ERROR: failed to build: failed to solve: exit code: 3")
        return 1
    if os.environ.get("FAKE_DOCKER_BUILD") == "forget":
        return 0
    user = ""
    for line in text.splitlines():
        if line.upper().startswith("USER "):
            user = line.split(None, 1)[1].strip()
    images = load_images()
    labels = dict(value.split("=", 1) for value in values(flags, "--label"))
    images[tag] = {"context": context, "user": user, "labels": labels}
    IMAGES.write_text(json.dumps(images), encoding="utf-8")
    print(f"#9 naming to {tag} done")
    return 0


def run(args: list[str]) -> int:
    flags, (image, *command) = parse(args)
    if "FAKE_DOCKER_RUN_EXIT" in os.environ:
        print("fake: the container was killed", file=sys.stderr)
        return int(os.environ["FAKE_DOCKER_RUN_EXIT"])
    images = load_images()
    if image not in images:
        print(f"Unable to find image '{image}' locally", file=sys.stderr)
        return 125
    mount = values(flags, "--tmpfs")[0].split(":", 1)[0]
    with tempfile.TemporaryDirectory(prefix="fake-container-") as tmp:
        root = Path(tmp)
        work, stage, bin_dir = root / "work", root / "stage", root / "bin"
        stage.mkdir()
        bin_dir.mkdir()
        seed = Path(str(images[image]["context"])) / "workspace"
        if seed.is_dir():
            shutil.copytree(seed, work)
        else:
            work.mkdir()
        if os.environ.get("FAKE_DOCKER_AS_ROOT"):
            fake_id = bin_dir / "id"
            fake_id.write_text("#!/bin/sh\necho 0\n", encoding="utf-8")
            fake_id.chmod(0o755)
        path = [str(bin_dir), str(Path(sys.executable).parent), "/usr/bin", "/bin"]
        env = {"PATH": os.pathsep.join(path), "HOME": str(root)}
        for assignment in values(flags, "--env"):
            key, _, value = assignment.partition("=")
            env[key] = value
        (entrypoint,) = values(flags, "--entrypoint")
        argv = [str(stage) if part == mount else part for part in command]
        return subprocess.run([entrypoint, *argv], cwd=work, env=env, check=False).returncode


def main() -> int:
    args = sys.argv[1:]
    with CALLS.open("a", encoding="utf-8") as log:
        log.write(json.dumps(args) + "\n")
    command = args[0]
    if command == "version":
        return version()
    if args[:2] == ["image", "inspect"]:
        return inspect(args[2:])
    if command == "build":
        return build(args[1:])
    if command == "run":
        return run(args[1:])
    if command == "kill":
        return 0
    print(f"fake docker: unsupported command {args}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
