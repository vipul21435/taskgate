from pathlib import Path, PurePosixPath

import pytest

from gitrepo import GitRepo
from taskgate import changes
from taskgate.changes import (
    ChangedTask,
    GitError,
    changed_tasks,
    owning_task,
    repo_root,
    resolve_base,
    task_roots,
)

TASK_FILES = ("instruction.md", "task.toml", "tests/test_x.py")


def add_task(repo: GitRepo, path: str) -> None:
    repo.write(f"{path}/task.toml", "[task]\n")
    repo.write(f"{path}/instruction.md", "Do it.\n")
    repo.write(f"{path}/tests/test_x.py", "def test_x() -> None:\n    pass\n")


def test_task_roots_keeps_outermost_and_skips_hidden_dirs() -> None:
    roots = task_roots(
        [
            "tasks/a/task.toml",
            "tasks/a/tests/fixtures/inner/task.toml",
            "tasks/b/task.toml",
            "tasks/b/README.md",
            ".github/templates/task.toml",
            "node_modules/pkg/task.toml",
            "task.toml.bak",
        ]
    )
    assert roots == frozenset({PurePosixPath("tasks/a"), PurePosixPath("tasks/b")})


def test_task_roots_accepts_a_root_level_task() -> None:
    assert task_roots(["task.toml", "sub/task.toml"]) == frozenset({PurePosixPath(".")})


def test_owning_task_returns_outermost_match_or_none() -> None:
    roots = frozenset({PurePosixPath("tasks/a")})
    assert owning_task("tasks/a/tests/test_x.py", roots) == PurePosixPath("tasks/a")
    assert owning_task("tasks/a", roots) is None
    assert owning_task("README.md", roots) is None


def test_added_modified_and_removed_tasks(repo: GitRepo) -> None:
    add_task(repo, "tasks/kept")
    add_task(repo, "tasks/dropped")
    add_task(repo, "tasks/untouched")
    repo.commit("base")
    repo.branch("pr")
    repo.write("tasks/kept/instruction.md", "Do it better.\n")
    repo.remove("tasks/dropped")
    add_task(repo, "tasks/new")
    repo.write("README.md", "# Repo\n")
    repo.commit("pr")

    result = changed_tasks(repo.root, "main")

    assert result.base == "main"
    assert result.head == "HEAD"
    assert result.other_files == ("README.md",)
    assert result.tasks == (
        ChangedTask(
            path=PurePosixPath("tasks/dropped"),
            change="removed",
            files=(
                "tasks/dropped/instruction.md",
                "tasks/dropped/task.toml",
                "tasks/dropped/tests/test_x.py",
            ),
        ),
        ChangedTask(
            path=PurePosixPath("tasks/kept"),
            change="modified",
            files=("tasks/kept/instruction.md",),
            tracked=TASK_FILES,
        ),
        ChangedTask(
            path=PurePosixPath("tasks/new"),
            change="added",
            files=(
                "tasks/new/instruction.md",
                "tasks/new/task.toml",
                "tasks/new/tests/test_x.py",
            ),
            tracked=TASK_FILES,
        ),
    )


def test_tracked_lists_every_committed_file_whatever_its_name(repo: GitRepo) -> None:
    repo.commit("base")
    repo.branch("pr")
    add_task(repo, "tasks/new")
    repo.write("tasks/new/solution/__pycache__/notes.txt", "committed\n")
    repo.write("tasks/new/.DS_Store", "committed\n")
    repo.write("tasks/new/inner/task.toml", "[task]\n")
    repo.write("tasks/new-sibling.txt", "not in the task\n")
    repo.git("add", "-A", "-f")
    repo.commit("pr")
    repo.write("tasks/new/untracked.txt", "not committed\n")

    (task,) = changed_tasks(repo.root, "main").tasks

    assert task.tracked == (
        ".DS_Store",
        "inner/task.toml",
        "instruction.md",
        "solution/__pycache__/notes.txt",
        "task.toml",
        "tests/test_x.py",
    )


def test_commits_on_the_base_after_the_fork_are_not_attributed(repo: GitRepo) -> None:
    add_task(repo, "tasks/a")
    repo.commit("base")
    repo.branch("pr")
    add_task(repo, "tasks/mine")
    repo.commit("pr adds a task")
    repo.checkout("main")
    add_task(repo, "tasks/landed-later")
    repo.commit("main moves on")
    repo.checkout("pr")

    result = changed_tasks(repo.root, "main")

    assert [task.path.as_posix() for task in result.tasks] == ["tasks/mine"]
    assert result.merge_base == repo.git("merge-base", "main", "pr").strip()


def test_rename_shows_as_removed_plus_added(repo: GitRepo) -> None:
    add_task(repo, "tasks/old-name")
    repo.commit("base")
    repo.branch("pr")
    repo.git("mv", "tasks/old-name", "tasks/new-name")
    repo.commit("rename")

    result = changed_tasks(repo.root, "main")

    assert [(t.path.as_posix(), t.change) for t in result.tasks] == [
        ("tasks/new-name", "added"),
        ("tasks/old-name", "removed"),
    ]


def test_no_changes_gives_an_empty_change_set(repo: GitRepo) -> None:
    add_task(repo, "tasks/a")
    repo.commit("base")
    result = changed_tasks(repo.root, "main")
    assert result.tasks == ()
    assert result.other_files == ()


def test_default_base_falls_back_from_origin_main_to_main(repo: GitRepo) -> None:
    repo.commit("base")
    assert resolve_base(repo.root, None) == "main"
    repo.git("update-ref", "refs/remotes/origin/main", "HEAD")
    assert resolve_base(repo.root, None) == "origin/main"


def test_unknown_base_and_head_are_errors(repo: GitRepo) -> None:
    repo.commit("base")
    with pytest.raises(GitError, match="base ref not found: nope"):
        resolve_base(repo.root, "nope")
    with pytest.raises(GitError, match="head ref not found: nope"):
        changed_tasks(repo.root, "main", head="nope")


def test_repo_root_from_a_subdirectory(repo: GitRepo) -> None:
    repo.write("a/b/c.txt")
    assert repo_root(repo.root / "a" / "b").resolve() == repo.root.resolve()


def test_not_a_repository_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(GitError, match="rev-parse"):
        repo_root(tmp_path)


def test_missing_git_binary_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_git(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(changes.subprocess, "run", no_git)
    with pytest.raises(GitError, match="not installed"):
        repo_root(tmp_path)
