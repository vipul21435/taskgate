"""The bundled pytest plugin that shuffles test order for TG501, run in-process."""

import random

import pytest

from taskgate import shuffle_plugin

PLUGIN = ["-p", "taskgate.shuffle_plugin"]
TESTS = "\n".join(f"def test_{n}():\n    pass\n" for n in "abcdef")


def order(pytester: pytest.Pytester, *args: str) -> list[str]:
    result = pytester.runpytest_inprocess(*PLUGIN, "-v", *args)
    result.assert_outcomes(passed=6)
    return [line.split("::")[1].split()[0] for line in result.outlines if "PASSED" in line]


def test_a_seed_shuffles_the_tests_the_same_way_every_time(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_order=TESTS)
    file_order = [f"test_{n}" for n in "abcdef"]
    assert order(pytester) == file_order
    shuffled = order(pytester, "--taskgate-seed", "3")
    expected = list(file_order)
    random.Random(3).shuffle(expected)
    assert shuffled == expected != file_order
    assert order(pytester, "--taskgate-seed", "3") == shuffled
    assert order(pytester, "--taskgate-seed", "4") != shuffled


def test_a_seed_also_seeds_the_random_module(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_draw="""
import random

FIRST = random.random()


def test_draw():
    assert FIRST == random.Random(9).random()
"""
    )
    pytester.runpytest_inprocess(*PLUGIN, "--taskgate-seed", "9").assert_outcomes(passed=1)
    pytester.runpytest_inprocess(*PLUGIN, "--taskgate-seed", "8").assert_outcomes(failed=1)


def test_the_header_names_the_seed(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_one="def test_one():\n    pass\n")
    seeded = pytester.runpytest_inprocess(*PLUGIN, "--taskgate-seed", "5")
    seeded.stdout.fnmatch_lines(["taskgate: test order shuffled with seed 5"])
    plain = pytester.runpytest_inprocess(*PLUGIN)
    assert "taskgate:" not in plain.stdout.str()


def test_the_option_is_named_as_the_runners_pass_it() -> None:
    assert shuffle_plugin.OPTION == "--taskgate-seed"
