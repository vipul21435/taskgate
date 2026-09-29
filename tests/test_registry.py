import tomllib
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from taskgate.gates import BUILTIN_GATES
from taskgate.registry import (
    BUILTIN,
    ENTRY_POINT_GROUP,
    GROUPS,
    RegistryError,
    group_label,
    group_of,
    load_plugins,
    load_registry,
)

EXAMPLE_PLUGIN = Path(__file__).resolve().parents[1] / "examples" / "plugin"


def ep(name: str, attr: str, module: str = "plugin_fixtures") -> EntryPoint:
    return EntryPoint(name=name, value=f"{module}:{attr}", group=ENTRY_POINT_GROUP)


def test_builtin_registry_is_sorted_and_marked_built_in() -> None:
    registry = load_registry(plugins=False)
    assert registry.gates == BUILTIN_GATES
    assert all(entry.builtin and entry.source == BUILTIN for entry in registry.entries)
    assert registry.plugin_count == 0
    assert registry.codes == {g.code for g in BUILTIN_GATES}


def test_groups_are_the_hundreds_digit() -> None:
    assert group_of("TG401") == 4
    assert group_label(2) == "TG2xx  hygiene"
    assert set(GROUPS) == set(range(1, 10))


def test_plugins_can_be_a_gate_a_sequence_a_factory_or_a_class_instance() -> None:
    registry = load_registry(
        eps=[
            ep("z-single", "SINGLE"),
            ep("pair", "PAIR"),
            ep("factory", "factory"),
            ep("class", "ClassGate"),
        ]
    )
    plugins = {e.gate.code: e.source for e in registry.entries if not e.builtin}
    assert plugins == {
        "TG801": "plugin z-single",
        "TG802": "plugin pair",
        "TG803": "plugin pair",
        "TG807": "plugin factory",
        "TG808": "plugin class",
    }
    codes = [g.code for g in registry.gates]
    assert codes == sorted(codes)
    assert registry.plugin_count == 5


@pytest.mark.parametrize(
    ("attr", "message"),
    [
        ("NOT_A_GATE", "expected a gate, a list of gates or a callable returning one, got int"),
        ("MIXED", "not a gate: str"),
        ("returns_callable", "got function"),
        ("broken_factory", "RuntimeError: cannot build gates"),
        ("MISSING", "AttributeError"),
    ],
)
def test_bad_entry_points_are_named_in_the_error(attr: str, message: str) -> None:
    with pytest.raises(RegistryError) as error:
        load_plugins([ep("bad", attr)])
    assert f"entry point 'bad' (plugin_fixtures:{attr})" in str(error.value)
    assert message in str(error.value)


def test_a_missing_module_is_reported() -> None:
    with pytest.raises(RegistryError, match="ModuleNotFoundError"):
        load_plugins([ep("gone", "GATE", module="no_such_module_here")])


def test_every_contract_violation_is_listed_at_once() -> None:
    with pytest.raises(RegistryError) as error:
        load_registry(
            eps=[
                ep("reserved", "RESERVED"),
                ep("malformed", "MALFORMED"),
                ep("bad-fields", "BAD_FIELDS"),
                ep("single", "SINGLE"),
                ep("again", "SINGLE"),
                ep("dup-name", "DUPLICATE_NAME"),
            ]
        )
    lines = str(error.value).splitlines()
    assert lines[0] == "invalid gate registry:"
    assert [line.strip() for line in lines[1:]] == [
        "TG150 (plugin reserved): TG1xx-TG6xx are reserved for built-in gates; use TG7xx-TG9xx",
        "'TG7' (plugin malformed): code must be TG followed by three digits",
        "TG801: registered twice (plugin again and plugin single)",
        "TG805 (plugin bad-fields): name 'Bad_Name' must be kebab-case",
        "TG805 (plugin bad-fields): severity must be a taskgate.results.Severity",
        "TG805 (plugin bad-fields): summary must be a non-empty string",
        "TG805 (plugin bad-fields): fix_hint must be a non-empty string",
        "TG805: requires TG999, which is not a gate with a lower code",
        "TG806: name 'single' is already used by TG801",
    ]


def test_installed_example_plugin_is_discovered_through_its_entry_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate `pip install examples/plugin`: a dist-info with the declared entry points."""
    project = tomllib.loads((EXAMPLE_PLUGIN / "pyproject.toml").read_text(encoding="utf-8"))
    declared = project["project"]["entry-points"][ENTRY_POINT_GROUP]
    dist_info = tmp_path / "taskgate_todo_gate-0.1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: taskgate-todo-gate\nVersion: 0.1.0\n", encoding="utf-8"
    )
    lines = [f"[{ENTRY_POINT_GROUP}]"] + [f"{k} = {v}" for k, v in declared.items()]
    (dist_info / "entry_points.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(EXAMPLE_PLUGIN))
    monkeypatch.syspath_prepend(str(tmp_path))

    registry = load_registry()
    (plugin,) = [entry for entry in registry.entries if not entry.builtin]
    assert (plugin.gate.code, plugin.gate.name, plugin.source) == (
        "TG701",
        "no-todo-markers",
        "plugin todo-markers",
    )
