"""Tests for scripts/deploy_estate.py.

Most of these exist because of something that actually happened against a real Fabric tenant, not
because of a hypothesis. The deployer's job is to be safe to re-run in front of a customer, so the
cases that matter are the ones where a naive implementation looks like it worked.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import deploy_estate as de  # noqa: E402  # pylint: disable=wrong-import-position


def _bundle(tmp_path: Path, workbooks: dict[str, bool]) -> Path:
    """Build a minimal bundle: {name: has_report}."""
    bundle = tmp_path / "bundle"
    for name, has_report in workbooks.items():
        model = bundle / "pbip" / name / f"{name}.SemanticModel"
        model.mkdir(parents=True)
        (model / "definition.pbism").write_text('{"version":"4.0"}', encoding="utf-8")
        if has_report:
            report = bundle / "pbip" / name / f"{name}.Report"
            report.mkdir(parents=True)
            (report / "definition.pbir").write_text(
                json.dumps({"datasetReference": {"byPath": {"path": f"../{name}.SemanticModel"}}}), encoding="utf-8"
            )
    return bundle


def _options(**kwargs) -> argparse.Namespace:
    defaults = {"dry_run": False, "force_unlock": False, "journal": None}
    return argparse.Namespace(**{**defaults, **kwargs})


# --------------------------------------------------------------------------- the binding


def test_a_report_is_rebound_from_bypath_to_byconnection(tmp_path):
    """A migrated PBIP binds by PATH, which cannot resolve in a service keyed by object id."""
    bundle = _bundle(tmp_path, {"WB": True})
    parts = de.parts_for(bundle / "pbip" / "WB" / "WB.Report")
    rebound = de.rebind(parts, "My Workspace", "WB", "1111-2222")
    pbir = json.loads(base64.b64decode(next(p["payload"] for p in rebound if p["path"] == "definition.pbir")))
    assert "byPath" not in pbir["datasetReference"]
    assert "byConnection" in pbir["datasetReference"]


def test_the_connection_string_carries_the_model_guid(tmp_path):
    """Schema 2.0.0 allows ONLY `connectionString`, so the guid has nowhere else to go.

    Omitting it is answered with `InvalidConnectionInformation`; putting it in a sibling property
    (the widely-quoted 1.0.0 shape) is answered with `Workload_FailedToParseFile`. Both measured.
    """
    bundle = _bundle(tmp_path, {"WB": True})
    parts = de.parts_for(bundle / "pbip" / "WB" / "WB.Report")
    pbir = json.loads(
        base64.b64decode(
            next(p["payload"] for p in de.rebind(parts, "WS", "WB", "GUID-1") if p["path"] == "definition.pbir")
        )
    )
    connection = pbir["datasetReference"]["byConnection"]
    assert list(connection) == ["connectionString"], "2.0.0 forbids any other property here"
    assert "semanticModelId=GUID-1" in connection["connectionString"]


def test_rebinding_leaves_every_other_part_untouched(tmp_path):
    """Only the binding changes; rewriting anything else would silently alter the report."""
    bundle = _bundle(tmp_path, {"WB": True})
    report = bundle / "pbip" / "WB" / "WB.Report"
    (report / "definition").mkdir()
    (report / "definition" / "report.json").write_text('{"x":1}', encoding="utf-8")
    before = de.parts_for(report)
    after = de.rebind(before, "WS", "WB", "G")
    unchanged = {p["path"]: p["payload"] for p in after if p["path"] != "definition.pbir"}
    assert unchanged == {p["path"]: p["payload"] for p in before if p["path"] != "definition.pbir"}


# --------------------------------------------------------------------------- crash safety


def test_an_unchanged_item_is_skipped_on_a_re_run(tmp_path):
    """The whole point of the journal: a resume must not redeploy what already landed."""
    bundle = _bundle(tmp_path, {"WB": False})
    journal = de.Journal(tmp_path / "j.jsonl")
    item = de.Item("WB", de.MODEL_TYPE, bundle / "pbip" / "WB" / "WB.SemanticModel")
    item.parts = de.parts_for(item.folder)
    journal.intent(item, "create")
    journal.outcome(item, "Succeeded", "id-1", "op-1")

    assert de.Journal(tmp_path / "j.jsonl").already_deployed(item) is not None


def test_a_CHANGED_item_is_not_skipped(tmp_path):
    """'An item with this name exists' is the check that silently ships a half-uploaded item."""
    bundle = _bundle(tmp_path, {"WB": False})
    folder = bundle / "pbip" / "WB" / "WB.SemanticModel"
    item = de.Item("WB", de.MODEL_TYPE, folder)
    item.parts = de.parts_for(folder)
    journal = de.Journal(tmp_path / "j.jsonl")
    journal.intent(item, "create")
    journal.outcome(item, "Succeeded", "id-1", "op-1")

    (folder / "definition.pbism").write_text('{"version":"4.0","changed":true}', encoding="utf-8")
    changed = de.Item("WB", de.MODEL_TYPE, folder)
    changed.parts = de.parts_for(folder)
    assert de.Journal(tmp_path / "j.jsonl").already_deployed(changed) is None


def test_an_intent_without_an_outcome_is_visible_as_unfinished(tmp_path):
    """Intent-before-mutation is what makes a crash mid-call distinguishable from never calling."""
    bundle = _bundle(tmp_path, {"WB": False})
    item = de.Item("WB", de.MODEL_TYPE, bundle / "pbip" / "WB" / "WB.SemanticModel")
    item.parts = de.parts_for(item.folder)
    de.Journal(tmp_path / "j.jsonl").intent(item, "create")
    assert de.Journal(tmp_path / "j.jsonl").unfinished(item) is not None


def test_a_torn_final_line_does_not_break_resume(tmp_path):
    """A hard kill can leave a partial line; refusing to load would strand the whole run."""
    path = tmp_path / "j.jsonl"
    path.write_text('{"phase":"intent","item":"A","type":"SemanticModel"}\n{"phase":"outc', encoding="utf-8")
    assert de.Journal(path).unfinished(de.Item("A", de.MODEL_TYPE, tmp_path)) is not None


def test_a_failed_outcome_is_not_treated_as_done(tmp_path):
    """A `202` that later FAILS looks identical to success unless the operation is read."""
    bundle = _bundle(tmp_path, {"WB": False})
    item = de.Item("WB", de.MODEL_TYPE, bundle / "pbip" / "WB" / "WB.SemanticModel")
    item.parts = de.parts_for(item.folder)
    journal = de.Journal(tmp_path / "j.jsonl")
    journal.intent(item, "create")
    journal.outcome(item, "Failed", None, "op-1", "boom")
    assert de.Journal(tmp_path / "j.jsonl").already_deployed(item) is None


# --------------------------------------------------------------------------- concurrency


def test_a_second_run_cannot_start_while_one_holds_the_lock(tmp_path):
    """Measured: two overlapping runs created DUPLICATE models and reports.

    Fabric does not reject a repeated report/model name, so the journal alone cannot prevent this -
    the runs have to be prevented from overlapping.
    """
    lock = de.RunLock(tmp_path / "run.lock")
    assert lock.acquire()[0] is True
    ok, message = de.RunLock(tmp_path / "run.lock").acquire()
    assert ok is False
    assert "DUPLICATE" in message


def test_a_stale_lock_can_be_forced(tmp_path):
    """A lock that cannot be cleared is worse than none when a session is live."""
    de.RunLock(tmp_path / "run.lock").acquire()
    assert de.RunLock(tmp_path / "run.lock").acquire(force=True)[0] is True


def test_releasing_lets_the_next_run_start(tmp_path):
    lock = de.RunLock(tmp_path / "run.lock")
    lock.acquire()
    lock.release()
    assert de.RunLock(tmp_path / "run.lock").acquire()[0] is True


# --------------------------------------------------------------------------- preflight and planning


def test_discovery_pairs_models_with_reports(tmp_path):
    bundle = _bundle(tmp_path, {"A": True, "B": False})
    found = {name: report for name, _model, report in de.discover(bundle)}
    assert found["A"] is not None
    assert found["B"] is None


def test_a_bundle_without_pbip_is_refused_with_an_actionable_message(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(SystemExit) as exc:
        de.discover(tmp_path / "empty")
    assert "run_estate.py" in str(exc.value)


def test_preflight_distinguishes_not_found_from_no_access(monkeypatch):
    """Conflating them sends someone looking for the wrong problem for an afternoon."""
    monkeypatch.setattr(de, "call", lambda *a, **k: (404, {}, {}))
    assert "does not exist" in de.preflight("ws", "tok", 1)[1]
    monkeypatch.setattr(de, "call", lambda *a, **k: (403, {}, {}))
    assert "Contributor" in de.preflight("ws", "tok", 1)[1]


def test_preflight_budgets_against_items_ALREADY_in_the_workspace(monkeypatch):
    """A customer-supplied landing zone is not necessarily empty."""
    existing = {"value": [{"id": str(i)} for i in range(880)]}
    monkeypatch.setattr(
        de,
        "call",
        lambda method, url, *a, **k: (200, {}, existing if url.endswith("/items") else {"displayName": "LZ"}),
    )
    ok, message, _ = de.preflight("ws", "tok", 50)
    assert ok is False
    assert "880 already there" in message


def test_preflight_keeps_headroom_rather_than_filling_to_the_limit(monkeypatch):
    """A re-deploy may create before deleting, and the customer adds their own content."""
    existing = {"value": [{"id": str(i)} for i in range(500)]}
    monkeypatch.setattr(
        de,
        "call",
        lambda method, url, *a, **k: (200, {}, existing if url.endswith("/items") else {"displayName": "LZ"}),
    )
    assert de.preflight("ws", "tok", 450)[0] is False, "should refuse: 500+450 exceeds the 900 usable budget"
    assert de.preflight("ws", "tok", 300)[0] is True


def test_dry_run_creates_nothing_and_reports_the_item_count(tmp_path, monkeypatch, caplog):
    """The item count is what a customer agrees BEFORE deploying - it carries their cost."""
    bundle = _bundle(tmp_path, {"A": True, "B": True})
    monkeypatch.setattr(
        de,
        "call",
        lambda method, url, *a, **k: (200, {}, {"value": []} if url.endswith("/items") else {"displayName": "LZ"}),
    )
    calls: list[str] = []
    monkeypatch.setattr(de, "create_item", lambda *a, **k: calls.append("created") or ("Succeeded", "x", ""))
    with caplog.at_level("INFO"):
        assert de.deploy(bundle, "ws", "tok", _options(dry_run=True)) == de.EXIT_OK
    assert not calls, "dry-run must create nothing"
    assert "4 item(s) would be created" in caplog.text


# --------------------------------------------------------------------------- outcome honesty


def test_a_partial_deploy_does_not_exit_zero(tmp_path, monkeypatch):
    """The engine's own wrapper exists because a soft failure that exits 0 is invisible."""
    bundle = _bundle(tmp_path, {"A": True})
    monkeypatch.setattr(
        de,
        "call",
        lambda method, url, *a, **k: (200, {}, {"value": []} if url.endswith("/items") else {"displayName": "LZ"}),
    )
    monkeypatch.setattr(de, "create_item", lambda ws, tok, item, journal: ("Failed", None, "boom"))
    assert de.deploy(bundle, "ws", "tok", _options(journal=tmp_path / "j.jsonl")) == de.EXIT_FAILED


def test_a_report_is_never_deployed_without_a_model_id(tmp_path, monkeypatch):
    """An unbindable report is worse than an absent one: it looks deployed and resolves to nothing."""
    bundle = _bundle(tmp_path, {"A": True})
    monkeypatch.setattr(
        de,
        "call",
        lambda method, url, *a, **k: (200, {}, {"value": []} if url.endswith("/items") else {"displayName": "LZ"}),
    )
    monkeypatch.setattr(de, "find_existing", lambda *a, **k: None)
    deployed: list[str] = []

    def _create(ws, tok, item, journal):  # noqa: ARG001
        deployed.append(item.item_type)
        return ("Succeeded", None, "")  # model id missing

    monkeypatch.setattr(de, "create_item", _create)
    assert de.deploy(bundle, "ws", "tok", _options(journal=tmp_path / "j.jsonl")) == de.EXIT_FAILED
    assert de.REPORT_TYPE not in deployed
