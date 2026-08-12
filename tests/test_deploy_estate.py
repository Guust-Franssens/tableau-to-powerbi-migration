"""Tests for scripts/deploy_estate.py.

Most of these exist because of something that actually happened against a real Fabric tenant, not
because of a hypothesis. The deployer's job is to be safe to re-run in front of a customer, so the
cases that matter are the ones where a naive implementation looks like it worked.
"""

from __future__ import annotations

import argparse
import base64
import json
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
    assert "semanticModelId=GUID-1" in connection["connectionString"]


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
    monkeypatch.setattr(de, "find_existing", lambda *a, **k: None)
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


def test_the_lock_is_taken_atomically(tmp_path):
    """exists()-then-write has a window two simultaneous starts can both pass."""
    lock = de.RunLock(tmp_path / "run.lock")
    assert lock.acquire()[0] is True
    # A second acquire must fail even though the first process is this same one.
    assert de.RunLock(tmp_path / "run.lock").acquire()[0] is False


def test_a_report_without_a_pbir_is_refused_rather_than_guessed(tmp_path):
    """Inventing a binding for a report whose shape we do not understand is worse than stopping."""
    with pytest.raises(ValueError, match="definition.pbir"):
        de.rebind([{"path": "definition/report.json", "payload": "e30=", "payloadType": "InlineBase64"}], "W", "M", "G")
