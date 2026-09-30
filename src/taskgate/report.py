"""Render a :class:`CheckReport` as terminal text, Markdown, JSON, JUnit XML or
GitHub annotations, and read a ``report.json`` back.

Every renderer is a pure function of the report, and reports hold no timings
or absolute paths, so the same task content always renders to the same bytes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree

from taskgate.results import (
    CheckReport,
    ConfigSummary,
    GateResult,
    OptionValue,
    Severity,
    Status,
    TaskReport,
)

SHORT_SHA = 7
ANNOTATED_FILE = "task.toml"
"""Every task's annotations sit on line 1 of its manifest (results carry no locations)."""

LEVELS = {Severity.ERROR: "error", Severity.WARNING: "warning", Severity.INFO: "notice"}
"""Workflow-command level per severity."""

CHECK_LEVELS = {Severity.ERROR: "failure", Severity.WARNING: "warning", Severity.INFO: "notice"}
"""Check-run ``annotation_level`` per severity."""

_XML_ILLEGAL = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


class ReportError(ValueError):
    """A file is not a ``report.json`` this version of TaskGate can read."""


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _scope(report: CheckReport) -> str:
    cached = f", {report.cached} cached" if report.cached else ""
    if report.mode == "all":
        tasks = _plural(len(report.tasks), "task")
        return f"all tasks, {tasks}, {report.runner} runner{cached}"
    merge_base = (report.merge_base or "")[:SHORT_SHA]
    scope = f"diff against {report.base} (merge base {merge_base}), "
    if report.pull_request is not None:
        scope += f"files of pull request #{report.pull_request}, "
    scope += _plural(len(report.tasks), "changed task")
    if report.other_files:
        scope += f", {_plural(len(report.other_files), 'other file')}"
    return f"{scope}, {report.runner} runner{cached}"


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


def headline(report: CheckReport) -> str:
    """``TaskGate: FAIL, 1 blocking failure``: the check run's title."""
    return f"TaskGate: {_result_line(report)}"


def _task_header(task: TaskReport) -> str:
    change = f"  {task.change}" if task.change else ""
    cached = "  (cached)" if task.cached else ""
    return f"{task.path}{change}  {task.verdict.upper()}{cached}"


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
            lines += [f"        - {_one_line(detail)}" for detail in result.details]
            if result.fix_hint:
                lines.append(f"        fix: {result.fix_hint}")
    lines.append(f"result: {_result_line(report)}")
    return "\n".join(lines) + "\n"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _cell(text: str) -> str:
    return _one_line(text).replace("|", "\\|")


def _detail(text: str) -> str:
    """A detail line for Markdown: a trailing ``reproduce: CMD`` is shown as code."""
    head, marker, command = _one_line(text).rpartition("reproduce: ")
    return f"{head}{marker}`{command}`" if marker else _one_line(text)


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
            if task.cached:
                verdict_cell += " (cached)"
            lines.append(f"| `{task.path}` | {task.change or '-'} | {verdict_cell} | {blocking} |")
        lines.append("")
    for task in report.tasks:
        if not task.results:
            continue
        lines += [f"### `{task.path}`", ""]
        if task.cached:
            lines += [
                "Cached result: the task's content, the TaskGate build, the gates, the "
                "config and the runner match an earlier run, so the gates did not run again.",
                "",
            ]
        lines += ["| Gate | Status | Message |", "| --- | --- | --- |"]
        lines += [
            f"| {r.code} {r.name} | {_status_cell(r)} | {_cell(r.message)} |" for r in task.results
        ]
        detailed = [r for r in task.results if r.details]
        if detailed:
            lines += ["", "Details:", ""]
            lines += [f"- **{r.code}**: {_detail(d)}" for r in detailed for d in r.details]
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
        "details": list(result.details),
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
        "pull_request": report.pull_request,
        "result": "pass" if report.passed else "fail",
        "blocking_failures": report.blocking_failures,
        "tasks": [
            {
                "path": task.path,
                "change": task.change,
                "verdict": task.verdict,
                "cached": task.cached,
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


def _config_from(data: Mapping[str, Any]) -> ConfigSummary:
    options = tuple(
        (str(key), tuple(str(v) for v in value) if isinstance(value, list) else value)
        for key, value in data["options"].items()
    )
    return ConfigSummary(
        source=data["source"],
        disabled=tuple(str(code) for code in data["disabled"]),
        severity=tuple((str(code), Severity(value)) for code, value in data["severity"].items()),
        options=options,
    )


def _result_from(data: Mapping[str, Any]) -> GateResult:
    return GateResult(
        code=str(data["code"]),
        name=str(data["name"]),
        severity=Severity(data["severity"]),
        status=Status(data["status"]),
        message=str(data["message"]),
        fix_hint=data["fix_hint"],
        details=tuple(str(detail) for detail in data["details"]),
    )


def from_dict(data: Any) -> CheckReport:
    """The :class:`CheckReport` that :func:`to_dict` turned into ``data``."""
    try:
        tasks = tuple(
            TaskReport(
                path=str(task["path"]),
                change=task["change"],
                results=tuple(_result_from(result) for result in task["gates"]),
                changed_files=tuple(str(path) for path in task["changed_files"]),
                cached=bool(task["cached"]),
            )
            for task in data["tasks"]
        )
        return CheckReport(
            version=str(data["taskgate_version"]),
            mode=str(data["mode"]),
            base=data["base"],
            merge_base=data["merge_base"],
            tasks=tasks,
            other_files=tuple(str(path) for path in data["other_files"]),
            config=_config_from(data["config"]),
            runner=str(data["runner"]),
            pull_request=data.get("pull_request"),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ReportError(f"not a TaskGate report.json ({type(exc).__name__}: {exc})") from exc


def from_json(text: str) -> CheckReport:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ReportError(f"not a TaskGate report.json (not JSON: {exc})") from exc
    return from_dict(data)


@dataclass(frozen=True, slots=True)
class Finding:
    """One failed gate on one task, located for GitHub: file, line, title and text."""

    path: str
    severity: Severity
    title: str
    message: str
    details: str
    """The gate's detail lines and its fix hint, one per line."""


def _join_path(prefix: str, *parts: str) -> str:
    kept = [part.strip("/") for part in (prefix, *parts) if part.strip("/") not in ("", ".")]
    return "/".join(kept)


def findings(report: CheckReport, prefix: str = "") -> list[Finding]:
    """Every failed gate result, located on its task's ``task.toml``.

    ``prefix`` is prepended to paths: task paths are relative to the checked
    directory, and GitHub wants them relative to the repository root.
    """
    found: list[Finding] = []
    for task in report.tasks:
        path = _join_path(prefix, task.path, ANNOTATED_FILE)
        for result in task.results:
            if result.status is not Status.FAIL:
                continue
            lines = [_one_line(detail) for detail in result.details]
            if result.fix_hint:
                lines.append(f"fix: {result.fix_hint}")
            found.append(
                Finding(
                    path=path,
                    severity=result.severity,
                    title=f"{result.code} {result.name}",
                    message=_one_line(result.message),
                    details="\n".join(lines),
                )
            )
    return found


def _escape_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    return _escape_data(text).replace(":", "%3A").replace(",", "%2C")


def to_annotations(report: CheckReport, prefix: str = "") -> str:
    """GitHub Actions workflow commands (``::error file=...::``), one per failed gate."""
    lines = []
    for finding in findings(report, prefix):
        text = "\n".join(part for part in (finding.message, finding.details) if part)
        lines.append(
            f"::{LEVELS[finding.severity]} file={_escape_property(finding.path)},line=1,"
            f"title={_escape_property(finding.title)}::{_escape_data(text)}"
        )
    return "".join(f"{line}\n" for line in lines)


def _xml(text: str) -> str:
    """``text`` without the characters XML 1.0 cannot hold (pytest output may have them)."""
    return _XML_ILLEGAL.sub("?", text)


def to_junit(report: CheckReport) -> str:
    """JUnit XML: a ``testsuite`` per checked task and a ``testcase`` per gate.

    A blocking failure is a ``failure``; a failed warning or info gate passes with
    the problem in ``system-out``; a skipped gate is ``skipped``. There are no
    timings, so the output is as stable as the other reports.
    """
    checked = [task for task in report.tasks if task.results]

    def counts(results: list[GateResult]) -> dict[str, str]:
        return {
            "tests": str(len(results)),
            "failures": str(sum(result.blocking for result in results)),
            "errors": "0",
            "skipped": str(sum(result.status is Status.SKIP for result in results)),
        }

    everything = [result for task in checked for result in task.results]
    root = ElementTree.Element("testsuites", {"name": "taskgate", **counts(everything)})
    for task in checked:
        suite = ElementTree.SubElement(
            root, "testsuite", {"name": _xml(task.path), **counts(list(task.results))}
        )
        if task.cached:
            properties = ElementTree.SubElement(suite, "properties")
            ElementTree.SubElement(properties, "property", {"name": "cached", "value": "true"})
        for result in task.results:
            case = ElementTree.SubElement(
                suite,
                "testcase",
                {"classname": _xml(task.path), "name": f"{result.code} {result.name}"},
            )
            body = "\n".join(
                [_one_line(detail) for detail in result.details]
                + ([f"fix: {result.fix_hint}"] if result.fix_hint else [])
            )
            if result.blocking:
                failure = ElementTree.SubElement(
                    case,
                    "failure",
                    {"message": _xml(_one_line(result.message)), "type": result.severity.value},
                )
                failure.text = _xml(body) or None
            elif result.status is Status.FAIL:
                out = ElementTree.SubElement(case, "system-out")
                out.text = _xml(
                    "\n".join(
                        part
                        for part in (f"{result.severity.value}: {result.message}", body)
                        if part
                    )
                )
            elif result.status is Status.SKIP:
                ElementTree.SubElement(case, "skipped", {"message": _xml(result.message)})
    ElementTree.indent(root)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        + ElementTree.tostring(root, encoding="unicode")
        + "\n"
    )
