"""Controlled tests for `scripts/pbir_revision.py` - slice A1a of issue #363.

The invariant under test: given an already-selected `fabric/` root and ONE report artifact, either
produce a strict typed census plus a deterministic revision digest, or refuse with positive evidence.
Nothing here touches Power BI Desktop, a PID, a screenshot, a receipt or a sign-off - those are later
slices, and a test that reached for them would be testing the wrong thing.

Three kinds of control run here:

* **Corpus** - every committed report tree in this repo, judged against an EXACT expected verdict
  table (`CORPUS`). A newly committed report shape fails this table rather than being adopted
  silently, which is the point of a closed census.
* **Positive** - a synthetic tree carrying one of every included entry type, so a mutation control
  exists for each of them.
* **Negative** - one control per refusal rule, each asserting the SPECIFIC code. A negative control
  that merely asserts "not established" would pass for the wrong reason (this happened during
  development: a fixture missing its `.pbip` refused as `pbip_absent` long before reaching the rule
  it was written for).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# ruff: noqa: E402  (the sys.path insert above must precede this import)
# pylint: disable=wrong-import-position
import pbir_revision as pbir

# --------------------------------------------------------------------------------------------
# Synthetic positive fixture: one of every entry type the census includes.
# --------------------------------------------------------------------------------------------

PAGE_ORDER = ["p_first", "p_second"]


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, (dict, list)):
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    else:
        path.write_text(str(payload), encoding="utf-8")


def _visual(name: str) -> dict[str, object]:
    return {"$schema": "https://example.invalid/visualContainer/2.9.0/schema.json", "name": name}


def _build(root: Path) -> tuple[Path, Path]:
    """A complete, valid fabric root. Returns (fabric_root, report_dir).

    The visual FOLDER names deliberately differ from the visual document ids, because 133 of the 869
    committed example visuals do exactly that - a fixture where they matched would let a
    folder-as-identity bug pass.
    """
    fabric = root / "fabric"
    report = fabric / "Demo.Report"
    model = fabric / "Demo.SemanticModel"
    _write(fabric / "Demo.pbip", {"version": "1.0", "artifacts": [{"report": {"path": "Demo.Report"}}]})
    _write(report / ".platform", {"metadata": {"type": "Report", "displayName": "Demo"}})
    _write(
        report / "definition.pbir",
        {"version": "4.0", "datasetReference": {"byPath": {"path": "../Demo.SemanticModel"}}},
    )
    _write(report / "definition" / "version.json", {"version": "2.0.0"})
    _write(
        report / "definition" / "report.json",
        {
            "themeCollection": {},
            "resourcePackages": [
                {
                    "name": "SharedResources",
                    "type": "SharedResources",
                    "items": [{"name": "CY24SU10", "path": "BaseThemes/CY24SU10.json", "type": "BaseTheme"}],
                },
                {
                    "name": "RegisteredResources",
                    "type": "RegisteredResources",
                    "items": [{"name": "theme.json", "path": "theme.json", "type": "CustomTheme"}],
                },
            ],
        },
    )
    _write(report / "StaticResources" / "RegisteredResources" / "theme.json", {"name": "DemoTheme"})
    _write(report / "definition" / "pages" / "pages.json", {"pageOrder": PAGE_ORDER, "activePageName": "p_first"})
    for index, page_id in enumerate(PAGE_ORDER):
        page_dir = report / "definition" / "pages" / page_id
        _write(page_dir / "page.json", {"name": page_id, "displayName": f"Page {index}", "height": 800, "width": 1400})
        _write(page_dir / "visuals" / f"friendly-name-{index}" / "visual.json", _visual(f"v{index}0"))
        _write(page_dir / "visuals" / f"another-{index}" / "visual.json", _visual(f"v{index}1"))
    _write(model / ".platform", {"metadata": {"type": "SemanticModel", "displayName": "Demo"}})
    _write(model / "definition.pbism", {"version": "4.2", "settings": {"qnaEnabled": True}})
    _write(model / "definition" / "model.tmdl", "model Model\n")
    _write(model / "definition" / "database.tmdl", "database Demo\n")
    _write(model / "definition" / "relationships.tmdl", "relationship r1\n")
    _write(model / "definition" / "expressions.tmdl", 'expression DataFolder = "x"\n')
    _write(model / "definition" / "cultures" / "en-US.tmdl", "cultureInfo en-US\n")
    _write(model / "definition" / "tables" / "Sales.tmdl", "table Sales\n")
    return fabric, report


@pytest.fixture(name="tree")
def tree_fixture(tmp_path: Path) -> tuple[Path, Path]:
    return _build(tmp_path)


def _establish(tree: tuple[Path, Path]) -> pbir.PbirRevision:
    result = pbir.establish_revision(*tree)
    assert isinstance(result, pbir.PbirRevision), getattr(result, "detail", result)
    return result


def _refuse(tree: tuple[Path, Path]) -> pbir.RevisionRefusal:
    result = pbir.establish_revision(*tree)
    assert isinstance(result, pbir.RevisionRefusal), "expected a refusal, got an established revision"
    return result


def _append_one_byte(path: Path) -> None:
    with path.open("ab") as handle:
        handle.write(b" ")


def _patch_json(path: Path, mutate) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------------------------
# Positive control
# --------------------------------------------------------------------------------------------


def test_synthetic_tree_establishes_with_full_census(tree: tuple[Path, Path]) -> None:
    revision = _establish(tree)
    assert revision.version == pbir.VERSION_TAG
    assert revision.pbip == "Demo.pbip"
    assert revision.report == "Demo.Report"
    assert revision.model == "Demo.SemanticModel"
    assert revision.model_binding == "../Demo.SemanticModel"
    assert revision.page_order == tuple(PAGE_ORDER)
    assert [page.page_id for page in revision.pages] == PAGE_ORDER
    assert revision.visual_count == 4
    included = set(dict(revision.files))
    assert included == {
        "Demo.pbip",
        "Demo.Report/.platform",
        "Demo.Report/definition.pbir",
        "Demo.Report/definition/report.json",
        "Demo.Report/definition/version.json",
        "Demo.Report/definition/pages/pages.json",
        "Demo.Report/definition/pages/p_first/page.json",
        "Demo.Report/definition/pages/p_first/visuals/another-0/visual.json",
        "Demo.Report/definition/pages/p_first/visuals/friendly-name-0/visual.json",
        "Demo.Report/definition/pages/p_second/page.json",
        "Demo.Report/definition/pages/p_second/visuals/another-1/visual.json",
        "Demo.Report/definition/pages/p_second/visuals/friendly-name-1/visual.json",
        "Demo.Report/StaticResources/RegisteredResources/theme.json",
        "Demo.SemanticModel/.platform",
        "Demo.SemanticModel/definition.pbism",
        "Demo.SemanticModel/definition/model.tmdl",
        "Demo.SemanticModel/definition/database.tmdl",
        "Demo.SemanticModel/definition/relationships.tmdl",
        "Demo.SemanticModel/definition/expressions.tmdl",
        "Demo.SemanticModel/definition/cultures/en-US.tmdl",
        "Demo.SemanticModel/definition/tables/Sales.tmdl",
    }


def test_visual_folder_and_document_identity_are_recorded_separately(tree: tuple[Path, Path]) -> None:
    """The folder is not the visual's identity - 133 of 869 committed examples prove it."""
    revision = _establish(tree)
    first = revision.pages[0].visuals[0]
    assert first.folder == "another-0"
    assert first.document_id == "v01"
    assert first.file == "Demo.Report/definition/pages/p_first/visuals/another-0/visual.json"


def test_a_page_with_no_visuals_directory_is_a_census_not_a_refusal(tree: tuple[Path, Path]) -> None:
    """`fixtures/large-refresh` commits exactly this shape, so it must census rather than refuse."""
    shutil.rmtree(tree[1] / "definition" / "pages" / "p_second" / "visuals")
    revision = _establish(tree)
    assert revision.pages[1].visuals == ()
    assert revision.visual_count == 2


def test_refusal_is_falsy_and_a_revision_is_truthy(tree: tuple[Path, Path]) -> None:
    """`if revision:` must fail CLOSED - a refusal that reads as true is a fail-open consumer."""
    assert bool(_establish(tree)) is True
    (tree[1] / "definition" / "pages" / "p_first" / "page.json").unlink()
    assert bool(_refuse(tree)) is False


# --------------------------------------------------------------------------------------------
# Determinism and order independence
# --------------------------------------------------------------------------------------------


def test_digest_is_stable_across_repeated_runs(tree: tuple[Path, Path]) -> None:
    assert _establish(tree).digest == _establish(tree).digest


def test_digest_is_independent_of_filesystem_enumeration_order(
    tree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A different `iterdir` order must not move the digest, the file list, or page/visual order."""
    baseline = _establish(tree)
    original = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", lambda self: iter(sorted(original(self), key=str, reverse=True)))
    shuffled = _establish(tree)
    assert shuffled.digest == baseline.digest
    assert shuffled.files == baseline.files
    assert shuffled.pages == baseline.pages


def test_digest_covers_the_version_tag() -> None:
    """The framing tag is part of the hash, so a future revision format cannot collide with v1."""
    assert pbir.revision_digest([("a", b"x")]) != pbir.revision_digest([("a", b"x"), (pbir.VERSION_TAG, b"")])


def test_revision_digest_sorts_its_own_input(tree: tuple[Path, Path]) -> None:
    """Order independence has TWO layers and both must hold.

    `_entries` normalises directory enumeration, and `revision_digest` sorts the file set it is
    handed. Mutation testing caught this gap: with only the filesystem-level control, deleting the
    sort inside `revision_digest` changed nothing, because the walk happened to insert in sorted
    order anyway. A caller passing the same files in another order must still get one digest.
    """
    files = [(rel, rel.encode("utf-8")) for rel, _sha in _establish(tree).files]
    assert pbir.revision_digest(files) == pbir.revision_digest(list(reversed(files)))


def test_digest_framing_resists_a_path_content_boundary_shift() -> None:
    """Without length delimiters these two file sets would hash identically."""
    assert pbir.revision_digest([("ab", b"c")]) != pbir.revision_digest([("a", b"bc")])


MUTABLE = [
    "Demo.pbip",
    "Demo.Report/.platform",
    "Demo.Report/definition.pbir",
    "Demo.Report/definition/report.json",
    "Demo.Report/definition/version.json",
    "Demo.Report/definition/pages/pages.json",
    "Demo.Report/definition/pages/p_first/page.json",
    "Demo.Report/definition/pages/p_first/visuals/another-0/visual.json",
    "Demo.Report/StaticResources/RegisteredResources/theme.json",
    "Demo.SemanticModel/.platform",
    "Demo.SemanticModel/definition.pbism",
    "Demo.SemanticModel/definition/model.tmdl",
    "Demo.SemanticModel/definition/cultures/en-US.tmdl",
    "Demo.SemanticModel/definition/tables/Sales.tmdl",
]


@pytest.mark.parametrize("relative", MUTABLE)
def test_one_byte_change_in_any_included_file_moves_the_digest(tree: tuple[Path, Path], relative: str) -> None:
    baseline = _establish(tree)
    _append_one_byte(tree[0] / relative)
    mutated = _establish(tree)
    assert mutated.digest != baseline.digest
    assert dict(mutated.files)[relative] != dict(baseline.files)[relative]


def test_one_byte_change_in_a_committed_corpus_report_moves_the_digest(tmp_path: Path) -> None:
    """The same control on a REAL committed tree, not only on the synthetic one."""
    copied = tmp_path / "fabric"
    shutil.copytree(REPO_ROOT / "fixtures" / "large-refresh" / "fabric", copied)
    report = copied / "LargeRefresh.Report"
    baseline = _establish((copied, report))
    _append_one_byte(report / "definition" / "pages" / "largeRefreshPage" / "page.json")
    assert _establish((copied, report)).digest != baseline.digest


# --------------------------------------------------------------------------------------------
# The explicit exclusion rule
# --------------------------------------------------------------------------------------------


def test_local_and_volatile_state_is_excluded_by_name_without_moving_the_digest(tree: tuple[Path, Path]) -> None:
    """`.pbi/`, `TMDLScripts/` and volatile sidecars are machine state, not a portable revision."""
    fabric, report = tree
    baseline = _establish(tree)
    _write(report / ".pbi" / "localSettings.json", {"local": True})
    (report / ".pbi" / "cache.abf").write_bytes(b"\x00" * 32)
    _write(fabric / "Demo.SemanticModel" / ".pbi" / "localSettings.json", {"local": True})
    _write(fabric / "Demo.SemanticModel" / "TMDLScripts" / "Script 1.tmdl", "table Scratch\n")
    _write(report / "definition" / "pages" / "p_first" / "page.json.tmp", "half written")
    _write(report / "definition" / "report.json.autosave", "recovered")
    _write(fabric / "Demo.SemanticModel" / "definition" / "model.tmdl.bak", "table Old\n")
    (fabric / "Demo.SemanticModel" / "cache.abf").write_bytes(b"\x00" * 8)
    _write(fabric / "~$Demo.pbip", "lock")
    mutated = _establish(tree)
    assert mutated.digest == baseline.digest
    reasons = dict(mutated.excluded)
    assert reasons["Demo.Report/.pbi"] == "local-desktop-state"
    assert reasons["Demo.SemanticModel/.pbi"] == "local-desktop-state"
    assert reasons["Demo.SemanticModel/TMDLScripts"] == "authoring-scratch"
    for sidecar in (
        "Demo.Report/definition/pages/p_first/page.json.tmp",
        "Demo.Report/definition/report.json.autosave",
        "Demo.SemanticModel/definition/model.tmdl.bak",
        "Demo.SemanticModel/cache.abf",
        "~$Demo.pbip",
    ):
        assert reasons[sidecar] == "volatile-sidecar", sidecar


def test_a_sibling_pbip_project_is_recorded_as_excluded_not_hashed(tree: tuple[Path, Path]) -> None:
    """Another project's bytes are not part of THIS report's revision, but must not vanish silently."""
    fabric, _report = tree
    baseline = _establish(tree)
    _write(fabric / "Other.pbip", {"version": "1.0", "artifacts": [{"report": {"path": "Other.Report"}}]})
    mutated = _establish(tree)
    assert mutated.digest == baseline.digest
    assert dict(mutated.excluded)["Other.pbip"] == "other-project"


# --------------------------------------------------------------------------------------------
# Negative controls - each asserts its OWN code
# --------------------------------------------------------------------------------------------


def test_missing_page_json_refuses(tree: tuple[Path, Path]) -> None:
    (tree[1] / "definition" / "pages" / "p_first" / "page.json").unlink()
    assert _refuse(tree).code == pbir.ENTRY_MISSING


def test_missing_visual_json_refuses(tree: tuple[Path, Path]) -> None:
    (tree[1] / "definition" / "pages" / "p_first" / "visuals" / "another-0" / "visual.json").unlink()
    assert _refuse(tree).code == pbir.ENTRY_MISSING


def test_page_order_naming_an_absent_page_refuses(tree: tuple[Path, Path]) -> None:
    pages = tree[1] / "definition" / "pages" / "pages.json"
    _patch_json(pages, lambda doc: doc["pageOrder"].append("p_ghost"))
    refusal = _refuse(tree)
    assert refusal.code == pbir.PAGE_MISSING
    assert refusal.evidence == ("p_ghost",)


def test_orphan_page_directory_refuses(tree: tuple[Path, Path]) -> None:
    orphan = tree[1] / "definition" / "pages" / "p_orphan"
    _write(orphan / "page.json", {"name": "p_orphan", "displayName": "Orphan"})
    refusal = _refuse(tree)
    assert refusal.code == pbir.PAGE_ORPHAN
    assert refusal.evidence == ("p_orphan",)


def test_page_document_id_disagreeing_with_its_folder_refuses(tree: tuple[Path, Path]) -> None:
    page = tree[1] / "definition" / "pages" / "p_first" / "page.json"
    _patch_json(page, lambda doc: doc.update({"name": "p_elsewhere"}))
    assert _refuse(tree).code == pbir.PAGE_ID_MISMATCH


def test_duplicate_page_id_in_page_order_refuses(tree: tuple[Path, Path]) -> None:
    pages = tree[1] / "definition" / "pages" / "pages.json"
    _patch_json(pages, lambda doc: doc.update({"pageOrder": ["p_first", "p_first"]}))
    assert _refuse(tree).code == pbir.PAGE_ORDER_MALFORMED


def test_active_page_outside_page_order_refuses(tree: tuple[Path, Path]) -> None:
    pages = tree[1] / "definition" / "pages" / "pages.json"
    _patch_json(pages, lambda doc: doc.update({"activePageName": "p_nowhere"}))
    assert _refuse(tree).code == pbir.PAGE_ORDER_MALFORMED


def test_duplicate_visual_document_id_across_pages_refuses(tree: tuple[Path, Path]) -> None:
    """Uniqueness is report-wide: a per-page check passes this tree and is a real defect class."""
    target = tree[1] / "definition" / "pages" / "p_second" / "visuals" / "another-1" / "visual.json"
    _patch_json(target, lambda doc: doc.update({"name": "v01"}))
    assert _refuse(tree).code == pbir.VISUAL_ID_DUPLICATE


def test_empty_visual_document_id_refuses(tree: tuple[Path, Path]) -> None:
    target = tree[1] / "definition" / "pages" / "p_first" / "visuals" / "another-0" / "visual.json"
    _patch_json(target, lambda doc: doc.update({"name": "   "}))
    assert _refuse(tree).code == pbir.JSON_TYPE


def test_unknown_file_in_the_report_root_refuses(tree: tuple[Path, Path]) -> None:
    _write(tree[1] / "mobileState.json", {"unknown": True})
    assert _refuse(tree).code == pbir.ENTRY_UNKNOWN


def test_unknown_directory_in_the_definition_refuses(tree: tuple[Path, Path]) -> None:
    _write(tree[1] / "definition" / "bookmarks" / "b1.json", {"unknown": True})
    assert _refuse(tree).code == pbir.ENTRY_UNKNOWN


def test_unknown_file_beside_a_visual_refuses(tree: tuple[Path, Path]) -> None:
    _write(tree[1] / "definition" / "pages" / "p_first" / "visuals" / "another-0" / "mobile.json", {})
    assert _refuse(tree).code == pbir.ENTRY_UNKNOWN


def test_loose_file_directly_inside_visuals_refuses(tree: tuple[Path, Path]) -> None:
    _write(tree[1] / "definition" / "pages" / "p_first" / "visuals" / "stray.json", {})
    assert _refuse(tree).code == pbir.ENTRY_UNKNOWN


def test_unknown_model_definition_entry_refuses(tree: tuple[Path, Path]) -> None:
    _write(tree[0] / "Demo.SemanticModel" / "definition" / "queryGroups.tmdl", "queryGroup x\n")
    assert _refuse(tree).code == pbir.ENTRY_UNKNOWN


def test_non_tmdl_file_in_a_model_object_folder_refuses(tree: tuple[Path, Path]) -> None:
    _write(tree[0] / "Demo.SemanticModel" / "definition" / "tables" / "Sales.json", {})
    assert _refuse(tree).code == pbir.ENTRY_UNKNOWN


def test_missing_model_definition_refuses(tree: tuple[Path, Path]) -> None:
    shutil.rmtree(tree[0] / "Demo.SemanticModel" / "definition")
    assert _refuse(tree).code == pbir.ENTRY_MISSING


def test_unknown_pages_metadata_key_refuses(tree: tuple[Path, Path]) -> None:
    pages = tree[1] / "definition" / "pages" / "pages.json"
    _patch_json(pages, lambda doc: doc.update({"futureField": 1}))
    assert _refuse(tree).code == pbir.ENTRY_UNKNOWN


def test_page_order_of_the_wrong_type_refuses(tree: tuple[Path, Path]) -> None:
    pages = tree[1] / "definition" / "pages" / "pages.json"
    _patch_json(pages, lambda doc: doc.update({"pageOrder": [{"name": "p_first"}, {"name": "p_second"}]}))
    assert _refuse(tree).code == pbir.PAGE_ORDER_MALFORMED


def test_duplicate_json_key_in_the_pbip_refuses(tree: tuple[Path, Path]) -> None:
    """`json.load` keeps the last duplicate silently - two different readers would disagree."""
    (tree[0] / "Demo.pbip").write_text(
        '{"version": "1.0", "artifacts": [{"report": {"path": "Demo.Report"}}], "version": "9.9"}',
        encoding="utf-8",
    )
    assert _refuse(tree).code == pbir.JSON_DUPLICATE_KEY


def test_duplicate_json_key_in_the_pbir_refuses(tree: tuple[Path, Path]) -> None:
    (tree[1] / "definition.pbir").write_text(
        '{"datasetReference": {"byPath": {"path": "../Demo.SemanticModel"}},'
        ' "datasetReference": {"byPath": {"path": "../Other.SemanticModel"}}}',
        encoding="utf-8",
    )
    assert _refuse(tree).code == pbir.JSON_DUPLICATE_KEY


def test_malformed_json_refuses(tree: tuple[Path, Path]) -> None:
    (tree[1] / "definition" / "report.json").write_text('{"themeCollection":', encoding="utf-8")
    assert _refuse(tree).code == pbir.JSON_MALFORMED


def test_non_object_json_root_refuses(tree: tuple[Path, Path]) -> None:
    (tree[1] / "definition" / "version.json").write_text("[]", encoding="utf-8")
    assert _refuse(tree).code == pbir.JSON_TYPE


def test_non_json_constant_refuses(tree: tuple[Path, Path]) -> None:
    """`NaN`/`Infinity` are Python extensions, not JSON - Power BI would not round-trip them."""
    (tree[1] / "definition" / "version.json").write_text('{"version": NaN}', encoding="utf-8")
    assert _refuse(tree).code == pbir.JSON_MALFORMED


def test_dangling_registered_resource_refuses(tree: tuple[Path, Path]) -> None:
    (tree[1] / "StaticResources" / "RegisteredResources" / "theme.json").unlink()
    assert _refuse(tree).code == pbir.RESOURCE_DANGLING


def test_registered_resource_escaping_the_package_refuses(tree: tuple[Path, Path]) -> None:
    report_json = tree[1] / "definition" / "report.json"
    _patch_json(
        report_json, lambda doc: doc["resourcePackages"][1]["items"][0].update({"path": "C:/themes/theme.json"})
    )
    assert _refuse(tree).code == pbir.RESOURCE_MALFORMED


def test_unregistered_resource_file_is_included_rather_than_refused(tree: tuple[Path, Path]) -> None:
    """A file with no registration cannot break the render, so its bytes are covered, not refused."""
    baseline = _establish(tree)
    _write(tree[1] / "StaticResources" / "RegisteredResources" / "spare.json", {"spare": True})
    revision = _establish(tree)
    assert "Demo.Report/StaticResources/RegisteredResources/spare.json" in dict(revision.files)
    assert revision.digest != baseline.digest


def test_unknown_static_resource_directory_refuses(tree: tuple[Path, Path]) -> None:
    _write(tree[1] / "StaticResources" / "FutureResources" / "x.json", {})
    assert _refuse(tree).code == pbir.ENTRY_UNKNOWN


# --- model binding -----------------------------------------------------------------------------


def test_byconnection_binding_refuses(tree: tuple[Path, Path]) -> None:
    pbir_file = tree[1] / "definition.pbir"
    _patch_json(pbir_file, lambda doc: doc.update({"datasetReference": {"byConnection": {"connectionString": "x"}}}))
    assert _refuse(tree).code == pbir.BINDING_REMOTE


def test_binding_carrying_both_bypath_and_byconnection_refuses(tree: tuple[Path, Path]) -> None:
    pbir_file = tree[1] / "definition.pbir"
    _patch_json(
        pbir_file,
        lambda doc: doc.update(
            {"datasetReference": {"byPath": {"path": "../Demo.SemanticModel"}, "byConnection": {"c": "x"}}}
        ),
    )
    assert _refuse(tree).code == pbir.BINDING_REMOTE


def test_empty_dataset_reference_refuses(tree: tuple[Path, Path]) -> None:
    _patch_json(tree[1] / "definition.pbir", lambda doc: doc.update({"datasetReference": {}}))
    assert _refuse(tree).code == pbir.BINDING_MALFORMED


def test_binding_to_a_missing_model_refuses(tree: tuple[Path, Path]) -> None:
    shutil.rmtree(tree[0] / "Demo.SemanticModel")
    assert _refuse(tree).code == pbir.MODEL_UNRESOLVED


def test_binding_to_the_report_itself_refuses(tree: tuple[Path, Path]) -> None:
    _patch_json(tree[1] / "definition.pbir", lambda doc: doc.update({"datasetReference": {"byPath": {"path": "."}}}))
    assert _refuse(tree).code == pbir.MODEL_NOT_A_MODEL


def test_binding_to_a_directory_that_is_not_a_semantic_model_refuses(tree: tuple[Path, Path]) -> None:
    (tree[0] / "NotAModel").mkdir()
    _patch_json(
        tree[1] / "definition.pbir", lambda doc: doc.update({"datasetReference": {"byPath": {"path": "../NotAModel"}}})
    )
    assert _refuse(tree).code == pbir.MODEL_NOT_A_MODEL


def test_binding_to_a_model_outside_the_fabric_root_refuses(tmp_path: Path) -> None:
    fabric, report = _build(tmp_path)
    outside = tmp_path / "shared" / "Demo.SemanticModel"
    shutil.copytree(fabric / "Demo.SemanticModel", outside)
    shutil.rmtree(fabric / "Demo.SemanticModel")
    _patch_json(
        report / "definition.pbir",
        lambda doc: doc.update({"datasetReference": {"byPath": {"path": "../../shared/Demo.SemanticModel"}}}),
    )
    refusal = _refuse((fabric, report))
    assert refusal.code == pbir.MODEL_NOT_CONTAINED
    assert refusal.evidence == (pbir.OPAQUE_MODEL,)


def test_backslash_binding_spelling_refuses(tree: tuple[Path, Path]) -> None:
    _patch_json(
        tree[1] / "definition.pbir",
        lambda doc: doc.update({"datasetReference": {"byPath": {"path": "..\\Demo.SemanticModel"}}}),
    )
    assert _refuse(tree).code == pbir.BINDING_MALFORMED


# --- pbip relation -----------------------------------------------------------------------------


def test_fabric_root_without_a_pbip_refuses(tree: tuple[Path, Path]) -> None:
    (tree[0] / "Demo.pbip").unlink()
    refusal = _refuse(tree)
    assert refusal.code == pbir.PBIP_ABSENT
    assert refusal.evidence == (pbir.OPAQUE_ROOT,)


def test_pbip_declaring_a_different_report_refuses(tree: tuple[Path, Path]) -> None:
    _patch_json(tree[0] / "Demo.pbip", lambda doc: doc.update({"artifacts": [{"report": {"path": "Other.Report"}}]}))
    assert _refuse(tree).code == pbir.PBIP_REPORT_UNRELATED


def test_two_pbips_declaring_the_same_report_refuse(tree: tuple[Path, Path]) -> None:
    _write(tree[0] / "Copy.pbip", {"version": "1.0", "artifacts": [{"report": {"path": "Demo.Report"}}]})
    refusal = _refuse(tree)
    assert refusal.code == pbir.PBIP_REPORT_AMBIGUOUS
    assert refusal.evidence == ("Copy.pbip", "Demo.pbip")


def test_a_directory_named_like_a_pbip_refuses(tree: tuple[Path, Path]) -> None:
    """A `.pbip` that is a directory is not a project file, and must not be read as one."""
    (tree[0] / "Fake.pbip").mkdir()
    assert _refuse(tree).code == pbir.ENTRY_TYPE


def test_pbip_with_an_unknown_artifact_kind_refuses(tree: tuple[Path, Path]) -> None:
    _patch_json(tree[0] / "Demo.pbip", lambda doc: doc["artifacts"].append({"dataset": {"path": "Demo.SemanticModel"}}))
    assert _refuse(tree).code == pbir.PBIP_MALFORMED


def test_pbip_with_an_absolute_report_path_refuses(tree: tuple[Path, Path]) -> None:
    _patch_json(
        tree[0] / "Demo.pbip", lambda doc: doc.update({"artifacts": [{"report": {"path": "C:/x/Demo.Report"}}]})
    )
    assert _refuse(tree).code == pbir.PBIP_MALFORMED


# --- caller-supplied targets --------------------------------------------------------------------


def test_report_outside_the_fabric_root_refuses(tmp_path: Path) -> None:
    fabric, report = _build(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    shutil.copytree(report, elsewhere / "Demo.Report")
    refusal = _refuse((fabric, elsewhere / "Demo.Report"))
    assert refusal.code == pbir.REPORT_NOT_CONTAINED
    assert refusal.evidence == (pbir.OPAQUE_REPORT,)


def test_a_target_that_is_not_a_report_directory_refuses(tree: tuple[Path, Path]) -> None:
    refusal = _refuse((tree[0], tree[0] / "Demo.SemanticModel"))
    assert refusal.code == pbir.REPORT_UNUSABLE


def test_a_missing_report_directory_refuses(tree: tuple[Path, Path]) -> None:
    assert _refuse((tree[0], tree[0] / "Absent.Report")).code == pbir.REPORT_UNUSABLE


def test_a_missing_fabric_root_refuses(tmp_path: Path) -> None:
    refusal = _refuse((tmp_path / "nope", tmp_path / "nope" / "Demo.Report"))
    assert refusal.code == pbir.ROOT_UNUSABLE


# --- alias / reparse identity --------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="case-insensitive path aliasing is a Windows behaviour")
def test_case_aliased_report_spelling_refuses(tree: tuple[Path, Path]) -> None:
    """A case alias opens the same directory but is not the canonical spelling.

    The #363 A1 audit measured Desktop preserving the caller's case in `currentFilePath`, so a
    census that silently canonicalised case would let two spellings claim one identity.
    """
    fabric, report = tree
    aliased = fabric / (report.name.replace(pbir.REPORT_SUFFIX, "").upper() + pbir.REPORT_SUFFIX)
    assert aliased.is_dir(), "the alias must open the same directory for this control to mean anything"
    refusal = _refuse((fabric, aliased))
    assert refusal.code == pbir.PATH_IDENTITY_MISMATCH


@pytest.mark.skipif(os.name != "nt", reason="case-insensitive path aliasing is a Windows behaviour")
def test_a_lowercased_report_suffix_is_not_accepted_as_a_report(tree: tuple[Path, Path]) -> None:
    """The `.Report` suffix is matched exactly; a case-folded spelling is a different name."""
    fabric, report = tree
    refusal = _refuse((fabric, fabric / report.name.replace(pbir.REPORT_SUFFIX, ".report")))
    assert refusal.code == pbir.REPORT_UNUSABLE


def _make_junction(link: Path, target: Path) -> bool:
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows reparse point")
def test_report_reached_through_a_junction_refuses(tmp_path: Path) -> None:
    fabric, report = _build(tmp_path)
    link = fabric / "Alias.Report"
    if not _make_junction(link, report):
        pytest.skip("mklink /J unavailable on this host")
    refusal = _refuse((fabric, link))
    assert refusal.code in {pbir.PATH_REPARSE, pbir.PATH_IDENTITY_MISMATCH}


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows reparse point")
def test_a_junction_inside_the_report_refuses(tmp_path: Path) -> None:
    fabric, report = _build(tmp_path)
    real = report / "definition" / "pages" / "p_first" / "visuals" / "another-0"
    link = report / "definition" / "pages" / "p_first" / "visuals" / "aliased"
    if not _make_junction(link, real):
        pytest.skip("mklink /J unavailable on this host")
    assert _refuse((fabric, report)).code == pbir.PATH_REPARSE


# --------------------------------------------------------------------------------------------
# Privacy: nothing shareable may carry an absolute path
# --------------------------------------------------------------------------------------------


def _shareable_text(result: object) -> str:
    return "\n".join([repr(result), json.dumps(asdict(result), default=str)])


def test_an_established_revision_leaks_no_absolute_path(tree: tuple[Path, Path]) -> None:
    text = _shareable_text(_establish(tree))
    assert str(tree[0]) not in text
    assert tree[0].drive.lower() not in text.lower() if tree[0].drive else True
    assert str(tree[0].parent.name) not in text


@pytest.mark.parametrize(
    "break_it",
    [
        pytest.param(
            lambda fabric, report: (report / "definition" / "pages" / "p_first" / "page.json").unlink(),
            id="missing-page",
        ),
        pytest.param(lambda fabric, report: (fabric / "Demo.pbip").unlink(), id="pbip-absent"),
        pytest.param(lambda fabric, report: shutil.rmtree(fabric / "Demo.SemanticModel"), id="model-unresolved"),
        pytest.param(lambda fabric, report: _write(report / "mobileState.json", {}), id="unknown-entry"),
    ],
)
def test_a_refusal_leaks_no_absolute_path(tmp_path: Path, break_it) -> None:
    fabric, report = _build(tmp_path)
    break_it(fabric, report)
    text = _shareable_text(_refuse((fabric, report)))
    assert str(fabric) not in text
    assert tmp_path.name not in text


# --------------------------------------------------------------------------------------------
# Corpus control - every committed report tree, against an exact expected table
# --------------------------------------------------------------------------------------------


def _committed_reports() -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "*definition.pbir"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return sorted(str(Path(rel).parent).replace(os.sep, "/") for rel in listed)


ESTABLISHING = tuple(
    f"examples/{slug}/fabric"
    for slug in (
        "airline-alliance-activity",
        "broadway-stage-to-screen",
        "eea-urban-adaptation",
        "electricity-per-capita",
        "fast-fashion-impact",
        "health-tracker",
        "interactive-resume",
        "price-of-prosperity",
        "quadruple-axis-charts",
        "sales-commission-model",
        "shipping-kpis",
        "spiraling-satellites",
        "superstore-sales-performance",
        "tale-of-100-entrepreneurs",
        "telecommunications-analytics",
        "wind-energy-utilization",
    )
) + ("fixtures/large-refresh/fabric",)

#: Gate fixtures that deliberately ship a partial tree with no `.pbip`. They are not capture targets:
#: Power BI Desktop opens the `.pbip`, so a root without one cannot be one. Listed explicitly so a
#: fixture gaining a `.pbip` has to be re-classified rather than silently changing verdict.
REFUSING = {
    "tests/fixtures/check-gates-dirty/pbip/Admin_Insights_Starter": pbir.PBIP_ABSENT,
    "tests/fixtures/check-unit-brownfield-partial-pbip/pbip/Admin_Insights_Starter": pbir.PBIP_ABSENT,
    "tests/fixtures/check-unit-brownfield-rearranged/PowerBI": pbir.PBIP_ABSENT,
    "tests/fixtures/check-unit-clean-integration/pbip/Book": pbir.PBIP_ABSENT,
    "tests/fixtures/shared-datasource/external-broken/workbooks/sales-wb/fabric": pbir.PBIP_ABSENT,
    "tests/fixtures/shared-datasource/external-resolves/workbooks/sales-wb/fabric": pbir.PBIP_ABSENT,
    "tests/fixtures/shared-datasource/model-local/fabric": pbir.PBIP_ABSENT,
    "tests/fixtures/shared-datasource/no-model/fabric": pbir.PBIP_ABSENT,
}


def test_every_committed_report_is_classified() -> None:
    """A newly committed report tree must be classified here, not adopted by silence."""
    reports = _committed_reports()
    roots = {str(Path(report).parent).replace(os.sep, "/") for report in reports}
    assert roots == set(ESTABLISHING) | set(REFUSING)
    assert len(reports) == 25


@pytest.mark.parametrize("root", ESTABLISHING)
def test_committed_production_trees_establish(root: str) -> None:
    fabric = REPO_ROOT / root
    report = next(path for path in sorted(fabric.iterdir()) if path.name.endswith(".Report"))
    revision = _establish((fabric, report))
    assert revision.page_order
    assert len(revision.pages) == len(revision.page_order)
    assert dict(revision.files)


@pytest.mark.parametrize("root", sorted(REFUSING))
def test_committed_gate_fixtures_refuse_for_their_named_reason(root: str) -> None:
    fabric = REPO_ROOT / root
    report = next(path for path in sorted(fabric.iterdir()) if path.name.endswith(".Report"))
    assert _refuse((fabric, report)).code == REFUSING[root]


def test_committed_examples_agree_with_the_measured_identity_semantics() -> None:
    """Page folder IS the page id; visual folder is NOT the visual id - both measured on the corpus."""
    folder_differs = 0
    pages = 0
    for root in ESTABLISHING:
        fabric = REPO_ROOT / root
        report = next(path for path in sorted(fabric.iterdir()) if path.name.endswith(".Report"))
        revision = _establish((fabric, report))
        for page in revision.pages:
            pages += 1
            assert page.folder == page.page_id
            folder_differs += sum(1 for visual in page.visuals if visual.folder != visual.document_id)
    assert pages == 37
    assert folder_differs == 133


# --- corpus-derived binding controls -------------------------------------------------------------


def _with_pbip(source: Path, destination: Path, relative_fabric: str = "") -> tuple[Path, Path]:
    """Copy a committed fixture tree and add the `.pbip` it omits, to reach the binding rules."""
    shutil.copytree(source, destination)
    fabric = destination / relative_fabric if relative_fabric else destination
    report = next(path for path in sorted(fabric.iterdir()) if path.name.endswith(".Report"))
    _write(fabric / "Sales.pbip", {"version": "1.0", "artifacts": [{"report": {"path": report.name}}]})
    return fabric, report


@pytest.mark.parametrize(
    ("fixture", "relative_fabric", "expected"),
    [
        ("tests/fixtures/shared-datasource/model-local/fabric", "", None),
        ("tests/fixtures/shared-datasource/no-model/fabric", "", pbir.BINDING_MALFORMED),
        (
            "tests/fixtures/shared-datasource/external-resolves",
            "workbooks/sales-wb/fabric",
            pbir.MODEL_NOT_CONTAINED,
        ),
        (
            "tests/fixtures/shared-datasource/external-broken",
            "workbooks/sales-wb/fabric",
            pbir.MODEL_UNRESOLVED,
        ),
    ],
)
def test_committed_shared_datasource_shapes_route_to_the_right_binding_verdict(
    tmp_path: Path, fixture: str, relative_fabric: str, expected: str | None
) -> None:
    """The shared-datasource fixtures are the repo's real binding corpus once given a `.pbip`.

    `external-resolves` is the load-bearing one: the model EXISTS and the binding resolves, and it is
    still refused, because it lives outside the fabric root the caller declared. A local capture
    cannot be established from a root that does not contain its own model.
    """
    target = _with_pbip(REPO_ROOT / fixture, tmp_path / "copy", relative_fabric)
    result = pbir.establish_revision(*target)
    if expected is None:
        assert isinstance(result, pbir.PbirRevision)
    else:
        assert isinstance(result, pbir.RevisionRefusal)
        assert result.code == expected
