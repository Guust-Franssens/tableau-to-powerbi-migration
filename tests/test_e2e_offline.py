"""The joined offline end-to-end test: Tableau site -> assess -> harvest -> convert -> Fabric.

Nothing here touches a network. The Tableau half is a loopback HTTP server serving this repo's own
fixture workbooks; the Fabric half is an in-process fake substituted at ``deploy_estate._request``.
Every script in between - ``assess_estate``, ``run_estate``, ``parse_tableau``, ``deploy_estate`` -
is the REAL one, unmodified.

The suite is written to FAIL when the pipeline regresses, which is a different goal from "runs
green". The mutations each assertion was proven against are listed in
``docs/offline-mock-harness.md``; in short, the E2E catches a lost duplicate check, a report bound
to the wrong model, models deployed after reports, a flattened folder tree and a re-run that
re-creates instead of updating.

Run just this file::

    python -m pytest tests/test_e2e_offline.py -q

Skip the slow half (the subprocess engine + the loopback server)::

    python -m pytest -q -m "not slow"
"""

from __future__ import annotations

import argparse
import base64
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import assess_estate as ae  # noqa: E402
import deploy_estate as de  # noqa: E402
import run_estate  # noqa: E402
from mocks import estate, tableau  # noqa: E402
from mocks.fabric import MODEL_TYPE, REPORT_TYPE, FabricService  # noqa: E402

pytestmark = pytest.mark.slow

# Two of the three fixture workbooks have convertible worksheets; `federated_multi_connection.twb`
# has none, so its report is legitimately empty. That is not a defect to work around - it is the
# MEASURED case (2 of 33 reports on a real estate) that `report_is_empty` exists for, and the E2E
# asserts the resulting item count rather than pretending every workbook yields two items.
EXPECTED_MODELS = ["Attic_Copy", "Ops_Dashboard", "Sales_Review"]
EXPECTED_REPORTS = ["Attic_Copy", "Sales_Review"]


def options(**overrides) -> argparse.Namespace:
    """The deployer's CLI namespace, with the defaults ``main()`` would have supplied."""
    base = {
        "dry_run": False,
        "force_unlock": False,
        "journal": None,
        "estate_db": None,
        "no_folders": False,
        "adopt_existing": False,
    }
    return argparse.Namespace(**(base | overrides))


@pytest.fixture(name="pipeline", scope="module")
def _pipeline(tmp_path_factory):
    """Everything up to (but not including) the deploy, run ONCE for the whole module.

    The front half is deterministic and read-only, so re-running it per test would only buy slower
    feedback. Each deploy test still gets its own workspace and its own copy of the bundle path.
    """
    work = tmp_path_factory.mktemp("estate")
    site = estate.build_site()

    with tableau.serve(site) as base:
        client = ae.Site(tableau.env_for(site, base))
        client.sign_in()
        raw = ae.collect(client, None)
        assembled = ae.assemble(raw, 0.99)
        assess_dir = work / "_assessment"
        assess_dir.mkdir()
        estate_db = ae.write_store(assess_dir, raw, assembled)
        harvested = estate.harvest(site, work, base_url=base, token=client.token)
        client.sign_out()

    engine = estate.install_fake_engine(work / "engine")
    code = run_estate.main(
        [
            "--input",
            str(work / "assets"),
            "--output",
            str(work / "bundle"),
            "--engine",
            str(engine),
            "--allow-noncanonical-engine",
        ]
    )
    assert code == 0, "the coordinator must accept the bundle before anything is deployed"

    return {
        "site": site,
        "work": work,
        "bundle": work / "bundle",
        "estate_db": estate_db,
        "harvested": harvested,
        "assembled": assembled,
    }


@pytest.fixture(name="bundle")
def _bundle(pipeline, tmp_path):
    """A private copy of the bundle, so a deploy's journal/lock cannot leak between tests."""
    import shutil  # noqa: PLC0415

    target = tmp_path / "bundle"
    shutil.copytree(pipeline["bundle"], target)
    return target


# ------------------------------------------------------------------ front half


def test_the_front_half_produced_real_artifacts_from_real_bytes(pipeline):
    """Harvest downloaded packaged workbooks and the engine turned them into a PBIP bundle."""
    assert {p.suffix for p in pipeline["harvested"]} == {".twbx", ".tdsx"}
    assert all(p.read_bytes()[:2] == b"PK" for p in pipeline["harvested"])

    report = json.loads((pipeline["bundle"] / "report.json").read_text(encoding="utf-8"))
    assert report["definition_of_done"]["status"] == "pass"
    assert {w["name"] for w in report["workbooks"]} == set(EXPECTED_MODELS)


def test_the_assessment_recorded_the_nested_project_tree(pipeline):
    """The deploy mirrors folders FROM this table, so the nesting has to survive the assessment."""
    with sqlite3.connect(pipeline["estate_db"]) as connection:
        luids = dict(connection.execute("select name, luid from project"))
        parents = dict(connection.execute("select name, parent_luid from project"))
    assert parents["Q1.2026"] == luids["Finance"]


# ------------------------------------------------------------- the joined E2E


def test_the_whole_chain_lands_the_expected_workspace(monkeypatch, bundle, pipeline):
    """The outcome assertion the whole harness exists for.

    Item count, ordering, binding, folder tree, zero duplicates - all read back out of the mock
    workspace rather than inferred from an exit code.
    """
    service = FabricService()
    service.install(monkeypatch, de)
    tok = service.token_for(de)

    code = de.deploy(bundle, service.workspace_id, tok, options(estate_db=pipeline["estate_db"]))
    assert code == de.EXIT_OK

    # 1. exactly what was expected, no more: the empty report is SKIPPED, not deployed empty.
    assert service.item_names(MODEL_TYPE) == EXPECTED_MODELS
    assert service.item_names(REPORT_TYPE) == EXPECTED_REPORTS

    # 2. zero duplicates. The mock would have ACCEPTED them (Fabric does), so this is a real check.
    assert service.duplicates() == []

    # 3. every model was created before the report that binds to it.
    creates = [url for method, url, _body in service.requests if method == "POST" and url.endswith("/items")]
    del creates  # ordering is asserted precisely below, from the item ids themselves
    order = [item.display_name for item in service.items.values()]
    for name in EXPECTED_REPORTS:
        model_at = order.index(name)
        report_at = len(order) - 1 - order[::-1].index(name)
        assert model_at < report_at, f"{name}: the model must exist before its report is bound"

    # 4. each report is bound BY CONNECTION to the guid the service returned for ITS model.
    models = {i.display_name: i.id for i in service.items.values() if i.item_type == MODEL_TYPE}
    for name in EXPECTED_REPORTS:
        assert service.model_binding(name) == models[name], f"{name} is bound to the wrong model"

    # 5. the Tableau project tree is mirrored, sanitised where Fabric rejects a character.
    #    `Q1.2026` cannot be a Fabric folder name (a dot is rejected ANYWHERE, not just trailing),
    #    and `R&D` cannot either - the deployer coerces both, and the NESTING under Finance is what
    #    proves the tree itself survived rather than collapsing to a flat list of root folders.
    placed = service.item_folder_paths()
    assert placed[f"Sales_Review ({MODEL_TYPE})"] == "Finance/Q1-2026"
    assert placed[f"Sales_Review ({REPORT_TYPE})"] == "Finance/Q1-2026"
    assert placed[f"Attic_Copy ({MODEL_TYPE})"] == "Finance"
    assert placed[f"Ops_Dashboard ({MODEL_TYPE})"] == "R-D"
    assert set(service.folder_paths().values()) == {"Finance", "Finance/Q1-2026", "R-D"}


def test_every_deployed_item_carries_a_provenance_stamp(monkeypatch, bundle, pipeline):
    """Ownership evidence lives in the SERVICE, not only in a local journal.

    A journal can be lost; the stamp is what lets the next run tell our item from a customer's
    same-named one, and it is the difference between a safe resume and overwriting their content.
    """
    service = FabricService()
    service.install(monkeypatch, de)
    de.deploy(bundle, service.workspace_id, service.token_for(de), options(estate_db=pipeline["estate_db"]))

    assert service.items
    for item in service.items.values():
        assert str(item.description or "").startswith(de.PROVENANCE), item.display_name


def test_a_report_is_never_left_bound_by_path(monkeypatch, bundle):
    """``byPath`` is what the engine emits and what the service cannot resolve.

    The mock refuses it outright (ASSUMED-strict), so a deploy that forgot to rebind fails loudly
    here instead of producing a report that opens to an error in the customer's tenant.
    """
    service = FabricService()
    service.install(monkeypatch, de)
    de.deploy(bundle, service.workspace_id, service.token_for(de), options())

    for item in service.items.values():
        if item.item_type != REPORT_TYPE:
            continue
        pbir = json.loads(service.part(item.id, "definition.pbir"))
        assert "byConnection" in pbir["datasetReference"]
        assert "byPath" not in pbir["datasetReference"]


def test_a_page_less_report_is_not_sent_to_fabric(tmp_path):
    """The deployer must refuse the service-invalid report shape before issuing a create call."""
    report = tmp_path / "empty.Report"
    pages = report / "definition" / "pages"
    pages.mkdir(parents=True)
    (pages / "pages.json").write_text('{"pageOrder": []}', encoding="utf-8")

    assert de.report_is_empty(report)


# --------------------------------------------------------- it has to CATCH things


def test_re_running_the_same_deploy_creates_nothing_new(monkeypatch, bundle, pipeline):
    """Idempotency, asserted on the WORKSPACE rather than on the exit code.

    Both runs share a journal (the same bundle), which is the normal resume shape.
    """
    service = FabricService()
    service.install(monkeypatch, de)
    tok = service.token_for(de)
    opts = options(estate_db=pipeline["estate_db"])

    assert de.deploy(bundle, service.workspace_id, tok, opts) == de.EXIT_OK
    first = {i.id: (i.display_name, i.item_type) for i in service.items.values()}
    folders_first = set(service.folders)

    assert de.deploy(bundle, service.workspace_id, tok, opts) == de.EXIT_OK
    second = {i.id: (i.display_name, i.item_type) for i in service.items.values()}

    assert second == first, "a re-run must UPDATE the same ids, never create new ones"
    assert service.duplicates() == []
    assert set(service.folders) == folders_first, "a re-run must reuse the folders it made"


def test_a_transient_failure_on_the_listing_stops_the_run_instead_of_duplicating(monkeypatch, bundle):
    """MEASURED root cause of real duplicates: a failed listing read as "the workspace is empty".

    Preflight refuses first, so nothing is created.
    """
    service = FabricService()
    service.install(monkeypatch, de)
    tok = service.token_for(de)

    assert de.deploy(bundle, service.workspace_id, tok, options()) == de.EXIT_OK
    before = {i.id for i in service.items.values()}

    service.fail_next(500, method="GET", contains="/items", times=6)
    code = de.deploy(bundle, service.workspace_id, tok, options())

    assert code == de.EXIT_PREFLIGHT, "an unreadable workspace must not be treated as an empty one"
    assert {i.id for i in service.items.values()} == before
    assert service.duplicates() == []


def test_a_listing_that_fails_AFTER_preflight_still_refuses_to_guess(monkeypatch, bundle):
    """The same defect one layer deeper, and it needs its own test.

    Preflight reads the workspace, then ``Landing.read`` reads it AGAIN - and it is the second read
    that decides whether an item already exists. A mutation that makes only ``Landing`` fall back to
    "empty" survives the test above, because preflight's read succeeded and short-circuited the run.
    Failing precisely the second listing is what exercises the layer that actually duplicates.
    """
    service = FabricService()
    service.install(monkeypatch, de)
    tok = service.token_for(de)

    assert de.deploy(bundle, service.workspace_id, tok, options()) == de.EXIT_OK
    before = {i.id for i in service.items.values()}

    original = service.request
    listings = {"n": 0}

    def fail_the_second_listing(method, url, bearer, body=None):
        if method == "GET" and "/items" in url:
            listings["n"] += 1
            if listings["n"] == 2:
                return 500, {}, {"errorCode": "InternalError", "message": "transient"}
        return original(method, url, bearer, body)

    monkeypatch.setattr(de, "_request", fail_the_second_listing)
    code = de.deploy(bundle, service.workspace_id, tok, options())

    assert code == de.EXIT_PREFLIGHT, "Landing.read must refuse, not fall back to an empty workspace"
    assert {i.id for i in service.items.values()} == before, "and above all, it must not duplicate"
    assert service.duplicates() == []


def test_a_throttled_listing_is_refused_rather_than_read_as_empty(monkeypatch, bundle):
    """A 429 on the landing-zone read is NOT retried, and that is the correct choice.

    Retrying would be nicer; guessing would be catastrophic. What matters is that the run stops with
    nothing created rather than deciding the workspace is empty and duplicating the estate. The
    ``Retry-After`` here is the RFC 9110 HTTP-date form - the variant that once raised ``ValueError``
    inside ``float()`` and killed a deploy mid-estate.
    """
    service = FabricService()
    service.install(monkeypatch, de)
    tok = service.token_for(de)
    service.throttle(retry_after="Wed, 21 Oct 2026 07:28:00 GMT", contains="/items", times=1)

    assert de.deploy(bundle, service.workspace_id, tok, options()) == de.EXIT_PREFLIGHT
    assert not service.items, "nothing may be created off the back of a listing we could not read"


def test_a_throttled_create_is_retried_after_the_http_date_delay(monkeypatch, bundle):
    """Where a 429 IS retried - on the create - the HTTP-date form must not crash the run.

    ``_post_item`` honours ``Retry-After`` exactly once. Feeding it the date form here is the
    regression test for the ``float()`` ``ValueError`` that killed a real deploy mid-estate.
    """
    service = FabricService()
    service.install(monkeypatch, de)
    tok = service.token_for(de)
    service.fail_next(
        429,
        method="POST",
        contains="/items",
        times=1,
        headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"},
        body={"errorCode": "TooManyRequests", "message": "Too many requests"},
    )

    assert de.deploy(bundle, service.workspace_id, tok, options()) == de.EXIT_OK
    assert service.item_names(MODEL_TYPE) == EXPECTED_MODELS
    assert service.duplicates() == []


def test_an_interrupted_run_resumes_without_duplicating(monkeypatch, bundle):
    """The resume path, driven by a real interruption rather than a hand-written journal.

    The first run is cut off after two items have genuinely landed; the second must adopt them -
    identified by the SERVICE-side provenance stamp - and finish the rest.
    """
    service = FabricService()
    service.install(monkeypatch, de)
    tok = service.token_for(de)

    original = service.request
    creates = {"n": 0}

    def die_after_two_creates(method, url, bearer, body=None):
        if method == "POST" and url.endswith("/items"):
            creates["n"] += 1
            if creates["n"] > 2:
                return 0, {}, {"error": {"message": "[Errno 11001] getaddrinfo failed"}}
        return original(method, url, bearer, body)

    monkeypatch.setattr(de, "_request", die_after_two_creates)
    assert de.deploy(bundle, service.workspace_id, tok, options()) == de.EXIT_FAILED
    landed = {i.id for i in service.items.values()}
    assert len(landed) == 2, "exactly the two creates that got through"

    monkeypatch.setattr(de, "_request", original)
    assert de.deploy(bundle, service.workspace_id, tok, options()) == de.EXIT_OK

    assert landed <= {i.id for i in service.items.values()}, "the survivors must be adopted, not orphaned"
    assert service.item_names(MODEL_TYPE) == EXPECTED_MODELS
    assert service.duplicates() == []


def test_a_partial_run_that_lost_its_journal_still_does_not_duplicate(monkeypatch, bundle):
    """The nastiest resume: the journal is GONE, so only the service-side stamp can identify our own
    items. Without the stamp this run would either duplicate everything or refuse to proceed."""
    service = FabricService()
    service.install(monkeypatch, de)
    tok = service.token_for(de)

    assert de.deploy(bundle, service.workspace_id, tok, options()) == de.EXIT_OK
    before = {i.id for i in service.items.values()}
    (bundle / "deploy-journal.jsonl").unlink()

    assert de.deploy(bundle, service.workspace_id, tok, options()) == de.EXIT_OK
    assert {i.id for i in service.items.values()} == before
    assert service.duplicates() == []


def test_a_customers_same_named_item_is_not_overwritten(monkeypatch, bundle):
    """A landing zone is not necessarily empty. An unstamped item of the same name is THEIRS.

    Overwriting it destroyed a customer's content on a real run; the refusal is what replaced that.
    """
    service = FabricService()
    service.install(monkeypatch, de)
    tok = service.token_for(de)

    theirs = service.call(
        "POST",
        f"https://api.fabric.microsoft.com/v1/workspaces/{service.workspace_id}/items",
        tok,
        {
            "displayName": "Sales_Review",
            "type": MODEL_TYPE,
            "definition": {
                "parts": [
                    {
                        "path": "definition.pbism",
                        "payload": base64.b64encode(b'{"version":"4.0"}').decode(),
                        "payloadType": "InlineBase64",
                    }
                ]
            },
        },
    )[2]["id"]
    original = service.part(theirs, "definition.pbism")

    code = de.deploy(bundle, service.workspace_id, tok, options())

    assert code != de.EXIT_OK, "refusing to overwrite unowned content must be a failure, not a warning"
    assert service.part(theirs, "definition.pbism") == original, "their content must be untouched"
    assert not service.items[theirs].description, "and it must not be stamped as ours"


def test_the_token_expiring_mid_run_does_not_lose_the_estate(monkeypatch, bundle):
    """MEASURED: a 66-item deploy outlived its token and every remaining call answered 401.

    ``call`` renews once and retries. Installing at ``_request`` is what keeps that under test.
    """
    service = FabricService()
    service.install(monkeypatch, de)
    tok = service.token_for(de)

    original_request = service.request
    state = {"calls": 0}

    def expire_after_three(method, url, bearer, body=None):
        state["calls"] += 1
        if state["calls"] == 3:
            service.expire_tokens()
        return original_request(method, url, bearer, body)

    monkeypatch.setattr(de, "_request", expire_after_three)

    assert de.deploy(bundle, service.workspace_id, tok, options()) == de.EXIT_OK
    assert service.item_names(MODEL_TYPE) == EXPECTED_MODELS
    assert service.duplicates() == []


def test_a_dry_run_creates_absolutely_nothing(monkeypatch, bundle, pipeline):
    """The rehearsal mode the workshop will actually use first."""
    service = FabricService()
    service.install(monkeypatch, de)

    code = de.deploy(
        bundle, service.workspace_id, service.token_for(de), options(dry_run=True, estate_db=pipeline["estate_db"])
    )

    assert code == de.EXIT_OK
    assert not service.items
    assert not service.folders
    assert not [m for m, _u, _b in service.requests if m == "POST"]


def test_the_workspace_item_limit_is_enforced_before_anything_is_created(monkeypatch, bundle):
    """A landing zone that cannot hold the estate must be refused up front, not discovered at 90%.

    Two independent limits are in play and they are worth telling apart: the SERVICE enforces 1000
    items (the mock reproduces that, see ``test_mock_fabric``), while the DEPLOYER refuses earlier,
    at 90% of its own constant, so it never gets close. This asserts the deployer's gate, which is
    the one that saves a half-deployed workspace.
    """
    service = FabricService()
    service.install(monkeypatch, de)
    monkeypatch.setattr(de, "WORKSPACE_ITEM_LIMIT", 2)

    code = de.deploy(bundle, service.workspace_id, service.token_for(de), options())

    assert code == de.EXIT_PREFLIGHT
    assert not service.items, "preflight must refuse before the first create"


def test_the_service_side_limit_is_a_hard_stop_not_a_silent_truncation(monkeypatch, bundle):
    """And if the deployer's own gate were removed, the SERVICE still refuses - loudly.

    Reproducing the real 400 rather than quietly dropping the item is what makes the failure legible
    in the run log instead of showing up as a missing report weeks later.
    """
    service = FabricService(item_limit=2)
    service.install(monkeypatch, de)

    code = de.deploy(bundle, service.workspace_id, service.token_for(de), options())

    assert code == de.EXIT_FAILED
    assert len(service.items) == 2, "the service caps it; nothing beyond the cap is invented"
