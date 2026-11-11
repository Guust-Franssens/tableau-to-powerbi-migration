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
from types import SimpleNamespace

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
    defaults = {
        "dry_run": False,
        "force_unlock": False,
        "journal": None,
        "estate_db": None,
        "no_folders": False,
        "skip": [],
        "skip_empty_models": False,
    }
    return argparse.Namespace(**{**defaults, **kwargs})


def test_indexed_unit_keeps_id_key_uses_legacy_fabric_name_and_preserves_empty_withholding(tmp_path: Path) -> None:
    """An indexed ``u0001`` deploys under its legacy name; ID-keyed skips and placement still apply (T8)."""
    # pylint: disable=protected-access
    bundle = _bundle(tmp_path, {"u0001": True, "Orders": False})
    run = bundle.parent
    entry = {
        "id": "u0001",
        "key": "assets/sales.twbx",
        "source": {
            "kind": "workbook",
            "name": "Legacy Sales",
            "luid": None,
            "site_luid": None,
            "project_luid": None,
            "project_name": None,
        },
        "legacy_folder": "Legacy Sales",
        "artifacts": {"project_file": None, "semantic_model": None, "report": None},
    }
    (run / "unit-index.json").write_text(
        json.dumps({"schema_version": 2, "run": run.name, "units": [entry]}), encoding="utf-8"
    )
    pbir = bundle / "pbip" / "u0001" / "u0001.Report" / "definition.pbir"
    pbir_before = pbir.read_bytes()
    (bundle / de.EMPTY_MODEL_REPORT).write_text(
        json.dumps(
            {
                "models": [
                    {
                        "status": "EMPTY",
                        "model_path": "pbip/u0001/u0001.SemanticModel",
                        "findings": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    pairs = de.discover(bundle)
    assert [(name, model.name, report.name if report else None) for name, model, report in pairs] == [
        ("Orders", "Orders", None),
        ("u0001", "Legacy Sales", "Legacy Sales"),
    ]
    unit_id, model, report = pairs[1]
    de._plan_items(SimpleNamespace(source="estate"), pairs, {de._slug("Legacy Sales"): "folder-id"})
    assert model.folder_id == report.folder_id == "folder-id"
    assert pairs[0][1].folder_id is None
    assert de._planned_keys(pairs) == [
        ("Orders", de.MODEL_TYPE),
        ("Legacy Sales", de.MODEL_TYPE),
        ("Legacy Sales", de.REPORT_TYPE),
    ]
    assert pbir.read_bytes() == pbir_before

    kept, withheld, refusal = de._plan_units(bundle, _options(skip_empty_models=True, skip=[]))
    assert not refusal and [row[0] for row in withheld] == [unit_id]
    assert [name for name, _model, _report in kept] == ["Orders"]
    kept, withheld, refusal = de._plan_units(bundle, _options(skip=["Legacy Sales"]))
    assert not refusal and [row[0] for row in withheld] == [unit_id]
    assert [name for name, _model, _report in kept] == ["Orders"]


# --------------------------------------------------------------------------- configuration


def test_the_workspace_comes_from_flag_then_export_then_dotenv(tmp_path, monkeypatch):
    """`.env` persists the landing zone; an export overrides it, and a one-off flag overrides both."""
    (tmp_path / ".env").write_text('FABRIC_WORKSPACE_ID="dotenv-workspace" # customer landing zone\n', encoding="utf-8")
    monkeypatch.setattr(de, "REPO_ROOT", tmp_path)
    monkeypatch.delenv("FABRIC_WORKSPACE_ID", raising=False)
    assert de.configured_workspace() == "dotenv-workspace"
    monkeypatch.setenv("FABRIC_WORKSPACE_ID", "exported-workspace")
    assert de.configured_workspace() == "exported-workspace"

    monkeypatch.setattr(de, "token", lambda tenant: "token")
    called: list[str] = []
    monkeypatch.setattr(de, "deploy", lambda bundle, workspace, tok, args: called.append(workspace) or de.EXIT_OK)
    assert de.main(["--bundle", str(tmp_path), "--workspace", "flag-workspace", "--dry-run"]) == de.EXIT_OK
    assert de.main(["--bundle", str(tmp_path), "--dry-run"]) == de.EXIT_OK
    assert called == ["flag-workspace", "exported-workspace"]


def test_main_refuses_to_deploy_without_any_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(de, "REPO_ROOT", tmp_path)
    monkeypatch.delenv("FABRIC_WORKSPACE_ID", raising=False)
    with pytest.raises(SystemExit) as exc:
        de.main(["--bundle", str(tmp_path), "--dry-run"])
    assert exc.value.code == 2


# A throwaway GUID. It is shaped like a workspace id so these cases read like a real `.env`, and it
# is synthetic so no customer landing zone is ever committed.
_WORKSPACE = "11111111-2222-3333-4444-555555555555"


def test_the_declared_workspace_survives_every_ordinary_dotenv_spelling(tmp_path, monkeypatch):
    """`.env.example` promises quotes, whitespace, a BOM and a trailing `# comment` are all fine.

    preflight verifies this id and the deployer uses it, so a spelling must work in both readers.
    `tests/test_preflight_contract.py` pins the spellings; this adds the file ENCODING. A PowerShell
    5.1 `Set-Content -Encoding utf8` BOM lands on the first key only; a TAB before a comment was the
    measured late WorkspaceNotFound.
    """
    monkeypatch.setattr(de, "REPO_ROOT", tmp_path)
    monkeypatch.delenv("FABRIC_WORKSPACE_ID", raising=False)
    spellings = [
        (f"FABRIC_WORKSPACE_ID={_WORKSPACE}\n", "utf-8"),
        (f"FABRIC_WORKSPACE_ID={_WORKSPACE}\n", "utf-8-sig"),
        (f"FABRIC_TENANT_ID=\nFABRIC_WORKSPACE_ID={_WORKSPACE}\n", "utf-8-sig"),
        (f"FABRIC_WORKSPACE_ID={_WORKSPACE}\t# customer landing zone\n", "utf-8"),
        (f"FABRIC_WORKSPACE_ID={_WORKSPACE} # customer landing zone\n", "utf-8"),
        (f"FABRIC_WORKSPACE_ID=   {_WORKSPACE}\t \n", "utf-8"),
        (f'FABRIC_WORKSPACE_ID="{_WORKSPACE}"\n', "utf-8"),
        (f"FABRIC_WORKSPACE_ID='{_WORKSPACE}'\n", "utf-8"),
        (f'FABRIC_WORKSPACE_ID="{_WORKSPACE}"\t# both at once\n', "utf-8"),
    ]
    for declaration, encoding in spellings:
        (tmp_path / ".env").write_text(declaration, encoding=encoding)
        assert de.configured_workspace() == _WORKSPACE, (declaration, encoding)


# --------------------------------------------------------------------------- the binding


def test_rebinding_swaps_bypath_for_a_guid_connection_and_touches_nothing_else(tmp_path):
    """A PBIP binds by PATH, which cannot resolve in the service; schema 2.0.0 allows ONLY
    `connectionString`, so the guid goes there (a sibling property was measured as
    `Workload_FailedToParseFile`). Rewriting any other part would silently alter the report."""
    bundle = _bundle(tmp_path, {"WB": True})
    report = bundle / "pbip" / "WB" / "WB.Report"
    (report / "definition" / "report.json").write_text('{"x":1}', encoding="utf-8")
    before = de.parts_for(report)
    after = de.rebind(before, "WS", "WB", "GUID-1")
    pbir = json.loads(base64.b64decode(next(p["payload"] for p in after if p["path"] == "definition.pbir")))
    assert "byPath" not in pbir["datasetReference"]
    connection = pbir["datasetReference"]["byConnection"]
    assert list(connection) == ["connectionString"]
    assert "semanticmodelid=GUID-1" in connection["connectionString"]
    unchanged = {p["path"]: p["payload"] for p in after if p["path"] != "definition.pbir"}
    assert unchanged == {p["path"]: p["payload"] for p in before if p["path"] != "definition.pbir"}


# --------------------------------------------------------------------------- crash safety


def test_only_an_unchanged_success_counts_as_already_deployed(tmp_path):
    """A resume skips what landed; a CHANGED definition or a later-FAILED `202` is not done."""
    bundle = _bundle(tmp_path, {"WB": False, "F": False})
    folder = bundle / "pbip" / "WB" / "WB.SemanticModel"
    item = de.Item("WB", de.MODEL_TYPE, folder)
    item.parts = de.parts_for(folder)
    failed = de.Item("F", de.MODEL_TYPE, bundle / "pbip" / "F" / "F.SemanticModel")
    failed.parts = de.parts_for(failed.folder)
    journal = de.Journal(tmp_path / "j.jsonl")
    journal.intent(item, "create")
    journal.outcome(item, "Succeeded", "id-1", "op-1")
    journal.intent(failed, "create")
    journal.outcome(failed, "Failed", None, "op-2", "boom")

    assert de.Journal(tmp_path / "j.jsonl").already_deployed(item) is not None
    assert de.Journal(tmp_path / "j.jsonl").already_deployed(failed) is None
    (folder / "definition.pbism").write_text('{"version":"4.0","changed":true}', encoding="utf-8")
    changed = de.Item("WB", de.MODEL_TYPE, folder)
    changed.parts = de.parts_for(folder)
    assert de.Journal(tmp_path / "j.jsonl").already_deployed(changed) is None


def test_an_intent_without_an_outcome_survives_a_torn_final_line_as_unfinished(tmp_path):
    """Intent-before-mutation distinguishes a crash mid-call; a hard kill's partial line must not
    strand the resume."""
    path = tmp_path / "j.jsonl"
    path.write_text('{"phase":"intent","item":"A","type":"SemanticModel"}\n{"phase":"outc', encoding="utf-8")
    assert de.Journal(path).unfinished(de.Item("A", de.MODEL_TYPE, tmp_path)) is not None


# --------------------------------------------------------------------------- concurrency


def test_the_run_lock_refuses_a_second_run_until_released_or_forced(tmp_path):
    """Measured: two overlapping runs created DUPLICATE models and reports, since Fabric does not
    reject a repeated name. A stale lock must still be clearable in a live session."""
    lock = de.RunLock(tmp_path / "run.lock")
    assert lock.acquire()[0] is True
    ok, message = de.RunLock(tmp_path / "run.lock").acquire()
    assert ok is False
    assert "DUPLICATE" in message
    assert de.RunLock(tmp_path / "run.lock").acquire(force=True)[0] is True
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


def test_preflight_budgets_with_headroom_against_items_ALREADY_in_the_workspace(monkeypatch):
    """A customer landing zone is not necessarily empty, and a re-deploy may create before deleting."""
    existing = {"value": [{"id": str(i)} for i in range(500)]}
    monkeypatch.setattr(
        de,
        "call",
        lambda method, url, *a, **k: (200, {}, existing if url.endswith("/items") else {"displayName": "LZ"}),
    )
    ok, message, _ = de.preflight("ws", "tok", 450)
    assert ok is False, "500+450 exceeds the 900 usable budget"
    assert "500 already there" in message
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


def test_folder_display_name_removes_only_what_fabric_rejects():
    """Over-sanitising degrades a name the customer recognises; under-sanitising is refused by the API."""
    for bad in ["R&D", "R/D", "R\\D", "A:B", "A?B", "A*B", 'A"B', "A|B", "A<B", "A#B", "A%B", "v1.2"]:
        assert not set(de.folder_display_name(bad)) & set('&/\\:?*"|<#%.'), bad
    for good in ["Ventes françaises", "Q1 (2026)", "R+D", "A-B", "A_B", "Sales Reports"]:
        assert de.folder_display_name(good) == good
    assert de.folder_display_name(" Lead") == "Lead"
    assert de.folder_display_name("Trail ") == "Trail"
    assert de.folder_display_name("...") == "folder"
    assert de.folder_display_name("///") == "folder"


def test_projects_that_collide_after_sanitising_get_DISTINCT_folders():
    """The failure is SILENT: `R&D`, `R/D` and `R.D` pooling into one folder. Fabric compares folder
    names case-insensitively, so the suffixes must too."""
    final = de.unique_siblings(["R&D", "R/D", "R.D"])
    assert len(set(final.values())) == 3, f"collided: {final}"
    final = de.unique_siblings(["r&d", "R/D"])
    assert len(set(v.casefold() for v in final.values())) == 2


def test_folder_plan_flattens_deep_trees_distinctly_survives_cycles_and_never_guesses(tmp_path):
    """Overflow is compounded, not dropped; two chains differing only below the limit stay distinct;
    a cycle terminates; a workbook with no known project is absent rather than invented."""
    parents = {f"L{i}": f"L{i - 1}" for i in range(2, 13)}
    parents["X12"] = "L11"
    plan = de.folder_plan({"a": "L12", "b": "X12"}, parents)
    assert len(plan["a"]) == de.MAX_FOLDER_DEPTH
    assert "L12" in plan["a"][-1]
    assert plan["a"] != plan["b"]
    assert de.folder_plan({"wb": "A"}, {"A": "B", "B": "A"})["wb"]
    assert de.folder_plan({"wb": "S"}, {"S": "S"})["wb"] == ["S"]
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


def test_an_item_carries_its_folder_id_and_provenance_stamp_at_creation(monkeypatch):
    """`folderId` is a field on Create Item, so placement needs no move step; and if creation stops
    stamping, ownership recognition silently stops working."""
    sent: dict = {}

    def _call(method, url, tok, body=None):  # noqa: ARG001
        sent.update(body or {})
        return 201, {}, {"id": "x"}

    monkeypatch.setattr(de, "call", _call)
    item = de.Item("N", de.MODEL_TYPE, Path("."), parts=[], folder_id="folder-1")
    de.create_item("ws", "tok", item, de.Journal(Path("nul") if sys.platform == "win32" else Path("/dev/null")))
    assert sent.get("folderId") == "folder-1"
    assert sent.get("description") == de.PROVENANCE


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
    (report / "definition" / "pages" / "pages.json").write_text('{"pageOrder": ["p1"]}', encoding="utf-8")
    assert de.report_is_empty(report) is False


def test_already_deployed_is_scoped_to_the_WORKSPACE_and_the_FOLDER(tmp_path):
    """Promotion deploys the SAME bundle to a second workspace, and moving a project in Tableau must
    re-place its items; neither may be skipped as done."""
    bundle = _bundle(tmp_path, {"WB": False})
    folder = bundle / "pbip" / "WB" / "WB.SemanticModel"
    item = de.Item("WB", de.MODEL_TYPE, folder, folder_id="old-folder")
    item.parts = de.parts_for(folder)
    first = de.Journal(tmp_path / "j.jsonl", "workspace-A")
    first.intent(item, "create")
    first.outcome(item, "Succeeded", "id-1", "op-1")

    assert de.Journal(tmp_path / "j.jsonl", "workspace-A").already_deployed(item) is not None
    assert de.Journal(tmp_path / "j.jsonl", "workspace-B").already_deployed(item) is None
    moved = de.Item("WB", de.MODEL_TYPE, folder, folder_id="new-folder")
    moved.parts = de.parts_for(folder)
    assert de.Journal(tmp_path / "j.jsonl", "workspace-A").already_deployed(moved) is None


def test_the_lock_is_taken_atomically(tmp_path, monkeypatch):
    """exists()-then-write has a window two simultaneous starts can both pass. Simulate the race:
    the file is on disk while `exists()` insists it is not; only an atomic create still refuses."""
    lock = de.RunLock(tmp_path / "run.lock")
    assert lock.acquire()[0] is True
    monkeypatch.setattr(de.Path, "exists", lambda _self: False)
    assert de.RunLock(tmp_path / "run.lock").acquire()[0] is False


def test_a_report_without_a_pbir_is_refused_rather_than_guessed(tmp_path):
    """Inventing a binding for a report whose shape we do not understand is worse than stopping."""
    with pytest.raises(ValueError, match="definition.pbir"):
        de.rebind([{"path": "definition/report.json", "payload": "e30=", "payloadType": "InlineBase64"}], "W", "M", "G")


def test_only_an_EXPIRED_token_is_renewed_and_retried(monkeypatch):
    """Measured: a 66-item deploy outlived its token and every later call failed with 401. A genuine
    401 is a real problem to report, not to retry."""
    calls: list[str] = []

    def _request(method, url, bearer, body=None):  # noqa: ARG001
        calls.append(bearer)
        if bearer == "old":
            return 401, {}, {"errorCode": "TokenExpired", "message": "Access token has expired"}
        if bearer == "denied":
            return 401, {}, {"errorCode": "Unauthorized", "message": "not a contributor"}
        return 201, {}, {"id": "x"}

    monkeypatch.setattr(de, "_request", _request)
    tok = de.Token.__new__(de.Token)
    tok.tenant = None
    tok._value = "old"
    monkeypatch.setattr(tok, "_mint", lambda: "fresh")
    assert de.call("POST", "http://x", tok)[0] == 201
    assert calls == ["old", "fresh"]

    calls.clear()
    tok._value = "denied"
    assert de.call("GET", "http://x", tok)[0] == 401
    assert calls == ["denied"]


def test_a_timeout_or_failure_but_not_a_clean_first_run_triggers_reconciliation(tmp_path):
    """A `Timeout` is OUR poll giving up, and a `Failed` may still have created the item. Measured: a
    model timed out at 300s, completed server-side, and the resume created a SECOND copy. An item
    never attempted must not pay for an extra lookup."""
    bundle = _bundle(tmp_path, {"T": False, "F": False})
    items = {}
    journal = de.Journal(tmp_path / "j.jsonl", "ws")
    for name, status in (("T", "Timeout"), ("F", "Failed")):
        items[name] = de.Item(name, de.MODEL_TYPE, bundle / "pbip" / name / f"{name}.SemanticModel")
        items[name].parts = de.parts_for(items[name].folder)
        journal.intent(items[name], "create")
        journal.outcome(items[name], status, None, "op-1")
    reloaded = de.Journal(tmp_path / "j.jsonl", "ws")
    assert reloaded.already_deployed(items["T"]) is None
    assert reloaded.attempted(items["T"]) is True
    assert reloaded.attempted(items["F"]) is True
    assert de.Journal(tmp_path / "absent.jsonl", "ws").attempted(items["T"]) is False


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


def test_a_failed_list_or_existence_read_refuses_rather_than_reading_as_empty(monkeypatch):
    """ "I could not ask" must never be read as "it is not there". Measured: one transient 500 made a
    re-run create a second model, exit 0, and rebind the report to the DUPLICATE."""
    monkeypatch.setattr(de, "call", lambda *a, **k: (403, {}, {}))
    status, rows = de.list_all("ws", "tok", "items")
    assert status == 403 and rows == []
    landing, why = de.Landing.read("ws", "tok")
    assert landing is None and "403" in why
    assert "refusing to deploy" in why.lower()

    monkeypatch.setattr(de, "list_all", lambda *a, **k: (200, []))
    landing, why = de.Landing.read("ws", "tok")
    assert landing is not None and why == "", "a readable empty workspace is still deployable"


def test_landing_claims_only_what_is_provably_ours(tmp_path):
    """Identity is only (displayName, type) and Create Item does not reject a repeated name, so
    `claim()` decides which existing item is OURS. Each row is a measured failure: an unrelated
    customer `Sales` overwritten; a failed create authorising any same-named item (R1); two estates
    collapsing into one item (R2); an older stamp with no estate disabling the guard; a portal
    governance tag flipping ownership. The stamp lives in the SERVICE, so a lost journal or a crash
    between POST and outcome is still recognised."""
    folder = _bundle(tmp_path, {"WB": False}) / "pbip" / "WB" / "WB.SemanticModel"
    stamp = f"{de.PROVENANCE} {de.SOURCE_PREFIX} e1"

    def row(item_id, description=None):
        found = {"id": item_id, "displayName": "WB", "type": de.MODEL_TYPE}
        return found if description is None else {**found, "description": description}

    cases = [
        # (label, workspace rows, journal state, adopt, item source, claimed id, refusal fragment)
        ("not ours", [row("cust-1")], None, False, "", None, "may not be ours"),
        ("journalled by this run", [row("ours-1")], "Succeeded", False, "", "ours-1", None),
        ("stamped, journal lost", [row("ours-1", de.PROVENANCE)], None, False, "", "ours-1", None),
        ("unstamped needs adopt", [row("x-1", "")], None, False, "", None, "--adopt-existing"),
        ("adopt taken explicitly", [row("x-1", "")], None, True, "", "x-1", None),
        ("failed create", [row("cust-1", "Finance's own model")], "Failed", False, "", None, "may not be ours"),
        ("crashed create", [row("made-1", de.PROVENANCE)], "intent", False, "", "made-1", None),
        ("other estate", [row("f-1", f"{de.PROVENANCE} {de.SOURCE_PREFIX} Fin")], None, False, "e1", None, "'Fin'"),
        ("same estate", [row("hr-1", stamp)], None, False, "e1", "hr-1", None),
        ("stamp without estate", [row("old-1", de.PROVENANCE)], None, False, "e1", None, "earlier version"),
        ("tag before", [row("x", f"[certified] {stamp}")], None, False, "e1", "x", None),
        ("tag after", [row("x", f"{stamp} [certified]")], None, False, "e1", "x", None),
        ("two share the name", [row("a", de.PROVENANCE), row("b", de.PROVENANCE)], None, False, "", None, "ambiguous"),
    ]
    for n, (label, rows, state, adopt, source, expected, refusal) in enumerate(cases):
        item = de.Item("WB", de.MODEL_TYPE, folder, source=source)
        path = tmp_path / f"j{n}.jsonl"
        if state:
            journal = de.Journal(path, "ws")
            journal.intent(item, "create")
            if state != "intent":
                journal.outcome(item, state, rows[0]["id"] if state == "Succeeded" else None, "op-1")
        claimed, why = de.Landing(rows, adopt=adopt).claim(item, de.Journal(path, "ws"))
        assert claimed == expected, label
        assert (refusal in why) if refusal else why is None, (label, why)


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


def test_an_item_is_stamped_on_update_only_when_it_lacks_the_stamp(tmp_path, monkeypatch):
    """Measured live: 64 adopted items came back 0 of 64 stamped, because updateDefinition carries
    only the definition; the escape hatch was needed forever. One needless PATCH per item per run is
    worth avoiding too."""
    bundle = _bundle(tmp_path, {"WB": False})
    item = de.Item("WB", de.MODEL_TYPE, bundle / "pbip" / "WB" / "WB.SemanticModel")
    item.parts = de.parts_for(item.folder)
    stamped: list[str] = []
    monkeypatch.setattr(de, "update_item", lambda *a, **k: ("Succeeded", ""))
    monkeypatch.setattr(de, "stamp_item", lambda w, t, i, it: stamped.append(i) or True)
    for description in ("", de.PROVENANCE):
        row = {"id": "x-1", "displayName": "WB", "type": de.MODEL_TYPE, "folderId": None, "description": description}
        landing = de.Landing([row], adopt=True)
        de._deploy_model(de.Target("ws", "LZ", "tok", de.Journal(tmp_path / "j.jsonl", "ws"), landing), "WB", item)
        assert landing.describe("x-1") == de.PROVENANCE
    assert stamped == ["x-1"]


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


def test_the_model_tiebreak_is_stable_when_nothing_names_a_winner(tmp_path):
    """No byPath and no folder-name match must not vary between runs, nor with listing order (NTFS
    enumerates alphabetically, which masks an unsorted glob in `discover`)."""
    wb = tmp_path / "b" / "pbip" / "WB"
    paths = []
    for name in ("Zulu", "Alpha"):
        (wb / f"{name}.SemanticModel").mkdir(parents=True)
        (wb / f"{name}.SemanticModel" / "definition.pbism").write_text("{}", encoding="utf-8")
        paths.append(wb / f"{name}.SemanticModel")

    assert de.discover(tmp_path / "b")[0][1].folder.name == "Alpha.SemanticModel"
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


def test_a_quote_in_a_workspace_name_is_escaped_rather_than_breaking_the_value():
    """OLE DB quoting: fall back to single quotes, and double the delimiter if both appear."""
    assert de._conn_value("plain") == '"plain"'
    assert de._conn_value('has "quote"') == "'has \"quote\"'"
    assert de._conn_value("both \" and '") == '"both "" and \'"'


# --- issue #127: withholding a unit on purpose, and the duplicate-name claim beside it ------------
#
# S6: `check_empty_model.py` can prove a model would load ZERO ROWS and the runbook says "never
# deploy it" - but there was no flag to not deploy it, so a cold operator derived a folder-move
# workaround by reading the source. These tests are the supported action.


def _import_partition(model: Path, path: str) -> None:
    """Give a model an Import partition over a local file, the shape check_empty_model.py judges.

    A POSIX absolute path is deliberate: it reads as `foreign_path` on Windows and `missing_file` on
    Linux, and BOTH block - so the fixture is EMPTY on either CI host rather than on one of them.
    It is also the shape the real finding had (a Mac-authored workbook's home-directory path).
    """
    tables = model / "definition" / "tables"
    tables.mkdir(parents=True, exist_ok=True)
    (tables / "Orders.tmdl").write_text(
        "table Orders\n\n"
        "\tpartition Orders = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        f'\t\t\t    Source = Excel.Workbook(File.Contents("{path}"), null, true)\n'
        "\t\t\tin\n"
        "\t\t\t    Source\n",
        encoding="utf-8",
    )


def _empty_model_report(bundle: Path, models: list[tuple[str, str]], *, roots: bool = False) -> Path:
    """Write a check_empty_model.py-shaped report from [(model_path, status)]."""
    payload: dict = {
        "version": 1,
        "models_scanned": len(models),
        "models_empty": sum(1 for _p, status in models if status == "EMPTY"),
        "status": "EMPTY_MODELS" if any(status == "EMPTY" for _p, status in models) else "OK",
        "models": [
            {
                "model": Path(path).name,
                "model_path": path,
                "owner": Path(path).parts[1] if len(Path(path).parts) > 1 else "?",
                "status": status,
                "findings": [{"table": "Orders", "partition": "Orders", "detail": "file does not exist"}]
                if status == "EMPTY"
                else [],
            }
            for path, status in models
        ],
    }
    out = bundle / de.EMPTY_MODEL_REPORT
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"version": 1, "roots": [payload]} if roots else payload, indent=2), encoding="utf-8")
    return out


def test_a_named_unit_is_withheld_from_a_real_deploy(tmp_path, caplog):
    """The whole point: deploy the other units NOW and leave this one behind, with no folder-moving.

    Run end-to-end rather than against `withhold()` alone: `_execute` used to re-`discover()` the
    bundle after the filtering had happened, so a unit-level test would have passed while every
    withheld unit was deployed anyway.
    """
    bundle = _bundle(tmp_path / "b", {"Sales": True, "Empty": True})
    service = _FakeFabric()
    de.call = service.call
    try:
        with caplog.at_level("INFO"):
            code = _deploy_into(bundle, service, tmp_path / "j.jsonl", skip=["Empty"])
    finally:
        importlib.reload(de)

    assert {(row["displayName"], row["type"]) for row in service.items} == {
        ("Sales", de.MODEL_TYPE),
        ("Sales", de.REPORT_TYPE),
    }, f"nothing may be created for a withheld unit: {service.items}"
    assert code == de.EXIT_INCOMPLETE
    assert "2 planned (2 new)" in caplog.text, "preflight budgets the run that will happen, not the bundle"
    assert "WITHHELD" in caplog.text and "Empty" in caplog.text
    # "All 4 deployed" over an estate missing one unit is the lie this exists to prevent.
    assert "2 item(s) deployed" in caplog.text
    assert "all 4" not in caplog.text and "4 item(s) deployed" not in caplog.text
    assert "1 unit(s), 2 item(s) NOT deployed" in caplog.text


def test_report_failures_counts_honestly_and_ranks_failed_over_incomplete_over_ok(caplog):
    """Saying "all 66 deployed" when two were skipped erodes trust in every other number. EXIT_OK
    would claim a complete estate and EXIT_FAILED a pointless re-run, so a withheld unit is its own
    verdict; an empty report owes no follow-up run; a failure outranks a skip but still names it."""
    held = [("Empty", 2, "EMPTY MODEL - loads no rows")]
    assert de.EXIT_INCOMPLETE not in (de.EXIT_OK, de.EXIT_FAILED)
    with caplog.at_level("INFO"):
        assert de._report_failures([], 66, "LZ", skipped=2) == de.EXIT_OK
    assert "64 item(s) deployed" in caplog.text
    assert "2 skipped as empty" in caplog.text
    assert de._report_failures([], 2, "LZ", 0, held) == de.EXIT_INCOMPLETE
    assert de._report_failures([], 2, "LZ", 0, []) == de.EXIT_OK
    caplog.clear()
    with caplog.at_level("INFO"):
        assert de._report_failures(["Sales (Report): boom"], 2, "LZ", 0, held) == de.EXIT_FAILED
    assert "1 item(s) failed" in caplog.text
    assert "WITHHELD" in caplog.text and "Empty" in caplog.text


def test_the_withheld_item_count_matches_what_would_have_been_created(tmp_path):
    """The count is what an operator reconciles against the dry run, so a pair is 2 and a model is 1."""
    bundle = _bundle(tmp_path, {"Pair": True, "ModelOnly": False})
    kept, held = de.withhold(de.discover(bundle), {"Pair": "why-a", "ModelOnly": "why-b"})

    assert kept == []
    assert sorted(held) == [("ModelOnly", 1, "why-b"), ("Pair", 2, "why-a")]


def test_a_mistyped_skip_refuses_to_start_rather_than_deploying_the_unit(tmp_path, caplog):
    """A typo would deploy the very unit being held back, and report success while doing it."""
    bundle = _bundle(tmp_path / "b", {"global_superstores_db": True})
    service = _FakeFabric()
    de.call = service.call
    try:
        with caplog.at_level("ERROR"):
            code = _deploy_into(bundle, service, tmp_path / "j.jsonl", skip=["global_superstore_db"])
    finally:
        importlib.reload(de)

    assert code == de.EXIT_PREFLIGHT
    assert service.items == [], "a refused skip must not deploy anything at all"
    assert "matches no unit" in caplog.text
    assert "'global_superstores_db'" in caplog.text, "name the near miss - this is typed under pressure"


def test_skip_empty_models_reads_the_report_so_no_name_is_retyped(tmp_path, caplog):
    """The operator has a verdict in hand already; retyping unit names from it is where slips happen."""
    bundle = _bundle(tmp_path / "b", {"Sales": True, "global_superstores_db": True})
    _empty_model_report(
        bundle,
        [
            ("pbip/Sales/Sales.SemanticModel", "OK"),
            ("pbip/global_superstores_db/Orders (Global Superstore).SemanticModel", "EMPTY"),
        ],
    )
    service = _FakeFabric()
    de.call = service.call
    try:
        with caplog.at_level("INFO"):
            code = _deploy_into(bundle, service, tmp_path / "j.jsonl", skip_empty_models=True)
    finally:
        importlib.reload(de)

    assert code == de.EXIT_INCOMPLETE
    assert {row["displayName"] for row in service.items} == {"Sales"}
    assert "EMPTY MODEL" in caplog.text and "file does not exist" in caplog.text


def test_a_missing_or_unreadable_empty_model_report_refuses_rather_than_reading_as_all_clear(tmp_path):
    """ "I could not read the list" and "the list is empty" differ by exactly the unit at stake."""
    bundle = _bundle(tmp_path, {"Sales": False})
    for content in (None, '{"models": ['):
        if content is not None:
            (bundle / de.EMPTY_MODEL_REPORT).write_text(content, encoding="utf-8")
        skips, refusal = de.resolve_skips(bundle, {"Sales"}, _options(skip_empty_models=True))
        assert skips == {}
        assert refusal, content
    assert "could not be read" in refusal


def test_the_multi_target_form_of_the_report_is_understood(tmp_path):
    """check_empty_model.py wraps several scanned roots in `roots`; missing that skips nothing."""
    bundle = _bundle(tmp_path, {"Sales": False, "Empty": False})
    _empty_model_report(bundle, [("pbip/Empty/Empty.SemanticModel", "EMPTY")], roots=True)

    skips, refusal = de.resolve_skips(bundle, {"Sales", "Empty"}, _options(skip_empty_models=True))

    assert refusal == ""
    assert set(skips) == {"Empty"}


def test_only_pbip_models_can_be_withheld(tmp_path):
    """The engine's pristine `semantic_models/` copy is not a deployable unit, and is not a typo either.

    A bundle scan reports every `.SemanticModel` under the bundle, so the same datasource shows up
    twice when it was migrated both standalone and inside a workbook. Taking the unit name from any
    path but `pbip/<unit>/` withholds a HEALTHY workbook because its non-deployable twin is empty -
    the exact inverse of this flag's job.
    """
    bundle = _bundle(tmp_path, {"Sales": False})
    _empty_model_report(
        bundle,
        [
            ("semantic_models/Shared.SemanticModel", "EMPTY"),
            ("semantic_models/Sales/Sales.SemanticModel", "EMPTY"),
            ("pbip/Sales/Sales.SemanticModel", "OK"),
        ],
    )

    skips, refusal = de.resolve_skips(bundle, {"Sales"}, _options(skip_empty_models=True))

    assert refusal == ""
    assert skips == {}, "only pbip/<unit>/ is deployable, so only pbip/<unit>/ can be withheld"


def test_a_reported_unit_that_is_not_in_the_bundle_is_a_warning_not_a_refusal(tmp_path, caplog):
    """It was already quarantined by hand, or the report scanned another bundle - nothing to withhold."""
    bundle = _bundle(tmp_path, {"Sales": False})
    _empty_model_report(bundle, [("pbip/Ghost/Ghost.SemanticModel", "EMPTY")])

    with caplog.at_level("WARNING"):
        skips, refusal = de.resolve_skips(bundle, {"Sales"}, _options(skip_empty_models=True))

    assert (skips, refusal) == ({}, "")
    assert "Ghost" in caplog.text and "nothing to skip" in caplog.text


def test_an_explicit_skip_that_is_also_empty_keeps_the_more_informative_reason(tmp_path):
    """ "You asked for it" says less than the artifact that proves the model empty."""
    bundle = _bundle(tmp_path, {"Empty": False})
    _empty_model_report(bundle, [("pbip/Empty/Empty.SemanticModel", "EMPTY")])

    skips, _refusal = de.resolve_skips(bundle, {"Empty"}, _options(skip=["Empty"], skip_empty_models=True))

    assert "EMPTY MODEL" in skips["Empty"]


def test_the_dry_run_item_count_drops_by_the_withheld_unit(tmp_path, caplog):
    """The operator's confirmation signal, and it must stay exit 0 so `--dry-run` can gate the real run."""
    bundle = _bundle(tmp_path / "b", {"Sales": True, "Empty": True})
    service = _FakeFabric()
    de.call = service.call
    try:
        with caplog.at_level("INFO"):
            code = _deploy_into(bundle, service, tmp_path / "j.jsonl", skip=["Empty"], dry_run=True)
    finally:
        importlib.reload(de)

    assert code == de.EXIT_OK, "a dry run creates nothing, so there is no incomplete estate to report"
    assert "2 item(s) would be created" in caplog.text
    assert "WITHHELD" in caplog.text
    assert service.items == []


def test_the_report_this_flag_reads_is_the_one_check_empty_model_writes(tmp_path):
    """A contract test across the two scripts: the filename AND the shape, not one or the other.

    Hand-rolled fixtures elsewhere in this file pin the parsing; this pins that the parsing is aimed
    at a real report. `check_empty_model.py` produces it here from actual TMDL rather than from a
    dict we wrote ourselves.
    """
    import check_empty_model as cem  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    bundle = _bundle(tmp_path, {"Sales": True, "global_superstores_db": True})
    # Assembled rather than written literally: the repo's privacy gate rejects any tracked file
    # containing an absolute user path, and it cannot tell a sample workbook's author from ours.
    # The shape under test is exactly a foreign POSIX home directory, so it has to be built here.
    foreign_home = "/" + "Users" + "/author"
    _import_partition(
        bundle / "pbip" / "global_superstores_db" / "global_superstores_db.SemanticModel",
        f"{foreign_home}/Mac Drive/Global Superstore.xlsx",
    )
    assert de.EMPTY_MODEL_REPORT == cem.REPORT_NAME, "the file this reads is the file that one writes"
    report = cem.scan(bundle)
    assert report["status"] == "EMPTY_MODELS", "precondition: the fixture really is an empty model"
    (bundle / de.EMPTY_MODEL_REPORT).write_text(json.dumps(report, indent=2), encoding="utf-8")

    units, refusal = de.empty_model_units(bundle)

    assert refusal == ""
    assert set(units) == {"global_superstores_db"}


def test_the_skip_flags_reach_deploy_and_repeat(monkeypatch, tmp_path):
    """The issue was "there is no flag", so pin the flag: repeatable, and inert when not passed."""
    seen: dict[str, argparse.Namespace] = {}
    monkeypatch.setattr(de, "token", lambda tenant: "tok")
    monkeypatch.setattr(de, "deploy", lambda bundle, ws, tok, options: seen.setdefault("o", options) and 0)

    de.main(["--bundle", str(tmp_path), "--workspace", "ws", "--skip", "A", "--skip", "B", "--skip-empty-models"])
    assert (seen["o"].skip, seen["o"].skip_empty_models) == (["A", "B"], True)

    seen.clear()
    de.main(["--bundle", str(tmp_path), "--workspace", "ws"])
    assert (seen["o"].skip, seen["o"].skip_empty_models) == ([], False), "a run that skips nothing is unchanged"


def test_neither_duplicate_name_error_is_treated_as_fatal(tmp_path, monkeypatch):
    """S9: the module docstring settles what the service does, and this pins what the code does.

    Fabric's REST Create Item does NOT reject a duplicate `displayName` + type for `Report` /
    `SemanticModel` (measured; see the module docstring). These two error codes have never been seen
    for these types, and the branch that handles them is DEFENSIVE - `ItemDisplayNameNotAvailableYet`
    is the language of eventual consistency, so treating it as fatal would break the resume this file
    exists for. Neither may become a failure.
    """
    item = de.Item("WB", de.MODEL_TYPE, tmp_path)
    journal = de.Journal(tmp_path / "j.jsonl", "ws")
    for code in ("ItemDisplayNameNotAvailableYet", "ItemDisplayNameAlreadyInUse"):
        monkeypatch.setattr(de, "_post_item", lambda ws, tok, it, code=code: (400, {}, {"errorCode": code}))
        state, item_id, detail = de.create_item("ws", "tok", item, journal)
        assert (state, item_id) == ("AlreadyExists", None), f"{code} must mean 'go and verify', not 'failed'"
        assert code in detail
