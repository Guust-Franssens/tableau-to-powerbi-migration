"""Direct controls for the package-local iteration/comparison receipt producer (issue #363, slice A).

Every test drives the REAL production entry points - `capture_powerbi_pages.run_iteration` and
`iteration_receipt.finalize` - through the same injectable `CaptureRuntime` the standalone capture
mode uses, so nothing here exercises a parallel implementation. Each negative control asserts the
NAMED refusal code, never merely a non-zero exit: "it failed" and "it failed for the reason this
guard exists" are different claims, and only the second one is a regression test.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# ruff: noqa: E402  (the sys.path inserts above must precede these imports)
import capture_powerbi_pages as capture
import current_artifact_revision as rev
import iteration_receipt as receipt
from png_fixtures import valid_png

WORKBOOK_LUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OTHER_LUID = "ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee"
PAGE_MAIN = "page-main0001"
PAGE_TREND = "page-trend0002"


class ManualClock:
    """The same controllable clock the standalone capture tests use."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _runtime(payloads: dict[str, bytes] | None = None) -> capture.CaptureRuntime:
    """A capture runtime that writes one settled frame per page."""
    clock = ManualClock()
    frames = payloads or {}

    def screenshotter(page_id: str, _pid: str, frame: Path) -> bool:
        frame.write_bytes(frames.get(page_id, valid_png(96, 72)))
        return True

    return capture.CaptureRuntime(screenshotter=screenshotter, sleep=clock.sleep, clock=clock)


def _options(page_ids: frozenset[str] | None = None) -> capture.CaptureOptions:
    return capture.CaptureOptions(poll=0.0, stable_seconds=0.0, max_wait=10.0, page_ids=page_ids)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _oracle_view(directory: Path, name: str, luid: str, *, height: int = 240) -> dict[str, object]:
    """One oracle view record plus its render.

    ⚠️ ``height`` varies per view ON PURPOSE. Exclusivity keys on the render DIGEST (see
    `check_reference_readiness._render_key`), so two byte-identical fixture PNGs are one render
    claimed by two pages and every page fails closed - which silently made the foreign-workbook and
    grade controls vacuous until the fixture was fixed.
    """
    blob = valid_png(320, height)
    image = directory / "oracle" / "dashboard" / "images" / f"{name}.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(blob)
    return {
        "view_luid": f"0000000{len(name)}-0000-0000-0000-000000000000",
        "view_name": name,
        "workbook_luid": luid,
        "workbook_name": "Unit",
        "view_type": "dashboard",
        "image": {
            "status": "ok",
            "path": f"dashboard/images/{name}.png",
            "sha256": hashlib.sha256(blob).hexdigest(),
            "bytes": len(blob),
            "dimensions_px": {"w": 320, "h": height},
        },
    }


def build_package(root: Path, *, workbook_luid: str = WORKBOOK_LUID) -> Path:
    """A minimal but REAL phase-2 package: manifest, PBIR report, model, spec, oracle capture."""
    package = root / "packages" / "Unit"
    report = package / "fabric" / "Unit.Report"
    model = package / "fabric" / "Unit.SemanticModel"

    for page_id, display, visuals in (
        (PAGE_MAIN, "main", ("v-1", "v-2")),
        (PAGE_TREND, "trend", ("v-3",)),
    ):
        page_dir = report / "definition" / "pages" / page_id
        _write_json(page_dir / "page.json", {"name": page_id, "displayName": display})
        for visual_id in visuals:
            _write_json(page_dir / "visuals" / visual_id / "visual.json", {"name": visual_id, "visual": {}})
    _write_json(report / "definition" / "pages" / "pages.json", {"pageOrder": [PAGE_MAIN, PAGE_TREND]})
    _write_json(report / "definition.pbir", {"datasetReference": {"byPath": {"path": "../Unit.SemanticModel"}}})
    (model / "definition").mkdir(parents=True, exist_ok=True)
    (model / "definition" / "model.tmdl").write_text("model Model\n", encoding="utf-8")
    (package / "fabric" / "Unit.pbip").write_text("{}", encoding="utf-8")

    asset = package / "assets" / f"{workbook_luid}_Unit.twb"
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_text("<workbook/>", encoding="utf-8")
    _write_json(
        package / "source-provenance.json",
        {
            "inputs": [
                {
                    "input": {"sha256": rev.sha256_of_file(asset)},
                    "origin": {"workbook_luid": workbook_luid, "matched_by": "luid", "revision_match": "same"},
                }
            ]
        },
    )
    _write_json(
        package / "migration-spec.json",
        {"limitations_encountered": [{"item": "fld.x", "issue": "LOD", "severity": "high", "stage": "parse"}]},
    )
    _write_json(
        package / "oracle" / "oracle-manifest.json",
        {
            "views": [
                _oracle_view(package, "main", workbook_luid, height=240),
                _oracle_view(package, "trend", workbook_luid, height=200),
            ]
        },
    )
    _write_json(
        package / "package-manifest.json",
        {
            "unit": "Unit",
            "kind": "workbook",
            "artifacts": {
                "report": "fabric/Unit.Report",
                "model": "fabric/Unit.SemanticModel",
                "asset": f"assets/{workbook_luid}_Unit.twb",
            },
        },
    )
    return package


@pytest.fixture(name="package")
def package_fixture(tmp_path: Path) -> Path:
    return build_package(tmp_path)


def _request(package: Path, **overrides: object) -> capture.IterationRequest:
    fields = {"package": package, "pid": "1234", "session_id": "sess-1"}
    fields.update(overrides)
    return capture.IterationRequest(**fields)  # type: ignore[arg-type]


def _iterate(package: Path, *, options: capture.CaptureOptions | None = None, **overrides: object) -> dict:
    return capture.run_iteration(_request(package, **overrides), options or _options(), _runtime())


def _receipt_path(package: Path, name: str = "001") -> Path:
    return receipt.iterations_root(package) / name / receipt.RECEIPT_NAME


def _pass_judgement(package: Path, name: str = "001", *, findings: list[dict] | None = None) -> dict:
    """Fill only the judgement fields, exactly as a reviewer would."""
    path = _receipt_path(package, name)
    payload = json.loads(path.read_text(encoding="utf-8"))
    for row in payload["judgement"]["pages"]:
        row["whole_page_status"] = receipt.STATUS_PASS
        for section in ("visual_results", "numeric_results"):
            for item in row[section]:
                item["status"] = receipt.STATUS_PASS
    if findings is not None:
        payload["judgement"]["findings"] = findings
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def _finalized(package: Path, name: str = "001", *, findings: list[dict] | None = None) -> dict:
    _pass_judgement(package, name, findings=findings)
    return receipt.finalize(package, name)


def _refusal(package: Path, name: str = "001") -> str:
    with pytest.raises(receipt.ReceiptError) as error:
        receipt.finalize(package, name)
    return error.value.code


# --------------------------------------------------------------------------------------------------
# positive controls
# --------------------------------------------------------------------------------------------------


def test_a_clean_all_page_capture_writes_one_canonical_first_iteration(package: Path) -> None:
    """The whole point: every current page settled, retained, and described by one strict receipt."""
    payload = _iterate(package)

    assert payload["iteration"] == "001"
    assert payload["mode"] == receipt.MODE_SIGN_OFF
    assert payload["generated"]["scope"] == receipt.SCOPE_ALL_PAGES
    assert [row["page_id"] for row in payload["generated"]["pages"]] == [PAGE_MAIN, PAGE_TREND]
    assert [row["expected_visual_ids"] for row in payload["generated"]["pages"]] == [["v-1", "v-2"], ["v-3"]]
    assert all(row["powerbi"]["converged"] for row in payload["generated"]["pages"])
    assert all(row["powerbi"]["byte_count"] > 0 for row in payload["generated"]["pages"])
    assert payload["generated"]["previous"] is None
    assert payload["generated"]["data_evidence"]["status"] == receipt.DATA_STATUS_PENDING
    assert _receipt_path(package).is_file()
    for row in payload["generated"]["pages"]:
        assert (receipt.iterations_root(package) / "001" / row["powerbi"]["path"]).is_file()


def test_the_page_denominator_comes_from_the_pbir_and_not_from_the_captured_output(package: Path) -> None:
    """Capture output is never its own denominator: a stray PNG cannot add a page to the receipt.

    The mirror of that is the load-bearing half - a page that EXISTS in the report but was never
    captured cannot be silently absent, because the inventory is read first and every page in it is
    captured or the run refuses.
    """
    payload = _iterate(package)
    stray = receipt.iterations_root(package) / "001" / receipt.PAGES_DIRNAME / "page-invented.png"
    stray.write_bytes(valid_png(96, 72))

    assert {row["page_id"] for row in payload["generated"]["pages"]} == {PAGE_MAIN, PAGE_TREND}
    assert _refusal(package) == "EXTRA_FILE"


def test_a_first_sign_off_iteration_can_complete_with_no_prior_findings(package: Path) -> None:
    """One clean pass must be representable in ONE iteration - the audit's positive control."""
    _iterate(package, data_evidence=_data_record(package))
    sealed = _finalized(package)

    assert sealed["state"] == receipt.STATE_FINAL
    assert sealed["outcome"] == receipt.OUTCOME_COMPLETE
    assert sealed["judgement"]["findings"] == []
    assert sealed["judgement"]["completed_at"]


def test_a_second_iteration_links_to_the_first_and_records_before_and_after_hashes(package: Path) -> None:
    """Page evolution is the RETAINED bytes plus a verified before/after pair, not a prose diff."""
    _iterate(package)
    first = _finalized(
        package,
        findings=[
            {
                "id": "F-001",
                "page_id": PAGE_MAIN,
                "visual_id": "v-1",
                "kind": "visual",
                "severity": "medium",
                "status": receipt.FINDING_OPEN,
                "detail": "the legend order differs",
                "limitation_ref": None,
            }
        ],
    )
    _pass_judgement(package)  # a still_open finding does not stop the first iteration being sealed
    (
        package / "fabric" / "Unit.Report" / "definition" / "pages" / PAGE_MAIN / "visuals" / "v-1" / "visual.json"
    ).write_text(json.dumps({"name": "v-1", "visual": {"visualType": "columnChart"}}), encoding="utf-8")
    second = capture.run_iteration(_request(package), _options(), _runtime({PAGE_MAIN: valid_png(128, 96)}))

    assert second["iteration"] == "002"
    assert second["generated"]["previous"]["iteration"] == "001"
    assert second["generated"]["previous"]["receipt_sha256"] == rev.sha256_of_file(_receipt_path(package, "001"))
    assert second["generated"]["previous"]["report_revision"] == first["generated"]["artifact"]["report_revision"]
    changed = {row["page_id"]: row for row in second["generated"]["changes_from_previous"]}
    assert changed[PAGE_MAIN]["before_sha256"] != changed[PAGE_MAIN]["after_sha256"]
    assert changed[PAGE_TREND]["before_sha256"] == changed[PAGE_TREND]["after_sha256"]


def test_an_admitted_oracle_render_stays_layout_text_grade(package: Path) -> None:
    """Tableau evidence is re-derived through reference_evidence, which CAPS an oracle at its grade."""
    payload = _iterate(package)
    tableau = {row["page_id"]: row["tableau"] for row in payload["generated"]["pages"]}

    assert tableau[PAGE_MAIN]["path"] == "oracle/dashboard/images/main.png"
    assert tableau[PAGE_MAIN]["grade"] != "validation-grade"
    assert "layout/text only" in tableau[PAGE_MAIN]["grade"]
    assert tableau[PAGE_MAIN]["manifest_sha256"] == rev.sha256_of_file(package / "oracle" / "oracle-manifest.json")


# --------------------------------------------------------------------------------------------------
# allocation and chain
# --------------------------------------------------------------------------------------------------


def test_an_already_taken_iteration_number_is_refused_rather_than_reused(package: Path) -> None:
    """Two producers racing for one number: the loser is refused, never allowed to overwrite."""
    original = Path.mkdir

    def racing_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        original(self, *args, **kwargs)
        if self.name == "001":
            raise FileExistsError(str(self))

    with pytest.raises(receipt.ReceiptError) as error:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(Path, "mkdir", racing_mkdir)
            _iterate(package)

    assert error.value.code == "ITERATION_NUMBER_TAKEN"


def test_a_gap_in_the_chain_refuses_before_anything_is_allocated(package: Path) -> None:
    """A missing 001 is not "start at 003" - the history it held is gone and cannot be reasoned about."""
    _iterate(package)
    _finalized(package)
    shutil.move(str(receipt.iterations_root(package) / "001"), str(receipt.iterations_root(package) / "003"))

    with pytest.raises(receipt.ReceiptError) as error:
        _iterate(package)
    assert error.value.code == "ITERATION_GAP"
    assert not (receipt.iterations_root(package) / "002").exists()


def test_a_noncanonical_iteration_name_is_refused(package: Path) -> None:
    """`001-retry` is not an iteration name; accepting it makes contiguity unprovable."""
    _iterate(package)
    _finalized(package)
    shutil.move(str(receipt.iterations_root(package) / "001"), str(receipt.iterations_root(package) / "001-retry"))

    with pytest.raises(receipt.ReceiptError) as error:
        _iterate(package)
    assert error.value.code == "NONCANONICAL_ITERATION"


def test_a_duplicate_receipt_file_beside_the_real_one_is_refused(package: Path) -> None:
    """Two receipts in one directory means two answers; the gate must not pick one."""
    _iterate(package)
    _finalized(package)
    directory = receipt.iterations_root(package) / "001"
    shutil.copy2(directory / receipt.RECEIPT_NAME, directory / "iteration (1).json")

    with pytest.raises(receipt.ReceiptError) as error:
        _iterate(package)
    assert error.value.code == "EXTRA_FILE"


def test_a_pending_predecessor_blocks_a_new_iteration(package: Path) -> None:
    """An unfinished iteration is not history yet - stacking on it loses the reviewer's work."""
    _iterate(package)

    with pytest.raises(receipt.ReceiptError) as error:
        _iterate(package)
    assert error.value.code == "PREVIOUS_NOT_FINAL"


def test_a_capture_failure_removes_the_directory_it_allocated(package: Path) -> None:
    """A numbered directory with no receipt would make the chain permanently unreadable."""
    clock = ManualClock()
    runtime = capture.CaptureRuntime(screenshotter=lambda *_args: False, sleep=clock.sleep, clock=clock)

    with pytest.raises(receipt.ReceiptError) as error:
        capture.run_iteration(_request(package), _options(), runtime)

    assert error.value.code == "CAPTURE_FAILED"
    assert not (receipt.iterations_root(package) / "001").exists()


# --------------------------------------------------------------------------------------------------
# scope
# --------------------------------------------------------------------------------------------------


def test_a_subset_capture_is_triage_and_can_never_be_labelled_sign_off(package: Path) -> None:
    """The pages nobody looked at are exactly the ones a partial sign-off would certify silently."""
    payload = capture.run_iteration(_request(package), _options(frozenset({PAGE_MAIN})), _runtime())

    assert payload["mode"] == receipt.MODE_TRIAGE
    assert payload["generated"]["scope"] == receipt.SCOPE_SUBSET
    assert [row["page_id"] for row in payload["generated"]["pages"]] == [PAGE_MAIN]

    with pytest.raises(receipt.ReceiptError) as error:
        capture.run_iteration(
            _request(package, mode=receipt.MODE_SIGN_OFF), _options(frozenset({PAGE_MAIN})), _runtime()
        )
    assert error.value.code == "SUBSET_CANNOT_SIGN_OFF"


def test_a_triage_iteration_can_never_finalize_as_complete(package: Path) -> None:
    """`outcome` is generated from scope and status, so triage cannot be read as a sign-off."""
    capture.run_iteration(_request(package), _options(frozenset({PAGE_MAIN})), _runtime())
    sealed = _finalized(package)

    assert sealed["outcome"] == receipt.OUTCOME_INCOMPLETE


def test_an_unknown_page_id_is_refused_before_any_allocation(package: Path) -> None:
    """A typo must not produce an empty, plausible-looking iteration."""
    with pytest.raises(receipt.ReceiptError) as error:
        capture.run_iteration(_request(package), _options(frozenset({"page-typo"})), _runtime())

    assert error.value.code == "UNKNOWN_PAGE_ID"
    assert not receipt.iterations_root(package).exists()


# --------------------------------------------------------------------------------------------------
# staleness: the artifact moved after capture
# --------------------------------------------------------------------------------------------------


def test_a_report_edit_after_capture_makes_the_iteration_stale(package: Path) -> None:
    """A screenshot of a report that no longer exists is not evidence about the report that does."""
    _iterate(package)
    _pass_judgement(package)
    theme = package / "fabric" / "Unit.Report" / "StaticResources" / "RegisteredResources" / "theme.json"
    theme.parent.mkdir(parents=True, exist_ok=True)
    theme.write_text("{}", encoding="utf-8")

    assert _refusal(package) == "REPORT_CHANGED"


def test_a_model_edit_after_capture_makes_the_iteration_stale(package: Path) -> None:
    """The model is the other half of what was rendered, so it is revision-bound too."""
    _iterate(package)
    _pass_judgement(package)
    (package / "fabric" / "Unit.SemanticModel" / "definition" / "model.tmdl").write_text(
        "model Model\n\n// edited\n", encoding="utf-8"
    )

    assert _refusal(package) == "MODEL_CHANGED"


def test_a_cache_change_after_capture_makes_the_iteration_stale(package: Path) -> None:
    """`.pbi/cache.abf` is DATA, hashed separately - and a refresh invalidates the capture."""
    cache = package / "fabric" / "Unit.SemanticModel" / ".pbi" / "cache.abf"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(b"CACHE-A")
    _iterate(package)
    _pass_judgement(package)
    cache.write_bytes(b"CACHE-B-REFRESHED")

    assert _refusal(package) == "CACHE_CHANGED"


def test_the_desktop_local_settings_folder_does_not_churn_the_report_revision(package: Path) -> None:
    """Opening the report in Desktop must not, by itself, invalidate an iteration."""
    _iterate(package)
    _pass_judgement(package)
    settings = package / "fabric" / "Unit.Report" / ".pbi" / "localSettings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text('{"version":"1"}', encoding="utf-8")

    assert receipt.finalize(package)["state"] == receipt.STATE_FINAL


def test_a_new_page_after_capture_is_an_inventory_mismatch(package: Path) -> None:
    """A sign-off must cover EVERY current page, so a page added afterwards invalidates it."""
    _iterate(package)
    _pass_judgement(package)
    report = package / "fabric" / "Unit.Report"
    _write_json(
        report / "definition" / "pages" / "page-new0003" / "page.json",
        {"name": "page-new0003", "displayName": "new"},
    )
    _write_json(report / "definition" / "pages" / "pages.json", {"pageOrder": [PAGE_MAIN, PAGE_TREND, "page-new0003"]})

    assert _refusal(package) == "INVENTORY_CHANGED"


def test_a_new_visual_after_capture_is_an_inventory_mismatch(package: Path) -> None:
    """Per-visual dispositions are only complete against the visual set that currently exists."""
    _iterate(package)
    _pass_judgement(package)
    _write_json(
        package / "fabric" / "Unit.Report" / "definition" / "pages" / PAGE_TREND / "visuals" / "v-9" / "visual.json",
        {"name": "v-9", "visual": {}},
    )

    assert _refusal(package) == "INVENTORY_CHANGED"


# --------------------------------------------------------------------------------------------------
# the retained screenshots
# --------------------------------------------------------------------------------------------------


def test_a_deleted_screenshot_is_refused(package: Path) -> None:
    payload = _iterate(package)
    _pass_judgement(package)
    (receipt.iterations_root(package) / "001" / payload["generated"]["pages"][0]["powerbi"]["path"]).unlink()

    assert _refusal(package) == "SCREENSHOT_MISSING"


def test_a_zero_byte_screenshot_is_refused(package: Path) -> None:
    payload = _iterate(package)
    _pass_judgement(package)
    (receipt.iterations_root(package) / "001" / payload["generated"]["pages"][0]["powerbi"]["path"]).write_bytes(b"")

    assert _refusal(package) == "SCREENSHOT_EMPTY"


def test_a_swapped_screenshot_is_refused_even_at_the_same_size(package: Path) -> None:
    """A same-length swap defeats a size check, which is why the hash is the check."""
    payload = _iterate(package)
    _pass_judgement(package)
    image = receipt.iterations_root(package) / "001" / payload["generated"]["pages"][0]["powerbi"]["path"]
    blob = bytearray(image.read_bytes())
    blob[-1] ^= 0xFF
    image.write_bytes(bytes(blob))

    assert _refusal(package) == "SCREENSHOT_CHANGED"


def test_a_capture_that_produced_no_bytes_is_refused_at_production_time(package: Path) -> None:
    """A zero-byte frame must never become an iteration's evidence in the first place."""
    clock = ManualClock()

    def empty(_page_id: str, _pid: str, frame: Path) -> bool:
        frame.write_bytes(b"")
        return True

    with pytest.raises(receipt.ReceiptError) as error:
        capture.run_iteration(_request(package), _options(), capture.CaptureRuntime(empty, clock.sleep, clock))
    assert error.value.code == "SCREENSHOT_EMPTY"


# --------------------------------------------------------------------------------------------------
# Tableau evidence
# --------------------------------------------------------------------------------------------------


def test_a_render_belonging_to_another_workbook_is_not_this_unit_s_evidence(tmp_path: Path) -> None:
    """Attribution is delegated to Evidence.attribution; a foreign LUID yields a REASON, not a match."""
    package = build_package(tmp_path)
    manifest = package / "oracle" / "oracle-manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    for view in payload["views"]:
        view["workbook_luid"] = OTHER_LUID
    _write_json(manifest, payload)

    generated = _iterate(package)["generated"]

    assert all(row["tableau"] is None for row in generated["pages"])
    assert all(row["tableau_reason"] for row in generated["pages"])


def test_a_stale_tableau_render_swapped_after_capture_is_refused(package: Path) -> None:
    """The manifest hash and the render hash are both pinned, so a re-capture invalidates sign-off."""
    _iterate(package)
    _pass_judgement(package)
    image = package / "oracle" / "dashboard" / "images" / "main.png"
    image.write_bytes(valid_png(320, 241))

    assert _refusal(package) in {"TABLEAU_EVIDENCE_CHANGED", "PACKAGE_CHANGED"}


def test_one_render_claimed_by_two_pages_certifies_neither(tmp_path: Path) -> None:
    """Exclusivity: two pages sharing one Tableau name cannot both own the same bytes."""
    package = build_package(tmp_path)
    page_dir = package / "fabric" / "Unit.Report" / "definition" / "pages" / PAGE_TREND
    _write_json(page_dir / "page.json", {"name": PAGE_TREND, "displayName": "main"})

    generated = _iterate(package)["generated"]

    assert all(row["tableau"] is None for row in generated["pages"])
    assert all("more than one page" in row["tableau_reason"] for row in generated["pages"])


# --------------------------------------------------------------------------------------------------
# data evidence
# --------------------------------------------------------------------------------------------------


def _data_record(package: Path, **overrides: object) -> Path:
    target = receipt.resolve_package(package)
    facts = receipt.artifact_facts(target)
    payload = {
        "tool": "probe_desktop_query",
        "verdict": receipt.DATA_VERDICT_OK,
        "mode": receipt.DATA_MODE_LIVE,
        "canaries": [{"table": "Orders", "row_count": 9994}],
        "model_revision": facts["model_revision"],
        "cache_sha256": None,
    }
    payload.update(overrides)
    path = package.parent / f"data-evidence-{len(list(package.parent.glob('data-evidence-*.json')))}.json"
    _write_json(path, payload)
    return path


def test_data_evidence_is_pending_by_default_and_names_the_blocked_seam(package: Path) -> None:
    """No invented success shape: the producer states exactly which seam blocks a structured result."""
    data = _iterate(package)["generated"]["data_evidence"]

    assert data["status"] == receipt.DATA_STATUS_PENDING
    assert data["verdict"] is None
    assert "probe_desktop_query" in data["pending_reason"]
    assert "refresh_pbip_model" in data["pending_reason"]


def test_a_cache_that_merely_exists_is_never_data_ok(package: Path) -> None:
    """Existence is not proof - `check_cache_freshness` says so, and this must not disagree."""
    cache = package / "fabric" / "Unit.SemanticModel" / ".pbi" / "cache.abf"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(b"CACHE")

    generated = _iterate(package)["generated"]

    assert generated["artifact"]["cache_sha256"]
    assert generated["data_evidence"]["status"] == receipt.DATA_STATUS_PENDING


def test_a_table_ok_verdict_cannot_be_ingested_as_data_evidence(package: Path) -> None:
    """`TABLE_OK` is a single arbitrary table, explicitly not a model-level DATA_OK."""
    record = _data_record(package, verdict="TABLE_OK")

    with pytest.raises(receipt.ReceiptError) as error:
        _iterate(package, data_evidence=record)
    assert error.value.code == "SCHEMA"


def test_a_canary_returning_no_rows_is_refused(package: Path) -> None:
    record = _data_record(package, canaries=[{"table": "Orders", "row_count": 0}])

    with pytest.raises(receipt.ReceiptError) as error:
        _iterate(package, data_evidence=record)
    assert error.value.code == "DATA_EVIDENCE_EMPTY_CANARY"


def test_data_evidence_with_no_canary_at_all_is_refused(package: Path) -> None:
    record = _data_record(package, canaries=[])

    with pytest.raises(receipt.ReceiptError) as error:
        _iterate(package, data_evidence=record)
    assert error.value.code == "DATA_EVIDENCE_NO_CANARIES"


def test_data_evidence_from_a_different_model_revision_is_refused(package: Path) -> None:
    record = _data_record(package, model_revision="sha256:0000")

    with pytest.raises(receipt.ReceiptError) as error:
        _iterate(package, data_evidence=record)
    assert error.value.code == "DATA_EVIDENCE_STALE_MODEL"


def test_a_persisted_claim_must_pin_the_cache_the_model_now_holds(package: Path) -> None:
    cache = package / "fabric" / "Unit.SemanticModel" / ".pbi" / "cache.abf"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(b"CACHE")
    record = _data_record(
        package,
        verdict=receipt.DATA_VERDICT_PERSISTED,
        mode=receipt.DATA_MODE_PERSISTED,
        cache_sha256="0" * 64,
    )

    with pytest.raises(receipt.ReceiptError) as error:
        _iterate(package, data_evidence=record)
    assert error.value.code == "DATA_EVIDENCE_CACHE_MISMATCH"


def test_accepted_data_evidence_goes_stale_when_the_model_moves(package: Path) -> None:
    _iterate(package, data_evidence=_data_record(package))
    _pass_judgement(package)
    (package / "fabric" / "Unit.SemanticModel" / "definition" / "model.tmdl").write_text("model M2\n", encoding="utf-8")

    assert _refusal(package) in {"MODEL_CHANGED", "DATA_EVIDENCE_STALE_MODEL"}


# --------------------------------------------------------------------------------------------------
# judgement, findings and the lifecycle
# --------------------------------------------------------------------------------------------------


def test_the_producer_never_turns_pending_into_pass(package: Path) -> None:
    """Every generated judgement slot starts pending, and finalization refuses one that stayed so."""
    payload = _iterate(package)
    statuses = {
        item["status"]
        for row in payload["judgement"]["pages"]
        for item in row["visual_results"] + row["numeric_results"]
    }

    assert statuses == {receipt.STATUS_PENDING}
    assert all(row["whole_page_status"] == receipt.STATUS_PENDING for row in payload["judgement"]["pages"])
    assert _refusal(package) == "PENDING_JUDGEMENT"


def test_an_unverified_visual_is_not_a_completing_status(package: Path) -> None:
    """`unverified` is never `pass` - it seals honestly, but it cannot complete a sign-off."""
    _iterate(package, data_evidence=_data_record(package))
    _pass_judgement(package)
    path = _receipt_path(package)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["judgement"]["pages"][0]["visual_results"][0]["status"] = receipt.STATUS_UNVERIFIED
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert receipt.finalize(package)["outcome"] == receipt.OUTCOME_INCOMPLETE


def test_a_prior_finding_may_not_silently_disappear(package: Path) -> None:
    """Omission is failure: a finding that vanishes reads exactly like a finding that was fixed."""
    _iterate(package)
    _finalized(
        package,
        findings=[
            {
                "id": "F-001",
                "page_id": PAGE_MAIN,
                "visual_id": "v-1",
                "kind": "visual",
                "severity": "high",
                "status": receipt.FINDING_OPEN,
                "detail": "axis title missing",
                "limitation_ref": None,
            }
        ],
    )
    _iterate(package)
    _pass_judgement(package, "002", findings=[])

    with pytest.raises(receipt.ReceiptError) as error:
        receipt.finalize(package, "002")
    assert error.value.code == "FINDING_DISAPPEARED"


def test_a_prior_finding_that_reappears_as_resolved_is_accepted(package: Path) -> None:
    """The lifecycle is explicit; carrying the id forward with a verdict is what closes it."""
    _iterate(package)
    finding = {
        "id": "F-001",
        "page_id": PAGE_MAIN,
        "visual_id": "v-1",
        "kind": "visual",
        "severity": "high",
        "status": receipt.FINDING_OPEN,
        "detail": "axis title missing",
        "limitation_ref": None,
    }
    _finalized(package, findings=[finding])
    _iterate(package, data_evidence=_data_record(package))
    resolved = dict(finding, status=receipt.FINDING_RESOLVED)
    sealed = _finalized(package, "002", findings=[resolved])

    assert sealed["outcome"] == receipt.OUTCOME_COMPLETE
    assert sealed["judgement"]["findings"][0]["status"] == receipt.FINDING_RESOLVED


def test_an_accepted_limitation_must_bind_to_a_current_spec_entry(package: Path) -> None:
    """`accepted_limitation` without a resolving limitation is an unbacked excuse."""
    _iterate(package)
    unbound = {
        "id": "F-001",
        "page_id": PAGE_MAIN,
        "visual_id": "v-1",
        "kind": "visual",
        "severity": "medium",
        "status": receipt.FINDING_ACCEPTED,
        "detail": "table calc not reproducible",
        "limitation_ref": None,
    }
    _pass_judgement(package, findings=[unbound])

    assert _refusal(package) == "ACCEPTED_LIMITATION_UNBOUND"


def test_an_accepted_limitation_whose_entry_text_changed_is_refused(package: Path) -> None:
    """Binding by index alone would let an entry be rewritten under a settled acceptance."""
    _iterate(package)
    spec = json.loads((package / "migration-spec.json").read_text(encoding="utf-8"))
    bound = {
        "id": "F-001",
        "page_id": PAGE_MAIN,
        "visual_id": "v-1",
        "kind": "visual",
        "severity": "medium",
        "status": receipt.FINDING_ACCEPTED,
        "detail": "table calc not reproducible",
        "limitation_ref": {
            "pointer": "/limitations_encountered/0",
            "sha256": receipt.limitation_entry_sha256(spec["limitations_encountered"][0]),
        },
    }
    _pass_judgement(package, findings=[bound])
    assert receipt.finalize(package)["state"] == receipt.STATE_FINAL

    _iterate(package)
    spec["limitations_encountered"][0]["issue"] = "rewritten after the fact"
    _write_json(package / "migration-spec.json", spec)
    _pass_judgement(package, "002", findings=[bound])

    with pytest.raises(receipt.ReceiptError) as error:
        receipt.finalize(package, "002")
    assert error.value.code == "ACCEPTED_LIMITATION_UNBOUND"


def test_a_finding_id_referenced_but_never_declared_is_refused(package: Path) -> None:
    _iterate(package)
    _pass_judgement(package)
    path = _receipt_path(package)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["judgement"]["pages"][0]["visual_results"][0]["finding_ids"] = ["F-404"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert _refusal(package) == "UNKNOWN_FINDING_ID"


def test_a_judgement_row_for_a_visual_that_does_not_exist_is_refused(package: Path) -> None:
    """The reviewer fills judgement fields; they may not invent the denominator."""
    _iterate(package)
    _pass_judgement(package)
    path = _receipt_path(package)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["judgement"]["pages"][0]["visual_results"].append(
        {"visual_id": "v-invented", "status": receipt.STATUS_PASS, "finding_ids": []}
    )
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert _refusal(package) == "JUDGEMENT_VISUAL_SET"


# --------------------------------------------------------------------------------------------------
# the predecessor link
# --------------------------------------------------------------------------------------------------


def test_an_altered_prior_receipt_breaks_the_pinned_hash(package: Path) -> None:
    """Rewriting settled history is exactly what the pinned predecessor hash exists to expose."""
    _iterate(package)
    _finalized(package)
    _iterate(package)
    _pass_judgement(package, "002")
    first = _receipt_path(package, "001")
    payload = json.loads(first.read_text(encoding="utf-8"))
    payload["judgement"]["pages"][0]["whole_page_status"] = receipt.STATUS_MISMATCH
    first.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    with pytest.raises(receipt.ReceiptError) as error:
        receipt.finalize(package, "002")
    assert error.value.code == "PREVIOUS_RECEIPT_MISMATCH"


def test_a_hand_written_predecessor_hash_is_refused(package: Path) -> None:
    _iterate(package)
    _finalized(package)
    _iterate(package)
    _pass_judgement(package, "002")
    path = _receipt_path(package, "002")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["generated"]["previous"]["receipt_sha256"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(receipt.ReceiptError) as error:
        receipt.finalize(package, "002")
    assert error.value.code == "PREVIOUS_RECEIPT_MISMATCH"


def test_a_hand_edited_before_after_pair_is_refused(package: Path) -> None:
    """`changes_from_previous` is generated, so "this page did not change" cannot be asserted."""
    _iterate(package)
    _finalized(package)
    _iterate(package)
    _pass_judgement(package, "002")
    path = _receipt_path(package, "002")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["generated"]["changes_from_previous"][0]["before_sha256"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(receipt.ReceiptError) as error:
        receipt.finalize(package, "002")
    assert error.value.code == "CHANGES_MISDECLARED"


# --------------------------------------------------------------------------------------------------
# strict reading
# --------------------------------------------------------------------------------------------------


def test_a_duplicate_json_key_is_refused_rather_than_last_one_wins(package: Path) -> None:
    """`json.loads` keeps the last value, so a document with two answers would silently have one."""
    _iterate(package)
    path = _receipt_path(package)
    path.write_text(path.read_text(encoding="utf-8").replace('"mode":', '"mode": "triage", "mode":', 1), "utf-8")

    with pytest.raises(receipt.ReceiptError) as error:
        receipt.read_chain(package)
    assert error.value.code == "DUPLICATE_JSON_KEY"


def test_an_unknown_field_is_refused(package: Path) -> None:
    """A closed schema: an unrecognised key is a document this producer did not write."""
    _iterate(package)
    path = _receipt_path(package)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["judgement"]["pages"][0]["overall_ok"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(receipt.ReceiptError) as error:
        receipt.read_chain(package)
    assert error.value.code == "UNKNOWN_FIELD"


def test_a_status_outside_the_vocabulary_is_refused(package: Path) -> None:
    _iterate(package)
    path = _receipt_path(package)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["judgement"]["pages"][0]["whole_page_status"] = "probably fine"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(receipt.ReceiptError) as error:
        receipt.read_chain(package)
    assert error.value.code == "SCHEMA"


# --------------------------------------------------------------------------------------------------
# paths and privacy
# --------------------------------------------------------------------------------------------------


def test_a_traversal_screenshot_path_is_refused(package: Path) -> None:
    """A receipt cannot be made to hash - or reach - bytes outside its own iteration."""
    _iterate(package)
    _pass_judgement(package)
    path = _receipt_path(package)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["generated"]["pages"][0]["powerbi"]["path"] = "../../../fabric/Unit.pbip"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert _refusal(package) == "UNSAFE_PATH"


def test_a_package_manifest_pointing_outside_the_package_is_refused(tmp_path: Path) -> None:
    """One canonical report, resolved from the caller-supplied package, with no ancestor search."""
    package = build_package(tmp_path)
    manifest = package / "package-manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["artifacts"]["report"] = "../../elsewhere/Other.Report"
    _write_json(manifest, payload)

    with pytest.raises(receipt.ReceiptError) as error:
        receipt.resolve_package(package)
    assert error.value.code == "UNSAFE_PATH"


@pytest.mark.skipif(sys.platform != "win32", reason="reparse points need Windows or admin symlinks")
def test_a_reparse_point_inside_the_iterations_tree_is_refused(package: Path, tmp_path: Path) -> None:
    """A junction can source "evidence" from a tree the package does not own."""
    _iterate(package)
    _finalized(package)
    outside = tmp_path / "outside"
    outside.mkdir()
    import subprocess  # noqa: PLC0415  (Windows-only, and only for this one control)

    linked = receipt.iterations_root(package) / "002"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(linked), str(outside)], capture_output=True, check=False)
    if result.returncode != 0:  # pragma: no cover - depends on host policy
        pytest.skip("this host does not permit creating a junction")

    with pytest.raises(receipt.ReceiptError) as error:
        receipt.read_chain(package)
    assert error.value.code == "REPARSE_POINT"


def test_the_receipt_discloses_no_host_path_user_or_server(package: Path) -> None:
    """The shareable half: a receipt is committed and reviewed, so it must carry no location."""
    _iterate(package, session_id="sess-42")
    text = _receipt_path(package).read_text(encoding="utf-8")

    assert str(package) not in text
    assert "Users" not in text
    assert "://" not in text
    assert ":\\" not in text and ":/" not in text.replace("sha256:", "")


def test_the_open_desktop_file_path_never_reaches_the_receipt(package: Path) -> None:
    """PID/currentFilePath equality is producer-time evidence: keep the boolean, drop the path."""
    pbip = package / "fabric" / "Unit.pbip"
    _iterate(package, desktop_file_path=str(pbip))
    text = _receipt_path(package).read_text(encoding="utf-8")
    payload = json.loads(text)

    assert payload["generated"]["review"]["desktop_binding_checked"] is True
    assert payload["generated"]["review"]["desktop_binding_matches"] is True
    assert str(pbip) not in text
    assert "Unit.pbip" not in text


def test_a_desktop_instance_showing_another_file_is_refused(package: Path, tmp_path: Path) -> None:
    """Screenshotting a different open file is the confusion this producer must not record."""
    other = tmp_path / "Other.pbip"
    other.write_text("{}", encoding="utf-8")

    with pytest.raises(receipt.ReceiptError) as error:
        _iterate(package, desktop_file_path=str(other))
    assert error.value.code == "DESKTOP_BINDING_MISMATCH"


def test_a_reviewer_cannot_paste_raw_tool_output_into_the_receipt(package: Path) -> None:
    """Multi-line free text is how a traceback or a bridge dump - and a host path - gets in."""
    _iterate(package)
    payload = json.loads(_receipt_path(package).read_text(encoding="utf-8"))
    payload["judgement"]["findings"] = [
        {
            "id": "F-001",
            "page_id": PAGE_MAIN,
            "visual_id": None,
            "kind": "other",
            "severity": "low",
            "status": receipt.FINDING_OPEN,
            "detail": 'Traceback (most recent call last):\n  File "x.py", line 1',
            "limitation_ref": None,
        }
    ]

    with pytest.raises(receipt.ReceiptError) as error:
        receipt.assert_shareable(payload)
    assert error.value.code == "PRIVACY"


def test_a_host_path_in_a_reviewer_field_is_refused(package: Path) -> None:
    """The guard runs over the WHOLE document, so it covers the reviewer's half too.

    ⚠️ The offending string is ASSEMBLED rather than written out: a literal profile path in a
    committed file is exactly what this repo's own commit gate (`set_data_folder.py --check`)
    refuses, and a test that has to be exempted from a gate to prove the gate works is not evidence.
    """
    leak = "compare against C:" + chr(92) + "Users" + chr(92) + "someone" + chr(92) + "main.png"
    _iterate(package)
    payload = json.loads(_receipt_path(package).read_text(encoding="utf-8"))
    payload["judgement"]["findings"] = [
        {
            "id": "F-001",
            "page_id": PAGE_MAIN,
            "visual_id": None,
            "kind": "other",
            "severity": "low",
            "status": receipt.FINDING_OPEN,
            "detail": leak,
            "limitation_ref": None,
        }
    ]

    with pytest.raises(receipt.ReceiptError) as error:
        receipt.assert_shareable(payload)
    assert error.value.code == "PRIVACY"


def test_a_tableau_server_url_in_a_reviewer_field_is_refused(package: Path) -> None:
    """A view URL names the server, the site and the project - none of which may be shared."""
    _iterate(package)
    payload = json.loads(_receipt_path(package).read_text(encoding="utf-8"))
    payload["judgement"]["findings"] = [
        {
            "id": "F-001",
            "page_id": PAGE_MAIN,
            "visual_id": None,
            "kind": "other",
            "severity": "low",
            "status": receipt.FINDING_OPEN,
            "detail": "see https://tableau.internal.example/#/site/finance/views/Unit/main",
            "limitation_ref": None,
        }
    ]

    with pytest.raises(receipt.ReceiptError) as error:
        receipt.assert_shareable(payload)
    assert error.value.code == "PRIVACY"


# --------------------------------------------------------------------------------------------------
# the standalone capture mode is untouched
# --------------------------------------------------------------------------------------------------


def test_the_existing_positional_capture_mode_still_parses_unchanged() -> None:
    """The subcommands are additive: an existing invocation must be byte-for-byte compatible."""
    args = capture.parse_args(["Book.Report", "out", "--pid", "1234", "--pages", "ReportSectionMap"])

    assert args.command is None
    assert args.report == Path("Book.Report")
    assert args.outdir == Path("out")
    assert args.pid == "1234"
    assert args.pages == frozenset({"ReportSectionMap"})


def test_the_positional_mode_still_writes_bare_pngs_and_no_receipt(tmp_path: Path) -> None:
    """`capture_report` remains evidence-free; nothing about iterations leaks into it."""
    report = tmp_path / "Book.Report"
    page = report / "definition" / "pages" / "ReportSectionMap"
    page.mkdir(parents=True)
    _write_json(page / "page.json", {"name": "ReportSectionMap", "displayName": "Map"})

    code = capture.capture_report(report, tmp_path / "out", "1234", _options(), _runtime())

    assert code == 0
    assert (tmp_path / "out" / "Map.png").is_file()
    assert not (tmp_path / "out" / receipt.RECEIPT_NAME).exists()


def test_the_iterate_subcommand_is_reachable_from_the_cli(package: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The production CLI path, not just the internal API, produces the iteration."""
    code = capture.cmd_iterate(
        capture.parse_args(
            ["iterate", "--package", str(package), "--pid", "1234", "--poll", "0", "--stable-seconds", "0"]
        ),
        _runtime(),
    )

    assert code == capture.EXIT_OK
    assert "ITERATION 001 (sign_off, all_pages)" in capsys.readouterr().out
    assert _receipt_path(package).is_file()


def test_a_cli_refusal_exits_three_and_names_the_code(package: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Tests assert the named refusal; so does the operator-facing output."""
    _iterate(package)
    code = capture.cmd_finalize(capture.parse_args(["finalize", "--package", str(package)]))

    assert code == capture.EXIT_REFUSED
    assert "REFUSED: PENDING_JUDGEMENT" in capsys.readouterr().out


def test_finalizing_twice_is_refused(package: Path) -> None:
    """A sealed iteration is immutable; re-sealing would let a verdict be rewritten in place."""
    _iterate(package)
    _finalized(package)

    with pytest.raises(receipt.ReceiptError) as error:
        receipt.finalize(package)
    assert error.value.code == "ALREADY_FINAL"


def test_the_generated_half_is_not_editable_by_the_reviewer(package: Path) -> None:
    """Immutable generated identities are re-derived immediately before sealing, never trusted."""
    original = _iterate(package)
    _pass_judgement(package)
    path = _receipt_path(package)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["generated"]["artifact"]["report_revision"] = "sha256:" + "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert _refusal(package) == "REPORT_CHANGED"
    assert original["generated"]["artifact"]["report_revision"].startswith("sha256:")


def test_a_receipt_deep_copy_round_trips_through_the_schema(package: Path) -> None:
    """The producer's own output must satisfy the schema it enforces on everyone else."""
    payload = _iterate(package)

    assert receipt.validate_receipt(copy.deepcopy(payload)) is not None
