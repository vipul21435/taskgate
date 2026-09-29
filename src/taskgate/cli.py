"""Command-line interface for TaskGate."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from taskgate import __version__
from taskgate.changes import GitError, changed_tasks, repo_root
from taskgate.engine import run_gates
from taskgate.layout import TaskDir, find_tasks
from taskgate.registry import (
    GROUPS,
    Registry,
    RegistryError,
    group_label,
    group_of,
    load_registry,
)
from taskgate.report import to_json, to_markdown, to_text
from taskgate.results import CheckReport, TaskReport

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


class OutputFormat(StrEnum):
    TEXT = "text"
    MARKDOWN = "markdown"
    JSON = "json"


RENDERERS = {
    OutputFormat.TEXT: to_text,
    OutputFormat.MARKDOWN: to_markdown,
    OutputFormat.JSON: to_json,
}


def _fail_usage(message: str) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(2)


def _registry() -> Registry:
    try:
        return load_registry()
    except RegistryError as exc:
        raise _fail_usage(str(exc)) from exc


def _check_all(root: Path) -> CheckReport:
    try:
        found = find_tasks(root)
    except NotADirectoryError as exc:
        raise _fail_usage(str(exc)) from exc
    gates = _registry().gates
    tasks = tuple(
        TaskReport(
            path=task.path.as_posix(),
            change=None,
            results=run_gates(root / task.path, gates=gates),
        )
        for task in found
    )
    return CheckReport(version=__version__, mode="all", tasks=tasks)


def _check_diff(repo: Path, base: str | None) -> CheckReport:
    try:
        root = repo_root(repo)
        changes = changed_tasks(root, base)
    except GitError as exc:
        raise _fail_usage(str(exc)) from exc
    gates = _registry().gates
    tasks = tuple(
        TaskReport(
            path=task.path.as_posix(),
            change=task.change,
            results=() if task.change == "removed" else run_gates(root / task.path, gates=gates),
            changed_files=task.files,
        )
        for task in changes.tasks
    )
    return CheckReport(
        version=__version__,
        mode="diff",
        base=changes.base,
        merge_base=changes.merge_base,
        tasks=tasks,
        other_files=changes.other_files,
    )


@app.command()
def check(
    repo: Annotated[
        Path,
        typer.Argument(help="Repository (diff mode) or directory (--all) to check."),
    ] = Path(),
    base: Annotated[
        str | None,
        typer.Option(help="Base ref to diff against. Default: origin/main, else main."),
    ] = None,
    all_tasks: Annotated[
        bool,
        typer.Option("--all", help="Check every task under REPO instead of the changed ones."),
    ] = False,
    out: Annotated[
        Path | None,
        typer.Option(help="Directory to write report.md and report.json into."),
    ] = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help="What to print on stdout.")
    ] = OutputFormat.TEXT,
) -> None:
    """Run the review gates on the tasks a pull request changes.

    Exits 0 when no blocking gate fails, 1 when one does, and 2 on usage errors.
    """
    report = _check_all(repo.resolve()) if all_tasks else _check_diff(repo, base)
    typer.echo(RENDERERS[output_format](report), nl=False)
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.md").write_text(to_markdown(report), encoding="utf-8")
        (out / "report.json").write_text(to_json(report), encoding="utf-8")
        typer.echo(f"wrote {out / 'report.md'} and {out / 'report.json'}", err=True)
    if not report.passed:
        raise typer.Exit(1)


def _gate_json(registry: Registry) -> list[dict[str, object]]:
    return [
        {
            "code": entry.gate.code,
            "name": entry.gate.name,
            "group": GROUPS[group_of(entry.gate.code)],
            "severity": entry.gate.severity.value,
            "summary": entry.gate.summary,
            "fix_hint": entry.gate.fix_hint,
            "requires": list(entry.gate.requires),
            "source": entry.source,
        }
        for entry in registry.entries
    ]


def _gate_lines(registry: Registry) -> list[str]:
    entries = registry.entries
    name_width = max(len(entry.gate.name) for entry in entries)
    lines: list[str] = []
    group = 0
    for entry in entries:
        gate = entry.gate
        if group_of(gate.code) != group:
            group = group_of(gate.code)
            lines.append(group_label(group))
        source = "" if entry.builtin else f"  [{entry.source}]"
        lines.append(
            f"  {gate.code}  {gate.severity.value:<7}  {gate.name:<{name_width}}  "
            f"{gate.summary}{source}"
        )
    builtin = len(entries) - registry.plugin_count
    lines.append(f"{len(entries)} gates: {builtin} built-in, {registry.plugin_count} from plugins")
    return lines


@app.command()
def gates(
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON instead of text.")] = False,
) -> None:
    """List every gate code with its severity, name and what it checks."""
    registry = _registry()
    if as_json:
        typer.echo(json.dumps(_gate_json(registry), indent=2))
    else:
        typer.echo("\n".join(_gate_lines(registry)))


def main() -> None:
    """Console-script entry point."""
    app()
