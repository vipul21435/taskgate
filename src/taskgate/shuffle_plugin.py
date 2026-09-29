"""Seeded test order: the pytest plugin behind the determinism gate (TG501).

TaskGate copies this file into every grader rerun as ``taskgate_shuffle.py``, puts
its directory on ``PYTHONPATH`` and loads it with ``-p taskgate_shuffle
--taskgate-seed N``. With a seed, the plugin seeds Python's ``random`` module
before collection and shuffles the collected tests with ``random.Random(N)``, so
a run's test order and its unseeded random draws follow from its seed alone.
Without the option it does nothing.

It imports nothing from TaskGate and uses hooks that have been stable for many
pytest releases, because inside a Docker task image it runs under whatever
Python and pytest that image provides.
"""

from __future__ import annotations

import random

import pytest

OPTION = "--taskgate-seed"
DEST = "taskgate_seed"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.getgroup("taskgate").addoption(
        OPTION,
        dest=DEST,
        type=int,
        default=None,
        metavar="N",
        help="shuffle the collected tests with seed N and seed the random module with N",
    )


def _seed(config: pytest.Config) -> int | None:
    value = config.getoption(DEST)
    return value if isinstance(value, int) else None


def pytest_configure(config: pytest.Config) -> None:
    seed = _seed(config)
    if seed is not None:
        random.seed(seed)


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Shuffle last, after any other plugin has reordered the tests."""
    seed = _seed(config)
    if seed is not None:
        random.Random(seed).shuffle(items)


def pytest_report_header(config: pytest.Config) -> str | None:
    seed = _seed(config)
    return None if seed is None else f"taskgate: test order shuffled with seed {seed}"
