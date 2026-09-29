"""Command-line interface for TaskGate."""

from __future__ import annotations

import json
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from taskgate import __version__, manifest
from taskgate.changes import GitError, changed_tasks, read_file_at, repo_root
from taskgate.config import (
    CONFIG_FILE,
    MAX_SEED,
    Config,
    ConfigError,
    discover,
    load_file,
    parse,
)
from taskgate.determinism import JUnitError, parse_junit
from taskgate.docker_runner import RunnerChoice, RunnerUnavailableError, select_runner
from taskgate.engine import run_gates
from taskgate.gates.core import run_failure
from taskgate.layout import TASK_MANIFEST, TaskDir, find_tasks
from taskgate.registry import (
    GROUPS,
    Registry,
    RegistryError,
    group_label,
    group_of,
    load_registry,
)
from taskgate.report import describe_config, to_json, to_markdown, to_text
from taskgate.results import CheckReport, TaskReport
from taskgate.runner import Runner

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


def _config(registry: Registry, path: Path | None, fallback: Callable[[], Config]) -> Config:
    """``--config PATH`` when given, else ``fallback()``; unknown gate codes are errors."""
    try:
        config = load_file(path) if path is not None else fallback()
        config.validate_codes(registry.codes)
    except (ConfigError, GitError) as exc:
        raise _fail_usage(str(exc)) from exc
    return config


def _config_at(root: Path, ref: str) -> Config:
    """``taskgate.toml`` as committed at ``ref`` (the defaults when it is not there)."""
    text = read_file_at(root, ref, CONFIG_FILE)
    return Config() if text is None else parse(text, f"{CONFIG_FILE} at {ref}")


def _runner(choice: RunnerChoice, config: Config) -> Runner:
    """The runner for ``--runner``; a fallback to the local runner is noted on stderr."""
    try:
        runner, note = select_runner(choice, config.runner)
    except RunnerUnavailableError as exc:
        raise _fail_usage(str(exc)) from exc
    if note:
        typer.echo(f"note: {note}", err=True)
    return runner


def _check_all(root: Path, config_path: Path | None, choice: RunnerChoice) -> CheckReport:
    try:
        found = find_tasks(root)
    except NotADirectoryError as exc:
        raise _fail_usage(str(exc)) from exc
    registry = _registry()
    config = _config(registry, config_path, lambda: discover(root))
    runner = _runner(choice, config)
    tasks = tuple(
        TaskReport(
            path=task.path.as_posix(),
            change=None,
            results=run_gates(
                root / task.path,
                gates=registry.gates,
                config=config,
                runner=runner,
                label=task.path.as_posix(),
            ),
        )
        for task in found
    )
    return CheckReport(
        version=__version__,
        mode="all",
        tasks=tasks,
        config=config.summary(),
        runner=runner.name,
    )


def _check_diff(
    repo: Path, base: str | None, config_path: Path | None, choice: RunnerChoice
) -> CheckReport:
    try:
        root = repo_root(repo)
        changes = changed_tasks(root, base)
    except GitError as exc:
        raise _fail_usage(str(exc)) from exc
    registry = _registry()
    config = _config(registry, config_path, lambda: _config_at(root, changes.base))
    runner = _runner(choice, config)
    tasks = tuple(
        TaskReport(
            path=task.path.as_posix(),
            change=task.change,
            results=()
            if task.change == "removed"
            else run_gates(
                root / task.path,
                gates=registry.gates,
                config=config,
                runner=runner,
                tracked=task.tracked,
                label=task.path.as_posix(),
            ),
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
        config=config.summary(),
        runner=runner.name,
    )


RunnerOption = Annotated[
    RunnerChoice,
    typer.Option(
        "--runner",
        envvar="TASKGATE_RUNNER",
        help=(
            "Where solutions and graders run: docker when the daemon answers, else "
            "local (auto); docker only (exit 2 without it); or local only."
        ),
    ),
]

ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config",
        help=(
            "taskgate.toml to use. Default: the one committed at the base ref (diff mode) "
            "or in the checked directory (--all)."
        ),
    ),
]


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
    config_path: ConfigOption = None,
    runner_choice: RunnerOption = RunnerChoice.AUTO,
) -> None:
    """Run the review gates on the tasks a pull request changes.

    Exits 0 when no blocking gate fails, 1 when one does, and 2 on usage errors.
    """
    report = (
        _check_all(repo.resolve(), config_path, runner_choice)
        if all_tasks
        else _check_diff(repo, base, config_path, runner_choice)
    )
    typer.echo(RENDERERS[output_format](report), nl=False)
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.md").write_text(to_markdown(report), encoding="utf-8")
        (out / "report.json").write_text(to_json(report), encoding="utf-8")
        typer.echo(f"wrote {out / 'report.md'} and {out / 'report.json'}", err=True)
    if not report.passed:
        raise typer.Exit(1)


@app.command()
def grade(
    task: Annotated[Path, typer.Argument(help="Task directory (the one holding task.toml).")],
    seed: Annotated[
        int,
        typer.Option(
            "--seed",
            min=0,
            max=MAX_SEED,
            help="The seed of the rerun to repeat, as TG501 reports it.",
        ),
    ],
    runner_choice: RunnerOption = RunnerChoice.AUTO,
    config_path: ConfigOption = None,
) -> None:
    """Repeat one TG501 rerun: the reference solution, then the grader with SEED.

    The tests run in the order the seed shuffles them into, with PYTHONHASHSEED
    and TASKGATE_SEED set to the seed, so a test TG501 reports as flipped fails
    the same way again. Prints pytest's output and each test's outcome in the
    order the tests ran. Exits 0 when the grader passes, 1 when the solution or
    the grader fails, and 2 on usage errors.
    """
    if not (task / TASK_MANIFEST).is_file():
        raise _fail_usage(f"{task} is not a task directory (no {TASK_MANIFEST})")
    config = _config(_registry(), config_path, lambda: discover(Path()))
    runner = _runner(runner_choice, config)
    timeout = manifest.load(task).timeout_sec
    typer.echo(
        f"taskgate grade {task.as_posix()}: reference solution, then the grader with seed "
        f"{seed} (shuffled order, PYTHONHASHSEED={seed}, TASKGATE_SEED={seed}), "
        f"{runner.name} runner"
    )
    regraded = runner.regrade(task, seeds=(seed,), timeout_sec=timeout)
    solution = regraded.solution
    if solution.output:
        typer.echo(solution.output)
    if solution.timed_out:
        why = f"solve.sh did not finish within 2 x task.timeout_sec = {2 * timeout} s"
    elif solution.solution_exit != 0 or solution.error is not None:
        why = run_failure(solution, "the solution run", timeout)
    elif not regraded.runs:
        why = regraded.error or "the grader did not run"
    else:
        why = ""
    if why:
        typer.echo(f"result: FAIL ({why})")
        raise typer.Exit(1)
    (run,) = regraded.runs
    typer.echo(run.output.rstrip("\n"))
    try:
        outcomes = parse_junit(run.junit)
    except JUnitError as exc:
        typer.echo(f"({exc})")
        outcomes = {}
    for test, outcome in outcomes.items():
        typer.echo(f"{outcome:<7}  {test}")
    if run.exit == 0:
        typer.echo("result: PASS")
        return
    if run.exit is None:
        why = f"the grader did not finish within 2 x task.timeout_sec = {2 * timeout} s"
    else:
        why = f"pytest exited {run.exit}"
    typer.echo(f"result: FAIL ({why})")
    raise typer.Exit(1)


def _gate_json(registry: Registry, config: Config) -> list[dict[str, object]]:
    return [
        {
            "code": entry.gate.code,
            "name": entry.gate.name,
            "group": GROUPS[group_of(entry.gate.code)],
            "enabled": entry.gate.code not in config.disabled,
            "severity": config.severity_for(entry.gate.code, entry.gate.severity).value,
            "default_severity": entry.gate.severity.value,
            "summary": entry.gate.summary,
            "fix_hint": entry.gate.fix_hint,
            "requires": list(entry.gate.requires),
            "source": entry.source,
        }
        for entry in registry.entries
    ]


def _gate_lines(registry: Registry, config: Config) -> list[str]:
    entries = registry.entries
    name_width = max(len(entry.gate.name) for entry in entries)
    lines: list[str] = []
    group = 0
    for entry in entries:
        gate = entry.gate
        if group_of(gate.code) != group:
            group = group_of(gate.code)
            lines.append(group_label(group))
        severity = (
            "off"
            if gate.code in config.disabled
            else config.severity_for(gate.code, gate.severity).value
        )
        source = "" if entry.builtin else f"  [{entry.source}]"
        lines.append(
            f"  {gate.code}  {severity:<7}  {gate.name:<{name_width}}  {gate.summary}{source}"
        )
    builtin = len(entries) - registry.plugin_count
    lines.append(f"{len(entries)} gates: {builtin} built-in, {registry.plugin_count} from plugins")
    described = describe_config(config.summary())
    if described:
        lines.append(f"config: {described}")
    return lines


@app.command()
def gates(
    root: Annotated[
        Path, typer.Argument(help="Directory whose taskgate.toml applies (if it has one).")
    ] = Path(),
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON instead of text.")] = False,
    config_path: ConfigOption = None,
) -> None:
    """List every gate code with its effective severity, name and what it checks.

    A gate that taskgate.toml disables is shown with severity "off".
    """
    registry = _registry()
    config = _config(registry, config_path, lambda: discover(root))
    if as_json:
        typer.echo(json.dumps(_gate_json(registry, config), indent=2))
    else:
        typer.echo("\n".join(_gate_lines(registry, config)))


def main() -> None:
    """Console-script entry point."""
    app()
