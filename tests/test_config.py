from pathlib import Path

import pytest

from taskgate.config import (
    CONFIG_FILE,
    Config,
    ConfigError,
    ManifestOptions,
    discover,
    load_file,
    parse,
)
from taskgate.results import ConfigSummary, Severity


def test_defaults_change_nothing() -> None:
    config = Config()
    assert config.source is None
    assert config.severity_for("TG101", Severity.ERROR) is Severity.ERROR
    assert config.summary() == ConfigSummary()


def test_disable_and_severity_overrides() -> None:
    config = parse(
        '[gates]\ndisable = ["TG203", "TG104"]\n[gates.severity]\nTG201 = "warning"\n',
        "taskgate.toml",
    )
    assert config.disabled == {"TG104", "TG203"}
    assert config.severity_for("TG201", Severity.ERROR) is Severity.WARNING
    assert config.summary() == ConfigSummary(
        source="taskgate.toml",
        disabled=("TG104", "TG203"),
        severity=(("TG201", Severity.WARNING),),
    )


def test_every_problem_is_listed_at_once() -> None:
    text = """\
extra = 1
[gates]
disable = ["TG1", 7]
colour = "red"
[gates.severity]
tg101 = "error"
TG102 = "fatal"
TG103 = ["error"]
"""
    with pytest.raises(ConfigError) as error:
        parse(text, "taskgate.toml")
    assert str(error.value).splitlines() == [
        "invalid taskgate.toml:",
        "  unknown section [extra]",
        "  unknown key gates.colour",
        "  gates.disable must be a list of gate codes",
        "  gates.severity: 'tg101' is not a gate code like TG101",
        "  gates.severity.TG102 must be one of error, warning, info, not 'fatal'",
        "  gates.severity.TG103 must be one of error, warning, info, not ['error']",
    ]


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("gates = 1\n", "[gates] must be a table"),
        ("[gates]\nseverity = 1\n", "gates.severity must be a table"),
        ('[gates]\ndisable = ["TG1"]\n', "gates.disable: 'TG1' is not a gate code like TG101"),
        ("[gates\n", "does not parse"),
    ],
)
def test_malformed_values(text: str, problem: str) -> None:
    with pytest.raises(ConfigError, match=r"invalid x\.toml:") as error:
        parse(text, "x.toml")
    assert problem in str(error.value)


def test_unknown_codes_are_rejected_against_the_registry() -> None:
    config = parse('[gates]\ndisable = ["TG101", "TG990"]\n', "taskgate.toml")
    config.validate_codes({"TG101"} | {"TG990"})
    with pytest.raises(ConfigError, match="unknown gate code"):
        config.validate_codes({"TG101"})


def test_discover_and_load_file(tmp_path: Path) -> None:
    assert discover(tmp_path) == Config()
    (tmp_path / CONFIG_FILE).write_text('[gates]\ndisable = ["TG101"]\n', encoding="utf-8")
    assert discover(tmp_path).source == "taskgate.toml"
    assert load_file(tmp_path / CONFIG_FILE).source == str(tmp_path / CONFIG_FILE)
    with pytest.raises(ConfigError, match="cannot read"):
        load_file(tmp_path / "missing.toml")


def test_manifest_section_sets_the_recommended_timeout_range() -> None:
    config = parse("[manifest]\nmin_timeout_sec = 30\nmax_timeout_sec = 900\n", "taskgate.toml")
    assert config.manifest == ManifestOptions(min_timeout_sec=30, max_timeout_sec=900)
    assert config.summary().options == (
        ("manifest.min_timeout_sec", 30),
        ("manifest.max_timeout_sec", 900),
    )
    same_as_default = parse("[manifest]\nmin_timeout_sec = 10\n", "taskgate.toml")
    assert same_as_default.summary().options == ()
    assert Config().manifest == ManifestOptions(min_timeout_sec=10, max_timeout_sec=1800)


def test_manifest_section_problems_are_listed_at_once() -> None:
    text = "[manifest]\nmin_timeout_sec = 0\nmax_timeout_sec = true\nmax = 3\n"
    with pytest.raises(ConfigError) as error:
        parse(text, "taskgate.toml")
    assert str(error.value).splitlines() == [
        "invalid taskgate.toml:",
        "  unknown key manifest.max",
        "  manifest.min_timeout_sec 0 is outside 1..3600",
        "  manifest.max_timeout_sec must be an integer",
    ]


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("manifest = 3\n", "[manifest] must be a table"),
        (
            "[manifest]\nmin_timeout_sec = 600\nmax_timeout_sec = 60\n",
            "manifest.min_timeout_sec 600 is above max_timeout_sec 60",
        ),
    ],
)
def test_manifest_section_values(text: str, problem: str) -> None:
    with pytest.raises(ConfigError) as error:
        parse(text, "taskgate.toml")
    assert str(error.value).splitlines()[1:] == [f"  {problem}"]
