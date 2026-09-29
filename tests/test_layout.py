from pathlib import Path, PurePosixPath

import pytest

from taskgate.layout import TaskDir, find_tasks, missing_parts


def make_task(task_dir: Path, *, skip: tuple[str, ...] = ()) -> Path:
    task_dir.mkdir(parents=True, exist_ok=True)
    for name in ("task.toml", "instruction.md"):
        if name not in skip:
            (task_dir / name).write_text("x\n", encoding="utf-8")
    for name in ("environment", "solution", "tests"):
        if name not in skip:
            (task_dir / name).mkdir(exist_ok=True)
    return task_dir


def test_complete_task_has_no_missing_parts(tmp_path: Path) -> None:
    assert missing_parts(make_task(tmp_path / "t")) == ()


def test_missing_parts_lists_files_then_directories(tmp_path: Path) -> None:
    task = make_task(tmp_path / "t", skip=("instruction.md", "solution", "tests"))
    assert missing_parts(task) == ("instruction.md", "solution/", "tests/")


def test_a_file_where_a_directory_is_required_counts_as_missing(tmp_path: Path) -> None:
    task = make_task(tmp_path / "t", skip=("tests",))
    (task / "tests").write_text("not a directory\n", encoding="utf-8")
    assert missing_parts(task) == ("tests/",)


def test_find_tasks_sorts_and_reports_each_task(tmp_path: Path) -> None:
    make_task(tmp_path / "tasks" / "zeta")
    make_task(tmp_path / "tasks" / "alpha", skip=("solution",))
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "notes.md").write_text("no task here\n", encoding="utf-8")

    found = find_tasks(tmp_path)

    assert found == [
        TaskDir(path=PurePosixPath("tasks/alpha"), missing=("solution/",)),
        TaskDir(path=PurePosixPath("tasks/zeta"), missing=()),
    ]
    assert [task.name for task in found] == ["alpha", "zeta"]
    assert [task.complete for task in found] == [False, True]


def test_nested_manifest_inside_a_task_is_not_a_new_task(tmp_path: Path) -> None:
    outer = make_task(tmp_path / "outer")
    make_task(outer / "tests" / "fixtures" / "inner")
    assert [task.path.as_posix() for task in find_tasks(tmp_path)] == ["outer"]


def test_hidden_and_tool_directories_are_skipped(tmp_path: Path) -> None:
    make_task(tmp_path / ".git" / "hidden-task")
    make_task(tmp_path / "node_modules" / "pkg")
    make_task(tmp_path / "real")
    assert [task.path.as_posix() for task in find_tasks(tmp_path)] == ["real"]


def test_root_itself_can_be_a_task(tmp_path: Path) -> None:
    make_task(tmp_path)
    (found,) = find_tasks(tmp_path)
    assert found.path.as_posix() == "."
    assert found.name == "."
    assert found.complete


def test_empty_tree_has_no_tasks(tmp_path: Path) -> None:
    assert find_tasks(tmp_path) == []


def test_find_tasks_rejects_a_file_root(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("x\n", encoding="utf-8")
    with pytest.raises(NotADirectoryError, match="not a directory"):
        find_tasks(target)
