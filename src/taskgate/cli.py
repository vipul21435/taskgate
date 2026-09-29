"""Command-line interface for TaskGate."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from taskgate import __version__
from taskgate.layout import TaskDir, find_tasks

app = typer.Typer(
    name="taskgate",
    help="Review gates for pull requests that add or change benchmark tasks.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def version() -> None:
    """Print the TaskGate version."""
    typer.echo(f"taskgate {__version__}")


def _task_json(task: TaskDir) -> dict[str, object]:
    return {
        "path": task.path.as_posix(),
        "complete": task.complete,
        "missing": list(task.missing),
    }


def _task_line(task: TaskDir) -> str:
    if task.complete:
        return f"ok       {task.path.as_posix()}"
    return f"missing  {task.path.as_posix()}  ({', '.join(task.missing)})"


@app.command()
def tasks(
    root: Annotated[
        Path,
        typer.Argument(help="Directory to scan for task directories (holding task.toml)."),
    ] = Path(),
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON instead of text.")] = False,
    strict: Annotated[
        bool, typer.Option("--strict", help="Exit with code 1 if any task is incomplete.")
    ] = False,
) -> None:
    """List the task directories under ROOT and the layout parts each one lacks."""
    try:
        found = find_tasks(root)
    except NotADirectoryError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    complete = sum(task.complete for task in found)
    if as_json:
        typer.echo(json.dumps([_task_json(task) for task in found], indent=2))
    else:
        for task in found:
            typer.echo(_task_line(task))
        typer.echo(f"{len(found)} task(s), {complete} complete")
    if strict and complete != len(found):
        raise typer.Exit(1)


def main() -> None:
    """Console-script entry point."""
    app()
