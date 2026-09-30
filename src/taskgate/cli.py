"""Command-line interface for TaskGate."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from taskgate import __version__, manifest
from taskgate.cache import DEFAULT_CACHE_DIR, CachedChecks, CacheError, CacheStats, ResultCache
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
from taskgate.github import TOKEN_ENV, Annotation, GitHubClient, GitHubError
from taskgate.layout import TASK_MANIFEST, TaskDir, find_tasks
from taskgate.registry import (
    GROUPS,
    Registry,
    RegistryError,
    group_label,
    group_of,
    load_registry,
)
from taskgate.report import (
    CHECK_LEVELS,
    ReportError,
    describe_config,
    findings,
    from_json,
    headline,
    to_annotations,
    to_json,
    to_junit,
    to_markdown,
    to_text,
)
from taskgate.results import CheckReport, TaskReport
from taskgate.runner import SOLUTION_ENTRY, Runner

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
    JUNIT = "junit"
    ANNOTATIONS = "annotations"


RENDERERS: dict[OutputFormat, Callable[[CheckReport], str]] = {
    OutputFormat.TEXT: to_text,
    OutputFormat.MARKDOWN: to_markdown,
    OutputFormat.JSON: to_json,
    OutputFormat.JUNIT: to_junit,
    OutputFormat.ANNOTATIONS: to_annotations,
}
REPORT_FILES = {"report.md": to_markdown, "report.json": to_json, "junit.xml": to_junit}
"""What ``check --out DIR`` writes."""


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


def _note(message: str) -> None:
    typer.echo(f"note: {message}", err=True)


def _cache_dir(root: Path, cache_dir: Path | None) -> Path:
    """``--cache-dir`` when given, else ``.taskgate/cache`` under ``root``."""
    return cache_dir if cache_dir is not None else root / DEFAULT_CACHE_DIR


def _checks(
    root: Path, cache: CacheSettings, registry: Registry, config: Config, runner: Runner
) -> CachedChecks:
    store = None if cache.off else ResultCache(_cache_dir(root, cache.directory))
    return CachedChecks(store, registry.gates, config, runner, note=_note)


def _task_report(
    checks: CachedChecks,
    root: Path,
    path: str,
    change: str | None,
    tracked: tuple[str, ...] | None = None,
    changed_files: tuple[str, ...] = (),
) -> TaskReport:
    """One task's report: from the cache when it has a hit, else from a run of the gates."""
    if change == "removed":
        return TaskReport(path=path, change=change, changed_files=changed_files)
    results, cached = checks.results(
        root / path,
        label=path,
        tracked=tracked,
        compute=lambda: run_gates(
            root / path,
            gates=checks.gates,
            config=checks.config,
            runner=checks.runner,
            tracked=tracked,
            label=path,
        ),
    )
    return TaskReport(
        path=path, change=change, results=results, changed_files=changed_files, cached=cached
    )


def _check_all(
    root: Path, config_path: Path | None, choice: RunnerChoice, cache: CacheSettings
) -> CheckReport:
    try:
        found = find_tasks(root)
    except NotADirectoryError as exc:
        raise _fail_usage(str(exc)) from exc
    registry = _registry()
    config = _config(registry, config_path, lambda: discover(root))
    runner = _runner(choice, config)
    checks = _checks(root, cache, registry, config, runner)
    tasks = tuple(_task_report(checks, root, task.path.as_posix(), None) for task in found)
    checks.close()
    return CheckReport(
        version=__version__,
        mode="all",
        tasks=tasks,
        config=config.summary(),
        runner=runner.name,
    )


def _pull_request_paths(pull: PullRequestSource) -> list[str]:
    """Every path the pull request touches (both sides of a rename), from the GitHub API."""
    if pull.repo is None:
        raise _fail_usage("--pr needs --repo OWNER/NAME (or GITHUB_REPOSITORY)")
    try:
        files = GitHubClient.from_env(pull.repo).pull_request_files(pull.number)
    except GitHubError as exc:
        raise _fail_usage(str(exc)) from exc
    return [path for item in files for path in (item.path, item.previous_path) if path]


def _check_diff(
    repo: Path,
    base: str | None,
    config_path: Path | None,
    choice: RunnerChoice,
    cache: CacheSettings,
    pull: PullRequestSource | None = None,
) -> CheckReport:
    paths = None if pull is None else _pull_request_paths(pull)
    try:
        root = repo_root(repo)
        changes = changed_tasks(root, base, paths=paths)
    except GitError as exc:
        raise _fail_usage(str(exc)) from exc
    registry = _registry()
    config = _config(registry, config_path, lambda: _config_at(root, changes.base))
    runner = _runner(choice, config)
    checks = _checks(root, cache, registry, config, runner)
    tasks = tuple(
        _task_report(
            checks, root, task.path.as_posix(), task.change, task.tracked, changed_files=task.files
        )
        for task in changes.tasks
    )
    checks.close()
    return CheckReport(
        version=__version__,
        mode="diff",
        base=changes.base,
        merge_base=changes.merge_base,
        tasks=tasks,
        other_files=changes.other_files,
        config=config.summary(),
        runner=runner.name,
        pull_request=None if pull is None else pull.number,
    )


@dataclass(frozen=True, slots=True)
class PullRequestSource:
    """``check --pr N --repo OWNER/NAME``: take the changed files from the GitHub API."""

    number: int
    repo: str | None


RepoOption = Annotated[
    str | None,
    typer.Option(
        "--repo",
        envvar="GITHUB_REPOSITORY",
        help="GitHub repository as OWNER/NAME (default: GITHUB_REPOSITORY).",
    ),
]

PrefixOption = Annotated[
    str,
    typer.Option(
        "--path-prefix",
        help=(
            "Prepended to task paths in annotations: the checked directory relative to "
            "the repository root, when --all checked a subdirectory."
        ),
    ),
]

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

CacheDirOption = Annotated[
    Path | None,
    typer.Option(
        "--cache-dir",
        envvar="TASKGATE_CACHE_DIR",
        help=(
            "Result cache directory. Default: .taskgate/cache under the repository root "
            "(diff mode) or the checked directory (--all)."
        ),
    ),
]


@dataclass(frozen=True, slots=True)
class CacheSettings:
    """``--no-cache`` and ``--cache-dir`` of one ``check``."""

    off: bool = False
    directory: Path | None = None


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
    no_cache: Annotated[
        bool,
        typer.Option(
            "--no-cache", help="Run every gate on every task; neither read nor write the cache."
        ),
    ] = False,
    cache_dir: CacheDirOption = None,
    pr: Annotated[
        int | None,
        typer.Option(
            "--pr",
            min=1,
            help=(
                "Take the changed files from this pull request's file list (GitHub API, "
                "TASKGATE_GITHUB_API and GITHUB_TOKEN) instead of git diff."
            ),
        ),
    ] = None,
    github_repo: RepoOption = None,
) -> None:
    """Run the review gates on the tasks a pull request changes.

    A task whose content, TaskGate build, gates, config and runner match an
    earlier passing run is not checked again: its results come from the cache
    and reports mark it cached. --out writes report.md, report.json and
    junit.xml. Exits 0 when no blocking gate fails, 1 when one does, and 2 on
    usage errors (including a GitHub API that --pr cannot read).
    """
    cache = CacheSettings(off=no_cache, directory=cache_dir)
    if all_tasks and pr is not None:
        raise _fail_usage("--pr lists a pull request's files; it cannot be used with --all")
    pull = None if pr is None else PullRequestSource(pr, github_repo)
    report = (
        _check_all(repo.resolve(), config_path, runner_choice, cache)
        if all_tasks
        else _check_diff(repo, base, config_path, runner_choice, cache, pull)
    )
    typer.echo(RENDERERS[output_format](report), nl=False)
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        for name, render in REPORT_FILES.items():
            (out / name).write_text(render(report), encoding="utf-8")
        written = [str(out / name) for name in REPORT_FILES]
        typer.echo(f"wrote {', '.join(written[:-1])} and {written[-1]}", err=True)
    if not report.passed:
        raise typer.Exit(1)


def _load_report(path: Path) -> CheckReport:
    try:
        return from_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise _fail_usage(f"cannot read {path}: {exc.strerror or exc}") from exc
    except ReportError as exc:
        raise _fail_usage(f"{path}: {exc}") from exc


ReportArgument = Annotated[
    Path, typer.Argument(help="A report.json written by taskgate check --out.")
]


@app.command("report")
def report_command(
    report_json: ReportArgument,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help="What to print on stdout.")
    ] = OutputFormat.MARKDOWN,
    prefix: PrefixOption = "",
) -> None:
    """Print a saved report.json in another format (annotations for a CI log, JUnit XML...)."""
    report = _load_report(report_json)
    if output_format is OutputFormat.ANNOTATIONS:
        typer.echo(to_annotations(report, prefix), nl=False)
    else:
        typer.echo(RENDERERS[output_format](report), nl=False)


@app.command()
def publish(
    report_json: ReportArgument,
    pr: Annotated[int, typer.Option("--pr", min=1, help="Pull request number.")],
    github_repo: RepoOption = None,
    sha: Annotated[
        str | None,
        typer.Option("--sha", help="Commit the check run belongs to (the pull request's head)."),
    ] = None,
    comment: Annotated[
        bool, typer.Option("--comment/--no-comment", help="Create or update the summary comment.")
    ] = True,
    check_run: Annotated[
        bool,
        typer.Option("--check-run/--no-check-run", help="Create a check run with annotations."),
    ] = True,
    check_name: Annotated[str, typer.Option("--check-name", help="Check run name.")] = "TaskGate",
    prefix: PrefixOption = "",
) -> None:
    """Post a report.json to a pull request: one summary comment, kept up to date, and a
    check run with an annotation per failed gate (sent 50 per request).

    The API base URL comes from TASKGATE_GITHUB_API (default https://api.github.com)
    and the token from GITHUB_TOKEN. Exits 0 when everything was posted, 1 when the
    API refused or could not be reached, and 2 on usage errors. The gates' verdict
    does not change the exit code: check reports that.
    """
    report = _load_report(report_json)
    if github_repo is None:
        raise _fail_usage("--repo OWNER/NAME (or GITHUB_REPOSITORY) is required")
    if check_run and not sha:
        raise _fail_usage("--sha is required for a check run (or pass --no-check-run)")
    if not os.environ.get(TOKEN_ENV):
        raise _fail_usage(f"{TOKEN_ENV} is not set; publish needs a token")
    try:
        client = GitHubClient.from_env(github_repo)
        if comment:
            posted = client.upsert_comment(pr, to_markdown(report))
            typer.echo(f"comment {posted.action}: {posted.url}")
        if check_run:
            annotations = [
                Annotation(f.path, CHECK_LEVELS[f.severity], f.title, f.message, f.details)
                for f in findings(report, prefix)
            ]
            run = client.create_check_run(
                name=check_name,
                head_sha=sha or "",
                conclusion="success" if report.passed else "failure",
                title=headline(report),
                summary=to_markdown(report),
                annotations=annotations,
            )
            typer.echo(
                f"check run {'success' if report.passed else 'failure'}: "
                f"{_count(run.annotations, 'annotation')} in {_count(run.requests, 'request')}: "
                f"{run.url}"
            )
    except GitHubError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


GRADE_NEEDS = (f"solution/{SOLUTION_ENTRY}", "tests/")
"""What ``taskgate grade`` needs besides ``task.toml`` to run a rerun at all."""


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
    missing = [part for part in GRADE_NEEDS if not (task / part.rstrip("/")).exists()]
    if missing:
        raise _fail_usage(
            f"{task.as_posix()} cannot be graded: missing {', '.join(missing)} (see TG101)"
        )
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
        typer.echo(f"({run.junit_problem or exc})")
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


cache_app = typer.Typer(
    name="cache",
    help="Inspect and prune the result cache that lets taskgate check skip unchanged tasks.",
    no_args_is_help=True,
)
app.add_typer(cache_app)

CacheRoot = Annotated[
    Path,
    typer.Argument(
        help="Repository root or --all directory whose .taskgate/cache to use.",
    ),
]


def _count(count: int, word: str) -> str:
    plural = word + ("es" if word.endswith("s") else "s")
    return f"{count} {word if count == 1 else plural}"


def _size(size: int) -> str:
    return f"{size} B" if size < 1024 else f"{size / 1024:.1f} KiB"


def _stats_json(stats: CacheStats) -> dict[str, object]:
    counts = stats.counts
    return {
        "directory": str(stats.directory),
        "entries": len(stats.entries),
        "bytes": stats.size,
        "current": stats.current,
        "stale": len(stats.entries) - stats.current,
        "tasks": stats.tasks,
        "runners": stats.runners,
        "hits": counts.hits,
        "misses": counts.misses,
        "stored": counts.stored,
        "not_stored": counts.not_stored,
    }


def _stats_lines(stats: CacheStats) -> list[str]:
    counts = stats.counts
    runners = ", ".join(f"{name} {count}" for name, count in stats.runners.items()) or "-"
    stale = len(stats.entries) - stats.current
    lookups = counts.hits + counts.misses
    rate = f" ({100 * counts.hits / lookups:.0f}% hits)" if lookups else ""
    return [
        f"cache      {stats.directory}",
        f"entries    {len(stats.entries)} ({_size(stats.size)}): {stats.current} usable by this "
        f"taskgate build, {stale} stale (taskgate cache prune removes them)",
        f"tasks      {stats.tasks} (runners: {runners})",
        f"lookups    {_count(counts.hits, 'hit')}, {_count(counts.misses, 'miss')}{rate}",
        f"stored     {_count(counts.stored, 'result set')}; {counts.not_stored} not stored "
        "because a blocking gate failed",
    ]


@cache_app.command("stats")
def cache_stats(
    root: CacheRoot = Path(),
    cache_dir: CacheDirOption = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON instead of text.")] = False,
) -> None:
    """Show how many results the cache holds and how often checks hit it."""
    stats = ResultCache(_cache_dir(root, cache_dir)).stats()
    if as_json:
        typer.echo(json.dumps(_stats_json(stats), indent=2))
    else:
        typer.echo("\n".join(_stats_lines(stats)))


PRUNE_TEXT = {
    "all": "removed by --all",
    "stale": "from another taskgate build or cache format",
    "unreadable": "unreadable",
    "superseded": "superseded by a newer entry for the same task and runner",
    "unused": "unused for longer than --older-than",
}


@cache_app.command("prune")
def cache_prune(
    root: CacheRoot = Path(),
    cache_dir: CacheDirOption = None,
    older_than: Annotated[
        float | None,
        typer.Option(
            "--older-than", min=0, help="Also remove entries not used for this many days."
        ),
    ] = None,
    everything: Annotated[bool, typer.Option("--all", help="Remove every entry.")] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Only say what would be removed.")
    ] = False,
) -> None:
    """Remove cache entries no check can hit, and older ones for the same task.

    By default that is entries written by another TaskGate build or cache
    format, unreadable ones, and all but the most recently used entry for each
    task and runner.
    """
    cache = ResultCache(_cache_dir(root, cache_dir))
    try:
        pruned = cache.prune(
            everything=everything,
            older_than_sec=None if older_than is None else older_than * 86400,
            dry_run=dry_run,
        )
    except CacheError as exc:
        raise _fail_usage(str(exc)) from exc
    verb = "would remove" if dry_run else "removed"
    reasons = "; ".join(
        f"{len(entries)} {PRUNE_TEXT[reason]}" for reason, entries in pruned.removed.items()
    )
    typer.echo(
        f"{verb} {pruned.count} of {pruned.count + len(pruned.kept)} entries "
        f"({_size(pruned.size)}){': ' + reasons if reasons else ''}; "
        f"{len(pruned.kept)} kept in {cache.directory}"
    )


def main() -> None:
    """Console-script entry point."""
    app()
