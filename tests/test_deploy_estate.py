"""Tests for scripts/deploy_estate.py.

Most of these exist because of something that actually happened against a real Fabric tenant, not
because of a hypothesis. The deployer's job is to be safe to re-run in front of a customer, so the
cases that matter are the ones where a naive implementation looks like it worked.
"""

from __future__ import annotations

import argparse
import base64
import importlib
import json
import shutil
import sqlite3
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
            (report / "definition" / "pages").mkdir(parents=True)
            (report / "definition.pbir").write_text(
                json.dumps({"datasetReference": {"byPath": {"path": f"../{name}.SemanticModel"}}}), encoding="utf-8"
            )
            # A page, so the report is not treated as empty: Fabric rejects a page-less report and
            # the deployer skips one before it reaches any other check.
            (report / "definition" / "pages" / "pages.json").write_text(
                json.dumps({"pageOrder": ["p1"], "activePageName": "p1"}), encoding="utf-8"
            )
    return bundle


def _options(**kwargs) -> argparse.Namespace:
    defaults = {"dry_run": False, "force_unlock": False, "journal": None, "estate_db": None, "no_folders": False}
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
    # Lowercase, matching the product's own serialisation rather than our earlier reconstruction.
    assert "semanticmodelid=GUID-1" in connection["connectionString"]


def test_rebinding_leaves_every_other_part_untouched(tmp_path):
    """Only the binding changes; rewriting anything else would silently alter the report."""
    bundle = _bundle(tmp_path, {"WB": True})
    report = bundle / "pbip" / "WB" / "WB.Report"
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
    monkeypatch.setattr(
        de,
        "call",
        lambda method, url, *a, **k: (200, {}, {"value": []} if url.endswith("/items") else {"displayName": "LZ"}),
    )
    deployed: list[str] = []

    def _create(ws, tok, item, journal):  # noqa: ARG001
        deployed.append(item.item_type)
        return ("Succeeded", None, "")  # model id missing

    monkeypatch.setattr(de, "create_item", _create)
    assert de.deploy(bundle, "ws", "tok", _options(journal=tmp_path / "j.jsonl")) == de.EXIT_FAILED
    assert de.REPORT_TYPE not in deployed


# --------------------------------------------------------------------------- folder mirroring
#
# Fabric's folder-name rules, measured against a live workspace (the API answers
# `InvalidFolderDisplayName`, it does not coerce):
#   rejected: &  /  \  :  ?  *  "  |  <  #  %  .   and a leading or trailing space
#   accepted: -  _  +  (  )  interior spaces, non-ASCII
# The dot is the trap: rejected ANYWHERE, and Tableau project names carry dots routinely.


@pytest.mark.parametrize("bad", ["R&D", "R/D", "R\\D", "A:B", "A?B", "A*B", 'A"B', "A|B", "A<B", "A#B", "A%B", "v1.2"])
def test_every_character_fabric_rejects_is_removed(bad):
    cleaned = de.folder_display_name(bad)
    assert not set(cleaned) & set('&/\\:?*"|<#%.')


@pytest.mark.parametrize("good", ["Ventes françaises", "Q1 (2026)", "R+D", "A-B", "A_B", "Sales Reports"])
def test_names_fabric_accepts_are_left_alone(good):
    """Over-sanitising is a real failure too: it degrades a name the customer recognises."""
    assert de.folder_display_name(good) == good


def test_a_leading_or_trailing_space_is_stripped():
    assert de.folder_display_name(" Lead") == "Lead"
    assert de.folder_display_name("Trail ") == "Trail"


def test_a_name_that_sanitises_to_nothing_still_gets_a_name():
    assert de.folder_display_name("...") == "folder"
    assert de.folder_display_name("///") == "folder"


def test_projects_that_collide_after_sanitising_get_DISTINCT_folders():
    """The failure this guards is SILENT: three Tableau projects pooling into one folder.

    `R&D`, `R/D` and `R.D` all coerce toward `R-D`. A customer would have no way to tell that the
    content of three projects had been merged.
    """
    final = de.unique_siblings(["R&D", "R/D", "R.D"])
    assert len(set(final.values())) == 3, f"collided: {final}"


def test_collision_suffixes_are_case_insensitive():
    """Fabric treats folder names case-insensitively for uniqueness; so must we."""
    final = de.unique_siblings(["r&d", "R/D"])
    assert len(set(v.casefold() for v in final.values())) == 2


def test_a_deep_tree_is_flattened_to_fabric_s_limit_not_dropped():
    parents = {f"L{i}": f"L{i - 1}" for i in range(2, 13)}
    plan = de.folder_plan({"wb": "L12"}, parents)
    assert len(plan["wb"]) == de.MAX_FOLDER_DEPTH
    assert "L12" in plan["wb"][-1], "the overflow must be preserved in the compound name, not dropped"


def test_two_deep_chains_differing_below_the_limit_do_not_collapse():
    """The subtlest collision: identical for 9 levels, differing only in the compounded tail."""
    parents = {f"L{i}": f"L{i - 1}" for i in range(2, 13)}
    parents["X12"] = "L11"
    plan = de.folder_plan({"a": "L12", "b": "X12"}, parents)
    assert plan["a"] != plan["b"], "two distinct projects flattened to the same folder path"


def test_a_cycle_in_the_project_tree_terminates():
    """A self-parent or an A->B->A loop must not hang the deploy."""
    assert de.folder_plan({"wb": "A"}, {"A": "B", "B": "A"})["wb"]
    assert de.folder_plan({"wb": "S"}, {"S": "S"})["wb"] == ["S"]


def test_a_workbook_with_no_known_project_is_absent_rather_than_guessed(tmp_path):
    """Inventing a folder would send the customer looking under a heading Tableau never had."""
    assert de.folder_plan({}, {}) == {}
    assert de.project_map(tmp_path, None) == {}


def test_the_slug_join_survives_the_engine_s_name_sanitising(tmp_path):
    """Tableau keeps `Meridian Multi-Source (3 systems)`; the engine writes `Meridian_Multi-Source__3_systems_`.

    An exact-match join placed only 8 of 33 workbooks and sent the rest to the root - which reads as
    "no project data" rather than as a bug.
    """
    assert de._slug("Meridian Multi-Source (3 systems)") == de._slug("Meridian_Multi-Source__3_systems_")


def test_an_unreadable_estate_db_degrades_to_flat_rather_than_crashing(tmp_path):
    broken = tmp_path / "not.db"
    broken.write_text("this is not sqlite", encoding="utf-8")
    assert de.project_parents(broken) == {}


def test_folders_are_created_parents_before_children(monkeypatch):
    """A child created before its parent lands at the root, silently flattening the tree."""
    created: list[str] = []

    def _call(method, url, tok, body=None):  # noqa: ARG001
        if method == "GET":
            return 200, {}, {"value": []}
        created.append(body["displayName"])
        return 201, {}, {"id": f"id-{len(created)}"}

    monkeypatch.setattr(de, "call", _call)
    de.ensure_folders("ws", "tok", [["Parent", "Child"]])
    assert created == ["Parent", "Child"]


def test_an_existing_folder_is_reused_not_duplicated(monkeypatch):
    """A re-run into a partly-populated landing zone must not double the tree."""
    created: list[str] = []

    def _call(method, url, tok, body=None):  # noqa: ARG001
        if method == "GET":
            return 200, {}, {"value": [{"id": "f1", "displayName": "Parent", "parentFolderId": None}]}
        created.append(body["displayName"])
        return 201, {}, {"id": "new"}

    monkeypatch.setattr(de, "call", _call)
    resolved = de.ensure_folders("ws", "tok", [["Parent"]])
    assert created == []
    assert resolved[("Parent",)] == "f1"


def test_an_unavailable_folders_api_degrades_to_a_flat_deploy(monkeypatch):
    """Folders are public preview; losing them must cost placement, not the whole deployment."""
    monkeypatch.setattr(de, "call", lambda *a, **k: (404, {}, {}))
    assert de.ensure_folders("ws", "tok", [["A"]]) == {}


def test_an_item_carries_its_folder_id_at_creation(monkeypatch):
    """`folderId` is a field on Create Item - placement happens at creation, with no move step."""
    sent: dict = {}

    def _call(method, url, tok, body=None):  # noqa: ARG001
        sent.update(body or {})
        return 201, {}, {"id": "x"}

    monkeypatch.setattr(de, "call", _call)
    item = de.Item("N", de.MODEL_TYPE, Path("."), parts=[], folder_id="folder-1")
    de.create_item("ws", "tok", item, de.Journal(Path("nul") if sys.platform == "win32" else Path("/dev/null")))
    assert sent.get("folderId") == "folder-1"


# --------------------------------------------------------------------------- adversarial findings


def test_ensure_folders_returns_ids_keyed_by_the_ORIGINAL_project_path(monkeypatch):
    """The critical one: it returned ids keyed by the SANITIZED path, which no caller holds.

    Every project whose name had to be changed (`v1.2`, `R&D`) therefore got its folder created,
    left EMPTY, and its content dumped at the workspace root - while --dry-run promised otherwise.
    """

    def _call(method, url, tok, body=None):  # noqa: ARG001
        if method == "GET":
            return 200, {}, {"value": []}
        return 201, {}, {"id": f"id-{body['displayName']}"}

    monkeypatch.setattr(de, "call", _call)
    resolved = de.ensure_folders("ws", "tok", [["R&D"], ["Finance", "Q1.2026"]])
    assert ("R&D",) in resolved, "lookup must work with the name the caller actually has"
    assert ("Finance", "Q1.2026") in resolved


def test_a_project_needing_sanitising_still_receives_its_items(monkeypatch, tmp_path):
    """End to end: the folder is created AND the workbook is placed in it, not at the root."""
    monkeypatch.setattr(de, "project_map", lambda *a, **k: {"wb": "R&D"})
    monkeypatch.setattr(de, "project_parents", lambda *a, **k: {})

    def _call(method, url, tok, body=None):  # noqa: ARG001
        if method == "GET":
            return 200, {}, {"value": []}
        return 201, {}, {"id": "folder-1"}

    monkeypatch.setattr(de, "call", _call)
    placed = de._resolve_folders(tmp_path, "ws", "tok", _options())
    assert placed.get("wb") == "folder-1", "the workbook must land in the folder created for it"


def test_two_workbooks_that_normalise_alike_are_both_left_unplaced(tmp_path):
    """`Q1 Report` and `Q1.Report` slug identically; filing one under the other's project is silent.

    Root placement is wrong-but-visible; a wrong folder is wrong-and-hidden.
    """
    db = tmp_path / "estate.db"
    conn = sqlite3.connect(db)
    conn.execute("create table workbook (name text, project text)")
    conn.executemany("insert into workbook values (?, ?)", [("Q1 Report", "Alpha"), ("Q1.Report", "Beta")])
    conn.commit()
    conn.close()
    assert de.project_map(tmp_path, db) == {}


def test_same_named_projects_under_different_parents_do_not_pool(tmp_path):
    """`Finance/Reports` and `HR/Reports` are indistinguishable by name; merging them is silent."""
    db = tmp_path / "estate.db"
    conn = sqlite3.connect(db)
    conn.execute("create table project (luid text, name text, parent_luid text)")
    conn.executemany(
        "insert into project values (?, ?, ?)",
        [("f", "Finance", None), ("h", "HR", None), ("r1", "Reports", "f"), ("r2", "Reports", "h")],
    )
    conn.commit()
    conn.close()
    assert "Reports" not in de.project_parents(db), "an ambiguous project must not be nested under a guess"


def test_a_report_with_no_pages_is_skipped_with_a_clear_reason(tmp_path, monkeypatch):
    """Fabric rejects an empty report as `invalid package content stream`, which explains nothing.

    Measured: 2 of 33 reports on a real estate had `pageOrder: []` because the source workbook had
    no convertible worksheets.
    """
    report = tmp_path / "X.Report"
    (report / "definition" / "pages").mkdir(parents=True)
    (report / "definition" / "pages" / "pages.json").write_text('{"pageOrder": []}', encoding="utf-8")
    assert de.report_is_empty(report) is True

    created: list[str] = []
    monkeypatch.setattr(de, "create_item", lambda *a, **k: created.append("x") or ("Succeeded", "id", ""))
    target = de.Target("ws", "LZ", "tok", de.Journal(tmp_path / "j.jsonl"))
    assert (
        de._deploy_report(target, "X", de.Item("X", de.MODEL_TYPE, tmp_path), de.Item("X", de.REPORT_TYPE, report), "m")
        is None
    )
    assert created == [], "an empty report must not be sent to the service"


def test_a_report_WITH_pages_is_not_mistaken_for_empty(tmp_path):
    report = tmp_path / "Y.Report"
    (report / "definition" / "pages").mkdir(parents=True)
    (report / "definition" / "pages" / "pages.json").write_text('{"pageOrder": ["p1"]}', encoding="utf-8")
    assert de.report_is_empty(report) is False


def test_the_journal_is_scoped_to_a_WORKSPACE(tmp_path):
    """Promotion deploys the SAME bundle to a second workspace; it must not be skipped as done.

    A journal keyed only by (item, type) would report success while creating nothing in the target.
    """
    bundle = _bundle(tmp_path, {"WB": False})
    item = de.Item("WB", de.MODEL_TYPE, bundle / "pbip" / "WB" / "WB.SemanticModel")
    item.parts = de.parts_for(item.folder)

    first = de.Journal(tmp_path / "j.jsonl", "workspace-A")
    first.intent(item, "create")
    first.outcome(item, "Succeeded", "id-1", "op-1")

    assert de.Journal(tmp_path / "j.jsonl", "workspace-A").already_deployed(item) is not None
    assert de.Journal(tmp_path / "j.jsonl", "workspace-B").already_deployed(item) is None


def test_moving_a_project_in_tableau_re_places_its_items(tmp_path):
    """The folder is part of the recorded state; otherwise items stay where last year's tree put them."""
    bundle = _bundle(tmp_path, {"WB": False})
    folder = bundle / "pbip" / "WB" / "WB.SemanticModel"
    item = de.Item("WB", de.MODEL_TYPE, folder, folder_id="old-folder")
    item.parts = de.parts_for(folder)
    journal = de.Journal(tmp_path / "j.jsonl", "ws")
    journal.intent(item, "create")
    journal.outcome(item, "Succeeded", "id-1", "op-1")

    moved = de.Item("WB", de.MODEL_TYPE, folder, folder_id="new-folder")
    moved.parts = de.parts_for(folder)
    assert de.Journal(tmp_path / "j.jsonl", "ws").already_deployed(moved) is None


def test_the_lock_is_taken_atomically(tmp_path, monkeypatch):
    """exists()-then-write has a window two simultaneous starts can both pass.

    Asserting only that a second acquire fails cannot tell the two implementations apart - an
    exists()-then-write version passes that too (mutation-verified). So we simulate the race the
    O_EXCL is there for: the file is on disk while `exists()` insists it is not, which is exactly
    what a check-then-act loses to. Only an atomic create still refuses.
    """
    lock = de.RunLock(tmp_path / "run.lock")
    assert lock.acquire()[0] is True
    assert de.RunLock(tmp_path / "run.lock").acquire()[0] is False

    monkeypatch.setattr(de.Path, "exists", lambda _self: False)
    assert de.RunLock(tmp_path / "run.lock").acquire()[0] is False, (
        "the lock must be taken by an atomic create, not by looking first"
    )


def test_a_report_without_a_pbir_is_refused_rather_than_guessed(tmp_path):
    """Inventing a binding for a report whose shape we do not understand is worse than stopping."""
    with pytest.raises(ValueError, match="definition.pbir"):
        de.rebind([{"path": "definition/report.json", "payload": "e30=", "payloadType": "InlineBase64"}], "W", "M", "G")


def test_an_expired_token_is_renewed_and_the_call_retried(monkeypatch):
    """Measured: a 66-item deploy outlived its token and every later call failed with 401.

    The run was resumable, but an operator should not have to notice and re-run in front of a
    customer.
    """
    calls: list[str] = []

    def _request(method, url, bearer, body=None):  # noqa: ARG001
        calls.append(bearer)
        if bearer == "old":
            return 401, {}, {"errorCode": "TokenExpired", "message": "Access token has expired"}
        return 201, {}, {"id": "x"}

    monkeypatch.setattr(de, "_request", _request)
    tok = de.Token.__new__(de.Token)
    tok.tenant = None
    tok._value = "old"
    monkeypatch.setattr(tok, "_mint", lambda: "fresh")
    status, _, _ = de.call("POST", "http://x", tok)
    assert status == 201
    assert calls == ["old", "fresh"], "the call must be retried with a renewed token"


def test_a_real_authorisation_failure_is_not_retried(monkeypatch):
    """Only an EXPIRED token earns a retry; a genuine 401 is a real problem to report."""
    calls: list[str] = []

    def _request(method, url, bearer, body=None):  # noqa: ARG001
        calls.append(bearer)
        return 401, {}, {"errorCode": "Unauthorized", "message": "not a contributor"}

    monkeypatch.setattr(de, "_request", _request)
    tok = de.Token.__new__(de.Token)
    tok.tenant = None
    tok._value = "tok"
    status, _, _ = de.call("GET", "http://x", tok)
    assert status == 401
    assert len(calls) == 1, "a non-expiry 401 must not be retried"


def test_a_timed_out_item_is_adopted_not_recreated(tmp_path, monkeypatch):
    """A `Timeout` is OUR poll giving up, not the service giving up.

    Measured on a real estate: a model timed out at 300s, completed server-side anyway, and the
    resume created a SECOND copy - Fabric does not reject a duplicate name, so nothing stopped it.
    """
    bundle = _bundle(tmp_path, {"WB": False})
    item = de.Item("WB", de.MODEL_TYPE, bundle / "pbip" / "WB" / "WB.SemanticModel")
    item.parts = de.parts_for(item.folder)

    journal = de.Journal(tmp_path / "j.jsonl", "ws")
    journal.intent(item, "create")
    journal.outcome(item, "Timeout", None, "op-1")

    reloaded = de.Journal(tmp_path / "j.jsonl", "ws")
    assert reloaded.already_deployed(item) is None, "a timeout is not a success"
    assert reloaded.attempted(item) is True, "but it MUST trigger reconciliation before creating"

    created: list[str] = []
    updated: list[str] = []
    monkeypatch.setattr(de, "create_item", lambda *a, **k: created.append("x") or ("Succeeded", "new", ""))
    monkeypatch.setattr(de, "update_item", lambda *a, **k: updated.append("u") or ("Succeeded", ""))
    landing = de.Landing(
        [
            {
                "id": "existing-id",
                "displayName": "WB",
                "type": de.MODEL_TYPE,
                "folderId": None,
                "description": de.PROVENANCE,
            }
        ]
    )
    target = de.Target("ws", "LZ", "tok", reloaded, landing)
    model_id, failure = de._deploy_model(target, "WB", item)

    assert failure is None
    assert model_id == "existing-id", "the item that already exists must be adopted"
    assert created == [], "creating a second copy is the defect this guards"
    assert updated == ["u"], "its definition is refreshed in place instead"


def test_a_failed_item_is_also_reconciled_before_recreating(tmp_path, monkeypatch):
    """A `Failed` outcome may still have created the item; only a clean success is proof it did not."""
    bundle = _bundle(tmp_path, {"WB": False})
    item = de.Item("WB", de.MODEL_TYPE, bundle / "pbip" / "WB" / "WB.SemanticModel")
    item.parts = de.parts_for(item.folder)
    journal = de.Journal(tmp_path / "j.jsonl", "ws")
    journal.intent(item, "create")
    journal.outcome(item, "Failed", None, "op-1", "boom")
    assert de.Journal(tmp_path / "j.jsonl", "ws").attempted(item) is True


def test_a_clean_first_run_does_not_pay_for_reconciliation(tmp_path):
    """An item never attempted must not trigger an extra lookup on every deploy."""
    bundle = _bundle(tmp_path, {"WB": False})
    item = de.Item("WB", de.MODEL_TYPE, bundle / "pbip" / "WB" / "WB.SemanticModel")
    item.parts = de.parts_for(item.folder)
    assert de.Journal(tmp_path / "absent.jsonl", "ws").attempted(item) is False


def test_the_summary_reports_what_was_deployed_not_what_was_planned(caplog):
    """Saying 'all 66 deployed' when two were skipped erodes trust in every other number."""
    with caplog.at_level("INFO"):
        de._report_failures([], 66, "LZ", skipped=2)
    assert "64 item(s) deployed" in caplog.text
    assert "2 skipped as empty" in caplog.text


def test_a_dropped_network_stops_the_run_instead_of_failing_every_item(tmp_path, monkeypatch, caplog):
    """Measured: a laptop moved between networks mid-deploy and burned through the whole estate.

    Marking 30 items "Failed" is noise, and each one then needs reconciling on the resume. Stopping
    after a few consecutive connectivity errors and saying so is the useful behaviour.
    """
    bundle = _bundle(tmp_path, {f"WB{i}": False for i in range(10)})
    monkeypatch.setattr(
        de,
        "call",
        lambda method, url, tok, body=None: (
            200,
            {},
            {"value": []} if url.endswith("/items") else {"displayName": "LZ"},
        ),
    )
    attempts: list[str] = []

    def _create(ws, tok, item, journal):  # noqa: ARG001
        attempts.append(item.name)
        return ("Failed", None, 'HTTP 0  {"error": "getaddrinfo failed"}')

    monkeypatch.setattr(de, "create_item", _create)
    with caplog.at_level("ERROR"):
        code = de.deploy(bundle, "ws", "tok", _options(journal=tmp_path / "j.jsonl"))
    assert code == de.EXIT_FAILED
    assert "network unreachable" in caplog.text
    assert len(attempts) == de.MAX_CONSECUTIVE_NETWORK_FAILURES, "it must stop, not grind through all 10"


def test_a_transient_failure_does_not_trip_the_network_guard(tmp_path, monkeypatch):
    """One bad item between good ones is not an outage; the counter must reset.

    The pattern matters. Two failures then a success then two more only distinguishes a resetting
    counter from a cumulative one if the cumulative count would CROSS the threshold - with a single
    failure the guard never trips either way, so the old version of this test passed happily even
    with `offline = 0` deleted (mutation-verified).
    """
    order = ["A", "B", "C", "D", "E", "F"]
    bundle = _bundle(tmp_path, dict.fromkeys(order, False))
    monkeypatch.setattr(
        de,
        "call",
        lambda method, url, tok, body=None: (
            200,
            {},
            {"value": []} if url.endswith("/items") else {"displayName": "LZ"},
        ),
    )
    seen: list[str] = []
    offline = {"A", "B", "D", "E"}  # 2 failures, a success, then 2 more: 4 cumulative, never 3 in a row

    def _create(ws, tok, item, journal):  # noqa: ARG001
        seen.append(item.name)
        return ("Failed", None, "HTTP 0 x") if item.name in offline else ("Succeeded", "id", "")

    monkeypatch.setattr(de, "create_item", _create)
    de.deploy(bundle, "ws", "tok", _options(journal=tmp_path / "j.jsonl"))
    assert "F" in seen, "a success between blips must reset the counter, not accumulate toward the cap"
    assert seen == order


def test_a_report_with_no_pages_is_counted_as_skipped_in_the_summary(tmp_path, monkeypatch):
    """The summary's "N skipped as empty" was only ever unit-tested with the count passed by hand."""
    bundle = _bundle(tmp_path, {"WB": True})
    pages = bundle / "pbip" / "WB" / "WB.Report" / "definition" / "pages" / "pages.json"
    pages.write_text(json.dumps({"pageOrder": [], "activePageName": ""}), encoding="utf-8")
    monkeypatch.setattr(
        de,
        "call",
        lambda method, url, tok, body=None: (
            200,
            {},
            {"value": []} if url.endswith("/items") else {"displayName": "LZ"},
        ),
    )
    monkeypatch.setattr(de, "create_item", lambda *a, **k: ("Succeeded", "id-1", ""))
    messages: list[str] = []
    monkeypatch.setattr(de.LOG, "info", lambda msg, *args: messages.append(str(msg) % args if args else str(msg)))

    assert de.deploy(bundle, "ws", "tok", _options(journal=tmp_path / "j.jsonl")) == 0
    assert any("1 skipped as empty" in m for m in messages), messages[-3:]


def test_an_item_that_already_exists_is_UPDATED_never_duplicated(tmp_path, monkeypatch):
    """The rule that closes every duplicate path: always ask the service before creating.

    Measured against a real workspace: changing an item's PLACEMENT made the deployer treat it as
    new and create a second copy - ten of them. Fabric does not reject a repeated name, so nothing
    downstream caught it.
    """
    bundle = _bundle(tmp_path, {"WB": False})
    item = de.Item("WB", de.MODEL_TYPE, bundle / "pbip" / "WB" / "WB.SemanticModel", folder_id="new-folder")
    item.parts = de.parts_for(item.folder)

    created: list[str] = []
    updated: list[str] = []
    monkeypatch.setattr(de, "create_item", lambda *a, **k: created.append("c") or ("Succeeded", "new", ""))
    monkeypatch.setattr(de, "update_item", lambda *a, **k: updated.append("u") or ("Succeeded", ""))

    journal = de.Journal(tmp_path / "j.jsonl", "ws")
    journal.intent(item, "create")
    journal.outcome(item, "Timeout", None, "op-1")
    journal = de.Journal(tmp_path / "j.jsonl", "ws")
    landing = de.Landing(
        [
            {
                "id": "already-there",
                "displayName": "WB",
                "type": de.MODEL_TYPE,
                "folderId": "old-folder",
                "description": de.PROVENANCE,
            }
        ]
    )
    target = de.Target("ws", "LZ", "tok", journal, landing)
    monkeypatch.setattr(de, "move_item", lambda *a, **k: (True, ""))
    item_id, failure = de._deploy_model(target, "WB", item)

    assert failure is None
    assert item_id == "already-there"
    assert created == [], "an existing item must never be created a second time"
    assert updated == ["u"], "it must be updated in place instead"


def test_a_genuinely_new_item_is_created(tmp_path, monkeypatch):
    """The update path must not swallow the normal case."""
    bundle = _bundle(tmp_path, {"WB": False})
    item = de.Item("WB", de.MODEL_TYPE, bundle / "pbip" / "WB" / "WB.SemanticModel")
    item.parts = de.parts_for(item.folder)
    created: list[str] = []
    monkeypatch.setattr(de, "create_item", lambda *a, **k: created.append("c") or ("Succeeded", "fresh", ""))
    target = de.Target("ws", "LZ", "tok", de.Journal(tmp_path / "j.jsonl", "ws"), de.Landing([]))
    assert de._deploy_model(target, "WB", item) == ("fresh", None)
    assert created == ["c"]


def test_a_failed_update_is_reported_not_swallowed(tmp_path, monkeypatch):
    bundle = _bundle(tmp_path, {"WB": False})
    item = de.Item("WB", de.MODEL_TYPE, bundle / "pbip" / "WB" / "WB.SemanticModel")
    item.parts = de.parts_for(item.folder)
    monkeypatch.setattr(de, "update_item", lambda *a, **k: ("Failed", "boom"))
    journal = de.Journal(tmp_path / "j.jsonl", "ws")
    journal.intent(item, "create")
    journal.outcome(item, "Timeout", None, "op-1")
    journal = de.Journal(tmp_path / "j.jsonl", "ws")
    landing = de.Landing(
        [{"id": "there", "displayName": "WB", "type": de.MODEL_TYPE, "folderId": None, "description": de.PROVENANCE}]
    )
    target = de.Target("ws", "LZ", "tok", journal, landing)
    item_id, failure = de._deploy_model(target, "WB", item)
    assert item_id is None
    assert "boom" in failure


def test_an_item_on_the_second_page_is_found_not_duplicated(monkeypatch):
    """Reading only page one makes a far-away item look absent, and absent means "create a copy"."""
    pages = [
        {"value": [{"displayName": "A", "type": de.MODEL_TYPE, "id": "id-a"}], "continuationToken": "tok-2"},
        {"value": [{"displayName": "Z", "type": de.MODEL_TYPE, "id": "id-z"}]},
    ]
    calls: list[str] = []

    def fake_call(_method, url, *_a, **_k):
        calls.append(url)
        return 200, {}, pages[len(calls) - 1]

    monkeypatch.setattr(de, "call", fake_call)
    landing, why = de.Landing.read("ws", "tok")
    assert landing is not None and why == ""
    assert [r["id"] for r in landing.matching("Z", de.MODEL_TYPE)] == ["id-z"]
    assert len(calls) == 2, "the second page was never requested"
    assert "tok-2" in calls[1], "the continuation token must be carried into the next request"


def test_a_repeating_continuation_token_cannot_loop_forever(monkeypatch):
    calls: list[str] = []

    def fake_call(_method, url, *_a, **_k):
        calls.append(url)
        return 200, {}, {"value": [], "continuationToken": "same-every-time"}

    monkeypatch.setattr(de, "call", fake_call)
    status, rows = de.list_all("ws", "tok", "items")
    assert (status, rows) == (200, [])
    assert len(calls) == 2, "a server repeating one token must not be followed indefinitely"


def test_a_list_failure_is_reported_rather_than_read_as_empty(monkeypatch):
    """The status must reach the caller. This test previously ALSO asserted that the lookup
    returned None on a 403 - which is precisely "read as empty", the defect its name denies."""
    monkeypatch.setattr(de, "call", lambda *a, **k: (403, {}, {}))
    status, rows = de.list_all("ws", "tok", "items")
    assert status == 403 and rows == []
    landing, why = de.Landing.read("ws", "tok")
    assert landing is None and "403" in why, "a refused read must not become an empty workspace"


# --- findings from the blind adversarial review of PR #105 -------------------------------------


def test_a_failed_existence_read_refuses_to_deploy_rather_than_creating_duplicates(monkeypatch):
    """ "I could not ask" must never be read as "it is not there".

    Measured by the reviewer: one transient 500 on the read made a re-run create a second copy of a
    model, exit 0, and rebind the report to the DUPLICATE - leaving the original as an orphan
    holding the pre-fix definition.
    """
    monkeypatch.setattr(de, "list_all", lambda *a, **k: (500, []))
    landing, why = de.Landing.read("ws", "tok")
    assert landing is None, "an unreadable workspace must not be treated as an empty one"
    assert "refusing to deploy" in why.lower()

    monkeypatch.setattr(de, "list_all", lambda *a, **k: (200, []))
    landing, why = de.Landing.read("ws", "tok")
    assert landing is not None and why == "", "a readable empty workspace is still deployable"


def test_an_item_we_did_not_create_is_never_overwritten(tmp_path):
    """A customer-supplied landing zone is not necessarily empty.

    Identity here is only (displayName, type), so an unrelated report called `Sales` was
    indistinguishable from ours and was overwritten in place - reported as "already existed".
    """
    bundle = _bundle(tmp_path, {"Sales": False})
    item = de.Item("Sales", de.MODEL_TYPE, bundle / "pbip" / "Sales" / "Sales.SemanticModel")
    journal = de.Journal(tmp_path / "j.jsonl", "ws")
    theirs = {"id": "customer-1", "displayName": "Sales", "type": de.MODEL_TYPE, "folderId": "their-folder"}

    item_id, refusal = de.Landing([theirs]).claim(item, journal)

    assert item_id is None, "we must not claim an item we have no record of creating"
    assert refusal and "may not be ours" in refusal


def test_an_item_this_run_created_is_ours_to_update(tmp_path):
    """The refusal above must not break the ordinary resume."""
    bundle = _bundle(tmp_path, {"Sales": False})
    item = de.Item("Sales", de.MODEL_TYPE, bundle / "pbip" / "Sales" / "Sales.SemanticModel")
    journal = de.Journal(tmp_path / "j.jsonl", "ws")
    journal.intent(item, "create")
    journal.outcome(item, "Succeeded", "ours-1", "op-1")
    reloaded = de.Journal(tmp_path / "j.jsonl", "ws")
    row = {"id": "ours-1", "displayName": "Sales", "type": de.MODEL_TYPE, "folderId": None}

    assert de.Landing([row]).claim(item, reloaded) == ("ours-1", None)


def test_a_journalled_item_deleted_in_the_portal_is_recreated(tmp_path, monkeypatch):
    """Deleting a broken item and re-running is the obvious way to force a clean redeploy.

    The journal fast path used to return success without asking the service, so the run reported a
    complete deploy over a workspace with no model, and the report kept a GUID that no longer
    resolved.
    """
    bundle = _bundle(tmp_path, {"WB": False})
    item = de.Item("WB", de.MODEL_TYPE, bundle / "pbip" / "WB" / "WB.SemanticModel")
    item.parts = de.parts_for(item.folder)
    journal = de.Journal(tmp_path / "j.jsonl", "ws")
    journal.intent(item, "create")
    journal.outcome(item, "Succeeded", "gone-1", "op-1")
    reloaded = de.Journal(tmp_path / "j.jsonl", "ws")
    assert reloaded.already_deployed(item) is not None, "the journal still believes it is deployed"

    created: list[str] = []
    monkeypatch.setattr(de, "create_item", lambda *a, **k: created.append("c") or ("Succeeded", "new-1", ""))
    target = de.Target("ws", "LZ", "tok", reloaded, de.Landing([]))  # the workspace is empty now

    item_id, failure = de._deploy_model(target, "WB", item)

    assert failure is None
    assert created == ["c"], "the item is gone from the workspace, so it must be created again"
    assert item_id == "new-1"


def test_a_re_placed_item_is_actually_moved(tmp_path, monkeypatch):
    """updateDefinition ignores placement, so without a move the folder never changes."""
    bundle = _bundle(tmp_path, {"WB": False})
    item = de.Item("WB", de.MODEL_TYPE, bundle / "pbip" / "WB" / "WB.SemanticModel", folder_id="new-folder")
    item.parts = de.parts_for(item.folder)
    journal = de.Journal(tmp_path / "j.jsonl", "ws")
    journal.intent(item, "create")
    journal.outcome(item, "Timeout", None, "op-1")
    journal = de.Journal(tmp_path / "j.jsonl", "ws")
    landing = de.Landing(
        [
            {
                "id": "it-1",
                "displayName": "WB",
                "type": de.MODEL_TYPE,
                "folderId": "old-folder",
                "description": de.PROVENANCE,
            }
        ]
    )

    moves: list[tuple] = []
    monkeypatch.setattr(de, "update_item", lambda *a, **k: ("Succeeded", ""))
    monkeypatch.setattr(de, "move_item", lambda w, t, i, f: moves.append((i, f)) or (True, ""))

    de._deploy_model(de.Target("ws", "LZ", "tok", journal, landing), "WB", item)

    assert moves == [("it-1", "new-folder")], "an item whose folder changed must be moved"


def test_a_move_that_fails_records_where_the_item_actually_is(tmp_path, monkeypatch):
    """Recording the INTENDED folder as achieved made every later run skip the item forever."""
    bundle = _bundle(tmp_path, {"WB": False})
    item = de.Item("WB", de.MODEL_TYPE, bundle / "pbip" / "WB" / "WB.SemanticModel", folder_id="new-folder")
    item.parts = de.parts_for(item.folder)
    journal = de.Journal(tmp_path / "j.jsonl", "ws")
    journal.intent(item, "create")
    journal.outcome(item, "Timeout", None, "op-1")
    journal = de.Journal(tmp_path / "j.jsonl", "ws")
    landing = de.Landing(
        [
            {
                "id": "it-1",
                "displayName": "WB",
                "type": de.MODEL_TYPE,
                "folderId": "old-folder",
                "description": de.PROVENANCE,
            }
        ]
    )
    monkeypatch.setattr(de, "update_item", lambda *a, **k: ("Succeeded", ""))
    monkeypatch.setattr(de, "move_item", lambda *a, **k: (False, "HTTP 400"))

    de._deploy_model(de.Target("ws", "LZ", "tok", journal, landing), "WB", item)

    rows = [json.loads(line) for line in (tmp_path / "j.jsonl").read_text(encoding="utf-8").splitlines() if line]
    final = [r for r in rows if r.get("phase") == "outcome" and r.get("status") == "Succeeded"][-1]
    assert final["folderId"] == "old-folder", "we must record where it IS, not where we wanted it"


def test_a_failed_parent_folder_does_not_scatter_its_children_at_the_root(monkeypatch):
    """The child was created at the root under its own bare name, indistinguishable from a sibling."""
    created: list[dict] = []

    def flaky(method, url, _tok, body=None):
        if method == "GET":
            return 200, {}, {"value": []}
        if body.get("displayName") == "Finance":
            return 500, {}, {"error": "boom"}
        created.append(body)
        return 201, {}, {"id": f"id-{len(created)}"}

    monkeypatch.setattr(de, "call", flaky)
    resolved = de.ensure_folders("ws", "tok", [["Finance", "Q1"], ["HR", "Q1"]])

    assert ("Finance", "Q1") not in resolved, "a child of a failed parent must not be resolved"
    assert [c["displayName"] for c in created] == ["HR", "Q1"]
    assert all(c.get("parentFolderId") or c["displayName"] == "HR" for c in created)


def test_folder_naming_does_not_depend_on_set_iteration_order(monkeypatch):
    """Which of two collision-prone projects owned which folder was decided by the hash seed."""
    monkeypatch.setattr(de, "call", lambda *a, **k: (200, {}, {"value": []}))
    first = de._display_names(sorted({("R&D",), ("R/D",)}, key=lambda p: (len(p), p)))
    second = de._display_names(sorted({("R/D",), ("R&D",)}, key=lambda p: (len(p), p)))
    assert first == second, "the same plan must produce the same folder names every run"


def test_a_resume_does_not_count_its_own_items_against_the_budget(monkeypatch):
    """existing + planned double-counted the run's own items, so a large estate could never re-run."""
    rows = [{"id": f"i{n}", "displayName": f"WB{n}", "type": de.MODEL_TYPE} for n in range(460)]
    monkeypatch.setattr(de, "call", lambda *a, **k: (200, {}, {"displayName": "LZ"}))
    monkeypatch.setattr(de, "list_all", lambda *a, **k: (200, rows))

    keys = [(f"WB{n}", de.MODEL_TYPE) for n in range(460)]
    ok, message, _ = de.preflight("ws", "tok", 460, keys)

    assert ok, f"a resume of an already-deployed estate must not be refused: {message}"
    assert "0 new" in message


def test_a_retry_after_date_does_not_crash_the_deploy():
    """RFC 9110 allows an HTTP-date; float() on it raised ValueError mid-estate."""
    assert de._retry_after({"retry-after": "30"}) == 30.0
    assert de._retry_after({"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}) == 0.0
    assert de._retry_after({}) == 20.0
    assert de._retry_after({"retry-after": "nonsense"}) == 20.0


def test_a_name_taken_but_unlocatable_item_is_a_failure_not_a_success(tmp_path, monkeypatch):
    """Reporting it as deployed declared a complete run over an empty workspace."""
    bundle = _bundle(tmp_path, {"WB": False})
    item = de.Item("WB", de.MODEL_TYPE, bundle / "pbip" / "WB" / "WB.SemanticModel")
    item.parts = de.parts_for(item.folder)
    monkeypatch.setattr(de, "create_item", lambda *a, **k: ("AlreadyExists", None, "taken"))
    monkeypatch.setattr(de, "list_all", lambda *a, **k: (200, []))
    target = de.Target("ws", "LZ", "tok", de.Journal(tmp_path / "j.jsonl", "ws"), de.Landing([]))

    item_id, failure = de._deploy_model(target, "WB", item)

    assert item_id is None
    assert failure and "no matching item" in failure


def test_an_item_stamped_by_a_previous_run_is_recognised_without_the_journal(tmp_path):
    """Ownership that lives only in a local file is lost with the file.

    Refusing to touch our own previous output because a temp journal was cleaned is safe but
    useless. The stamp is written to the SERVICE at creation, so any later run can read it.
    """
    bundle = _bundle(tmp_path, {"WB": False})
    item = de.Item("WB", de.MODEL_TYPE, bundle / "pbip" / "WB" / "WB.SemanticModel")
    empty_journal = de.Journal(tmp_path / "fresh.jsonl", "ws")
    stamped = {
        "id": "ours-1",
        "displayName": "WB",
        "type": de.MODEL_TYPE,
        "folderId": None,
        "description": de.PROVENANCE,
    }

    assert de.Landing([stamped]).claim(item, empty_journal) == ("ours-1", None)


def test_adopt_existing_is_required_to_take_over_an_unstamped_item(tmp_path):
    """The escape hatch must be explicit - and must actually work when asked for."""
    bundle = _bundle(tmp_path, {"WB": False})
    item = de.Item("WB", de.MODEL_TYPE, bundle / "pbip" / "WB" / "WB.SemanticModel")
    journal = de.Journal(tmp_path / "fresh.jsonl", "ws")
    theirs = {"id": "x-1", "displayName": "WB", "type": de.MODEL_TYPE, "folderId": None}

    refused_id, refusal = de.Landing([theirs]).claim(item, journal)
    assert refused_id is None and "--adopt-existing" in refusal

    assert de.Landing([theirs], adopt=True).claim(item, journal) == ("x-1", None)


def test_every_created_item_carries_the_provenance_stamp(tmp_path, monkeypatch):
    """If creation stops stamping, the recognition above silently stops working."""
    bundle = _bundle(tmp_path, {"WB": False})
    item = de.Item("WB", de.MODEL_TYPE, bundle / "pbip" / "WB" / "WB.SemanticModel")
    item.parts = de.parts_for(item.folder)
    sent: list[dict] = []
    monkeypatch.setattr(de, "call", lambda method, url, tok, body=None: (sent.append(body), (201, {}, {"id": "i"}))[1])

    de._post_item("ws", "tok", item)

    assert sent[0]["description"] == de.PROVENANCE


def test_adopting_an_item_stamps_it_so_the_next_run_needs_no_flag(tmp_path, monkeypatch):
    """Measured live: 64 items were adopted and 0 of 64 came back stamped.

    updateDefinition carries only the definition, so without an explicit mark the escape hatch
    would be needed on every future run - which is the same as having no marker at all.
    """
    bundle = _bundle(tmp_path, {"WB": False})
    item = de.Item("WB", de.MODEL_TYPE, bundle / "pbip" / "WB" / "WB.SemanticModel")
    item.parts = de.parts_for(item.folder)
    unstamped = {"id": "x-1", "displayName": "WB", "type": de.MODEL_TYPE, "folderId": None, "description": ""}
    landing = de.Landing([unstamped], adopt=True)

    stamped: list[str] = []
    monkeypatch.setattr(de, "update_item", lambda *a, **k: ("Succeeded", ""))
    monkeypatch.setattr(de, "stamp_item", lambda w, t, i, it: stamped.append(i) or True)
    target = de.Target("ws", "LZ", "tok", de.Journal(tmp_path / "j.jsonl", "ws"), landing)

    de._deploy_model(target, "WB", item)

    assert stamped == ["x-1"], "an adopted item must be marked as ours"
    assert landing.describe("x-1") == de.PROVENANCE


def test_an_already_stamped_item_is_not_stamped_again(tmp_path, monkeypatch):
    """One needless PATCH per item per run, across a whole estate, is worth avoiding."""
    bundle = _bundle(tmp_path, {"WB": False})
    item = de.Item("WB", de.MODEL_TYPE, bundle / "pbip" / "WB" / "WB.SemanticModel")
    item.parts = de.parts_for(item.folder)
    row = {"id": "x-1", "displayName": "WB", "type": de.MODEL_TYPE, "folderId": None, "description": de.PROVENANCE}
    stamped: list[str] = []
    monkeypatch.setattr(de, "update_item", lambda *a, **k: ("Succeeded", ""))
    monkeypatch.setattr(de, "stamp_item", lambda w, t, i, it: stamped.append(i) or True)
    target = de.Target("ws", "LZ", "tok", de.Journal(tmp_path / "j.jsonl", "ws"), de.Landing([row]))

    de._deploy_model(target, "WB", item)

    assert stamped == []


# --- round 2: findings from re-reviewing the fixes -----------------------------------------------


def test_a_failed_create_does_not_authorise_overwriting_a_strangers_item(tmp_path):
    """R1, critical: the first fix moved this boundary instead of removing it.

    Ownership used to include `journal.attempted(item)`, which never looked at the row - so one
    failed create authorised overwriting ANY item of that name for the life of the journal, and a
    create that failed is exactly what this deployer exists to resume from. It destroyed the
    customer's content and then stamped it as ours, making the damage permanent.
    """
    bundle = _bundle(tmp_path, {"Sales": False})
    item = de.Item("Sales", de.MODEL_TYPE, bundle / "pbip" / "Sales" / "Sales.SemanticModel")
    journal = de.Journal(tmp_path / "j.jsonl", "ws")
    journal.intent(item, "create")
    journal.outcome(item, "Failed", None, "op-1", "HTTP 400")
    reloaded = de.Journal(tmp_path / "j.jsonl", "ws")
    assert reloaded.attempted(item) is True, "precondition: the journal remembers the failed attempt"

    theirs = {"id": "cust-1", "displayName": "Sales", "type": de.MODEL_TYPE, "description": "Finance's own model"}
    item_id, refusal = de.Landing([theirs]).claim(item, reloaded)

    assert item_id is None, "a failed attempt must not authorise touching someone else's item"
    assert refusal


def test_a_crashed_create_is_still_recovered_because_the_item_is_stamped(tmp_path):
    """Removing `attempted()` must not lose what it was there for.

    `_post_item` sends the marker in the CREATE body, so an item created just before a crash is
    already stamped when we come back for it - better evidence than the journal ever had.
    """
    bundle = _bundle(tmp_path, {"WB": False})
    item = de.Item("WB", de.MODEL_TYPE, bundle / "pbip" / "WB" / "WB.SemanticModel")
    journal = de.Journal(tmp_path / "j.jsonl", "ws")
    journal.intent(item, "create")  # crashed before the outcome was written
    reloaded = de.Journal(tmp_path / "j.jsonl", "ws")
    orphan = {"id": "made-1", "displayName": "WB", "type": de.MODEL_TYPE, "description": de.PROVENANCE}

    assert de.Landing([orphan]).claim(item, reloaded) == ("made-1", None)


def test_two_estates_sharing_a_workbook_name_do_not_collapse_into_one_item(tmp_path):
    """R2: the stamp said "some run of this tool", not WHICH estate."""
    bundle = _bundle(tmp_path, {"Sales": False})
    mine = de.Item("Sales", de.MODEL_TYPE, bundle / "pbip" / "Sales" / "Sales.SemanticModel", source="HR-estate")
    theirs = {
        "id": "fin-1",
        "displayName": "Sales",
        "type": de.MODEL_TYPE,
        "description": f"{de.PROVENANCE} {de.SOURCE_PREFIX} Finance-estate",
    }

    item_id, refusal = de.Landing([theirs]).claim(mine, de.Journal(tmp_path / "j.jsonl", "ws"))

    assert item_id is None
    assert refusal and "came from 'Finance-estate'" in refusal


def test_the_same_estate_redeployed_is_still_recognised(tmp_path):
    """The source check must not break the ordinary re-run."""
    bundle = _bundle(tmp_path, {"Sales": False})
    mine = de.Item("Sales", de.MODEL_TYPE, bundle / "pbip" / "Sales" / "Sales.SemanticModel", source="HR-estate")
    row = {
        "id": "hr-1",
        "displayName": "Sales",
        "type": de.MODEL_TYPE,
        "description": f"{de.PROVENANCE} {de.SOURCE_PREFIX} HR-estate",
    }
    assert de.Landing([row]).claim(mine, de.Journal(tmp_path / "j.jsonl", "ws")) == ("hr-1", None)


def test_a_refusal_stops_the_run_before_anything_is_created(tmp_path, monkeypatch):
    """R6: creating the model and THEN refusing the report left an orphan in the customer's zone."""
    bundle = _bundle(tmp_path, {"Sales": True})
    created: list[str] = []
    monkeypatch.setattr(de, "create_item", lambda *a, **k: created.append("c") or ("Succeeded", "id", ""))
    monkeypatch.setattr(
        de,
        "call",
        lambda method, url, tok, body=None: (
            200,
            {},
            {"value": [{"id": "cust-1", "displayName": "Sales", "type": de.REPORT_TYPE, "description": "theirs"}]}
            if url.endswith("/items")
            else {"displayName": "LZ"},
        ),
    )

    code = de.deploy(bundle, "ws", "tok", _options(journal=tmp_path / "j.jsonl"))

    assert code == de.EXIT_FAILED
    assert created == [], "nothing may be created when part of the plan is refused"


def test_an_item_moved_by_hand_in_the_portal_is_put_back(tmp_path, monkeypatch):
    """R4: the fast path compared folders against the journal, so service-side drift was invisible."""
    bundle = _bundle(tmp_path, {"WB": False})
    item = de.Item("WB", de.MODEL_TYPE, bundle / "pbip" / "WB" / "WB.SemanticModel", folder_id="planned")
    item.parts = de.parts_for(item.folder)
    journal = de.Journal(tmp_path / "j.jsonl", "ws")
    journal.intent(item, "create")
    journal.outcome(item, "Succeeded", "it-1", "op-1")
    reloaded = de.Journal(tmp_path / "j.jsonl", "ws")
    assert reloaded.already_deployed(item) is not None, "the journal believes it is where we put it"

    landing = de.Landing(
        [
            {
                "id": "it-1",
                "displayName": "WB",
                "type": de.MODEL_TYPE,
                "folderId": "moved-by-hand",
                "description": de.PROVENANCE,
            }
        ]
    )
    moves: list[tuple] = []
    monkeypatch.setattr(de, "update_item", lambda *a, **k: ("Succeeded", ""))
    monkeypatch.setattr(de, "move_item", lambda w, t, i, f: moves.append((i, f)) or (True, ""))
    monkeypatch.setattr(de, "stamp_item", lambda *a, **k: True)

    de._deploy_model(de.Target("ws", "LZ", "tok", reloaded, landing), "WB", item)

    assert moves == [("it-1", "planned")], "an item moved in the portal must be put back"


def test_a_refused_move_is_reported_as_a_failure(tmp_path, monkeypatch):
    """R5: exit 0 forever while the estate does not match the plan is indistinguishable from success."""
    bundle = _bundle(tmp_path, {"WB": False})
    item = de.Item("WB", de.MODEL_TYPE, bundle / "pbip" / "WB" / "WB.SemanticModel", folder_id="wanted")
    item.parts = de.parts_for(item.folder)
    journal = de.Journal(tmp_path / "j.jsonl", "ws")
    landing = de.Landing(
        [{"id": "it-1", "displayName": "WB", "type": de.MODEL_TYPE, "folderId": None, "description": de.PROVENANCE}]
    )
    monkeypatch.setattr(de, "update_item", lambda *a, **k: ("Succeeded", ""))
    monkeypatch.setattr(de, "move_item", lambda *a, **k: (False, "HTTP 400"))
    monkeypatch.setattr(de, "stamp_item", lambda *a, **k: True)

    _id, failure = de._deploy_model(de.Target("ws", "LZ", "tok", journal, landing), "WB", item)

    assert failure and "could not be placed" in failure


def test_ensure_folders_itself_is_order_independent(monkeypatch):
    """M1: the previous test applied the sort in its OWN body, so it could not fail.

    It exercised `_display_names(sorted(...))` rather than `ensure_folders`, which is where the
    ordering bug lived.
    """
    seen = []
    for plan in ([["R&D"], ["R/D"]], [["R/D"], ["R&D"]]):
        created: list[str] = []
        monkeypatch.setattr(
            de,
            "call",
            lambda method, url, tok, body=None: (
                (200, {}, {"value": []})
                if method == "GET"
                else (created.append(body["displayName"]), (201, {}, {"id": f"id-{len(created)}"}))[1]
            ),
        )
        resolved = de.ensure_folders("ws", "tok", plan)
        seen.append({"/".join(k): created[int(v.split("-")[1]) - 1] for k, v in resolved.items()})
    assert seen[0] == seen[1], f"folder assignment depends on input order: {seen}"


def test_the_model_named_by_the_report_is_the_one_deployed(tmp_path):
    """M13/R3: sorting made the choice deterministic and deterministically WRONG."""
    bundle = tmp_path / "b"
    wb = bundle / "pbip" / "WB"
    for name in ("Bravo", "WB"):
        (wb / f"{name}.SemanticModel").mkdir(parents=True)
        (wb / f"{name}.SemanticModel" / "definition.pbism").write_text(f'{{"iam":"{name}"}}', encoding="utf-8")
    report = wb / "WB.Report"
    (report / "definition" / "pages").mkdir(parents=True)
    (report / "definition.pbir").write_text(
        json.dumps({"datasetReference": {"byPath": {"path": "../WB.SemanticModel"}}}), encoding="utf-8"
    )
    (report / "definition" / "pages" / "pages.json").write_text(json.dumps({"pageOrder": ["p1"]}), encoding="utf-8")

    (_name, model, _report) = de.discover(bundle)[0]

    assert model.folder.name == "WB.SemanticModel", "the report's own byPath names the right model"


def test_two_items_sharing_a_name_are_refused_rather_than_guessed(tmp_path):
    """M18: the ambiguity branch had no test at all."""
    bundle = _bundle(tmp_path, {"WB": False})
    item = de.Item("WB", de.MODEL_TYPE, bundle / "pbip" / "WB" / "WB.SemanticModel")
    rows = [
        {"id": "a", "displayName": "WB", "type": de.MODEL_TYPE, "description": de.PROVENANCE},
        {"id": "b", "displayName": "WB", "type": de.MODEL_TYPE, "description": de.PROVENANCE},
    ]
    item_id, refusal = de.Landing(rows).claim(item, de.Journal(tmp_path / "j.jsonl", "ws"))
    assert item_id is None
    assert refusal and "ambiguous" in refusal


# --- round 3: the identity chain, tested END TO END rather than at its midpoint ------------------


def _deploy_into(bundle, service, journal, **opts):
    """Run a full deploy against an in-memory Fabric stand-in. Returns the exit code."""
    return de.deploy(bundle, "ws", "tok", _options(journal=journal, **opts))


class _FakeFabric:
    """Enough of Fabric to exercise identity: it does NOT reject a repeated displayName."""

    def __init__(self):
        self.items: list[dict] = []
        self.n = 0

    def call(self, method, url, _tok, body=None):
        if method == "GET" and url.endswith("/items"):
            return 200, {}, {"value": self.items}
        if method == "GET":
            return 200, {}, {"displayName": "LZ"}
        if method == "POST" and url.endswith("/items"):
            self.n += 1
            row = {
                "id": f"i{self.n}",
                "displayName": body["displayName"],
                "type": body["type"],
                "description": body.get("description", ""),
            }
            self.items.append(row)
            return 201, {}, row
        if method == "PATCH":
            for row in self.items:
                if url.endswith(row["id"]):
                    row["description"] = body["description"]
            return 200, {}, {}
        return 200, {}, {}


def test_a_bundle_copied_or_renamed_still_recognises_its_own_estate(tmp_path):
    """A dated output folder, a copy to another machine, or `bundle (1)` must not brick the estate.

    Measured on the previous revision: renaming the bundle directory made a legitimate re-run refuse
    EVERY workbook, advising the operator to use a different landing zone - the opposite of right.
    """
    first = _bundle(tmp_path / "estate-2026-08-13", {"Sales": False})
    service = _FakeFabric()
    de.call = service.call
    try:
        assert _deploy_into(first, service, tmp_path / "j.jsonl") == de.EXIT_OK
        shutil.copytree(first, tmp_path / "estate-2026-08-14" / "bundle")
        renamed = tmp_path / "estate-2026-08-14" / "bundle"

        code = _deploy_into(renamed, service, tmp_path / "j2.jsonl")  # different journal too

        assert code == de.EXIT_OK, "the same bundle under a new folder name must still be ours"
        assert len(service.items) == 1, f"a rename must not create a second copy: {service.items}"
    finally:
        importlib.reload(de)


def test_two_different_estates_in_folders_of_the_same_name_do_not_collide(tmp_path):
    """`bundle` is the literal placeholder in our own docs, so two customers sharing it is the norm."""
    a = _bundle(tmp_path / "customerA" / "bundle", {"Sales": False})
    b = _bundle(tmp_path / "customerB" / "bundle", {"Sales": False})
    (b / "pbip" / "Sales" / "Sales.SemanticModel" / "definition.pbism").write_text('{"iam":"B"}', encoding="utf-8")
    service = _FakeFabric()
    de.call = service.call
    try:
        assert _deploy_into(a, service, tmp_path / "ja.jsonl") == de.EXIT_OK
        code = _deploy_into(b, service, tmp_path / "jb.jsonl")

        assert code == de.EXIT_FAILED, "a different estate must not silently overwrite the first"
        assert len(service.items) == 1
        assert service.items[0]["description"].endswith(de.estate_identity(a))
    finally:
        importlib.reload(de)


def test_the_estate_identity_travels_inside_the_bundle(tmp_path):
    """It is written into the bundle precisely so a copy carries it."""
    bundle = _bundle(tmp_path / "b", {"WB": False})
    minted = de.estate_identity(bundle)
    assert (bundle / de.ESTATE_ID_FILE).read_text(encoding="utf-8").strip() == minted
    assert de.estate_identity(bundle) == minted, "a second call must not mint a new identity"
    assert de.estate_identity(bundle, "explicit-id") == "explicit-id"


def test_an_item_stamped_without_an_estate_is_refused_not_silently_taken(tmp_path):
    """An empty source on EITHER side used to disable the guard, then re-stamp the victim as ours."""
    bundle = _bundle(tmp_path, {"Sales": False})
    mine = de.Item("Sales", de.MODEL_TYPE, bundle / "pbip" / "Sales" / "Sales.SemanticModel", source="mine")
    legacy = {"id": "old-1", "displayName": "Sales", "type": de.MODEL_TYPE, "description": de.PROVENANCE}

    item_id, refusal = de.Landing([legacy]).claim(mine, de.Journal(tmp_path / "j.jsonl", "ws"))

    assert item_id is None
    assert refusal and "earlier version" in refusal


def test_a_governance_tag_added_in_the_portal_does_not_flip_ownership(tmp_path):
    """Prepending or appending to the description is a routine edit; startswith turned it fatal."""
    bundle = _bundle(tmp_path, {"WB": False})
    item = de.Item("WB", de.MODEL_TYPE, bundle / "pbip" / "WB" / "WB.SemanticModel", source="e1")
    journal = de.Journal(tmp_path / "j.jsonl", "ws")
    for description in (
        f"[certified] {de.PROVENANCE} {de.SOURCE_PREFIX} e1",
        f"{de.PROVENANCE} {de.SOURCE_PREFIX} e1 [certified]",
    ):
        row = {"id": "x", "displayName": "WB", "type": de.MODEL_TYPE, "description": description}
        assert de.Landing([row]).claim(item, journal) == ("x", None), description


def test_one_foreign_name_does_not_block_the_rest_of_the_estate(tmp_path, monkeypatch):
    """Per-estate refusal turned one collision into an outage over provably-ours workbooks."""
    bundle = _bundle(tmp_path / "b", {"Clean": False, "Clash": False})
    service = _FakeFabric()
    service.items.append({"id": "cust", "displayName": "Clash", "type": de.MODEL_TYPE, "description": "theirs"})
    de.call = service.call
    try:
        code = _deploy_into(bundle, service, tmp_path / "j.jsonl")
        deployed = [i["displayName"] for i in service.items if i["id"] != "cust"]

        assert code == de.EXIT_FAILED, "the collision must still be reported"
        assert deployed == ["Clean"], f"the clean workbook must still deploy: {service.items}"
    finally:
        importlib.reload(de)


def test_an_empty_report_sharing_a_name_does_not_block_its_workbook(tmp_path):
    """It is skipped before deployment, so it cannot clash - refusing over it aborted clean runs."""
    bundle = _bundle(tmp_path / "b", {"WB": True})
    pages = bundle / "pbip" / "WB" / "WB.Report" / "definition" / "pages" / "pages.json"
    pages.write_text(json.dumps({"pageOrder": []}), encoding="utf-8")
    service = _FakeFabric()
    service.items.append({"id": "cust", "displayName": "WB", "type": de.REPORT_TYPE, "description": "theirs"})
    de.call = service.call
    try:
        code = _deploy_into(bundle, service, tmp_path / "j.jsonl")
        assert code == de.EXIT_OK, "an empty report is never deployed, so its name cannot clash"
        assert [i["displayName"] for i in service.items if i["id"] != "cust"] == ["WB"]
    finally:
        importlib.reload(de)


def test_the_report_names_its_model_even_when_the_fallback_would_disagree(tmp_path):
    """The previous fixture let the folder-name fallback give the same answer, so it proved nothing."""
    bundle = tmp_path / "b"
    wb = bundle / "pbip" / "WB"
    for name in ("Alpha", "WB", "Zulu"):
        (wb / f"{name}.SemanticModel").mkdir(parents=True)
        (wb / f"{name}.SemanticModel" / "definition.pbism").write_text(f'{{"iam":"{name}"}}', encoding="utf-8")
    report = wb / "WB.Report"
    (report / "definition" / "pages").mkdir(parents=True)
    (report / "definition.pbir").write_text(
        json.dumps({"datasetReference": {"byPath": {"path": "../Zulu.SemanticModel"}}}), encoding="utf-8"
    )
    (report / "definition" / "pages" / "pages.json").write_text(json.dumps({"pageOrder": ["p1"]}), encoding="utf-8")

    (_name, model, _report) = de.discover(bundle)[0]

    assert model.folder.name == "Zulu.SemanticModel", "byPath is ground truth and must beat the fallback"


def test_folder_names_do_not_depend_on_the_order_they_arrive_in(caplog):
    """`unique_siblings` assigns "(2)" in input order, so the order must not vary.

    Set-derived order is hash-randomised per process: the same two projects could swap folders
    between runs, filing new content into the one holding the OTHER project. Sorting inside
    `_display_names` makes that impossible regardless of what the caller passes.
    """
    forward = [("R&D",), ("R/D",), ("Ops", "Q1.2"), ("Ops", "Q1-2"), ("Ops",)]
    assert de._display_names(forward) == de._display_names(list(reversed(forward)))


def test_a_stamp_the_service_refused_is_not_recorded_as_applied(monkeypatch):
    """Believing an unapplied stamp makes the NEXT run refuse the item it thinks it marked."""
    monkeypatch.setattr(de, "call", lambda *a, **k: (403, {}, {}))
    item = de.Item("WB", de.MODEL_TYPE, Path("."), source="e1")
    assert de.stamp_item("ws", "tok", "id-1", item) is False

    monkeypatch.setattr(de, "call", lambda *a, **k: (200, {}, {}))
    assert de.stamp_item("ws", "tok", "id-1", item) is True


def test_the_model_choice_is_deterministic_when_nothing_names_a_winner(tmp_path):
    """No byPath and no folder-name match still must not vary between runs."""
    bundle = tmp_path / "b"
    wb = bundle / "pbip" / "WB"
    for name in ("Zulu", "Alpha"):
        (wb / f"{name}.SemanticModel").mkdir(parents=True)
        (wb / f"{name}.SemanticModel" / "definition.pbism").write_text("{}", encoding="utf-8")

    (_name, model, _report) = de.discover(bundle)[0]

    assert model.folder.name == "Alpha.SemanticModel", "the tiebreak must be stable, not glob order"


def test_the_model_tiebreak_does_not_depend_on_directory_listing_order(tmp_path):
    """Filesystem-independent: NTFS enumerates alphabetically, which masks an unsorted glob here."""
    wb = tmp_path / "WB"
    paths = []
    for name in ("Zulu", "Alpha"):
        path = wb / f"{name}.SemanticModel"
        path.mkdir(parents=True)
        paths.append(path)

    assert de._preferred_model(wb, list(paths), []) == wb / "Alpha.SemanticModel"
    assert de._preferred_model(wb, list(reversed(paths)), []) == wb / "Alpha.SemanticModel"


def test_the_binding_matches_the_products_own_serialisation(tmp_path):
    """Ground truth, not reconstruction.

    Captured from `byconnection.Report`, authored in Power BI Desktop against the World_Indicators
    model this deployer had landed in the real landing zone (guid verified against the workspace).
    Our hand-derived form differed in two ways: the data source was unquoted, and `access mode` was
    missing entirely.
    """
    parts = [{"path": "definition.pbir", "payload": "e30=", "payloadType": "InlineBase64"}]
    out = de.rebind(parts, "Tableau Landing Zone", "World_Indicators", "a317a5b3-ebca-4110-ada7-7d9920059109")
    conn = json.loads(base64.b64decode(out[0]["payload"]))["datasetReference"]["byConnection"]["connectionString"]

    assert conn == (
        'Data Source="powerbi://api.powerbi.com/v1.0/myorg/Tableau Landing Zone";'
        "initial catalog=World_Indicators;"
        "access mode=readonly;"
        "integrated security=ClaimsToken;"
        "semanticmodelid=a317a5b3-ebca-4110-ada7-7d9920059109"
    )


def test_a_semicolon_in_a_workspace_name_cannot_truncate_the_connection_string(tmp_path):
    """A bare `;` ends a connection-string segment: unquoted, the binding silently loses everything
    after it, including the semanticmodelid that makes the whole recipe work."""
    parts = [{"path": "definition.pbir", "payload": "e30=", "payloadType": "InlineBase64"}]
    out = de.rebind(parts, "Sales; Finance", "M", "guid-1")
    conn = json.loads(base64.b64decode(out[0]["payload"]))["datasetReference"]["byConnection"]["connectionString"]

    assert conn.startswith('Data Source="powerbi://api.powerbi.com/v1.0/myorg/Sales; Finance";')
    assert "semanticmodelid=guid-1" in conn


def test_a_quote_in_a_workspace_name_is_escaped_rather_than_breaking_the_value():
    """OLE DB quoting: fall back to single quotes, and double the delimiter if both appear."""
    assert de._conn_value("plain") == '"plain"'
    assert de._conn_value('has "quote"') == "'has \"quote\"'"
    assert de._conn_value("both \" and '") == '"both "" and \'"'
