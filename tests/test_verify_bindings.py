"""Tests for scripts/verify_bindings.py.

The whole point of that script is one measured trap: `getDefinition` answers **202 Accepted with an
empty body**, and reading that body reports every report in the estate as bound to nothing. So the
fake service below behaves the way the real one did - 202 first, the answer only at
`Location` -> `/result` - and a test that would still pass if the 202 envelope were read as the
answer is worthless here. Each test names the mutation it kills.

No network: `vb.call` is replaced by a scripted transport, and the poll loop is driven by a fake
clock, so the suite is deterministic and instant.
"""

from __future__ import annotations

import base64
import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import verify_bindings as vb  # noqa: E402  # pylint: disable=wrong-import-position

WS = "11111111-1111-1111-1111-111111111111"
MODEL_ID = "22222222-2222-2222-2222-222222222222"
OTHER_MODEL_ID = "99999999-9999-9999-9999-999999999999"
REPORT_ID = "33333333-3333-3333-3333-333333333333"
TENANT = "44444444-4444-4444-4444-444444444444"


# --------------------------------------------------------------------------- fixtures & fakes


def _connection(model_id: str, catalog: str = "Sales") -> str:
    """The product's own byConnection string shape, with the guid carried inside it."""
    return (
        'Data Source="powerbi://api.powerbi.com/v1.0/myorg/Landing Zone";'
        f"initial catalog={catalog};"
        "access mode=readonly;"
        "integrated security=ClaimsToken;"
        f"semanticmodelid={model_id}"
    )


def _pbir(reference: dict) -> dict:
    return {"version": "4.0", "datasetReference": reference}


def _parts(pbir: dict) -> list[dict]:
    payload = base64.b64encode(json.dumps(pbir).encode("utf-8")).decode("ascii")
    return [
        {"path": "definition.pbir", "payloadType": "InlineBase64", "payload": payload},
        {"path": "definition/report.json", "payloadType": "InlineBase64", "payload": "e30="},
    ]


def byconnection_parts(model_id: str = MODEL_ID) -> list[dict]:
    """A report bound the way a deployed report should be."""
    return _parts(_pbir({"byConnection": {"connectionString": _connection(model_id)}}))


def bypath_parts(path: str = "../Sales.SemanticModel") -> list[dict]:
    """A report still carrying the Git-integration binding the service cannot resolve."""
    return _parts(_pbir({"byPath": {"path": path}}))


class FakeFabric:
    """A Fabric that answers the way the real one does, including the 202 that started all this.

    `getDefinition` NEVER returns the definition in its own response unless a test explicitly asks
    for the inline-200 shape: the parts live behind the operation, exactly as in the service.
    """

    def __init__(self, items: list[dict], definitions: dict[str, list[dict]] | None = None) -> None:
        self.items = items
        self.definitions = definitions or {}
        self.op_states: dict[str, list[dict]] = {}
        self.op_status_codes: dict[str, list[int]] = {}
        self.inline: set[str] = set()
        self.pages: list[dict] | None = None
        self.workspace_status = 200
        # What the real endpoint sends on its 202, measured against a live workspace.
        self.retry_after = "20"
        self.calls: list[tuple[str, str]] = []

    # -- scripting helpers -------------------------------------------------

    def operation_reports(self, item_id: str, states: list[dict], codes: list[int] | None = None) -> None:
        """Script what polling `Location` for this item returns, in order."""
        self.op_states[item_id] = states
        if codes:
            self.op_status_codes[item_id] = codes

    def _next(self, item_id: str) -> tuple[int, dict]:
        states = self.op_states.get(item_id) or [{"status": "Succeeded"}]
        codes = self.op_status_codes.get(item_id) or []
        index = min(len([c for c in self.calls if c == ("GET", self._op_url(item_id))]) - 1, len(states) - 1)
        code = codes[min(index, len(codes) - 1)] if codes else 200
        return code, states[index]

    @staticmethod
    def _op_url(item_id: str) -> str:
        return f"https://api.fabric.microsoft.com/v1/operations/op-{item_id}"

    # -- the transport ----------------------------------------------------

    def call(self, method: str, url: str, tok, body=None) -> tuple[int, dict, dict]:  # noqa: ARG002
        """Stand-in for `vb.call`."""
        self.calls.append((method, url))
        if method == "GET" and url == f"{vb.API}/workspaces/{WS}":
            if self.workspace_status != 200:
                return self.workspace_status, {}, {"error": {"message": "nope"}}
            return 200, {}, {"displayName": "Landing Zone"}
        if method == "GET" and url.startswith(f"{vb.API}/workspaces/{WS}/items"):
            return self._list(url)
        if method == "POST" and url.endswith("/getDefinition"):
            item_id = url.split("/items/")[1].split("/")[0]
            if item_id in self.inline:
                return 200, {}, {"definition": {"parts": self.definitions.get(item_id, [])}}
            # The trap, reproduced faithfully: 202, EMPTY body, answer only behind Location - and
            # the `Retry-After: 20` the real service sends for a sub-second operation.
            headers = {"location": self._op_url(item_id), "x-ms-operation-id": f"op-{item_id}"}
            if self.retry_after:
                headers["retry-after"] = self.retry_after
            return 202, headers, {}
        if method == "GET" and url.endswith("/result"):
            item_id = url.split("/operations/op-")[1].split("/")[0]
            return 200, {}, {"definition": {"parts": self.definitions.get(item_id, [])}}
        if method == "GET" and "/operations/op-" in url:
            item_id = url.split("/operations/op-")[1]
            code, state = self._next(item_id)
            return code, {}, state
        raise AssertionError(f"unscripted call: {method} {url}")

    def _list(self, url: str) -> tuple[int, dict, dict]:
        if self.pages is None:
            return 200, {}, {"value": self.items}
        index = 1 if "continuationToken=" in url else 0
        return 200, {}, self.pages[index]


@pytest.fixture(autouse=True)
def fast_clock(monkeypatch):
    """Drive the poll loop from a fake clock so no test ever waits."""
    clock = {"t": 0.0}
    monkeypatch.setattr(vb, "_now", lambda: clock["t"])
    monkeypatch.setattr(vb, "_sleep", lambda seconds: clock.__setitem__("t", clock["t"] + max(seconds, 0.5)))
    return clock


@pytest.fixture
def fabric(monkeypatch):
    """A workspace holding one model and one report, wired into `vb.call`."""
    items = [
        {"id": MODEL_ID, "type": "SemanticModel", "displayName": "Sales"},
        {"id": REPORT_ID, "type": "Report", "displayName": "Sales Overview"},
    ]
    service = FakeFabric(items, {REPORT_ID: byconnection_parts()})
    monkeypatch.setattr(vb, "call", service.call)
    return service


# --------------------------------------------------------------------------- the 202 trap


def test_the_202_envelope_is_never_read_as_the_answer(fabric):
    """MUTATION KILLED: returning the 202 body's `definition.parts` instead of polling.

    This is the entire bug. The 202 body is empty, so reading it yields no parts for every report -
    which downstream reads as an estate bound to nothing.
    """
    definition = vb.fetch_definition(WS, REPORT_ID, "tok")
    assert definition.error == ""
    assert definition.parts, "the definition must come from the operation result, not the 202 body"
    assert ("GET", f"https://api.fabric.microsoft.com/v1/operations/op-{REPORT_ID}/result") in fabric.calls


def test_an_empty_definition_is_never_reported_as_bypath():
    """MUTATION KILLED: collapsing 'no parts' into a binding verdict.

    A read that produced nothing must say so. Calling it `byPath` is how the false defect gets its
    convincing wording.
    """
    check = vb.classify({"displayName": "R", "id": REPORT_ID}, vb.Definition(parts=[]), {})
    assert check.status == vb.UNREADABLE
    assert check.status != vb.BY_PATH


def test_a_failed_operation_is_unreadable_not_a_binding_defect(fabric):
    """MUTATION KILLED: treating a Failed operation as 'no definition, therefore unbound'."""
    fabric.operation_reports(REPORT_ID, [{"status": "Failed", "error": {"message": "backend blew up"}}])
    check = vb.classify(fabric.items[1], vb.fetch_definition(WS, REPORT_ID, "tok"), {MODEL_ID.lower(): "Sales"})
    assert check.status == vb.UNREADABLE
    assert "Failed" in check.detail and "backend blew up" in check.detail


def test_a_timed_out_operation_is_unreadable_and_says_so(fabric):
    """MUTATION KILLED: an operation still Running at the deadline silently becoming a verdict."""
    fabric.operation_reports(REPORT_ID, [{"status": "Running"}])
    definition = vb.fetch_definition(WS, REPORT_ID, "tok", timeout=10.0)
    assert "Timeout" in definition.error
    assert vb.classify(fabric.items[1], definition, {}).status == vb.UNREADABLE


def test_polling_rides_out_a_transient_500(fabric):
    """A 500 mid-poll is the service having a moment, not an answer about the report."""
    fabric.operation_reports(REPORT_ID, [{}, {"status": "Succeeded"}], codes=[500, 200])
    definition = vb.fetch_definition(WS, REPORT_ID, "tok")
    assert definition.error == ""
    assert definition.parts


def test_an_inline_200_definition_needs_no_polling(fabric):
    """Not every response is deferred; when the answer is inline, use it."""
    fabric.inline.add(REPORT_ID)
    definition = vb.fetch_definition(WS, REPORT_ID, "tok")
    assert definition.parts
    assert not any("/operations/" in url for _, url in fabric.calls)


def test_a_202_with_nothing_to_poll_is_an_error_not_a_verdict(monkeypatch):
    """A 202 carrying neither Location nor an operation id leaves nothing to poll - say that."""
    monkeypatch.setattr(vb, "call", lambda *a, **k: (202, {}, {}))
    definition = vb.fetch_definition(WS, REPORT_ID, "tok")
    assert "nothing to poll" in definition.error


def test_the_first_look_ignores_a_long_retry_after(fabric, fast_clock):
    """MUTATION KILLED: obeying `Retry-After: 20` before the FIRST poll.

    Measured against a live workspace: getDefinition answers 202 with `Retry-After: 20` for an
    operation that completes in ~0.3s. Waiting 20s per report made a 36-report check take 12
    minutes - and a check nobody will run is precisely what this script replaces.
    """
    fabric.retry_after = "20"
    vb.fetch_definition(WS, REPORT_ID, "tok")
    assert fast_clock["t"] <= vb.FIRST_POLL, f"waited {fast_clock['t']}s before the first look"


def test_a_still_running_operation_then_backs_off_to_the_service_s_hint(fabric, fast_clock):
    """The hint is honoured once the quick look shows the operation really is still going."""
    fabric.retry_after = "20"
    fabric.operation_reports(REPORT_ID, [{"status": "Running"}, {"status": "Succeeded"}])
    definition = vb.fetch_definition(WS, REPORT_ID, "tok")
    assert definition.parts
    assert fast_clock["t"] >= 20.0, "the second look should respect the Retry-After the service sent"


def test_an_http_date_retry_after_does_not_crash_the_poll_loop(fabric):
    """RFC 9110 allows an HTTP-date; `float()` on one killed a deploy mid-estate once."""
    fabric.retry_after = "Wed, 21 Oct 2026 07:28:00 GMT"
    assert vb.fetch_definition(WS, REPORT_ID, "tok").parts


# --------------------------------------------------------------------------- the binding verdict


def test_a_bound_report_resolves_to_the_model_in_this_workspace(fabric):
    checks, facts = vb.verify(WS, "tok")
    assert [c.status for c in checks] == [vb.RESOLVED]
    assert checks[0].model_id == MODEL_ID
    assert checks[0].model_name == "Sales"
    assert facts["reports"] == 1 and facts["models"] == 1


def test_a_genuine_bypath_is_reported_as_bypath_with_the_fix(fabric):
    """The real defect this check exists to find, when it really is one."""
    fabric.definitions[REPORT_ID] = bypath_parts()
    check = vb.classify(fabric.items[1], vb.fetch_definition(WS, REPORT_ID, "tok"), {MODEL_ID.lower(): "Sales"})
    assert check.status == vb.BY_PATH
    assert "Sales.SemanticModel" in check.detail
    assert "deploy_estate.py" in vb.ACTIONS[vb.BY_PATH]


def test_a_guid_that_is_not_a_model_here_is_a_finding(fabric):
    """MUTATION KILLED: accepting any byConnection guid without checking it resolves HERE.

    A report carrying a guid from another workspace looks perfectly healthy field-by-field.
    """
    fabric.definitions[REPORT_ID] = byconnection_parts(OTHER_MODEL_ID)
    checks, _ = vb.verify(WS, "tok")
    assert checks[0].status == vb.UNRESOLVED
    assert OTHER_MODEL_ID in checks[0].detail


def test_a_guid_pointing_at_a_REPORT_id_does_not_count_as_resolved(fabric):
    """Only a SemanticModel resolves a semantic-model binding - any item id is not enough."""
    fabric.definitions[REPORT_ID] = byconnection_parts(REPORT_ID)
    checks, _ = vb.verify(WS, "tok")
    assert checks[0].status == vb.UNRESOLVED


def test_guid_comparison_is_case_insensitive(fabric):
    """Fabric is not consistent about guid case; a case difference is not a defect."""
    fabric.definitions[REPORT_ID] = byconnection_parts(MODEL_ID.upper())
    checks, _ = vb.verify(WS, "tok")
    assert checks[0].status == vb.RESOLVED


def test_a_report_with_no_definition_pbir_is_unreadable():
    check = vb.classify({"displayName": "R", "id": REPORT_ID}, vb.Definition(parts=[{"path": "x", "payload": ""}]), {})
    assert check.status == vb.UNREADABLE


def test_a_dataset_reference_of_an_unknown_shape_is_not_silently_passed():
    parts = _parts(_pbir({"bySomethingNew": {"id": MODEL_ID}}))
    check = vb.classify({"displayName": "R", "id": REPORT_ID}, vb.Definition(parts=parts), {})
    assert check.status == vb.UNRESOLVED


@pytest.mark.parametrize(
    ("connection", "expected"),
    [
        (_connection(MODEL_ID), MODEL_ID),
        (f"initial catalog=S;SemanticModelId={MODEL_ID};", MODEL_ID),
        (f'semanticmodelid="{MODEL_ID}"', MODEL_ID),
        (f"  semanticmodelid = {MODEL_ID} ", MODEL_ID),
        ("initial catalog=S;access mode=readonly", ""),
        ("", ""),
        # A different key that merely CONTAINS the name must not be mistaken for it.
        (f"xsemanticmodelid={MODEL_ID}", ""),
    ],
)
def test_semantic_model_id_is_read_out_of_the_connection_string(connection, expected):
    assert vb.semantic_model_id(connection) == expected


# --------------------------------------------------------------------------- listing the estate


def test_a_report_on_the_second_page_is_still_checked(monkeypatch):
    """MUTATION KILLED: reading only the first page of items.

    A report past the page boundary is simply absent, so the check reports a smaller estate than
    exists - and still exits 0.
    """
    second = {"id": "55555555-5555-5555-5555-555555555555", "type": "Report", "displayName": "Page Two"}
    service = FakeFabric([], {REPORT_ID: byconnection_parts(), second["id"]: byconnection_parts()})
    service.pages = [
        {
            "value": [
                {"id": MODEL_ID, "type": "SemanticModel", "displayName": "Sales"},
                {"id": REPORT_ID, "type": "Report", "displayName": "Sales Overview"},
            ],
            "continuationToken": "next",
        },
        {"value": [second]},
    ]
    monkeypatch.setattr(vb, "call", service.call)
    checks, facts = vb.verify(WS, "tok")
    assert facts["reports"] == 2
    assert {c.name for c in checks} == {"Sales Overview", "Page Two"}


def test_a_service_repeating_its_continuation_token_does_not_loop_forever(monkeypatch):
    body = {"value": [{"id": MODEL_ID, "type": "SemanticModel"}], "continuationToken": "same"}
    monkeypatch.setattr(vb, "call", lambda *a, **k: (200, {}, body))
    status, rows = vb.list_all(WS, "tok")
    assert status == 200 and len(rows) == 2


def test_a_failed_listing_is_not_an_empty_workspace(monkeypatch):
    """'I could not ask' must never be rendered as 'there is nothing there'."""
    monkeypatch.setattr(
        vb, "call", lambda method, url, *a, **k: (200, {}, {"displayName": "LZ"}) if url.endswith(WS) else (500, {}, {})
    )
    with pytest.raises(vb.CannotCheck, match="Could not list items"):
        vb.verify(WS, "tok")


# --------------------------------------------------------------------------- exit codes


def test_exit_zero_when_every_report_resolves(fabric, caplog):
    with caplog.at_level(logging.INFO):
        checks, facts = vb.verify(WS, "tok")
        assert vb.print_summary(checks, facts) == vb.EXIT_OK
    assert "does not prove any visual RENDERS" in caplog.text


def test_exit_non_zero_when_anything_fails(fabric):
    """MUTATION KILLED: a gate that reports findings but still exits 0."""
    fabric.definitions[REPORT_ID] = bypath_parts()
    checks, facts = vb.verify(WS, "tok")
    assert vb.print_summary(checks, facts) == vb.EXIT_FINDINGS


def test_a_workspace_with_no_reports_does_not_pass_vacuously(monkeypatch, caplog):
    """MUTATION KILLED: 0/0 rendered as a pass. A check that proves nothing is not a green check."""
    service = FakeFabric([{"id": MODEL_ID, "type": "SemanticModel", "displayName": "Sales"}])
    monkeypatch.setattr(vb, "call", service.call)
    with caplog.at_level(logging.INFO):
        checks, facts = vb.verify(WS, "tok")
        assert vb.print_summary(checks, facts) == vb.EXIT_CANNOT_CHECK
    assert "proves nothing" in caplog.text


def test_the_findings_name_the_report_and_what_to_do(fabric, caplog):
    fabric.definitions[REPORT_ID] = bypath_parts()
    with caplog.at_level(logging.INFO):
        checks, facts = vb.verify(WS, "tok")
        vb.print_summary(checks, facts)
    assert "Sales Overview" in caplog.text
    assert REPORT_ID in caplog.text
    assert "deploy_estate.py" in caplog.text


def test_an_unreadable_report_is_flagged_as_a_failed_READ_in_the_summary(fabric, caplog):
    """The summary must not let an unreadable report be quoted as a broken binding."""
    fabric.operation_reports(REPORT_ID, [{"status": "Failed"}])
    with caplog.at_level(logging.INFO):
        checks, facts = vb.verify(WS, "tok")
        assert vb.print_summary(checks, facts) == vb.EXIT_FINDINGS
    assert "not evidence" in caplog.text.lower() or "not evidence" in vb.ACTIONS[vb.UNREADABLE]
    assert "could not be READ" in caplog.text


def test_the_json_artifact_records_every_report(fabric, tmp_path):
    checks, facts = vb.verify(WS, "tok")
    out = tmp_path / "bindings.json"
    vb.write_json(out, checks, facts)
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["resolved"] == 1
    assert payload["results"][0]["semantic_model_id"] == MODEL_ID
    assert payload["workspace"] == WS


# --------------------------------------------------------------------------- the tenant trap


def _jwt(tid: str, secret: str = "SECRET-SIGNATURE") -> str:
    """A structurally real JWT: header.payload.signature, base64url, unpadded."""

    def segment(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode("utf-8")).decode("ascii").rstrip("=")

    return f"{segment({'typ': 'JWT'})}.{segment({'tid': tid, 'aud': 'fabric'})}.{secret}"


def test_the_token_s_tenant_is_read_from_its_claims():
    assert vb.token_tenant(_jwt(TENANT)) == TENANT


@pytest.mark.parametrize("bad", ["", "not-a-jwt", "a.b", "a.!!!!.c"])
def test_an_unreadable_token_yields_no_tenant_rather_than_crashing(bad):
    assert vb.token_tenant(bad) == ""


def test_a_missing_workspace_names_the_tenant_the_token_is_for(monkeypatch):
    """MUTATION KILLED: reporting a 404 as a plain absence.

    `az account get-access-token` can succeed against the WRONG tenant, and Fabric then answers
    WorkspaceNotFound for a workspace that exists.
    """
    monkeypatch.setattr(vb.Token, "_mint", lambda self: _jwt(TENANT))
    monkeypatch.setattr(vb, "call", lambda *a, **k: (404, {}, {}))
    with pytest.raises(vb.CannotCheck) as caught:
        vb.verify(WS, vb.Token(None))
    message = str(caught.value)
    assert TENANT in message
    assert "--tenant" in message and "wrong tenant" in message


def test_a_token_for_a_different_tenant_than_asked_for_stops_before_any_call(monkeypatch, caplog):
    """The mismatch is knowable before the first request, so it is checked there."""
    monkeypatch.setattr(vb.Token, "_mint", lambda self: _jwt("00000000-0000-0000-0000-000000000000"))
    monkeypatch.setattr(vb, "call", lambda *a, **k: pytest.fail("no call should be made"))
    with caplog.at_level(logging.INFO):
        code = vb.main(["--workspace", WS, "--tenant", TENANT])
    assert code == vb.EXIT_CANNOT_CHECK
    assert "az login --tenant" in caplog.text


def test_the_token_is_never_printed(monkeypatch, caplog, capsys):
    """A diagnostic that leaks the bearer token would be a worse bug than the one being diagnosed."""
    monkeypatch.setattr(vb.Token, "_mint", lambda self: _jwt(TENANT, secret="SUPER-SECRET-SIGNATURE"))
    monkeypatch.setattr(vb, "call", lambda *a, **k: (404, {}, {}))
    with caplog.at_level(logging.INFO):
        assert vb.main(["--workspace", WS]) == vb.EXIT_CANNOT_CHECK
    printed = caplog.text + capsys.readouterr().out
    assert "SUPER-SECRET-SIGNATURE" not in printed
    assert TENANT in printed


def test_a_forbidden_workspace_is_reported_as_permission_not_absence(monkeypatch):
    monkeypatch.setattr(vb, "call", lambda *a, **k: (403, {}, {}))
    with pytest.raises(vb.CannotCheck, match="Viewer"):
        vb.verify(WS, "tok")


def test_an_expired_token_is_re_minted_once(monkeypatch):
    """A long read must not turn into a page of 401s that look like findings."""
    monkeypatch.setattr(vb.Token, "_mint", lambda self: _jwt(TENANT))
    tok = vb.Token(None)
    seen: list[str] = []

    def _request(method, url, bearer, body=None):  # noqa: ARG001
        seen.append(bearer)
        if len(seen) == 1:
            return 401, {}, {"error": {"errorCode": "TokenExpired"}}
        return 200, {}, {"ok": True}

    monkeypatch.setattr(vb, "_request", _request)
    status, _, body = vb.call("GET", "https://example.invalid", tok)
    assert status == 200 and body == {"ok": True}
    assert len(seen) == 2


def test_a_plain_401_is_not_retried(monkeypatch):
    """Only an EXPIRED token is worth a second attempt; anything else is a real authz problem."""
    monkeypatch.setattr(vb.Token, "_mint", lambda self: _jwt(TENANT))
    attempts: list[int] = []

    def _request(method, url, bearer, body=None):  # noqa: ARG001
        attempts.append(1)
        return 401, {}, {"error": {"errorCode": "Unauthorized"}}

    monkeypatch.setattr(vb, "_request", _request)
    status, _, _ = vb.call("GET", "https://example.invalid", vb.Token(None))
    assert status == 401 and len(attempts) == 1


# --------------------------------------------------------------------------- end to end


def test_main_returns_zero_on_a_clean_estate(monkeypatch, tmp_path):
    """The whole path an operator runs, with the service faked at the transport."""
    items = [{"id": MODEL_ID, "type": "SemanticModel", "displayName": "Sales"}]
    definitions = {}
    for index in range(3):
        report_id = f"3333333{index}-3333-3333-3333-333333333333"
        items.append({"id": report_id, "type": "Report", "displayName": f"Report {index}"})
        definitions[report_id] = byconnection_parts()
    service = FakeFabric(items, definitions)
    for report_id in definitions:
        service.operation_reports(report_id, [{"status": "Running"}, {"status": "Succeeded"}])
    monkeypatch.setattr(vb, "call", service.call)
    monkeypatch.setattr(vb.Token, "_mint", lambda self: _jwt(TENANT))

    out = tmp_path / "bindings.json"
    assert vb.main(["--workspace", WS, "--json", str(out)]) == vb.EXIT_OK
    assert json.loads(out.read_text(encoding="utf-8"))["resolved"] == 3
