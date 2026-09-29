from pathlib import Path

import pytest

from taskgate.config import (
    CONFIG_FILE,
    Config,
    ConfigError,
    DeterminismOptions,
    FileOptions,
    ManifestOptions,
    RunnerOptions,
    SecretOptions,
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


def test_secrets_and_files_sections() -> None:
    text = """\
[secrets]
allow = ["^EXAMPLE"]
exclude = ["tests/data/*"]
min_length = 32
entropy_threshold = 4.5

[files]
max_file_bytes = 2048
max_task_bytes = 65536
binary_allow = ["environment/workspace/*.png"]
"""
    config = parse(text, "taskgate.toml")
    assert config.secrets == SecretOptions(
        allow=("^EXAMPLE",), exclude=("tests/data/*",), min_length=32, entropy_threshold=4.5
    )
    assert config.files == FileOptions(
        max_file_bytes=2048, max_task_bytes=65536, binary_allow=("environment/workspace/*.png",)
    )
    assert dict(config.summary().options) == {
        "secrets.allow": ("^EXAMPLE",),
        "secrets.exclude": ("tests/data/*",),
        "secrets.min_length": 32,
        "secrets.entropy_threshold": 4.5,
        "files.max_file_bytes": 2048,
        "files.max_task_bytes": 65536,
        "files.binary_allow": ("environment/workspace/*.png",),
    }
    assert parse("[secrets]\nentropy_threshold = 5\n", "t").secrets.entropy_threshold == 5.0


def test_secrets_and_files_problems_are_listed_at_once() -> None:
    text = """\
[secrets]
allow = ["(unclosed"]
exclude = "tests/*"
min_length = 4
entropy_threshold = "high"
colour = 1

[files]
max_file_bytes = 0
max_task_bytes = 1.5
binary_allow = [1]
"""
    with pytest.raises(ConfigError) as error:
        parse(text, "taskgate.toml")
    lines = str(error.value).splitlines()
    assert lines[0] == "invalid taskgate.toml:"
    assert lines[1] == "  unknown key secrets.colour"
    assert lines[2].startswith("  secrets.allow: '(unclosed' is not a valid regex (")
    assert lines[3:] == [
        "  secrets.exclude must be a list of strings",
        "  secrets.min_length 4 is outside 8..1024",
        "  secrets.entropy_threshold must be a number",
        "  files.max_file_bytes 0 is outside 1..1099511627776",
        "  files.max_task_bytes must be an integer",
        "  files.binary_allow must be a list of strings",
    ]


def test_entropy_threshold_range() -> None:
    with pytest.raises(ConfigError, match=r"secrets.entropy_threshold 9.0 is outside 1.0..8.0"):
        parse("[secrets]\nentropy_threshold = 9.0\n", "taskgate.toml")


def test_runner_section_sets_the_docker_limits() -> None:
    text = "[runner]\ncpus = 2\nmemory_mb = 512\npids_limit = 128\nbuild_timeout_sec = 60\n"
    config = parse(text, "taskgate.toml")
    assert config.runner == RunnerOptions(
        cpus=2.0, memory_mb=512, pids_limit=128, build_timeout_sec=60
    )
    assert dict(config.summary().options) == {
        "runner.cpus": 2.0,
        "runner.memory_mb": 512,
        "runner.pids_limit": 128,
        "runner.build_timeout_sec": 60,
    }
    assert Config().runner == RunnerOptions(
        cpus=1.0, memory_mb=1024, pids_limit=256, build_timeout_sec=900
    )


def test_runner_section_problems_are_listed_at_once() -> None:
    text = (
        "[runner]\ncpus = 0\nmemory_mb = 32\npids_limit = 1.5\nbuild_timeout_sec = 9000\nswap = 1\n"
    )
    with pytest.raises(ConfigError) as error:
        parse(text, "taskgate.toml")
    assert str(error.value).splitlines() == [
        "invalid taskgate.toml:",
        "  unknown key runner.swap",
        "  runner.cpus 0 is outside 0.1..64.0",
        "  runner.memory_mb 32 is outside 64..65536",
        "  runner.pids_limit must be an integer",
        "  runner.build_timeout_sec 9000 is outside 10..7200",
    ]


def test_determinism_runs_and_seed() -> None:
    config = parse("[determinism]\nruns = 10\nseed = 100\n", "taskgate.toml")
    assert config.determinism == DeterminismOptions(runs=10, seed=100)
    assert config.determinism.seeds == tuple(range(100, 110))
    assert Config().determinism.seeds == (1, 2, 3, 4, 5)
    assert config.summary().options == (("determinism.runs", 10), ("determinism.seed", 100))


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("[determinism]\nruns = 1\n", "determinism.runs 1 is outside 2..100"),
        ("[determinism]\nruns = 101\n", "determinism.runs 101 is outside 2..100"),
        ("[determinism]\nseed = -1\n", "determinism.seed -1 is outside 0..2147483647"),
        ('[determinism]\nseed = "1"\n', "determinism.seed must be an integer"),
        ("[determinism]\nrerun = 3\n", "unknown key determinism.rerun"),
    ],
)
def test_bad_determinism_options(text: str, problem: str) -> None:
    with pytest.raises(ConfigError) as error:
        parse(text, "taskgate.toml")
    assert problem in str(error.value)
