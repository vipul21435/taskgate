"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from fakedocker import FakeDocker, install
from gitrepo import GitRepo

pytest_plugins = ("pytester",)


@pytest.fixture(autouse=True)
def local_runner_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI runs in tests use the local runner unless a test asks for another."""
    monkeypatch.setenv("TASKGATE_RUNNER", "local")


@pytest.fixture
def repo(tmp_path: Path) -> GitRepo:
    return GitRepo(tmp_path / "repo")


@pytest.fixture
def fake_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    """A fake ``docker`` first on PATH (see tests/fake_docker_cli.py)."""
    return install(tmp_path, monkeypatch)
