"""Render a :class:`CheckReport` as terminal text, Markdown or JSON.

Every renderer is a pure function of the report, and reports hold no timings
or absolute paths, so the same task content always renders to the same bytes.
"""

from __future__ import annotations

import json
from typing import Any

from taskgate.results import (
    CheckReport,
    ConfigSummary,
    GateResult,
    OptionValue,
    Status,
    TaskReport,
)

SHORT_SHA = 7


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _scope(report: CheckReport) -> str:
    if report.mode == "all":
        return f"all tasks, {_plural(len(report.tasks), 'task')}, {report.runner} runner"
    merge_base = (report.merge_base or "")[:SHORT_SHA]
    scope = (
        f"diff against {report.base} (merge base {merge_base}), "
        f"{_plural(len(report.tasks), 'changed task')}"
    )
    if report.other_files:
        scope += f", {_plural(len(report.other_files), 'other file')}"
    return f"{scope}, {report.runner} runner"


def describe_config(config: ConfigSummary) -> str | None:
    """One line naming the config file and what it changes; ``None`` for the defaults."""
    if config.source is None:
        return None
    parts = [config.source]
    if config.disabled:
        parts.append(f"disabled {', '.join(config.disabled)}")
    if config.severity:
        overrides = ", ".join(f"{code}={severity.value}" for code, severity in config.severity)
        parts.append(f"severity {overrides}")
    if config.options:
        options = ", ".join(f"{key}={_option_text(value)}" for key, value in config.options)
        parts.append(f"options {options}")
    return "; ".join(parts)


def _option_text(value: OptionValue) -> str:
    return json.dumps(list(value)) if isinstance(value, tuple) else str(value)


def _option_json(value: OptionValue) -> int | float | list[str]:
    return list(value) if isinstance(value, tuple) else value


def _result_line(report: CheckReport) -> str:
    verdict = "PASS" if report.passed else "FAIL"
    return f"{verdict}, {_plural(report.blocking_failures, 'blocking failure')}"


def _task_header(task: TaskReport) -> str:
    change = f"  {task.change}" if task.change else ""
    return f"{task.path}{change}  {task.verdict.upper()}"


def to_text(report: CheckReport) -> str:
    """Human-readable summary for the terminal."""
    lines = [f"taskgate {report.version}: {_scope(report)}"]
    config = describe_config(report.config)
    if config:
        lines.append(f"config: {config}")
    if not report.tasks:
        lines.append("no task directories to check")
    for task in report.tasks:
        lines.append(_task_header(task))
        if not task.checked:
            lines.append("  removed tasks are not checked")
        width = max((len(result.name) for result in task.results), default=0)
        for result in task.results:
            status = result.status.value.upper() if result.blocking else result.status.value
            name = f"{result.name:<{width}}"
            lines.append(f"  {status:<4}  {result.code}  {name}  {_one_line(result.message)}")
            if result.fix_hint:
                lines.append(f"        fix: {result.fix_hint}")
    lines.append(f"result: {_result_line(report)}")
    return "\n".join(lines) + "\n"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _cell(text: str) -> str:
    return _one_line(text).replace("|", "\\|")


def _status_cell(result: GateResult) -> str:
    if result.status is Status.FAIL:
        return f"**fail** ({result.severity.value})"
    return result.status.value


def to_markdown(report: CheckReport) -> str:
    """Pull-request summary comment."""
    verdict = "PASS" if report.passed else "FAIL"
    lines = [
        f"## TaskGate: {verdict}",
        "",
        f"{_plural(report.blocking_failures, 'blocking failure')}; {_scope(report)}.",
        "",
    ]
    config = describe_config(report.config)
    if config:
        lines += [f"Config: {config}.", ""]
    if not report.tasks:
        lines += ["No task directories to check.", ""]
    else:
        lines += ["| Task | Change | Verdict | Blocking gates |", "| --- | --- | --- | --- |"]
        for task in report.tasks:
            blocking = ", ".join(r.code for r in task.results if r.blocking) or "-"
            verdict_cell = f"**{task.verdict}**" if task.verdict == "fail" else task.verdict
            lines.append(f"| `{task.path}` | {task.change or '-'} | {verdict_cell} | {blocking} |")
        lines.append("")
    for task in report.tasks:
        if not task.results:
            continue
        lines += [
            f"### `{task.path}`",
            "",
            "| Gate | Status | Message |",
            "| --- | --- | --- |",
        ]
        lines += [
            f"| {r.code} {r.name} | {_status_cell(r)} | {_cell(r.message)} |" for r in task.results
        ]
        hints = [r for r in task.results if r.fix_hint]
        if hints:
            lines += ["", "How to fix:", ""]
            lines += [f"- **{r.code}**: {r.fix_hint}" for r in hints]
        lines.append("")
    lines.append(f"<sub>taskgate {report.version}</sub>")
    return "\n".join(lines) + "\n"


def _result_dict(result: GateResult) -> dict[str, Any]:
    return {
        "code": result.code,
        "name": result.name,
        "severity": result.severity.value,
        "status": result.status.value,
        "blocking": result.blocking,
        "message": result.message,
        "fix_hint": result.fix_hint,
    }


def to_dict(report: CheckReport) -> dict[str, Any]:
    """Plain data for JSON, with keys in a fixed order."""
    return {
        "taskgate_version": report.version,
        "mode": report.mode,
        "runner": report.runner,
        "base": report.base,
        "merge_base": report.merge_base,
        "result": "pass" if report.passed else "fail",
        "blocking_failures": report.blocking_failures,
        "tasks": [
            {
                "path": task.path,
                "change": task.change,
                "verdict": task.verdict,
                "blocking_failures": task.blocking_failures,
                "changed_files": list(task.changed_files),
                "gates": [_result_dict(result) for result in task.results],
            }
            for task in report.tasks
        ],
        "other_files": list(report.other_files),
        "config": {
            "source": report.config.source,
            "disabled": list(report.config.disabled),
            "severity": {code: severity.value for code, severity in report.config.severity},
            "options": {key: _option_json(value) for key, value in report.config.options},
        },
    }


def to_json(report: CheckReport) -> str:
    return json.dumps(to_dict(report), indent=2) + "\n"
