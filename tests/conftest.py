"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitrepo import GitRepo


@pytest.fixture
def repo(tmp_path: Path) -> GitRepo:
    return GitRepo(tmp_path / "repo")
