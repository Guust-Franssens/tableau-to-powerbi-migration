"""Regression tests for the earned-clear path out of the credential gate (#346).

A successful probe is the ONLY way to earn a clear. Until 2026-08-27 it could not actually do so on
any real estate: `_lift_gate` passed a human-readable count ("2 live source(s)") as `--sources`,
`clear_block` diffed that against the marker's real source names, nothing matched, and the
partial-clear branch left the gate **armed** -- while still writing a `probe-cleared` audit entry
and exiting 0.

The failure was invisible from the probe's own output (it printed `PROBE: DATA_OK`) and fail-safe
for artifacts (nothing unvalidated could be written), which is exactly why it survived: the only
symptom was that humans on a real 44-unit estate found `authorize` was the only thing that worked,
and so permanently marked every build UNVALIDATED.

Two properties are pinned here, and the second matters more than the first:

1. proving EVERY live source leg earns a full clear;
2. proving a SUBSET earns only a PARTIAL clear -- a full clear on partial proof would lift a gate
   covering sources nobody ever contacted.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import credential_gate as cg  # noqa: E402
import preflight_source_credentials as pf  # noqa: E402
import probe_live_source as pls  # noqa: E402
from parse_tableau import parse_workbook  # noqa: E402

FEDERATED_FIXTURE = REPO / "tests" / "fixtures" / "federated_multi_connection.twb"
SOURCE_NAMES = ["A", "B"]
NAMED = [
    pf._leg_key(
        {"name": name, "connection": {}},
        index,
        {"class": "sqlserver", "server": f"{name}.example", "database": "db", "powerbi_target": "live_source"},
    )
    for index, name in enumerate(SOURCE_NAMES)
]


def _armed(tmp_path: Path) -> Path:
    """A migration whose gate is armed with NAMED sources -- what the real classifier produces."""
    d = tmp_path / "unit"
    (d / "fabric").mkdir(parents=True)
    cg.apply_block(d, list(NAMED), force_scope=True)
    assert (d / cg.MARKER).exists(), "fixture must start armed, or it proves nothing"
    return d


def _audit_actions(d: Path) -> list[str]:
    return [json.loads(line)["action"] for line in (d / cg.AUDIT).read_text(encoding="utf-8").splitlines()]


def test_proving_every_live_source_fully_clears_a_named_source_gate(tmp_path: Path) -> None:
    """The #346 regression. Before the fix this left the marker in place and status at 1."""
    d = _armed(tmp_path)
    pls._lift_gate(d, "2 live source leg(s)", NAMED)

    assert not (d / cg.MARKER).exists(), "gate must be fully lifted when every live source was proved"
    assert cg.status(d) == 0
    assert "probe-cleared" in _audit_actions(d)


def test_the_count_string_is_never_sent_as_a_source_name(tmp_path: Path) -> None:
    """Root cause, pinned directly.

    `what` is prose for humans and is already carried in `--reason`. If it ever returns to
    `--sources`, it matches no marker entry and the partial-clear branch silently re-arms the bug.
    """
    d = _armed(tmp_path)
    pls._lift_gate(d, "2 live source leg(s)", NAMED)

    detail = " ".join(
        json.loads(line).get("detail", "") for line in (d / cg.AUDIT).read_text(encoding="utf-8").splitlines()
    )
    assert "sources=['2 live source leg(s)']" not in detail
    assert "live source leg(s)" in detail, "the count should still appear, as the human-readable reason"


def test_proving_only_a_subset_partially_clears_the_gate(tmp_path: Path) -> None:
    """The restored #357 guard refuses a partial proof for a multi-key marker."""
    d = _armed(tmp_path)
    pls._lift_gate(d, "1 live source leg(s)", [NAMED[0]])

    assert (d / cg.MARKER).exists(), "a partial proof must leave the gate armed"
    marker = json.loads((d / cg.MARKER).read_text(encoding="utf-8"))
    assert marker["sources"] == NAMED
    assert cg.status(d) == 1
    assert "probe-cleared" not in _audit_actions(d), "partial proof must not record earned evidence"


def test_run_probe_passes_marker_names_not_indices_or_counts(tmp_path: Path, monkeypatch) -> None:
    """`run_probe` must speak the marker's name keyspace, not datasource indices or counts."""
    calls: list[list[str]] = []

    def _stub(_migration, _what, source_names):
        calls.append(source_names)
        return bool(source_names)

    monkeypatch.setattr(pls, "_lift_gate", _stub)
    monkeypatch.setattr(pls, "_probe_one", lambda *_a, **_k: (0, "DATA_OK"))

    sources = [
        {
            "name": SOURCE_NAMES[0],
            "connection": {
                "class": "sqlserver",
                "server": "a.example",
                "database": "db",
                "powerbi_target": "live_source",
            },
            "tables": [{"name": "Orders"}],
            "fields": [{"kind": "column", "internal_name": "[Order ID]"}],
        },
        {
            "name": SOURCE_NAMES[1],
            "connection": {
                "class": "sqlserver",
                "server": "b.example",
                "database": "db",
                "powerbi_target": "live_source",
            },
            "tables": [{"name": "Orders"}],
            "fields": [{"kind": "column", "internal_name": "[Order ID]"}],
        },
    ]
    expected = [pf._leg_key(source, index, source["connection"]) for index, source in enumerate(sources)]
    bundle = type("B", (), {"data_sources": sources, "kind": "spec", "label": "x", "migration_dir": tmp_path})()
    monkeypatch.setattr(pls, "load_bundle", lambda _p: bundle)

    assert pls.run_probe(tmp_path, None, 60, False) == 0
    assert calls == [expected]

    calls.clear()
    assert pls.run_probe(tmp_path, 0, 60, False) == 0
    assert calls == [[expected[0]]], "a --source-index run must pass the proven source's marker key"


def _bundle(tmp_path: Path, live_count: int, monkeypatch):
    """A bundle with `live_count` live sources whose names match the armed gate."""
    d = tmp_path / f"u{live_count}"
    (d / "fabric").mkdir(parents=True)
    cg.apply_block(d, list(NAMED), force_scope=True)
    sources = [
        {
            "name": SOURCE_NAMES[i],
            "connection": {
                "class": "sqlserver",
                "server": f"{SOURCE_NAMES[i]}.example",
                "database": "db",
                "powerbi_target": "live_source",
            },
            "tables": [{"name": "Orders"}],
            "fields": [{"kind": "column", "internal_name": "[Order ID]"}],
        }
        for i in range(live_count)
    ]
    if not sources:
        sources = [{"connection": {"powerbi_target": "flat_file"}}]
    bundle = type("B", (), {"data_sources": sources, "kind": "spec", "label": "x", "migration_dir": d})()
    monkeypatch.setattr(pls, "load_bundle", lambda _p: bundle)
    monkeypatch.setattr(pls, "_probe_one", lambda *_a, **_k: (0, "DATA_OK"))
    return d


def test_an_empty_probe_result_never_clears_anything(tmp_path: Path, monkeypatch) -> None:
    """Fail-open guard: zero live legs proved must never produce an earned clear."""
    d = _bundle(tmp_path, 0, monkeypatch)
    rc = pls.run_probe(d, 0, 60, False)

    assert (d / cg.MARKER).exists(), "empty proof must never clear"
    assert cg.status(d) == 1
    assert "probe-cleared" not in _audit_actions(d)
    assert rc != 0


def test_source_index_never_clears_unmatched_marker_names(tmp_path: Path, monkeypatch) -> None:
    """A probe may only clear marker names it actually matches."""
    d = tmp_path / "unmatched"
    (d / "fabric").mkdir(parents=True)
    cg.apply_block(d, ["source-key:not-in-probe"], force_scope=True)
    sources = [
        {
            "name": "not-in-marker",
            "connection": {
                "class": "sqlserver",
                "server": "not-in-marker.example",
                "database": "db",
                "powerbi_target": "live_source",
            },
            "tables": [{"name": "Orders"}],
            "fields": [{"kind": "column", "internal_name": "[Order ID]"}],
        }
    ]
    bundle = type("B", (), {"data_sources": sources, "kind": "spec", "label": "x", "migration_dir": d})()
    monkeypatch.setattr(pls, "load_bundle", lambda _p: bundle)
    monkeypatch.setattr(pls, "_probe_one", lambda *_a, **_k: (0, "DATA_OK"))
    rc = pls.run_probe(d, 0, 60, False)

    assert (d / cg.MARKER).exists(), "unknown names must never clear"
    assert cg.status(d) == 1, "the deny-ACL must still be applied"
    assert "probe-cleared" not in _audit_actions(d), "no earned evidence may be written"
    assert rc != 0, "a refusal must not report success"


def test_duplicate_display_names_do_not_clear_an_uncontacted_sibling(tmp_path: Path, monkeypatch) -> None:
    """HIGH 1: duplicate human names are not identities; stable keys keep siblings separate."""
    d = tmp_path / "duplicate-display"
    (d / "fabric").mkdir(parents=True)
    sources = [
        {
            "name": "Sales",
            "connection": {
                "class": "sqlserver",
                "server": "one.example",
                "database": "db",
                "powerbi_target": "live_source",
            },
            "tables": [{"name": "Orders"}],
            "fields": [{"kind": "column", "internal_name": "[Order ID]"}],
        },
        {
            "name": "Sales",
            "connection": {
                "class": "sqlserver",
                "server": "two.example",
                "database": "db",
                "powerbi_target": "live_source",
            },
            "tables": [{"name": "Orders"}],
            "fields": [{"kind": "column", "internal_name": "[Order ID]"}],
        },
    ]
    keys = [pf._leg_key(source, index, source["connection"]) for index, source in enumerate(sources)]
    cg.apply_block(d, keys, force_scope=True)
    contacted: list[str] = []

    monkeypatch.setattr(
        pls,
        "load_bundle",
        lambda _p: type("B", (), {"data_sources": sources, "kind": "spec", "label": "x", "migration_dir": d})(),
    )
    monkeypatch.setattr(
        pls,
        "_probe_one",
        lambda _migration, _sources, idx, *_args: contacted.append(keys[idx]) or (0, "DATA_OK"),
    )

    assert pls.run_probe(d, 0, 60, False) == 3
    marker = json.loads((d / cg.MARKER).read_text(encoding="utf-8"))
    assert marker["sources"] == keys
    assert contacted == [keys[0]]


def test_semicolon_in_source_key_does_not_forget_real_clear(tmp_path: Path) -> None:
    """HIGH 1: structured audit sources survive semicolons in source identities."""
    d = tmp_path / "semicolon"
    (d / "fabric").mkdir(parents=True)
    key = "source[0];west"
    cg.apply_block(d, [key], force_scope=True)

    assert cg.clear_block(d, "probe", earned=True, sources=[key]) == 0
    assert cg._clear_was_earned(d) == "probe-cleared"  # pylint: disable=protected-access
    (d / "fabric" / "Model.tmdl").write_text("table x", encoding="utf-8")
    assert cg.verify(d) == 0


def test_refusing_to_clear_does_not_report_success(tmp_path: Path, monkeypatch) -> None:
    """`run_probe` used to log 'DATA_OK all N reachable' and return 0 after deliberately refusing.

    Self-contradictory output, and a caller keying on exit 0 as 'gate lifted, proceed' is misled.
    """
    d = _bundle(tmp_path, 2, monkeypatch)
    assert pls.run_probe(d, 0, 60, False) == 3
    marker = json.loads((d / cg.MARKER).read_text(encoding="utf-8"))
    assert marker["sources"] == NAMED, "--source-index must not partially clear a multi-source marker"
    assert pls.run_probe(d, None, 60, False) == 0, "the all-sources path must still succeed"


def test_reorder_between_arm_and_clear_does_not_clear_an_uncontacted_endpoint(tmp_path: Path, monkeypatch) -> None:
    """HIGH 1: positional aliasing must not let probing A twice clear B."""
    first = [
        {
            "name": "A",
            "connection": {
                "class": "sqlserver",
                "server": "a.example",
                "database": "db",
                "powerbi_target": "live_source",
            },
        },
        {
            "name": "B",
            "connection": {
                "class": "sqlserver",
                "server": "b.example",
                "database": "db",
                "powerbi_target": "live_source",
            },
        },
    ]
    for source in first:
        source["tables"] = [{"name": "Orders"}]
        source["fields"] = [{"kind": "column", "internal_name": "[Order ID]"}]
    swapped = [first[1], first[0]]
    names = [pf._leg_key(source, index, source["connection"]) for index, source in enumerate(first)]
    d = tmp_path / "reordered"
    (d / "fabric").mkdir(parents=True)
    cg.apply_block(d, names, force_scope=True)
    contacted: list[str] = []
    current_sources = first

    def _bundle(_path):
        return type("B", (), {"data_sources": current_sources, "kind": "spec", "label": "x", "migration_dir": d})()

    def _probe_one(_migration, sources, idx, *_args):
        contacted.append(sources[idx]["connection"]["server"])
        return 0, "DATA_OK"

    monkeypatch.setattr(pls, "load_bundle", _bundle)
    monkeypatch.setattr(pls, "_probe_one", _probe_one)

    assert pls.run_probe(d, 0, 60, False) == 3
    current_sources = swapped
    assert pls.run_probe(d, 1, 60, False) == 3
    assert contacted == ["a.example", "a.example"]
    assert (d / cg.MARKER).exists()
    assert "probe-cleared" not in _audit_actions(d)


def test_source_keys_survive_datasource_reordering() -> None:
    """HIGH 1: marker keys are endpoint identities, not array positions."""
    first = [
        {
            "name": "A",
            "connection": {
                "class": "sqlserver",
                "server": "a.example",
                "database": "db",
                "powerbi_target": "live_source",
            },
        },
        {
            "name": "B",
            "connection": {
                "class": "sqlserver",
                "server": "b.example",
                "database": "db",
                "powerbi_target": "live_source",
            },
        },
    ]
    swapped = [first[1], first[0]]

    first_keys = {
        source["name"]: pf._leg_key(source, index, source["connection"]) for index, source in enumerate(first)
    }
    swapped_keys = {
        source["name"]: pf._leg_key(source, index, source["connection"]) for index, source in enumerate(swapped)
    }

    assert first_keys == swapped_keys


def test_snowflake_role_changes_the_source_key() -> None:
    """HIGH 1: a role changes the generated M query and therefore the credential proof identity."""
    base = {"class": "snowflake", "server": "acct.snowflakecomputing.com", "database": "DB", "warehouse": "WH"}
    analyst = {**base, "role": "ANALYST"}
    restricted = {**base, "role": "RESTRICTED"}

    assert pf._leg_key({}, 0, analyst) != pf._leg_key({}, 0, restricted)
    analyst_query, _ = pls.build_m_query(analyst, "Orders", "Order ID")
    restricted_query, _ = pls.build_m_query(restricted, "Orders", "Order ID")
    assert analyst_query != restricted_query


def test_schema_changes_the_source_key_for_probe_connectors() -> None:
    """HIGH 2: schema changes generated M for every supported probe connector."""
    cases = [
        {"class": "sqlserver", "server": "sql.example", "database": "DB"},
        {
            "class": "databricks",
            "server": "adb.example",
            "database": "DB",
            "http_path": "/sql/1.0/warehouses/x",
        },
        {"class": "snowflake", "server": "acct.snowflakecomputing.com", "database": "DB", "warehouse": "WH"},
    ]
    for base in cases:
        dbo = {**base, "schema": "dbo"}
        restricted = {**base, "schema": "restricted"}
        assert pf._leg_key({}, 0, dbo) != pf._leg_key({}, 0, restricted)
        assert pls.build_m_query(dbo, "Orders", "Order ID")[0] != pls.build_m_query(restricted, "Orders", "Order ID")[0]


def _query_and_key(conn: dict) -> tuple[str, str]:
    return pls.build_m_query(conn, "Orders", "Order ID")[0], pf._leg_key({}, 0, conn)


class RecordingDict(dict):
    """Mapping that records every key a consumer could use to branch on field identity."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.accessed: set[str] = set()

    def get(self, key, default=None):  # noqa: ANN001, ANN202 - dict protocol mirror
        self.accessed.add(str(key))
        return super().get(key, default)

    def __getitem__(self, key):  # noqa: ANN001, ANN204 - dict protocol mirror
        self.accessed.add(str(key))
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        self.accessed.add(str(key))
        return super().__contains__(key)

    def __iter__(self):
        self.accessed.update(str(key) for key in super().keys())
        return super().__iter__()

    def items(self):
        self.accessed.update(str(key) for key in super().keys())
        return super().items()

    def keys(self):
        self.accessed.update(str(key) for key in super().keys())
        return super().keys()

    def values(self):
        self.accessed.update(str(key) for key in super().keys())
        return super().values()


def _query_fields(conn: dict) -> set[str]:
    recording = RecordingDict(conn)
    pls.build_m_query(recording, "Orders", "Order ID")
    return recording.accessed


def test_recording_dict_discovers_get_item_contains_iteration_and_views() -> None:
    """The property-test probe must see every key-access shape it promises to guard."""

    def _reader(conn: RecordingDict) -> None:
        _ = conn.get("via_get")
        _ = conn["via_item"]
        _ = "via_contains" in conn
        for _key in conn:
            pass
        _ = list(conn.items())
        _ = list(conn.keys())
        _ = list(conn.values())

    recorder = RecordingDict(
        {
            "via_get": 1,
            "via_item": 2,
            "via_contains": 3,
            "via_iter": 4,
            "via_items": 5,
            "via_keys": 6,
            "via_values": 7,
        }
    )
    _reader(recorder)
    assert {
        "via_get",
        "via_item",
        "via_contains",
        "via_iter",
        "via_items",
        "via_keys",
        "via_values",
    }.issubset(recorder.accessed)


def test_source_key_changes_whenever_connection_field_changes_probe_query() -> None:
    """Behavioural drift guard: if generated M changes, the credential proof identity must change."""
    # Tracks the connector branches in probe_live_source.build_m_query. If a branch is added there,
    # this expected set must change or the per-connector coverage assertion below fails.
    expected_connectors = {"sqlserver", "azure_sqldb", "databricks", "snowflake"}
    cases = [
        {
            "class": "sqlserver",
            "server": "sql.example",
            "database": "DB",
            "schema": "dbo",
            "port": "1433",
            "something_new": "BASE",
        },
        {"class": "azure_sqldb", "server": "sql.example", "database": "DB", "schema": "dbo", "something_new": "BASE"},
        {
            "class": "databricks",
            "server": "adb.example",
            "database": "DB",
            "schema": "default",
            "http_path": "/sql/1.0/warehouses/x",
            "something_new": "BASE",
        },
        {
            "class": "snowflake",
            "server": "acct.snowflakecomputing.com",
            "database": "DB",
            "schema": "PUBLIC",
            "warehouse": "WH",
            "role": "ANALYST",
            "something_new": "BASE",
        },
    ]
    assert {case["class"] for case in cases} == expected_connectors
    mutations = {
        "class": lambda value: value.upper(),
        "server": lambda value: value.upper(),
        "database": lambda value: value.swapcase(),
        "schema": lambda value: value.swapcase(),
        "http_path": lambda value: value + "/case",
        "warehouse": lambda value: value.swapcase(),
        "role": lambda value: value.swapcase(),
        "port": lambda value: str(int(value) + 1) if str(value).isdigit() else value + "2",
    }

    def fallback_mutation(field: str) -> str:
        return f"{field}_CHANGED"

    documented_exceptions = {
        "class": "connector class dispatch is intentionally case-insensitive",
        "server": "DNS hostnames are intentionally case-insensitive for credential identity",
    }
    exercised_by_connector: dict[str, int] = {}
    for base in cases:
        connector = base["class"]
        exercised_by_connector[connector] = 0
        base_query, base_key = _query_and_key(base)
        fields = _query_fields(base)
        assert fields, f"{connector}: instrumented mapping discovered no build_m_query fields"
        for field in fields:
            mutate = mutations.get(field, fallback_mutation)
            original = str(base.get(field) or field)
            changed = {**base, field: mutate(original)}
            changed_query, changed_key = _query_and_key(changed)
            if changed_query == base_query:
                continue
            exercised_by_connector[connector] += 1
            if field in documented_exceptions:
                assert changed_key == base_key, documented_exceptions[field]
            else:
                assert changed_key != base_key, f"{connector} {field} changes M and must change the key"
    assert exercised_by_connector, "property test generated no connector cases"
    assert all(count > 0 for count in exercised_by_connector.values()), exercised_by_connector


def test_missing_endpoint_identity_refuses_a_source_key() -> None:
    """HIGH 1: no fallback index/name key when endpoint identity is unavailable."""
    with pytest.raises(ValueError):
        pf._leg_key({}, 0, {})
    with pytest.raises(ValueError):
        pf._leg_key({"name": "OnlyName"}, 0, {"class": "sqlserver"})


# --- #353: armed per LEG, cleared per DATASOURCE -----------------------------------------------


def _federated(legs: list[dict]) -> dict:
    """One datasource whose connection wraps several named connections, as Tableau federates them."""
    return {
        "name": "Sales",
        "connection": {"powerbi_target": "live_source", "class": "federated", "connections": legs},
        "tables": [{"name": "Orders"}],
        "fields": [{"kind": "column", "internal_name": "[Order ID]"}],
    }


def _run_with(tmp_path: Path, monkeypatch, src: dict, names: list[str], reachable: bool):
    """Arm the gate with `names`, then run a FULL probe over `src`. Returns (rc, migration dir)."""
    d = tmp_path / "u"
    (d / "fabric").mkdir(parents=True)
    cg.apply_block(d, names, force_scope=True)
    monkeypatch.setattr(pls, "_probe_one", lambda *_a, **_k: (0, "DATA_OK" if reachable else "SKIPPED"))
    monkeypatch.setattr(
        pls,
        "load_bundle",
        lambda _p: type("B", (), {"data_sources": [src], "kind": "spec", "label": "x", "migration_dir": d})(),
    )
    return pls.run_probe(d, None, 60, False), d


def test_a_federated_source_cannot_clear_a_marker_naming_more_legs(tmp_path: Path, monkeypatch) -> None:
    """#353. A federated source that contacts no live legs proves nothing.

    `preflight_source_credentials._classify_legs` walks `connection.connections[]`, because one
    Tableau datasource can join Azure SQL + Snowflake + Databricks -- and its docstring records that
    under-reporting live sources "is the one direction this must never fail in". The clearing side
    had exactly that bug: measured 2026-08-27, a marker naming 3 legs cleared in full after the
    probe contacted the outer `federated` connection, which carries no server at all.
    """
    src = _federated(
        [
            {"class": "sqlserver", "server": "a.invalid", "dbname": "S"},
            {"class": "snowflake", "server": "b.invalid", "dbname": "S", "warehouse": "WH"},
            {"class": "databricks", "server": "c.invalid", "dbname": "S", "http_path": "/sql/1.0/warehouses/x"},
        ]
    )
    names = [key for key, _display, _verdict, _reason in pf._classify_legs(src, 0)]
    assert len(names) == 3, "fixture must arm the gate with three leg names, or it proves nothing"

    rc, d = _run_with(tmp_path, monkeypatch, src, names, reachable=False)

    assert (d / cg.MARKER).exists(), "a marker naming 3 legs must not clear on 0 endpoints contacted"
    assert cg.status(d) == 1
    assert "probe-cleared" not in _audit_actions(d), "no earned evidence for endpoints never reached"
    assert rc != 0


def test_a_federated_source_clears_after_all_legs_are_contacted(tmp_path: Path, monkeypatch) -> None:
    """#357 regression guard: federated sources are no longer permanently un-clearable."""
    src = _federated(
        [
            {"class": "sqlserver", "server": "a.example", "powerbi_target": "live_source"},
            {"class": "snowflake", "server": "b.example", "warehouse": "WH", "powerbi_target": "live_source"},
            {
                "class": "databricks",
                "server": "c.example",
                "http_path": "/sql/1.0/warehouses/x",
                "powerbi_target": "live_source",
            },
        ]
    )
    names = [key for key, _display, _verdict, _reason in pf._classify_legs(src, 0)]
    d = tmp_path / "federated-all"
    (d / "fabric").mkdir(parents=True)
    cg.apply_block(d, names, force_scope=True)
    contacted: list[str] = []

    def _probe_leg(_migration, leg_name, _conn, _target, _opts):
        contacted.append(leg_name)
        return 0, "DATA_OK"

    monkeypatch.setattr(pls, "_probe_leg", _probe_leg)
    monkeypatch.setattr(
        pls,
        "load_bundle",
        lambda _p: type("B", (), {"data_sources": [src], "kind": "spec", "label": "x", "migration_dir": d})(),
    )

    assert pls.run_probe(d, None, 60, False) == 0
    assert contacted == names
    assert not (d / cg.MARKER).exists()


def test_marker_with_more_leg_keys_than_probe_refuses(tmp_path: Path, monkeypatch) -> None:
    """The restored #357 guard: fewer proven legs than marker keys is a refusal."""
    src = _federated(
        [
            {"class": "sqlserver", "server": "a.example", "powerbi_target": "live_source"},
            {"class": "snowflake", "server": "b.example", "warehouse": "WH", "powerbi_target": "live_source"},
        ]
    )
    names = [key for key, _display, _verdict, _reason in pf._classify_legs(src, 0)]
    d = tmp_path / "marker-has-extra-leg"
    (d / "fabric").mkdir(parents=True)
    cg.apply_block(d, [*names, "source-key:extra"], force_scope=True)
    contacted: list[str] = []

    def _probe_leg(_migration, leg_name, _conn, _target, _opts):
        contacted.append(leg_name)
        return 0, "DATA_OK"

    monkeypatch.setattr(pls, "_probe_leg", _probe_leg)
    monkeypatch.setattr(
        pls,
        "load_bundle",
        lambda _p: type("B", (), {"data_sources": [src], "kind": "spec", "label": "x", "migration_dir": d})(),
    )

    assert pls.run_probe(d, None, 60, False) == 3
    assert contacted == names
    assert (d / cg.MARKER).exists()
    assert "probe-cleared" not in _audit_actions(d)


def test_a_federated_source_refuses_when_one_leg_fails(tmp_path: Path, monkeypatch) -> None:
    """A federated source earns no clear until every named leg is contacted successfully."""
    src = _federated(
        [
            {"class": "sqlserver", "server": "a.example", "powerbi_target": "live_source"},
            {"class": "snowflake", "server": "b.example", "warehouse": "WH", "powerbi_target": "live_source"},
            {
                "class": "databricks",
                "server": "c.example",
                "http_path": "/sql/1.0/warehouses/x",
                "powerbi_target": "live_source",
            },
        ]
    )
    names = [key for key, _display, _verdict, _reason in pf._classify_legs(src, 0)]
    d = tmp_path / "federated-one-fails"
    (d / "fabric").mkdir(parents=True)
    cg.apply_block(d, names, force_scope=True)
    contacted: list[str] = []

    def _probe_leg(_migration, leg_name, _conn, _target, _opts):
        contacted.append(leg_name)
        if len(contacted) == 2:
            return 1, "UNREACHABLE"
        return 0, "DATA_OK"

    monkeypatch.setattr(pls, "_probe_leg", _probe_leg)
    monkeypatch.setattr(
        pls,
        "load_bundle",
        lambda _p: type("B", (), {"data_sources": [src], "kind": "spec", "label": "x", "migration_dir": d})(),
    )

    assert pls.run_probe(d, None, 60, False) == 1
    assert contacted == names[:2]
    assert (d / cg.MARKER).exists()
    assert "probe-cleared" not in _audit_actions(d)


def test_real_federated_fixture_builds_probe_queries_for_azure_sqldb() -> None:
    """HIGH 4: the real federated fixture's Azure SQL leg must reach probe-query construction."""
    source = parse_workbook(FEDERATED_FIXTURE)["data_sources"][0]
    targets = pls._resolve_probe_targets([source], 0)  # pylint: disable=protected-access
    first_name, first_conn, tables, column = targets[0]

    assert first_conn["class"] == "azure_sqldb"
    assert first_name.startswith("source-key:")
    query, note = pls.build_m_query(first_conn, tables[0]["name"], column)
    assert "Sql.Database" in query
    assert "tableaumigration.database.windows.net" in note
    assert pls._default_port(first_conn) == 1433  # pylint: disable=protected-access


def test_a_single_connection_source_still_earns_its_clear(tmp_path: Path, monkeypatch) -> None:
    """The guard must not break the ordinary case -- all 51 sources in this repo's corpus are single.

    Without this, #353's fix would be indistinguishable from breaking the earned route again, which
    is the failure #346 already cost us once.
    """
    src = {
        "name": "Sales",
        "connection": {"powerbi_target": "live_source", "class": "snowflake", "server": "a.invalid", "warehouse": "WH"},
        "tables": [{"name": "Orders"}],
        "fields": [{"kind": "column", "internal_name": "[Order ID]"}],
    }
    names = [key for key, _display, _verdict, _reason in pf._classify_legs(src, 0)]
    assert len(names) == 1

    rc, d = _run_with(tmp_path, monkeypatch, src, names, reachable=True)

    assert not (d / cg.MARKER).exists(), "one named source, one endpoint contacted -> must clear"
    assert cg.status(d) == 0
    assert "probe-cleared" in _audit_actions(d)
    assert rc == 0


def test_skipped_sources_never_contribute_clear_names(tmp_path: Path, monkeypatch) -> None:
    """A SKIPPED source contacted nothing and must not contribute marker names to clear.

    This is the precise hole that produced #353: the outer `federated` connection resolves no probe
    target, so `_resolve_probe_target` returns None and `_probe_one` reports SKIPPED.

    ⚠️ The fixture is built so that counting *iterations* and collecting *proven marker names* give
    different answers. Here TWO live datasources are iterated against TWO marker names, and both are
    SKIPPED: proven names gives none and refuses, counting iterations would clear.
    """
    src_a = {
        "name": "A",
        "connection": {"powerbi_target": "live_source", "class": "snowflake", "server": "a.example", "warehouse": "WH"},
        "tables": [{"name": "Orders"}],
        "fields": [{"kind": "column", "internal_name": "[Order ID]"}],
    }
    src_b = {
        "name": "B",
        "connection": {"powerbi_target": "live_source", "class": "snowflake", "server": "b.example", "warehouse": "WH"},
        "tables": [{"name": "Orders"}],
        "fields": [{"kind": "column", "internal_name": "[Order ID]"}],
    }
    d = tmp_path / "u"
    (d / "fabric").mkdir(parents=True)
    cg.apply_block(d, ["one", "two"], force_scope=True)
    monkeypatch.setattr(pls, "_probe_one", lambda *_a, **_k: (0, "SKIPPED"))
    monkeypatch.setattr(
        pls,
        "load_bundle",
        lambda _p: type("B", (), {"data_sources": [src_a, src_b], "kind": "spec", "label": "x", "migration_dir": d})(),
    )

    rc = pls.run_probe(d, None, 60, False)

    assert (d / cg.MARKER).exists(), "two SKIPPED sources contacted nothing; 2 named must not clear"
    assert "probe-cleared" not in _audit_actions(d)
    assert rc != 0
