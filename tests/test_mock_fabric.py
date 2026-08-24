"""Fidelity self-tests for the offline Fabric mock.

Every test here asserts a behaviour of the MOCK, not of the deployer, and each names the evidence
that put it there. That matters more than it sounds: a mock is only useful if it is at least as
strict as the service, and the only way to keep it that way is to pin each strictness in a test that
fails when someone relaxes it. Where a behaviour is ASSUMED rather than measured, the test says so in
its docstring and asserts the *shape* (status / effect) rather than an invented error code.

See ``docs/offline-mock-harness.md`` for the full evidence table.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import deploy_estate as de  # noqa: E402
from mocks.fabric import API, MODEL_TYPE, REPORT_TYPE, WORKSPACE_ITEM_LIMIT, FabricService  # noqa: E402


def part(path: str, payload: str) -> dict:
    """One inline base64 definition part, the shape the Fabric items API takes."""
    return {"path": path, "payload": base64.b64encode(payload.encode()).decode(), "payloadType": "InlineBase64"}


def model_body(name: str, **extra) -> dict:
    return {
        "displayName": name,
        "type": MODEL_TYPE,
        "definition": {"parts": [part("definition.pbism", '{"version":"4.0"}')]},
        **extra,
    }


def report_body(name: str, model_id: str, **extra) -> dict:
    pbir = json.dumps(
        {
            "version": "4.0",
            "datasetReference": {
                "byConnection": {
                    "connectionString": f"Data Source=powerbi://api.powerbi.com/v1.0/myorg/ws;"
                    f"Initial Catalog={name};Integrated Security=ClaimsToken;"
                    f"semanticModelId={model_id}"
                }
            },
        }
    )
    return {
        "displayName": name,
        "type": REPORT_TYPE,
        "definition": {
            "parts": [
                part("definition.pbir", pbir),
                part("definition/pages/pages.json", json.dumps({"pageOrder": ["p1"]})),
            ]
        },
        **extra,
    }


@pytest.fixture(name="service")
def _service() -> FabricService:
    return FabricService()


@pytest.fixture(name="tok")
def _tok(service: FabricService) -> str:
    return service.issue_token()


def create(service: FabricService, tok: str, body: dict):
    return service.call("POST", f"{API}/workspaces/{service.workspace_id}/items", tok, body)


# --------------------------------------------------------------------- MEASURED


def test_a_duplicate_display_name_is_accepted_because_fabric_accepts_it(service, tok):
    """MEASURED, and the single most important behaviour in this mock.

    Fabric does NOT reject a repeated Report/SemanticModel displayName; it creates a second item
    with the same name. That is exactly why duplicates went undetected on a real estate, and a mock
    that rejected the second create would have made the whole class of bug untestable.
    """
    first = create(service, tok, model_body("Sales"))
    second = create(service, tok, model_body("Sales"))

    assert first[0] == 201
    assert second[0] == 201, "Fabric accepts a duplicate display name; the mock must too"
    assert first[2]["id"] != second[2]["id"]
    assert service.item_names(MODEL_TYPE) == ["Sales", "Sales"]
    assert service.duplicates() == [("Sales", MODEL_TYPE)]


def test_folder_id_is_omitted_at_the_root_not_returned_as_null(service, tok):
    """MEASURED: a root-level item's listing row has NO ``folderId`` key at all.

    ``.get("folderId")`` cannot tell the two apart, but ``"folderId" in row`` can - and a mock that
    returned ``None`` would let a client that depends on the key existing pass here and fail live.
    """
    create(service, tok, model_body("Root Item"))
    folder = service.call("POST", f"{API}/workspaces/{service.workspace_id}/folders", tok, {"displayName": "Sales"})
    create(service, tok, model_body("Filed Item", folderId=folder[2]["id"]))

    _status, _headers, listing = service.call("GET", f"{API}/workspaces/{service.workspace_id}/items", tok)
    rows = {row["displayName"]: row for row in listing["value"]}

    assert "folderId" not in rows["Root Item"], "root items omit folderId entirely"
    assert rows["Filed Item"]["folderId"] == folder[2]["id"]


def test_a_listing_row_carries_id_display_name_type_and_description(service, tok):
    """MEASURED shape of ``GET /items``: ``{"value": [...]}`` with those fields."""
    create(service, tok, model_body("Sales", description="deployed by tests"))
    _status, _headers, listing = service.call("GET", f"{API}/workspaces/{service.workspace_id}/items", tok)
    row = listing["value"][0]

    assert set(row) >= {"id", "displayName", "type", "description"}
    assert row["type"] == MODEL_TYPE
    assert row["description"] == "deployed by tests"


def test_paging_emits_a_continuation_token_and_uri(service, tok):
    """MEASURED: 68 items came back in ONE page with no token.

    So paging is real but not reachable by accident - the page size is configurable precisely so a
    test can force it. A client that ignores ``continuationToken`` sees a short list and believes it.
    """
    service.page_size = 2
    for index in range(5):
        create(service, tok, model_body(f"Item {index}"))

    status, _headers, page = service.call("GET", f"{API}/workspaces/{service.workspace_id}/items", tok)
    assert status == 200
    assert len(page["value"]) == 2
    assert page["continuationToken"]
    assert page["continuationUri"].startswith("http")

    seen = list(page["value"])
    token = page["continuationToken"]
    while token:
        _s, _h, page = service.call(
            "GET", f"{API}/workspaces/{service.workspace_id}/items?continuationToken={token}", tok
        )
        seen += page["value"]
        token = page.get("continuationToken")
    assert len(seen) == 5


def test_list_all_in_the_deployer_follows_the_continuation_token(monkeypatch, service):
    """The deployer's own pager against the mock's pager - the pairing that matters."""
    service.page_size = 2
    tok = service.issue_token()
    for index in range(5):
        create(service, tok, model_body(f"Item {index}"))
    service.install(monkeypatch, de)

    status, rows = de.list_all(service.workspace_id, tok, "items")
    assert status == 200
    assert len(rows) == 5


def test_update_definition_leaves_description_and_folder_untouched(service, tok):
    """MEASURED: ``updateDefinition`` replaces only the definition.

    This is why the deployer stamps a description and moves an item as SEPARATE calls; a mock that
    let ``updateDefinition`` carry them would hide an omission that only shows up live.
    """
    folder = service.call("POST", f"{API}/workspaces/{service.workspace_id}/folders", tok, {"displayName": "Sales"})
    _s, _h, created = create(service, tok, model_body("Sales", description="v1", folderId=folder[2]["id"]))
    item_id = created["id"]

    status, _headers, _body = service.call(
        "POST",
        f"{API}/workspaces/{service.workspace_id}/items/{item_id}/updateDefinition",
        tok,
        {"definition": {"parts": [part("definition.pbism", '{"version":"9.9"}')]}},
    )

    assert status in (200, 202)
    item = service.items[item_id]
    assert item.description == "v1", "updateDefinition must not clear the description"
    assert item.folder_id == folder[2]["id"], "updateDefinition must not move the item"
    assert b"9.9" in service.part(item_id, "definition.pbism")


def test_patch_updates_the_description_and_move_replaces_the_item(service, tok):
    """MEASURED: ``PATCH {"description": ...}`` and ``POST /move {"targetFolderId": ...}``."""
    _s, _h, created = create(service, tok, model_body("Sales"))
    item_id = created["id"]
    folder = service.call("POST", f"{API}/workspaces/{service.workspace_id}/folders", tok, {"displayName": "Sales"})

    service.call("PATCH", f"{API}/workspaces/{service.workspace_id}/items/{item_id}", tok, {"description": "hello"})
    service.call(
        "POST",
        f"{API}/workspaces/{service.workspace_id}/items/{item_id}/move",
        tok,
        {"targetFolderId": folder[2]["id"]},
    )

    assert service.items[item_id].description == "hello"
    assert service.items[item_id].folder_id == folder[2]["id"]


def test_move_with_an_empty_body_means_the_workspace_root(service, tok):
    """MEASURED: an empty ``/move`` body re-places the item at the root."""
    folder = service.call("POST", f"{API}/workspaces/{service.workspace_id}/folders", tok, {"displayName": "Sales"})
    _s, _h, created = create(service, tok, model_body("Sales", folderId=folder[2]["id"]))

    service.call("POST", f"{API}/workspaces/{service.workspace_id}/items/{created['id']}/move", tok, {})

    assert service.items[created["id"]].folder_id is None


@pytest.mark.parametrize(
    "bad", ["Q1.2026", "R&D", "a/b", "a\\b", "a:b", "a?b", "a*b", 'a"b', "a|b", "a<b", "a#b", "a%b"]
)
def test_a_folder_name_with_a_rejected_character_is_refused_not_coerced(service, tok, bad):
    """MEASURED: the API REJECTS with ``InvalidFolderDisplayName``; it does not clean the name up.

    The dot is the one that surprises: it is rejected ANYWHERE, not just trailing, which is why a
    Tableau project called ``Q1.2026`` cannot be mirrored verbatim.
    """
    status, _headers, body = service.call(
        "POST", f"{API}/workspaces/{service.workspace_id}/folders", tok, {"displayName": bad}
    )

    assert status == 400
    assert body["errorCode"] == "InvalidFolderDisplayName"
    assert not service.folders


@pytest.mark.parametrize("bad", [" leading", "trailing "])
def test_a_folder_name_with_surrounding_space_is_refused(service, tok, bad):
    """MEASURED: a leading or trailing space is rejected (interior spaces are fine)."""
    status, _headers, _body = service.call(
        "POST", f"{API}/workspaces/{service.workspace_id}/folders", tok, {"displayName": bad}
    )
    assert status == 400


@pytest.mark.parametrize("good", ["Ventes fran\u00e7aises", "90 - Torture Chamber", "a_b", "a+b", "a (2)"])
def test_the_accepted_folder_characters_really_are_accepted(service, tok, good):
    """MEASURED acceptances - and just as important as the rejections.

    A mock that over-rejects is not "safely strict": it makes the deployer's sanitiser look wrong and
    invites someone to mangle names Fabric was perfectly happy with. ``Ventes fran\u00e7aises`` was
    accepted by the real API.
    """
    status, _headers, body = service.call(
        "POST", f"{API}/workspaces/{service.workspace_id}/folders", tok, {"displayName": good}
    )
    assert status == 201, body


def test_folder_nesting_deeper_than_ten_is_refused(service, tok):
    """MEASURED: level 11 answers ``FolderDepthOutOfRange``."""
    parent = None
    for level in range(10):
        status, _headers, body = service.call(
            "POST",
            f"{API}/workspaces/{service.workspace_id}/folders",
            tok,
            {"displayName": f"L{level}", "parentFolderId": parent},
        )
        assert status == 201, f"level {level + 1} should be allowed"
        parent = body["id"]

    status, _headers, body = service.call(
        "POST",
        f"{API}/workspaces/{service.workspace_id}/folders",
        tok,
        {"displayName": "L10", "parentFolderId": parent},
    )
    assert status == 400
    assert body["errorCode"] == "FolderDepthOutOfRange"


def test_a_folder_listing_row_carries_id_display_name_and_parent(service, tok):
    """MEASURED shape of ``GET /folders``."""
    _s, _h, root = service.call("POST", f"{API}/workspaces/{service.workspace_id}/folders", tok, {"displayName": "A"})
    service.call(
        "POST",
        f"{API}/workspaces/{service.workspace_id}/folders",
        tok,
        {"displayName": "B", "parentFolderId": root["id"]},
    )

    _status, _headers, listing = service.call("GET", f"{API}/workspaces/{service.workspace_id}/folders", tok)
    rows = {row["displayName"]: row for row in listing["value"]}

    assert set(rows["A"]) >= {"id", "displayName"}
    assert "parentFolderId" not in rows["A"], "a root folder omits parentFolderId"
    assert rows["B"]["parentFolderId"] == root["id"]


def test_the_thousand_item_workspace_limit_is_enforced(tok):
    """MEASURED capacity limit. Exercised at a small limit so the test stays instant."""
    service = FabricService(item_limit=3)
    tok = service.issue_token()
    for index in range(3):
        assert create(service, tok, model_body(f"Item {index}"))[0] == 201

    status, _headers, body = create(service, tok, model_body("One Too Many"))
    assert status == 400
    assert "limit" in json.dumps(body).lower()
    assert len(service.items) == 3


def test_the_default_workspace_limit_is_the_measured_one_thousand():
    """The number itself, not just that SOME limit exists.

    Without this, a mock configured with a limit of a billion still passes the test above (it only
    ever exercises an explicitly-supplied small limit), and the harness would quietly stop modelling
    the capacity ceiling the deployer's own preflight is sized against.
    """
    assert WORKSPACE_ITEM_LIMIT == 1000
    assert FabricService().item_limit == 1000
    assert de.WORKSPACE_ITEM_LIMIT == WORKSPACE_ITEM_LIMIT, "the mock and the deployer must agree"


def test_token_expiry_answers_a_named_error_code_and_the_deployer_renews_once(monkeypatch, service):
    """MEASURED: ``401`` with ``{"errorCode": "TokenExpired"}``, distinct from a plain bad token.

    Installing at ``_request`` (not ``call``) is what keeps the renewal under test - patching ``call``
    would delete the very policy this asserts.
    """
    service.install(monkeypatch, de)
    tok = service.token_for(de)
    assert create(service, tok, model_body("Before"))[0] == 201

    service.expire_tokens()
    status, _headers, body = de.call("POST", f"{API}/workspaces/{service.workspace_id}/items", tok, model_body("After"))

    assert status == 201, body
    assert service.item_names() == ["After", "Before"]


def test_an_invalid_token_is_not_reported_as_expired(service):
    """MEASURED distinction: a token that was never valid must NOT carry ``TokenExpired``.

    Conflating them would make the deployer renew (and retry) on a credential that will never work.
    """
    status, _headers, body = service.call("GET", f"{API}/workspaces/{service.workspace_id}/items", "not-a-token")
    assert status == 401
    assert body.get("errorCode") != "TokenExpired"


@pytest.mark.parametrize("retry_after", [3, "Wed, 21 Oct 2026 07:28:00 GMT"])
def test_retry_after_is_served_in_both_measured_forms(monkeypatch, service, retry_after):
    """MEASURED both forms. The HTTP-date form once raised ``ValueError`` inside ``float()``.

    A mock that only ever emitted an integer would never have caught it.
    """
    service.install(monkeypatch, de)
    tok = service.issue_token()
    service.throttle(retry_after=retry_after, contains="/items")

    status, headers, _body = de.call("GET", f"{API}/workspaces/{service.workspace_id}/items", tok)
    assert status == 429
    assert headers["retry-after"] == str(retry_after)

    delay = de._retry_after(headers)  # noqa: SLF001  # pylint: disable=protected-access
    assert isinstance(delay, float), "the HTTP-date form must not escape as a ValueError"
    assert delay >= 0


def test_a_network_level_failure_surfaces_as_http_zero(monkeypatch, service):
    """The deployer distinguishes "the service said no" from "we never reached the service".

    ``_request`` maps ``URLError`` to status ``0``, and a detail containing ``HTTP 0`` is what stops
    a run rather than marking every remaining workbook failed.
    """
    service.install(monkeypatch, de)
    tok = service.issue_token()
    service.drop_network(contains="/items")

    status, _headers, body = de.call("GET", f"{API}/workspaces/{service.workspace_id}/items", tok)
    assert status == 0
    assert "error" in body


def test_a_long_running_create_must_be_polled_to_completion(monkeypatch):
    """MEASURED: ``202`` + ``Location``/``x-ms-operation-id``, with an EMPTY body.

    A client that reads an id out of the 202 gets nothing; it has to poll, then fetch ``/result``.
    """
    service = FabricService(async_create=True)
    service.install(monkeypatch, de)
    tok = service.issue_token()

    status, headers, body = service.call("POST", f"{API}/workspaces/{service.workspace_id}/items", tok, model_body("S"))
    assert status == 202
    assert body == {}, "the 202 body is empty - the id is only available via the operation"
    assert headers.get("location") or headers.get("x-ms-operation-id")

    state, _payload = de.await_operation(headers["location"], tok)
    assert state == "Succeeded"
    _s, _h, result = service.call("GET", f"{headers['location']}/result", tok)
    assert service.items[result["id"]].display_name == "S"


def test_a_failed_operation_is_indistinguishable_from_success_unless_polled(monkeypatch):
    """MEASURED incident: a probe reported both items deployed; one had FAILED and was never created.

    The 202 looks identical either way. Only ``GET /operations/{id}`` shows ``status: Failed``.
    """
    service = FabricService(async_create=True)
    service.install(monkeypatch, de)
    tok = service.issue_token()
    service.fail_next_operation()

    status, headers, body = service.call(
        "POST", f"{API}/workspaces/{service.workspace_id}/items", tok, model_body("Doomed")
    )
    assert (status, body) == (202, {}), "the failure is not visible in the immediate response"

    state, payload = de.await_operation(headers["location"], tok)
    assert state == "Failed"
    assert payload.get("error"), "a Failed operation carries the reason"
    assert not service.items, "a Failed operation creates nothing"


def test_a_stalled_operation_still_created_the_item_server_side(monkeypatch):
    """MEASURED shape: our poll gives up (``Timeout``) while the service completes anyway.

    That is the exact input that produces a duplicate on resume if the client does not re-list, so
    the mock has to be able to produce it.
    """
    service = FabricService(async_create=True)
    service.install(monkeypatch, de)
    tok = service.issue_token()
    service.stall_operations()

    _status, headers, _body = service.call(
        "POST", f"{API}/workspaces/{service.workspace_id}/items", tok, model_body("Slow")
    )
    state, _payload = de.await_operation(headers["location"], tok, timeout=1.0)
    assert state == "Timeout"
    assert service.item_names() == ["Slow"], "the item exists even though our poll timed out"


# --------------------------------------------------------------------- ASSUMED
# These encode the STRICTEST plausible behaviour where the real service was not measured. Each
# asserts the effect, never an invented error-code string. See the ASSUMED table in the doc.


def test_a_report_bound_by_path_is_refused(service, tok):
    """ASSUMED (strict). The service cannot resolve a relative filesystem path, so a ``byPath``
    reference must not deploy. Treated as a hard rejection because accepting it would let the
    single most common rebinding bug through the harness silently."""
    body = report_body("Sales", "irrelevant")
    body["definition"]["parts"][0] = part(
        "definition.pbir", json.dumps({"datasetReference": {"byPath": {"path": "../Sales.SemanticModel"}}})
    )
    status, _headers, _payload = create(service, tok, body)
    assert status == 400
    assert not service.items


def test_a_byconnection_block_with_extra_properties_is_refused(service, tok):
    """MEASURED against the PBIR 2.0.0 schema: ``byConnection`` sets ``additionalProperties: false``
    and permits only ``connectionString``. The five-field 1.0.0 shape is rejected."""
    _s, _h, model = create(service, tok, model_body("Sales"))
    body = report_body("Sales", model["id"])
    payload = json.loads(base64.b64decode(body["definition"]["parts"][0]["payload"]))
    payload["datasetReference"]["byConnection"].update(
        {"pbiModelDatabaseName": "x", "connectionType": "pbiServiceXmlaStyleLive"}
    )
    body["definition"]["parts"][0] = part("definition.pbir", json.dumps(payload))

    status, _headers, error = create(service, tok, body)
    assert status == 400
    assert "pbiModelDatabaseName" in json.dumps(error)


def test_a_connection_string_without_a_semantic_model_id_is_refused(service, tok):
    """ASSUMED (strict): a connection string that names no model id cannot bind to anything."""
    body = report_body("Sales", "")
    payload = json.loads(base64.b64decode(body["definition"]["parts"][0]["payload"]))
    payload["datasetReference"]["byConnection"]["connectionString"] = "Data Source=powerbi://x;Initial Catalog=Sales"
    body["definition"]["parts"][0] = part("definition.pbir", json.dumps(payload))

    status, _headers, _error = create(service, tok, body)
    assert status == 400


def test_a_report_bound_to_a_model_that_is_not_here_is_refused(service, tok):
    """ASSUMED (strict), and the assumption that gives the E2E its teeth.

    Binding to a GUID that does not resolve to a SemanticModel IN THIS WORKSPACE is refused, so
    "the report was bound to the duplicate/deleted model" becomes a test failure rather than a
    plausible-looking success. If the real service is more forgiving, this mock over-rejects - which
    is the safe direction."""
    status, _headers, _error = create(service, tok, report_body("Sales", "00000000-0000-0000-0000-000000000000"))
    assert status == 400


def test_a_report_with_no_pages_is_refused(service, tok):
    """MEASURED message, ASSUMED trigger boundary: Fabric answered "Content provider provided
    invalid package content stream" for reports whose ``pageOrder`` was empty."""
    _s, _h, model = create(service, tok, model_body("Sales"))
    body = report_body("Sales", model["id"])
    body["definition"]["parts"][1] = part("definition/pages/pages.json", json.dumps({"pageOrder": []}))

    status, _headers, error = create(service, tok, body)
    assert status == 400
    assert "package content stream" in json.dumps(error)


def test_a_non_base64_part_payload_is_refused(service, tok):
    """ASSUMED (strict): ``InlineBase64`` that is not base64 cannot be decoded by anything."""
    body = model_body("Sales")
    body["definition"]["parts"][0]["payload"] = "not base64!!"
    assert create(service, tok, body)[0] == 400


def test_an_unknown_item_type_is_refused(service, tok):
    """ASSUMED (strict): a typo in ``type`` must not silently create something."""
    assert create(service, tok, model_body("Sales") | {"type": "SemanticModle"})[0] == 400


def test_a_folder_id_that_does_not_exist_is_refused(service, tok):
    """ASSUMED (strict): the alternative is an item filed nowhere, which is worse than an error."""
    assert create(service, tok, model_body("Sales", folderId="folder-does-not-exist"))[0] == 400


def test_an_item_display_name_is_not_held_to_the_folder_character_rules(service, tok):
    """Deliberately NOT strict, because inventing a limit invents a failure.

    Item display names routinely contain dots (``Sales v1.2``). The folder rules were measured on
    FOLDERS; importing them here would make the mock reject something Fabric accepts, which is the
    one kind of infidelity that wastes a day chasing a bug that does not exist."""
    assert create(service, tok, model_body("Sales v1.2 (EMEA) & Ops"))[0] == 201


def test_an_empty_item_display_name_is_refused(service, tok):
    """ASSUMED (strict): an unnamed item is not addressable."""
    assert create(service, tok, model_body(""))[0] == 400


def test_a_forbidden_workspace_answers_403_not_404(service, tok):
    """The deployer must distinguish "no permission" from "no such workspace"; both are fatal but
    they need different remediation text."""
    service.forbidden_workspaces.add(service.workspace_id)
    status, _headers, _body = service.call("GET", f"{API}/workspaces/{service.workspace_id}", tok)
    assert status == 403


def test_the_request_log_records_what_was_actually_sent(service, tok):
    """Inspection surface, not fidelity - but the tests below lean on it, so it is pinned here."""
    create(service, tok, model_body("Sales"))
    methods = [method for method, _url, _body in service.requests]
    assert "POST" in methods
