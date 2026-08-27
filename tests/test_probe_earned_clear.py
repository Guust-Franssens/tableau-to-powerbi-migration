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

1. proving EVERY live source earns a full clear;
2. proving a SUBSET must NOT clear -- a full clear on partial proof would lift a gate covering
   sources nobody ever contacted, which is the precise hole `run_probe`'s plural guarantee exists
   to close. Fixing (1) by blanket-omitting `--sources` would have opened (2).
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

NAMED = ["snowflake:acme/PROD", "databricks:acme/wh"]


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
    pls._lift_gate(d, "2 live source(s)", proved_all=True, contacted=2)

    assert not (d / cg.MARKER).exists(), "gate must be fully lifted when every live source was proved"
    assert cg.status(d) == 0
    assert "probe-cleared" in _audit_actions(d)


def test_the_count_string_is_never_sent_as_a_source_name(tmp_path: Path) -> None:
    """Root cause, pinned directly.

    `what` is prose for humans and is already carried in `--reason`. If it ever returns to
    `--sources`, it matches no marker entry and the partial-clear branch silently re-arms the bug.
    """
    d = _armed(tmp_path)
    pls._lift_gate(d, "2 live source(s)", proved_all=True, contacted=2)

    detail = " ".join(
        json.loads(line).get("detail", "") for line in (d / cg.AUDIT).read_text(encoding="utf-8").splitlines()
    )
    assert "sources=['2 live source(s)']" not in detail
    assert "live source(s)" in detail, "the count should still appear, as the human-readable reason"


def test_proving_only_a_SUBSET_must_not_clear_the_gate(tmp_path: Path) -> None:
    """The safety property that makes the naive one-line fix wrong.

    `--source-index` narrows the probe to one source. Clearing in full there would lift a gate
    covering sources never contacted -- a worse defect than the one being fixed.
    """
    d = _armed(tmp_path)
    pls._lift_gate(d, "1 live source(s)", proved_all=False, contacted=1)

    assert (d / cg.MARKER).exists(), "a partial proof must leave the gate armed"
    assert cg.status(d) == 1
    assert "probe-cleared" not in _audit_actions(d), "a partial proof must not record earned evidence"


def test_proved_all_keys_on_HOW_the_probe_was_invoked_not_on_the_bundle(tmp_path: Path, monkeypatch) -> None:
    """`proved_all` must be exactly `source_index is None`.

    An earlier version derived it from the bundle -- `set(live) >= set(all_live)` -- which reads as
    "did I cover everything?" but is a SUPERSET test, and so is vacuously true against an empty set.
    That made a `--source-index` run clear the gate whenever the bundle had 0 or 1 live sources.

    The deeper reason set arithmetic can never be right here: `live` holds bundle **indices** while
    the marker is keyed by source **names**, and `clear_block` clears against the marker. The two
    sets are independent, so a superset relation over one says nothing about the other.
    """
    calls: list[bool] = []

    def _stub(_m, _w, proved_all, contacted):
        _ = contacted
        calls.append(proved_all)
        return proved_all

    monkeypatch.setattr(pls, "_lift_gate", _stub)
    monkeypatch.setattr(pls, "_probe_one", lambda *_a, **_k: (0, "DATA_OK"))

    sources = [
        {"connection": {"powerbi_target": "live_source"}},
        {"connection": {"powerbi_target": "live_source"}},
    ]
    bundle = type("B", (), {"data_sources": sources, "kind": "spec", "label": "x", "migration_dir": tmp_path})()
    monkeypatch.setattr(pls, "load_bundle", lambda _p: bundle)

    assert pls.run_probe(tmp_path, None, 60, False) == 0
    assert calls == [True], "an all-sources run must report proved_all=True"

    calls.clear()
    assert pls.run_probe(tmp_path, 0, 60, False) == 3
    assert calls == [False], "a --source-index run must report proved_all=False"


def _bundle(tmp_path: Path, live_count: int, monkeypatch):
    """A bundle with `live_count` live sources; the gate is armed naming TWO real sources."""
    d = tmp_path / f"u{live_count}"
    (d / "fabric").mkdir(parents=True)
    cg.apply_block(d, list(NAMED), force_scope=True)
    sources = [{"connection": {"powerbi_target": "live_source"}} for _ in range(live_count)]
    if not sources:
        sources = [{"connection": {"powerbi_target": "flat_file"}}]
    bundle = type("B", (), {"data_sources": sources, "kind": "spec", "label": "x", "migration_dir": d})()
    monkeypatch.setattr(pls, "load_bundle", lambda _p: bundle)
    monkeypatch.setattr(pls, "_probe_one", lambda *_a, **_k: (0, "DATA_OK"))
    return d


@pytest.mark.parametrize("live_count", [0, 1, 2])
def test_source_index_never_clears_regardless_of_how_many_live_sources_exist(
    tmp_path: Path, monkeypatch, live_count: int
) -> None:
    """The blind-review finding: the refusal must not depend on the bundle's live-source count.

    The first fix used `set(live) >= set(all_live)`, which is a SUPERSET test and therefore
    vacuously true against an empty set. Measured 2026-08-27:

        0 live -> set([0]) >= set([])     -> True   cleared on ZERO proof
        1 live -> set([0]) >= set([0])    -> True   cleared a marker naming TWO sources
        2 live -> set([0]) >= set([0, 1]) -> False  refused (the only case the old test covered)

    Both `True` rows were fail-OPEN against master, which left the gate armed. The original test
    only ever built a 2-live bundle, so the suite credited a guarantee the code did not provide --
    which is exactly why this parametrises the count instead of picking one.
    """
    d = _bundle(tmp_path, live_count, monkeypatch)
    rc = pls.run_probe(d, 0, 60, False)

    assert (d / cg.MARKER).exists(), f"{live_count} live source(s): --source-index must never clear"
    assert cg.status(d) == 1, "the deny-ACL must still be applied"
    assert "probe-cleared" not in _audit_actions(d), "no earned evidence may be written"
    assert rc != 0, "a refusal must not report success"


def test_refusing_to_clear_does_not_report_success(tmp_path: Path, monkeypatch) -> None:
    """`run_probe` used to log 'DATA_OK all N reachable' and return 0 after deliberately refusing.

    Self-contradictory output, and a caller keying on exit 0 as 'gate lifted, proceed' is misled.
    """
    d = _bundle(tmp_path, 2, monkeypatch)
    assert pls.run_probe(d, 0, 60, False) == 3
    assert pls.run_probe(d, None, 60, False) == 0, "the all-sources path must still succeed"


# --- #353: armed per LEG, cleared per DATASOURCE -----------------------------------------------


def _federated(legs: list[dict]) -> dict:
    """One datasource whose connection wraps several named connections, as Tableau federates them."""
    return {
        "name": "Sales",
        "connection": {"powerbi_target": "live_source", "class": "federated", "connections": legs},
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
    """#353. The gate is armed per connection LEG; the probe enumerates DATASOURCES.

    `preflight_source_credentials._classify_legs` walks `connection.connections[]`, because one
    Tableau datasource can join Azure SQL + Snowflake + Databricks -- and its docstring records that
    under-reporting live sources "is the one direction this must never fail in". The clearing side
    had exactly that bug: measured 2026-08-27, a marker naming 3 legs cleared in full after the
    probe contacted the outer `federated` connection, which carries no server at all.
    """
    src = _federated(
        [
            {"class": "sqlserver", "server": "a.invalid", "dbname": "S"},
            {"class": "snowflake", "server": "b.invalid", "dbname": "S"},
            {"class": "databricks", "server": "c.invalid", "dbname": "S"},
        ]
    )
    names = [n for n, _v, _r in pf._classify_legs(src, 0)]
    assert len(names) == 3, "fixture must arm the gate with three leg names, or it proves nothing"

    rc, d = _run_with(tmp_path, monkeypatch, src, names, reachable=False)

    assert (d / cg.MARKER).exists(), "a marker naming 3 legs must not clear on 0 endpoints contacted"
    assert cg.status(d) == 1
    assert "probe-cleared" not in _audit_actions(d), "no earned evidence for endpoints never reached"
    assert rc != 0


def test_a_single_connection_source_still_earns_its_clear(tmp_path: Path, monkeypatch) -> None:
    """The guard must not break the ordinary case -- all 51 sources in this repo's corpus are single.

    Without this, #353's fix would be indistinguishable from breaking the earned route again, which
    is the failure #346 already cost us once.
    """
    src = {
        "name": "Sales",
        "connection": {"powerbi_target": "live_source", "class": "snowflake", "server": "a.invalid"},
    }
    names = [n for n, _v, _r in pf._classify_legs(src, 0)]
    assert len(names) == 1

    rc, d = _run_with(tmp_path, monkeypatch, src, names, reachable=True)

    assert not (d / cg.MARKER).exists(), "one named source, one endpoint contacted -> must clear"
    assert cg.status(d) == 0
    assert "probe-cleared" in _audit_actions(d)
    assert rc == 0


def test_contacted_counts_endpoints_reached_not_sources_iterated(tmp_path: Path, monkeypatch) -> None:
    """A SKIPPED source contacted nothing and must not count toward the cardinality budget.

    This is the precise hole that produced #353: the outer `federated` connection resolves no probe
    target, so `_resolve_probe_target` returns None and `_probe_one` reports SKIPPED.

    ⚠️ The fixture is built so that counting *iterations* and counting *endpoints* give different
    answers. An earlier version used one datasource against two marker names, where `contacted += 1`
    still refused (1 < 2) -- so the test passed under the very mutation it existed to catch, and was
    vacuous. Here TWO live datasources are iterated against TWO marker names, and both are SKIPPED:
    counting endpoints gives 0 and refuses, counting iterations gives 2 and clears.
    """
    src_a = {"name": "A", "connection": {"powerbi_target": "live_source", "class": "snowflake"}}
    src_b = {"name": "B", "connection": {"powerbi_target": "live_source", "class": "snowflake"}}
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
