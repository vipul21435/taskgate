"""Example third-party TaskGate gate: flag TODO, FIXME and XXX markers in a task.

Third-party codes live in TG7xx to TG9xx. The entry point in this directory's
pyproject.toml registers the gate under the ``taskgate.gates`` group.
"""

from __future__ import annotations

import re

from taskgate.gates import Check, TaskContext, gate, is_binary
from taskgate.results import Severity

MARKER = re.compile(rb"\b(?:TODO|FIXME|XXX)\b")
MAX_LISTED = 3


@gate(
    "TG701",
    "no-todo-markers",
    severity=Severity.WARNING,
    summary="no TODO, FIXME or XXX markers left in task files",
    fix_hint="Resolve each marker or remove it before asking for review.",
)
def no_todo_markers(ctx: TaskContext) -> Check:
    hits: list[str] = []
    for relative in ctx.files:
        data = ctx.read_bytes(relative)
        if is_binary(data):
            continue
        for number, line in enumerate(data.splitlines(), start=1):
            if MARKER.search(line):
                hits.append(f"{relative}:{number}")
    if not hits:
        return Check.ok("no TODO markers")
    listed = ", ".join(hits[:MAX_LISTED])
    more = f" and {len(hits) - MAX_LISTED} more" if len(hits) > MAX_LISTED else ""
    return Check.fail(f"{len(hits)} marker(s): {listed}{more}")
