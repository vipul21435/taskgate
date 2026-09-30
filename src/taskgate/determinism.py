"""Compare grader reruns for the determinism gate (TG501).

Each rerun (:class:`taskgate.runner.GraderRun`) carries pytest's exit code and
the JUnit XML it wrote. :func:`parse_junit` turns that XML into ``test id ->
outcome``, rebuilding pytest's own ids (``tests/test_x.py::TestY::test_z[1]``)
from the ``file`` attribute of the ``xunit1`` format. :func:`compare` then finds
every test that flipped: its outcome differs between reruns, or it failed in
every rerun although the grader passed TG401's run (file order,
``PYTHONHASHSEED=0``), so it flipped against that run.

The XML comes from the task's own grader process, so both runners read it only
up to :data:`taskgate.runner.MAX_JUNIT_BYTES` (a bigger file is reported as such,
not parsed) and parse it with the standard library's
expat parser, which does not resolve external entities and, since expat 2.4.1,
refuses entity-expansion bombs (a test checks both).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from xml.etree import ElementTree

from taskgate.runner import GraderRun

PASSED = "passed"
FAILED = "failed"
ERROR = "error"
SKIPPED = "skipped"
NOT_RUN = "not run"
"""A test that other reruns reported and this one did not."""

_SEVERITY = {PASSED: 0, SKIPPED: 1, FAILED: 2, ERROR: 3}
_BAD = frozenset({FAILED, ERROR})


class JUnitError(ValueError):
    """The JUnit XML is missing or cannot be read."""


def node_id(file: str | None, classname: str, name: str) -> str:
    """pytest's node id for a JUnit ``testcase``, as far as the attributes allow.

    ``file="tests/test_a.py" classname="tests.test_a.TestK" name="test_m"`` becomes
    ``tests/test_a.py::TestK::test_m``; a collection error for a whole file becomes
    the file. Without a usable ``file`` the id is ``classname::name``.
    """
    if file and file.endswith(".py"):
        module = file.removesuffix(".py").replace("/", ".")
        if classname == module:
            return f"{file}::{name}"
        if classname.startswith(f"{module}."):
            return "::".join([file, *classname[len(module) + 1 :].split("."), name])
        if not classname and name == module:
            return file
    return f"{classname}::{name}" if classname else name


def _outcome(case: ElementTree.Element) -> str:
    tags = {child.tag for child in case}
    if "error" in tags:
        return ERROR
    if "failure" in tags:
        return FAILED
    return SKIPPED if "skipped" in tags else PASSED


def parse_junit(text: str) -> dict[str, str]:
    """``test id -> outcome`` in the order the tests ran; raise :class:`JUnitError`.

    Outcomes are ``passed``, ``failed``, ``error`` and ``skipped`` (an expected
    failure counts as skipped, as pytest reports it). A test reported twice keeps
    its worse outcome.
    """
    if not text.strip():
        raise JUnitError("no JUnit XML")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise JUnitError(f"unreadable JUnit XML ({exc})") from exc
    outcomes: dict[str, str] = {}
    for case in root.iter("testcase"):
        test = node_id(case.get("file"), case.get("classname", ""), case.get("name", ""))
        outcome = _outcome(case)
        previous = outcomes.get(test)
        if previous is None or _SEVERITY[outcome] > _SEVERITY[previous]:
            outcomes[test] = outcome
    return outcomes


@dataclass(frozen=True, slots=True)
class Rerun:
    """One grader rerun as the gate sees it."""

    number: int
    """1-based position among the reruns."""

    seed: int
    exit: int | None
    """pytest's exit code; ``None`` when the run did not finish within the budget."""

    outcomes: Mapping[str, str]
    """``test id -> outcome``; empty when :attr:`problem` is set."""

    problem: str | None = None
    """Why this rerun has no per-test outcomes to compare."""

    @property
    def label(self) -> str:
        return f"run {self.number} (seed {self.seed})"

    @property
    def failing(self) -> bool:
        """pytest finished and reported failure (any exit code but 0)."""
        return self.exit not in (0, None)


def rerun(number: int, run: GraderRun) -> Rerun:
    """Read one :class:`GraderRun`'s outcomes."""
    if run.exit is None:
        return Rerun(number, run.seed, None, {}, "did not finish within the budget")
    if run.junit_problem is not None:
        problem = f"pytest exited {run.exit} with {run.junit_problem}"
        return Rerun(number, run.seed, run.exit, {}, problem)
    try:
        outcomes = parse_junit(run.junit)
    except JUnitError as exc:
        return Rerun(number, run.seed, run.exit, {}, f"pytest exited {run.exit} with {exc}")
    return Rerun(number, run.seed, run.exit, outcomes)


def runs_text(reruns: Sequence[Rerun]) -> str:
    """``run 2 (seed 2)`` or ``runs 2, 5 (seeds 2, 5)``."""
    numbers = ", ".join(str(run.number) for run in reruns)
    seeds = ", ".join(str(run.seed) for run in reruns)
    if len(reruns) == 1:
        return f"run {numbers} (seed {seeds})"
    return f"runs {numbers} (seeds {seeds})"


@dataclass(frozen=True, slots=True)
class Flip:
    """A test whose outcome was not the same in every rerun, or that always failed."""

    test: str
    groups: tuple[tuple[str, tuple[Rerun, ...]], ...]
    """``(outcome, reruns)`` for each outcome the test had, in order of first appearance."""

    @property
    def reproduce(self) -> Rerun:
        """The first rerun in which the test did not pass: the one worth running again."""
        runs = sorted(
            (run for outcome, group in self.groups if outcome != PASSED for run in group),
            key=lambda run: run.number,
        )
        return runs[0]

    def describe(self) -> str:
        """``test: passed in runs 1, 4 (seeds 1, 4); failed in run 2 (seed 2)``."""
        parts = [f"{outcome} in {runs_text(group)}" for outcome, group in self.groups]
        text = f"{self.test}: {'; '.join(parts)}"
        if len(self.groups) == 1:
            text += " but the grader passed TG401's run (file order, PYTHONHASHSEED=0)"
        return text


def flips(reruns: Sequence[Rerun]) -> tuple[Flip, ...]:
    """Every test that flipped across the reruns that have outcomes, sorted by id."""
    usable = [run for run in reruns if run.problem is None]
    tests = sorted({test for run in usable for test in run.outcomes})
    found: list[Flip] = []
    for test in tests:
        groups: dict[str, list[Rerun]] = {}
        for run in usable:
            groups.setdefault(run.outcomes.get(test, NOT_RUN), []).append(run)
        if len(groups) > 1 or set(groups) & _BAD:
            found.append(Flip(test, tuple((o, tuple(runs)) for o, runs in groups.items())))
    return tuple(found)


def count_tests(reruns: Sequence[Rerun]) -> int:
    """How many distinct tests the reruns reported."""
    return len({test for run in reruns for test in run.outcomes})
