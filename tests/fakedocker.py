"""Put the fake Docker CLI (``fake_docker_cli.py``) on PATH and read what it recorded."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

FAKE_CLI = Path(__file__).with_name("fake_docker_cli.py")


@dataclass(frozen=True)
class FakeDocker:
    state: Path
    bin_dir: Path

    def calls(self, command: str | None = None) -> list[list[str]]:
        """Every recorded argv, or only those whose first word is ``command``."""
        log = self.state / "calls.jsonl"
        if not log.exists():
            return []
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        return [argv for argv in calls if command is None or argv[0] == command]

    def images(self) -> dict[str, Any]:
        path = self.state / "images.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    """Shadow any real ``docker`` with the fake for the rest of the test."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    shim = bin_dir / "docker"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLI}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    state = tmp_path / "fake-docker-state"
    state.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(state))
    for knob in (
        "FAKE_DOCKER_DAEMON",
        "FAKE_DOCKER_BUILD",
        "FAKE_DOCKER_AS_ROOT",
        "FAKE_DOCKER_RUN_EXIT",
    ):
        monkeypatch.delenv(knob, raising=False)
    return FakeDocker(state, bin_dir)
