"""Review gates: the :class:`Gate` protocol and the gates TaskGate ships with.

Codes are ``TG`` plus three digits, grouped by hundreds and never reused:
TG1xx layout and manifest, TG2xx hygiene, TG3xx environment, TG4xx solution and
baselines, TG5xx determinism; TG6xx is reserved. TG7xx to TG9xx belong to
third-party gates registered through the ``taskgate.gates`` entry-point group
(see :mod:`taskgate.registry`).
"""

from __future__ import annotations

from taskgate.gates.base import (
    Check,
    CheckFunction,
    FunctionGate,
    Gate,
    TaskContext,
    gate,
    is_binary,
)
from taskgate.gates.core import CORE_GATES, missing_layout
from taskgate.gates.hygiene import HYGIENE_GATES
from taskgate.gates.lint import LINT_GATES

BUILTIN_GATES: tuple[Gate, ...] = tuple(
    sorted((*CORE_GATES, *LINT_GATES, *HYGIENE_GATES), key=lambda g: g.code)
)
"""Every gate TaskGate ships with, in code order (the order they run in)."""

__all__ = [
    "BUILTIN_GATES",
    "Check",
    "CheckFunction",
    "FunctionGate",
    "Gate",
    "TaskContext",
    "gate",
    "is_binary",
    "missing_layout",
]
