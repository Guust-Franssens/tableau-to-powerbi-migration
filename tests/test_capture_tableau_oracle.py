"""Resilience contract for the Tableau oracle capture.

Every test here corresponds to a failure mode **measured against a live Tableau Cloud site**, not to a
hypothetical. The capture is a long sequential loop over an estate (29 views took 262s), so a single
unhandled blip discards minutes of work -- and worse, a *silently* recovered one produces a partial
result that is indistinguishable from a clean run.

The two rules the tests exist to pin:

* **A transient fault is retried; a missing credential is not.** No number of retries conjures a
  credential, so a ``FederatedDataSourceException`` must fail on the FIRST attempt. Retrying it would
  burn the retry budget and still fail, while hiding an actionable message from the human who can fix
  it.
* **Recovery is recorded, never silent.** ``reauths`` / ``retries`` land in the manifest, because a
  capture that healed itself must not look identical to one that never faltered.
"""

from __future__ import annotations

import ast
import gzip
import inspect
import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import capture_tableau_oracle as oracle  # noqa: E402  # pylint: disable=wrong-import-position
import build_reconcile_items  # noqa: E402  # pylint: disable=wrong-import-position
import tableau_oracle_manifest as verdict  # noqa: E402  # pylint: disable=wrong-import-position
import tableau_view_types as view_types_mod  # noqa: E402  # pylint: disable=wrong-import-position
import tableau_payload_facts as payload_facts  # noqa: E402  # pylint: disable=wrong-import-position

FEDERATED_401 = (
    "<?xml version='1.0'?><tsResponse><error code='401002'><summary>Unauthorized Access</summary></error></tsResponse>"
)
FEDERATED_CREDENTIAL = (
    "<error code='400081'><summary>Bad Request</summary><detail>There was a problem querying the data "
    "(com.tableausoftware.nativeapi.exceptions.FederatedDataSourceException: \n\nOne or more connections "
    "in this data source need attention:\n\nadb-4224091552383811.11.azuredatabricks.net: Tableau needs an "
    "unexpired OAuth refresh token to connect to the data. Authorize refresh tokens or ask the datasource "
    "owner for help.\n tableau_error_source=Client|tableau_status_code=16)</detail></error>"
)


def _creds() -> oracle.SiteCredentials:
    return oracle.SiteCredentials(
        base="https://example.online.tableau.com",
        site="site",
        pat_name="name",
        pat_secret="secret",
        version="3.29",
    )


class FakeSession(oracle.TableauSession):
    """A session whose HTTP layer is a scripted list of ``(status, body, headers)`` responses."""

    def __init__(self, responses, retry=None):
        super().__init__(_creds(), retry)
        self.responses = list(responses)
        self.calls: list[str] = []
        self.signin_count = 0
        self.token, self.site_id = "tok", "sid"

    def _request(self, method, path, *, body=None, accept=None, authed=True, api=None, deadline=None):  # noqa: ARG002
        self.calls.append(path)
        status, payload, headers = self.responses.pop(0)
        payload = payload.encode() if isinstance(payload, str) else payload
        headers = dict(headers)
        if status == 200 and "Content-Length" not in headers and "Transfer-Encoding" not in headers:
            headers["Content-Length"] = str(len(payload))
        return status, payload, headers

    def sign_in(self):
        self.signin_count += 1
        self.token, self.site_id = "tok", "sid"


class CloseDelimitedFakeSession(FakeSession):
    """A scripted successful response with EOF-as-framing, for the one case FakeSession defaults away."""

    def _request(self, method, path, *, body=None, accept=None, authed=True, api=None, deadline=None):  # noqa: ARG002
        self.calls.append(path)
        status, payload, headers = self.responses.pop(0)
        payload = payload.encode() if isinstance(payload, str) else payload
        return status, payload, headers


class _LoopbackDataExport(BaseHTTPRequestHandler):
    """One scripted `/data` response over the real urllib/http.client path."""

    protocol_version = "HTTP/1.1"
    response_headers: list[tuple[str, str]] = []
    body = b""

    def do_GET(self):  # noqa: N802
        self.wfile.write(b"HTTP/1.1 200 OK\r\n")
        for name, value in self.response_headers:
            self.wfile.write(f"{name}: {value}\r\n".encode())
        self.wfile.write(b"Connection: close\r\n\r\n")
        self.wfile.write(self.body)
        self.wfile.flush()
        self.close_connection = True

    def log_message(self, _format, *_args):  # noqa: A002
        return


def _loopback_capture(tmp_path: Path, headers: list[tuple[str, str]], body: bytes) -> tuple[int, dict]:
    handler = type("_ScriptedLoopbackDataExport", (_LoopbackDataExport,), {"response_headers": headers, "body": body})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        session = oracle.TableauSession(
            oracle.SiteCredentials(
                base=f"http://127.0.0.1:{port}",
                site="site",
                pat_name="name",
                pat_secret="secret-value",
                version="3.29",
            ),
            oracle.RetryPolicy(max_attempts=1, budget_sec=1),
        )
        session.token, session.site_id = "tok", "sid"
        view = {
            "id": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa",
            "name": "Real Time Availability",
            "workbook": {"id": "wb"},
        }
        record = oracle.capture_view(session, view, tmp_path, frozenset(), None)
        record["workbook_name"] = "Network Ops"
        return _named_manifest(tmp_path, [record])
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Backoff must not actually sleep in tests, but the delay still has to be computed."""
    slept: list[float] = []
    monkeypatch.setattr(oracle.time, "sleep", slept.append)
    return slept


# --------------------------------------------------------------------------- classification


def test_session_loss_is_classified_for_reauth():
    kind, _ = oracle.classify_export_error(401, FEDERATED_401)
    assert kind == "session_lost"


@pytest.mark.parametrize("status", [0, 429, 500, 502, 503, 504])
def test_gateway_and_network_faults_are_transient(status):
    kind, _ = oracle.classify_export_error(status, "gateway timeout")
    assert kind == "transient"


def test_expired_oauth_token_is_a_credential_block_not_a_transient_fault():
    kind, detail = oracle.classify_export_error(400, FEDERATED_CREDENTIAL)
    assert kind == "source_credential"
    assert "azuredatabricks.net" in detail
    assert "OAuth refresh token" in detail
    assert "tableau_error_source" not in detail, "internal diagnostic noise must not reach the human"


def test_a_5xx_mentioning_authentication_is_still_transient():
    """Ordering rule: a gateway 503 whose body happens to say 'authentication' must not be
    misfiled as a permanent credential block, which would abandon a recoverable view."""
    kind, _ = oracle.classify_export_error(503, "authentication service temporarily unavailable")
    assert kind == "transient"


def test_unknown_failure_is_not_retried():
    kind, _ = oracle.classify_export_error(404, "not found")
    assert kind == "failed"


# --------------------------------------------------------------------------- backoff


def test_backoff_grows_and_is_capped():
    delays = [oracle.backoff_delay(n, jitter=False) for n in range(1, 8)]
    assert delays == sorted(delays)
    assert max(delays) <= oracle.BACKOFF_CAP_SEC


def test_backoff_honours_retry_after():
    assert oracle.backoff_delay(1, retry_after="7", jitter=False) == 7.0


def test_backoff_ignores_a_malformed_retry_after():
    assert oracle.backoff_delay(1, retry_after="in a bit", jitter=False) == oracle.BACKOFF_BASE_SEC


def test_jitter_stays_within_half_the_deterministic_delay():
    for _ in range(50):
        assert 0.5 <= oracle.backoff_delay(2, jitter=True) <= 2.0


# --------------------------------------------------------------------------- export loop


def test_transient_failure_is_retried_then_succeeds_and_is_recorded():
    session = FakeSession(
        [
            (503, "gateway", {}),
            (200, "a,b\n1,2\n", {}),
        ]
    )
    payload, _, stats = session.export("/views/x/data")
    assert payload == b"a,b\n1,2\n"
    assert stats["retries"] == 1
    assert session.retry_count == 1, "a healed retry must still be visible in the manifest"


def test_credential_failure_is_never_retried():
    """The single most important test in this file: retrying cannot conjure a credential, and each
    retry delays the actionable message reaching the only person who can fix it."""
    session = FakeSession([(400, FEDERATED_CREDENTIAL, {})])
    with pytest.raises(oracle.ExportFailed) as excinfo:
        session.export("/views/x/data")
    assert excinfo.value.kind == "source_credential"
    assert len(session.calls) == 1, "a credential block must fail on the FIRST attempt"
    assert session.retry_count == 0


def test_session_loss_triggers_reauth_and_is_counted():
    session = FakeSession([(401, FEDERATED_401, {}), (200, "a\n1\n", {})])
    _, _, stats = session.export("/views/x/data")
    assert session.signin_count == 1
    assert stats["reauths"] == 1
    assert session.reauth_count == 1


def test_reauth_is_bounded_so_a_dead_token_cannot_loop():
    session = FakeSession([(401, FEDERATED_401, {})] * 6)
    with pytest.raises(oracle.ExportFailed):
        session.export("/views/x/data")
    assert session.signin_count <= oracle.MAX_REAUTH_PER_VIEW


def test_retries_stop_at_max_attempts():
    session = FakeSession([(503, "gw", {})] * 9, retry=oracle.RetryPolicy(max_attempts=3))
    with pytest.raises(oracle.ExportFailed):
        session.export("/views/x/data")
    assert len(session.calls) == 3


def test_retry_budget_stops_a_slow_failure_before_max_attempts(monkeypatch):
    """Attempts alone are not enough: with a long Retry-After, 5 attempts could block for minutes."""
    session = FakeSession(
        [(429, "slow down", {"Retry-After": "30"})] * 9,
        retry=oracle.RetryPolicy(max_attempts=9, budget_sec=10.0),
    )
    with pytest.raises(oracle.ExportFailed) as excinfo:
        session.export("/views/x/data")
    assert excinfo.value.kind == "transient"
    assert len(session.calls) < 9, "the wall-clock budget must cut the loop short"


def test_retry_after_header_is_used_for_the_delay(_no_real_sleep):
    session = FakeSession([(429, "slow", {"Retry-After": "3"}), (200, "a\n1\n", {})])
    session.export("/views/x/data")
    assert _no_real_sleep == [3.0]


def test_network_failure_becomes_status_zero_rather_than_an_exception(monkeypatch):
    """A reset connection mid-estate must be retryable, not fatal to the whole run."""

    def boom(*_args, **_kwargs):
        raise ConnectionResetError("connection reset by peer")

    session = oracle.TableauSession(_creds())
    monkeypatch.setattr(oracle.urllib.request, "urlopen", boom)
    status, body, _ = session._request("GET", "/x")  # pylint: disable=protected-access
    assert status == oracle.NETWORK_ERROR_STATUS
    assert b"ConnectionResetError" in body


def test_reflected_session_token_is_redacted_from_exceptions_and_manifest(tmp_path):
    """A WAF/proxy/debug endpoint can echo ``X-Tableau-Auth`` after sign-in.

    That token is not the PAT secret, but it authorizes the same Tableau session. The status and
    response shape must survive for diagnosis; only the credential value is removed.
    """

    token = "SENTINEL_SESSION_TOKEN_FULL_PERMISSION"

    class EchoHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = f"status=400 X-Tableau-Auth: {self.headers.get('X-Tableau-Auth')} path={self.path}".encode()
            self.send_response(400)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), EchoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        session = oracle.TableauSession(
            oracle.SiteCredentials(
                base=f"http://127.0.0.1:{server.server_port}",
                site="site",
                pat_name="pat-name",
                pat_secret="PAT_SECRET_1234567890",
                version="3.29",
            ),
            oracle.RetryPolicy(max_attempts=1, budget_sec=1),
        )
        session.token, session.site_id = token, "site-id"

        with pytest.raises(RuntimeError) as excinfo:
            session.get_json("/sites/site-id/views")
        assert token not in str(excinfo.value)
        assert "HTTP 400" in str(excinfo.value)
        assert "X-Tableau-Auth: [REDACTED]" in str(excinfo.value)

        record = oracle.capture_view(
            session,
            {
                "id": "eb00995d-1ff1-4a42-9ac9-28846f861d31",
                "name": "Echo",
                "workbook": {"id": "wb", "name": "Workbook"},
            },
            tmp_path,
            frozenset(),
        )
        oracle.write_manifest(
            [record],
            oracle.CaptureRun(
                session,
                {"TABLEAU_SERVER_URL": "http://example", "TABLEAU_SITE": "site", "TABLEAU_REST_API_VERSION": "3.29"},
                tmp_path,
                0.0,
            ),
        )
        manifest = (tmp_path / "oracle-manifest.json").read_text(encoding="utf-8")
        assert token not in manifest
        assert "HTTP 400" in manifest
        assert "X-Tableau-Auth: [REDACTED]" in manifest
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------- payload handling


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["19.5%", "-13.8%"], "percent"),
        (["$12", "$1,204"], "currency"),
        (["1,204", "35,777"], "thousands_separated"),
        (["12", "13"], None),
        ([], None),
    ],
)
def test_display_formatting_is_detected(values, expected):
    """/views/{id}/data returns display-formatted text, so a naive numeric diff would be garbage."""
    assert payload_facts.detect_format(values) == expected


def test_csv_summary_proves_a_capture_is_non_empty():
    summary = oracle.summarise_csv(b"Region,Profit Ratio\nWest,19.5%\nEast,-3.0%\n")
    assert summary["row_count"] == 2
    assert summary["columns"] == ["Region", "Profit Ratio"]
    assert summary["format_hints"] == {"Profit Ratio": "percent"}


def test_empty_csv_reports_zero_rows_rather_than_failing():
    assert oracle.summarise_csv(b"")["row_count"] == 0


def test_artifact_paths_are_built_only_from_a_verified_luid(tmp_path):
    """Replaces `test_slug_is_filesystem_safe`, because `safe_slug` is gone rather than fixed.

    A view NAME is response data. Slugging one into a filename truncated a reflected session token
    into a prefix no redactor could then match (#405 round 6), so the name no longer reaches a path at
    all: the only input is a LUID, whose UUID shape is verifiable in full. An identifier that is not a
    LUID is refused rather than sanitised -- sanitising is the screen this replaces.
    """
    assert oracle.artifact_stem("EB00995D-1FF1-4A42-9AC9-28846F861D31") == "eb00995d-1ff1-4a42-9ac9-28846f861d31"
    for bad in ("view-id-12345678", "", "../../etc/passwd", "eb00995d-1ff1-4a42-9ac9-28846f861d3"):
        with pytest.raises(ValueError):
            oracle.artifact_stem(bad)


def test_a_view_whose_identifier_is_not_a_luid_is_refused_not_named(tmp_path):
    session = FakeSession([(200, "a\n1\n", {})])
    record = oracle.capture_view(session, {"id": "not-a-luid", "name": "V"}, tmp_path, frozenset())
    assert record["data"]["status"] == "failed"
    assert not list(tmp_path.rglob("*.csv"))


def test_a_luid_that_IS_one_of_our_credentials_is_refused_too(tmp_path):
    """Closes the one residual the LUID allowlist leaves on its own.

    The shape check alone says "this is a UUID", not "this is not a credential". Measured against the
    live site, none of ours could pass it -- PAT secret, PAT name and session token are 57/20/92
    characters and none is hex-and-dash-only -- so this is belt and braces. It is what makes the claim
    unconditional rather than true-of-today's-credentials.
    """
    luid = "eb00995d-1ff1-4a42-9ac9-28846f861d31"
    session = FakeSession([(200, "a\n1\n", {})])
    session.token = luid
    record = oracle.capture_view(session, {"id": luid, "name": "V"}, tmp_path, frozenset())
    assert record["data"]["status"] == oracle.CREDENTIAL_REFLECTED
    assert not list(tmp_path.rglob("*.csv"))


# ------------------ #405 round 3, finding 2: the api override changed a signature every double copies


def test_the_request_contract_carries_the_api_override():
    """``export``/``raw_get`` pass ``api`` on EVERY call, so it is part of ``_request``'s contract."""
    parameters = inspect.signature(oracle.TableauSession._request).parameters  # pylint: disable=protected-access
    assert "api" in parameters
    assert parameters["api"].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.parametrize("api", [None, "3.29"])
def test_export_and_raw_get_forward_the_api_override(api):
    """Including ``None``. Passing the keyword unconditionally is the deliberate choice: the
    alternative -- omitting it when there is no override -- lets a stale adapter work for ordinary
    exports and crash only on the rare floor-re-probe path, which is the failure arriving LATE."""
    seen: list[str | None] = []

    class _Recording(FakeSession):
        def _request(self, method, path, *, body=None, accept=None, authed=True, api=None, deadline=None):  # noqa: ARG002
            seen.append(api)
            return super()._request(method, path, body=body, accept=accept, authed=authed, api=api, deadline=deadline)

    session = _Recording([(200, "a\n1\n", {}), (200, b"x", {})])
    session.export("/views/x/data", api=api)
    session.raw_get("/views/x/image?format=svg", api=api)
    assert seen == [api, api]


def test_every_scripted_session_double_in_this_suite_accepts_every_pass_through_keyword():
    """The gate that would have caught the red CI without running the file.

    ``TimedSession`` in ``test_capture_tableau_oracle_retry_budget.py`` overrides ``_request`` with
    the PREVIOUS signature, so five ordinary-export tests died on ``unexpected keyword argument
    'api'`` -- invisible to a test selection made from the changed *source* files, because that
    double exercises the same class through a subclass in a differently-named module. The adapter set
    is closed (all under ``tests/``), so updating them is right; this keeps it closed.

    ⚠️ The keyword list is DERIVED from the real method, not hand-maintained. It was hand-maintained
    and named only ``api``; ``deadline`` was then added to the production signature and thirteen
    doubles went stale at once, which this gate could not see because nobody remembered to widen it.

    ⚠️ And the comparison is by EXACT keyword-only NAME, parsed with ``ast`` -- not by substring, and
    not by regex over the parameter text. Measured: a double declaring ``api_version=None`` and
    ``deadline_seconds=None`` SATISFIED the substring form for both ``api`` and ``deadline``,
    reporting nothing stale, while calling it with the production arguments raised
    ``TypeError: unexpected keyword argument 'api'`` immediately. Deriving the right names and then
    comparing them wrongly is the same shape as the defect it was written to catch: the guard names
    the keyword and matches a prefix of it.

    A double that takes ``**kwargs`` genuinely accepts everything, so it is exempt -- but only if it
    really declares one, which the AST can tell and a regex cannot.
    """
    real = inspect.signature(oracle.TableauSession._request)  # pylint: disable=protected-access
    expected = {
        name
        for name, parameter in real.parameters.items()
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY and parameter.default is not inspect.Parameter.empty
    }
    assert expected, "the real _request has no optional keywords, so this gate proves nothing"

    overrides = []
    for path in sorted(Path(__file__).resolve().parent.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != "_request":
                continue
            positional = [arg.arg for arg in node.args.posonlyargs + node.args.args]
            if positional[:1] != ["self"]:
                continue  # a module-level `_request` fake for another script, not a session double
            accepted = {arg.arg for arg in node.args.kwonlyargs}
            overrides.append((f"{path.name}:{node.lineno}", accepted, node.args.kwarg is not None))

    assert overrides, "the scan found no session doubles at all -- it has stopped testing anything"
    stale = [
        f"{where} (missing {sorted(expected - accepted)})"
        for where, accepted, takes_kwargs in overrides
        if not takes_kwargs and expected - accepted
    ]
    assert not stale, f"session double(s) missing a pass-through keyword, so every export through them raises: {stale}"


def test_the_double_gate_rejects_a_merely_similar_keyword():
    """Positive control for the exactness above, built from the reproduction that broke it.

    ``api_version`` contains ``api``; ``deadline_seconds`` contains ``deadline``. Under the substring
    comparison this double was reported clean. It must now be reported stale, and a ``**kwargs`` double
    must still be accepted -- otherwise "exact" would just mean "stricter", and the exemption that
    makes the gate usable would be gone.
    """
    similar = (
        "class D:\n"
        "    def _request(self, method, path, *, body=None, api_version=None, deadline_seconds=None):\n"
        "        pass\n"
    )
    catchall = "class D:\n    def _request(self, method, path, *, body=None, **kwargs):\n        pass\n"

    def accepted_by(source: str) -> tuple[set[str], bool]:
        node = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef))
        return {arg.arg for arg in node.args.kwonlyargs}, node.args.kwarg is not None

    names, takes_kwargs = accepted_by(similar)
    assert "api" not in names and "deadline" not in names, "the control no longer differs from the real names"
    assert not takes_kwargs
    assert "api_version" in names, "the control must actually declare the confusable name"

    names, takes_kwargs = accepted_by(catchall)
    assert takes_kwargs, "a **kwargs double must remain exempt, or the exemption is untested"


# --------------------------------------------------------------------------- manifest contract


def _record(status: str, rows: int = 1, image_status: str | None = None, columns: list[str] | None = None) -> dict:
    data = {"status": status}
    if status == "ok":
        # ⚠️ `certification` first, because since #480 round 3 it is what makes this record a
        # measured one: a `row_count` written without it is the legacy shape, which is unassessable
        # by construction. A fixture that meant "a normal successful capture" must say so.
        data.update({"certification": "certified", "row_count": rows, "elapsed_sec": 1.0, "reauths": 0, "retries": 0})
        # ⚠️ Only when asked for. A real `_capture_data` always merges `summarise_csv`, so `columns`
        # is always present in the field -- but an OLDER manifest predates that, and the default
        # here keeps at least one path exercising a record that never recorded a header.
        if columns is not None:
            data["columns"] = columns
    else:
        data["detail"] = "adb.example.net: Tableau needs an unexpired OAuth refresh token"
    record = {"view_name": "V", "workbook_name": "W", "data": data}
    if image_status is not None:
        image = {"status": image_status}
        if image_status != "ok":
            image["detail"] = "image export failed"
        record["image"] = image
    return record


def _write(tmp_path, records):
    session = oracle.TableauSession(_creds())
    env = {"TABLEAU_SERVER_URL": "https://x", "TABLEAU_SITE": "s", "TABLEAU_REST_API_VERSION": "3.29"}
    return oracle.write_manifest(records, oracle.CaptureRun(session, env, tmp_path, 0.0))


def test_a_clean_capture_exits_zero(tmp_path):
    """The baseline: nothing to report, nothing for a caller to act on."""
    assert _write(tmp_path, [_record("ok")]) == 0


def test_a_clean_image_capture_exits_zero(tmp_path):
    """A data+image run is successful only when the image succeeded too."""
    assert _write(tmp_path, [_record("ok", image_status="ok")]) == 0


def test_zero_selected_views_exits_four(tmp_path):
    """Selecting nothing is a failed invocation, not a successful capture of an empty estate."""
    assert _write(tmp_path, []) == 4


def test_every_image_failed_exits_three(tmp_path):
    """Data success alone is not enough when the requested reference images all failed."""
    assert _write(tmp_path, [_record("ok", image_status="failed"), _record("ok", image_status="failed")]) == 3


def test_partial_image_success_exits_one(tmp_path):
    """A caller must be able to distinguish a partial reference-image set from a clean capture."""
    assert _write(tmp_path, [_record("ok", image_status="ok"), _record("ok", image_status="failed")]) == 1


def test_a_credential_block_exits_two_not_one(tmp_path):
    """Exit 2 is distinct on purpose: it means 'a human must act', not 'the tool is broken'."""
    assert _write(tmp_path, [_record("ok"), _record("source_credential")]) == 2


def test_partial_hard_failure_outranks_a_credential_block(tmp_path):
    """A partly broken run must not be downgraded to 'just needs a credential' by a co-occurring block."""
    assert _write(tmp_path, [_record("ok"), _record("failed"), _record("source_credential")]) == 1


def test_manifest_records_recovery_counts(tmp_path):
    """Recovery must be legible after the fact -- a healed run and a clean run must not look alike."""
    _write(tmp_path, [_record("ok")])
    manifest = json.loads((tmp_path / "oracle-manifest.json").read_text(encoding="utf-8"))
    assert "total_reauths" in manifest
    assert "total_retries" in manifest
    assert manifest["credential_blocked"] == 0


# --- #471: a zero-row capture is COUNTED, NAMED, FLAGGED and WARNED -- and is not a failure ------
#
# Reported by SES from a live run against a 94-view site: 12 views came back with no data rows, every
# one recorded `status: "ok"`, indistinguishable in the manifest from a view that returned 900,000.
# The count already existed; the LIST was built and immediately reduced to its `len()`, so a fidelity
# reviewer could learn that twelve views were empty and never which twelve.
#
# The tests below pin four separable properties, because a fix could satisfy any one and miss the
# others: the views are NAMED, the fact is on the RECORD, the console is LOUD, and none of it moves
# the EXIT CODE. The last is the regression risk: `status` drives the exit code and the
# blocked/failed partitions, and "just mark it failed" would turn a legible run into a broken one.


def _manifest(tmp_path, records) -> tuple[int, dict]:
    """Both halves of the contract at once: the exit code AND the manifest that explains it."""
    code = _write(tmp_path, records)
    return code, json.loads((tmp_path / "oracle-manifest.json").read_text(encoding="utf-8"))


def _named_manifest(tmp_path, records) -> tuple[int, dict]:
    """As above, but through a session whose credentials cannot collide with a manifest field NAME.

    ⚠️ Not fussiness. The shared `_creds()` sets ``pat_name="name"``, and the sink redacts dict KEYS
    as well as values -- so with that session every ``view_name`` key comes back as
    ``view_[REDACTED]`` and any assertion about a NAMED view silently tests the redactor instead of
    the feature. Measured while writing these tests, on the first assertion that read a name back.
    """
    session = oracle.TableauSession(
        oracle.SiteCredentials(
            base="https://example.online.tableau.com",
            site="site",
            pat_name="oracle-empty-pat-name",
            pat_secret="oracle-empty-pat-secret",
            version="3.29",
        )
    )
    env = {"TABLEAU_SERVER_URL": "https://x", "TABLEAU_SITE": "s", "TABLEAU_REST_API_VERSION": "3.29"}
    code = verdict.write_manifest(records, verdict.CaptureRun(session, env, tmp_path, 0.0))
    manifest = json.loads((tmp_path / "oracle-manifest.json").read_text(encoding="utf-8"))
    assert manifest["credential_scrubbed_at_sink"] == [], "the sink fired, so a name assertion below proves nothing"
    return code, manifest


def test_an_empty_capture_is_named_not_merely_counted(tmp_path):
    """The defect itself. `data_empty: 12` is unactionable; a reviewer needs the twelve NAMES.

    Same shape as `render_unestablished_views`, and for the same stated reason: the count answers
    "how much of my oracle is empty", the list answers "on which views can I not make a finding".
    """
    empty = _record("ok", rows=0, columns=["Region", "Sales"])
    empty["view_name"] = "Real Time Availability"
    empty["workbook_name"] = "Network Ops"
    _code, manifest = _named_manifest(tmp_path, [_record("ok"), empty, _record("ok")])

    assert manifest["data_empty"] == 1
    named = manifest["data_empty_views"]
    assert [entry["view_name"] for entry in named] == ["Real Time Availability"]
    assert named[0]["workbook_name"] == "Network Ops"
    assert named[0]["classification"] == verdict.EMPTY_QUERY_NO_ROWS


def test_a_capture_with_rows_is_never_named_empty(tmp_path):
    """Positive control: the list must be able to come back EMPTY, or it is a view count renamed."""
    _code, manifest = _manifest(tmp_path, [_record("ok", rows=5), _record("ok", rows=1)])
    assert manifest["data_empty"] == 0
    assert manifest["data_empty_views"] == []


def test_the_named_list_and_the_count_describe_the_same_views(tmp_path):
    """One predicate, or a count and a list eventually disagree about the same capture."""
    records = [_record("ok", rows=n, columns=["c"] if n else []) for n in (0, 3, 0, 0, 7)]
    _code, manifest = _manifest(tmp_path, records)
    assert manifest["data_empty"] == 3
    assert len(manifest["data_empty_views"]) == manifest["data_empty"]


def test_a_header_with_no_rows_is_classified_as_a_real_query(tmp_path):
    """A CSV that names its columns PROVES a query ran and returned a shape. No fieldless sheet can."""
    _code, manifest = _manifest(tmp_path, [_record("ok", rows=0, columns=["Region", "Sales", "Profit"])])
    assert manifest["data_empty_views"][0]["classification"] == verdict.EMPTY_QUERY_NO_ROWS


def test_a_payload_with_no_header_at_all_reads_as_CANNOT_CLASSIFY(tmp_path):
    """The correction that narrows the defect: 2 of the 14 flagged views were glossary sheets.

    ⚠️ Measured over `summarise_csv`, a 0-byte body (a sheet with no underlying query) and a 2-byte
    CRLF body (a real query returning nothing) BOTH land on `row_count=0, columns=[]` -- they differ
    only in `bytes`, and "glossary means 0 bytes" is one site's observation at n=14, not a Tableau
    contract. So this case must read as UNCLASSIFIED, never as either cause.
    """
    _code, manifest = _manifest(tmp_path, [_record("ok", rows=0, columns=[])])
    assert manifest["data_empty_views"][0]["classification"] == verdict.EMPTY_CANNOT_CLASSIFY


def test_the_byte_count_is_not_consulted_as_the_discriminator(tmp_path):
    """The forbidden heuristic, pinned: 0 bytes and 2 bytes must reach the SAME verdict.

    Both are `row_count=0, columns=[]` in a real capture. A fix that separated them by `bytes` would
    look right against the reporting site and be wrong at the first site with a different export.
    """
    glossary = _record("ok", rows=0, columns=[])
    glossary["data"]["bytes"] = 0
    blank = _record("ok", rows=0, columns=[])
    blank["data"]["bytes"] = 2
    _code, manifest = _manifest(tmp_path, [glossary, blank])
    classes = {entry["classification"] for entry in manifest["data_empty_views"]}
    assert classes == {verdict.EMPTY_CANNOT_CLASSIFY}


def test_an_empty_capture_keeps_status_ok_and_does_not_move_the_exit_code(tmp_path):
    """⚠️ The regression risk. `status` drives the exit code AND the blocked/failed partitions.

    The HTTP call succeeded, so an empty capture is a DIAGNOSTIC. A run that is otherwise clean must
    still exit 0 -- overloading `status` would make an operator debug the transport for a view whose
    filter is simply pointed at a day with no data.
    """
    code, manifest = _manifest(tmp_path, [_record("ok", rows=0, columns=[]), _record("ok", rows=0, columns=["c"])])
    assert code == 0
    assert [view["data"]["status"] for view in manifest["views"]] == ["ok", "ok"]
    assert manifest["failed"] == 0
    assert manifest["credential_blocked"] == 0
    assert manifest["data_ok"] == 2
    assert manifest["captured_complete"] == 2


@pytest.mark.parametrize(
    ("sibling", "expected"),
    [(None, 0), ("source_credential", 2), ("failed", 1)],
)
def test_an_empty_capture_never_changes_the_code_a_run_would_otherwise_exit(tmp_path, sibling, expected):
    """Before/after, across every code an empty view could plausibly disturb.

    Each pair is the SAME run with and without an empty view beside it, so the assertion is about the
    empty view's contribution rather than about the codes themselves.
    """
    others = [_record("ok")] + ([_record(sibling)] if sibling else [])
    assert _write(tmp_path, others) == expected, "the baseline moved -- this test proves nothing"
    assert _write(tmp_path, [*others, _record("ok", rows=0, columns=[])]) == expected


def test_the_empty_fact_rides_on_the_view_record_as_a_flag(tmp_path):
    """A per-view flag, so every downstream SLICE of the capture carries it, not just the manifest.

    Deliberately not a new `status` value and not a mutation of the leg record: a consumer asking
    "did this view's export succeed" and one asking "does this view carry evidence" are two
    questions, and this answers the second without corrupting the first.
    """
    _code, manifest = _manifest(tmp_path, [_record("ok", rows=0, columns=["c"]), _record("ok", rows=4)])
    empty_view, full_view = manifest["views"]
    assert empty_view["flags"] == [verdict.FLAG_DATA_EMPTY, verdict.EMPTY_QUERY_NO_ROWS]
    assert "flags" not in full_view


def test_flagging_does_not_mutate_the_caller_s_records(tmp_path):
    """`write_manifest` is handed the live list the capture loop built; it does not own it."""
    records = [_record("ok", rows=0, columns=["c"])]
    _write(tmp_path, records)
    assert "flags" not in records[0]


def test_an_older_record_with_no_row_count_is_not_claimed_empty(tmp_path):
    """Backwards compatibility, and the reason it matters: absence is not a zero.

    A record whose data leg recorded no `row_count` never measured emptiness, so claiming it as an
    empty capture would invent a diagnostic. The predicate this replaces raised `KeyError` on it.

    ⚠️ **Not claimed empty is not the same as claimed clean, and PR #480 round 1 shipped the second.**
    Returning `None` here was read downstream as "not empty", so the view was reported successful,
    evidence-complete, unflagged and unnamed -- identical to a capture that returned 900,000 rows.
    The `KeyError` this replaced was at least fail-closed. So the assertions below are in two halves:
    it must not be counted EMPTY, and it must not be counted CLEAN either.
    """
    old = {"view_name": "V", "workbook_name": "W", "data": {"status": "ok"}}
    code, manifest = _named_manifest(tmp_path, [old])
    assert code == 3
    assert manifest["data_empty"] == 0
    assert manifest["data_empty_views"] == []
    assert manifest["views"][0]["flags"] == [verdict.FLAG_DATA_UNASSESSABLE, verdict.UNASSESSABLE_NO_ROW_COUNT]
    assert manifest["data_unassessable"] == 1
    assert [entry["view_name"] for entry in manifest["data_unassessable_views"]] == ["V"]
    assert manifest["data_unassessable_views"][0]["reason"] == verdict.UNASSESSABLE_NO_ROW_COUNT
    assert manifest["captured_complete"] == 0, "a view nothing measured is not evidence-complete"
    assert manifest["data_ok"] == 1, "the transport DID succeed -- collapsing that loses a real distinction"


def test_a_failed_data_leg_is_not_ALSO_reported_as_empty(tmp_path):
    """One root cause, counted once. A failed export's emptiness is explained by the failure."""
    _code, manifest = _manifest(tmp_path, [_record("failed"), _record("source_credential")])
    assert manifest["data_empty"] == 0
    assert manifest["data_empty_views"] == []


# --- #480 finding 1: a missing `row_count` must not be clean evidence -------------------------
#
# The blind review's reproduction record, verbatim, driven through the capture manifest. The other
# two consumers it names are covered where they live: `subset_manifest()` in
# `test_group_oracle_by_workbook.py` and `_scope_oracle_manifest()` in `test_package_unit.py`.
#
# ⚠️ The defect is one level UP from #471, and that is the whole point: this PR fixed "a zero-row
# capture reads as ok" and introduced "an UNASSESSABLE capture reads as ok". Before the fix this
# record produced `exit=0 status=ok data_ok=1 captured_complete=1 data_empty=0 data_empty_views=[]`
# with no flags -- byte for byte what a 900,000-row capture produces.

REVIEW_RECORD = {"view_name": "V", "workbook_name": "W", "data": {"status": "ok", "columns": ["Region"]}}


def test_a_row_count_that_was_never_recorded_is_UNASSESSABLE_not_clean(tmp_path):
    """The reproduction record. Every observed value in the reviewer's table, asserted."""
    code, manifest = _named_manifest(tmp_path, [dict(REVIEW_RECORD)])
    assert code == 3, "unassessable numeric evidence is a non-pass, even though the transport succeeded"
    assert manifest["views"][0]["data"]["status"] == "ok", "the HTTP call DID succeed -- keep that distinction"
    assert manifest["data_ok"] == 1
    assert manifest["captured_complete"] == 0, "was 1 -- an unmeasured view was counted evidence-complete"
    assert manifest["data_empty"] == 0, "absence is not a zero; it must not be claimed empty either"
    assert manifest["data_empty_views"] == []
    assert manifest["data_unassessable"] == 1
    assert manifest["views"][0]["flags"] == [verdict.FLAG_DATA_UNASSESSABLE, verdict.UNASSESSABLE_NO_ROW_COUNT]
    named = manifest["data_unassessable_views"]
    assert [entry["view_name"] for entry in named] == ["V"]
    assert named[0]["reason"] == verdict.UNASSESSABLE_NO_ROW_COUNT


def test_a_capture_with_rows_is_never_called_unassessable(tmp_path):
    """Positive control: the third state must be able to come back EMPTY, or it names every view."""
    _code, manifest = _manifest(tmp_path, [_record("ok", rows=5), _record("ok", rows=0, columns=["c"])])
    assert manifest["data_unassessable"] == 0
    assert manifest["data_unassessable_views"] == []
    assert manifest["captured_complete"] == 2, "a measured zero is still a measurement"


def test_a_failed_data_leg_is_not_reported_unassessable_either(tmp_path):
    """Same one-root-cause rule as the empty half: a failure explains its own missing row count."""
    _code, manifest = _manifest(tmp_path, [_record("failed"), _record("source_credential")])
    assert manifest["data_unassessable"] == 0
    assert manifest["data_unassessable_views"] == []


def test_an_unassessable_capture_makes_an_otherwise_clean_exit_partial(tmp_path):
    """Unassessable numeric evidence is retained, but automation must not read the run as clean."""
    assert _write(tmp_path, [_record("ok")]) == 0, "the baseline moved -- this test proves nothing"
    assert _write(tmp_path, [_record("ok"), dict(REVIEW_RECORD)]) == 1


def test_the_per_view_line_for_an_unassessable_capture_is_a_WARNING_and_does_not_raise(caplog):
    """⚠️ The ordinary line interpolates `data["row_count"]`, which this record does not have.

    So this is two claims at once: the line must be a WARNING naming the reason, and printing it
    must not raise the `KeyError` that would take the whole run down at the console.
    """
    caplog.set_level(logging.INFO, logger="tableau-oracle")
    verdict.log_progress(3, 94, dict(REVIEW_RECORD))
    (line,) = caplog.records
    assert line.levelno == logging.WARNING
    assert verdict.UNASSESSABLE_NO_ROW_COUNT in line.getMessage()
    assert "0 rows" not in line.getMessage(), "printing a zero here is the invented measurement, again"


def test_the_run_end_block_names_the_unassessable_views_and_what_they_cost(tmp_path, caplog):
    """A count says how much evidence is missing, never which page a reviewer must open by hand."""
    caplog.set_level(logging.INFO, logger="tableau-oracle")
    record = {**REVIEW_RECORD, "view_name": "Metrics Dictionary"}
    _named_manifest(tmp_path, [record])
    warnings = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "Metrics Dictionary" in warnings
    assert "NOT BE ASSESSED" in warnings


def test_the_unassessable_run_end_block_is_silent_when_everything_was_measured(tmp_path, caplog):
    """Control: a diagnostic that fires on a clean run is one an operator learns to skip."""
    caplog.set_level(logging.INFO, logger="tableau-oracle")
    _write(tmp_path, [_record("ok", rows=7)])
    assert "NOT BE ASSESSED" not in "\n".join(r.getMessage() for r in caplog.records)


def test_a_view_name_is_redacted_before_the_unassessable_diagnostic_prints_it(tmp_path, caplog):
    """The same seam as the empty block: a view NAME is response data and CI keeps its logs."""
    token = "tableau-session-token-value-that-must-not-print"
    session = oracle.TableauSession(
        oracle.SiteCredentials(
            base="https://x", site="s", pat_name="pat-name-here", pat_secret="pat-secret-here", version="3.29"
        )
    )
    session.token = token
    record = {**REVIEW_RECORD, "view_name": token, "workbook_name": token}
    caplog.set_level(logging.INFO, logger="tableau-oracle")

    verdict.log_progress(1, 1, record, session.redact_text)
    env = {"TABLEAU_SERVER_URL": "https://x", "TABLEAU_SITE": "s", "TABLEAU_REST_API_VERSION": "3.29"}
    verdict.write_manifest([record], verdict.CaptureRun(session, env, tmp_path, 0.0))

    printed = "\n".join(r.getMessage() for r in caplog.records)
    assert "NOT BE ASSESSED" in printed, "the run-end block did not fire, so this proves nothing"
    assert "UNASSESSABLE" in printed, "the per-view line did not fire, so this proves nothing"
    assert token not in printed
    assert token not in (tmp_path / "oracle-manifest.json").read_text(encoding="utf-8")


def test_a_row_count_of_True_is_a_corrupt_record_not_a_measurement_of_one_row(tmp_path):
    """`isinstance(True, int)` is true in Python, so the bool exclusion is load-bearing, not style."""
    record = {"view_name": "V", "workbook_name": "W", "data": {"status": "ok", "row_count": True}}
    _code, manifest = _named_manifest(tmp_path, [record])
    assert manifest["data_unassessable"] == 1
    assert manifest["captured_complete"] == 0


def test_the_per_view_line_for_an_empty_capture_is_a_WARNING(caplog):
    """`0 rows` inside an INFO line among 94 is technically visible and practically invisible."""
    caplog.set_level(logging.INFO, logger="tableau-oracle")
    verdict.log_progress(3, 94, _record("ok", rows=0, columns=[]))
    (line,) = caplog.records
    assert line.levelno == logging.WARNING
    assert verdict.EMPTY_CANNOT_CLASSIFY in line.getMessage()


def test_the_per_view_line_for_a_normal_capture_stays_an_INFO(caplog):
    """Control for the line above: a WARN on every view is a WARN nobody reads."""
    caplog.set_level(logging.INFO, logger="tableau-oracle")
    verdict.log_progress(3, 94, _record("ok", rows=900_000))
    (line,) = caplog.records
    assert line.levelno == logging.INFO


def test_the_run_end_block_names_the_empty_views_and_what_they_cost(tmp_path, caplog):
    """A count in the summary line says how much evidence is missing, never which page to open."""
    caplog.set_level(logging.INFO, logger="tableau-oracle")
    empty = _record("ok", rows=0, columns=[])
    empty["view_name"] = "Metrics Dictionary"
    _named_manifest(tmp_path, [empty])
    warnings = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "Metrics Dictionary" in warnings
    assert "ZERO DATA ROWS" in warnings
    # The unclassifiable case must SAY it is unclassifiable, rather than landing silently in a bucket.
    assert "NO HEADER" in warnings


def test_the_run_end_block_is_silent_when_nothing_is_empty(tmp_path, caplog):
    """Control: a diagnostic that fires on a clean run is one an operator learns to skip."""
    caplog.set_level(logging.INFO, logger="tableau-oracle")
    _write(tmp_path, [_record("ok", rows=7)])
    assert "ZERO DATA ROWS" not in "\n".join(r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("\r\n", "empty_cannot_classify"),
        ("Region,Sales\r\n", "empty_query_no_rows"),
    ],
)
def test_a_REAL_empty_export_travels_the_whole_path_from_capture_to_manifest(tmp_path, payload, expected):
    """End to end over the PRODUCTION path, not a hand-built record.

    ⚠️ Every other test in this section constructs the record itself, which proves the verdict layer
    and says nothing about whether a real capture can produce its input. This one starts at
    `capture_view` with a scripted HTTP response, so `_capture_data` -> `summarise_csv` ->
    `empty_classification` is exercised as a chain. Both fixtures are shapes the reporting site
    actually returned: a 2-byte CRLF body, and a header with no rows.
    """
    session = FakeSession([(200, payload, {"Content-Type": "text/csv"})])
    view = {"id": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa", "name": "Real Time Availability", "workbook": {"id": "wb"}}
    record = oracle.capture_view(session, view, tmp_path, frozenset(), None)
    record["workbook_name"] = "Network Ops"

    assert record["data"]["status"] == "ok", "the export must SUCCEED, or this tests the failure path"
    assert record["data"]["row_count"] == 0
    _code, manifest = _named_manifest(tmp_path, [record])
    assert manifest["data_empty"] == 1
    assert manifest["data_empty_views"][0]["view_name"] == "Real Time Availability"
    assert manifest["data_empty_views"][0]["classification"] == expected
    assert manifest["views"][0]["flags"] == ["data_empty", expected]


# --- #480 finding 2: an HTTP 200 body is not evidence until it is certified as CSV ------------
#
# Every row of the blind review's second reproduction table, driven through the SAME production
# chain as the test above -- `capture_view` -> `export` -> `_capture_data` -> `certify_csv` -- with
# only the HTTP response scripted. Measured on this branch BEFORE the certification existed:
#
#   200 text/html  `<html>\n<body>Error</body>\n</html>\n`  -> ok, columns ["<html>"], row_count 2
#   200 text/csv   `Region,Sales\r\nWest,"unterminated`     -> ok, columns [Region, Sales], row_count 1
#   200 octet      `not CSV at all`                         -> ok, row_count 0, empty_query_no_rows
#
# The third is the worst of the three and the reason this is a certification rather than a sniff: it
# is not merely wrong, it is CONFIDENTLY wrong -- a specific diagnosis ("the query ran and returned
# no rows") about a payload never established to be CSV at all.

_UNCERTIFIABLE = [
    ("text/html", "<html>\n<body>Error</body>\n</html>\n", "content_type_not_csv"),
    ("text/csv", 'Region,Sales\r\nWest,"unterminated', "payload_malformed_csv"),
    ("application/octet-stream", "not CSV at all", "content_type_not_csv"),
]


@pytest.mark.parametrize(("content_type", "body", "certification"), _UNCERTIFIABLE)
def test_an_uncertifiable_200_is_never_recorded_as_rows(tmp_path, content_type, body, certification):
    """No row count, no header, no classification, and no file on disk claiming to be data."""
    session = FakeSession([(200, body, {"Content-Type": content_type})])
    view = {"id": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa", "name": "Real Time Availability", "workbook": {"id": "wb"}}
    record = oracle.capture_view(session, view, tmp_path, frozenset(), None)

    data = record["data"]
    assert data["status"] == "format_mismatch"
    assert data["certification"] == certification
    assert "row_count" not in data, "a row count read off an uncertified body is fiction with a number attached"
    assert "columns" not in data
    assert "path" not in data and not list((tmp_path / "data").glob("*.csv")), (
        "persisting a non-CSV body to data/<luid>.csv manufactures the evidence this capture prevents"
    )
    assert data["bytes"] == len(body.encode()), "the refusal must still say how much arrived"


@pytest.mark.parametrize(("content_type", "body", "_certification"), _UNCERTIFIABLE)
def test_an_uncertifiable_200_is_never_classified_from_its_first_line(tmp_path, content_type, body, _certification):
    """The manifest half: no empty classification, no clean bucket, and a LOUD verdict."""
    session = FakeSession([(200, body, {"Content-Type": content_type})])
    view = {"id": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa", "name": "Real Time Availability", "workbook": {"id": "wb"}}
    record = oracle.capture_view(session, view, tmp_path, frozenset(), None)
    record["workbook_name"] = "Network Ops"

    code, manifest = _named_manifest(tmp_path, [record])
    assert manifest["data_empty"] == 0
    assert manifest["data_empty_views"] == [], "the octet-stream row diagnosed `empty_query_no_rows` here"
    assert "flags" not in manifest["views"][0]
    assert manifest["data_ok"] == 0
    assert manifest["captured_complete"] == 0
    assert manifest["failed"] == 1
    assert code == 3, "an uncertifiable body is a failed capture, not a legible one"


def test_a_real_CSV_declared_as_CSV_is_still_certified_and_still_counted(tmp_path):
    """⚠️ The positive control. A gate that refuses everything is as useless as one that refuses
    nothing, and it would be invisible in the three tests above."""
    session = FakeSession([(200, "Region,Sales\r\nWest,10\r\nEast,20\r\n", {"Content-Type": "text/csv;charset=utf-8"})])
    view = {"id": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa", "name": "Real Time Availability", "workbook": {"id": "wb"}}
    record = oracle.capture_view(session, view, tmp_path, frozenset(), None)

    assert record["data"]["status"] == "ok"
    assert record["data"]["certification"] == payload_facts.CSV_CERTIFIED
    assert record["data"]["row_count"] == 2
    assert record["data"]["columns"] == ["Region", "Sales"]
    assert (tmp_path / record["data"]["path"]).is_file()
    _code, manifest = _named_manifest(tmp_path, [record])
    assert manifest["captured_complete"] == 1


@pytest.mark.parametrize(
    ("headers", "expected_framing"),
    [
        ({"Content-Type": "text/csv", "Content-Length": "32"}, "content_length"),
        ({"Content-Type": "text/csv", oracle.RESPONSE_FRAMING_HEADER: "chunked"}, "chunked"),
    ],
)
def test_a_csv_capture_is_evidence_only_when_transport_framing_can_detect_early_eof(
    tmp_path, headers, expected_framing
):
    """Content-Length and chunked framing are the two defended transport regimes for terminatorless CSV."""
    body = "Region,Sales\r\nWest,10\r\nEast,20\r\n"
    session = FakeSession([(200, body, headers)])
    view = {"id": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa", "name": "Real Time Availability", "workbook": {"id": "wb"}}
    record = oracle.capture_view(session, view, tmp_path, frozenset(), None)
    record["workbook_name"] = "Network Ops"

    data = record["data"]
    assert data["status"] == "ok"
    assert data["certification"] == payload_facts.CSV_CERTIFIED
    assert data["row_count"] == 2
    assert data["response_framing"] == expected_framing

    code, manifest = _named_manifest(tmp_path, [record])
    assert code == 0
    assert manifest["captured_complete"] == 1


def test_a_close_delimited_csv_capture_is_retained_but_not_numeric_evidence(tmp_path):
    """EOF-as-framing cannot distinguish a complete CSV from a truncated prefix, so it is unassessable."""
    body = "Region,Sales\r\nWest,10\r\nEast,20\r\n"
    session = CloseDelimitedFakeSession([(200, body, {"Content-Type": "text/csv"})])
    view = {"id": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa", "name": "Real Time Availability", "workbook": {"id": "wb"}}
    record = oracle.capture_view(session, view, tmp_path, frozenset(), None)
    record["workbook_name"] = "Network Ops"

    data = record["data"]
    assert data["status"] == "ok", "the HTTP request returned bytes; only numeric evidence is withheld"
    assert data["certification"] == payload_facts.CSV_TRANSPORT_CLOSE_DELIMITED
    assert data["response_framing"] == payload_facts.CSV_TRANSPORT_CLOSE_DELIMITED
    assert "row_count" not in data and "path" not in data
    assert data[verdict.RETAINED_PATH_KEY].startswith(f"{verdict.RETAINED_DIR}/")

    code, manifest = _named_manifest(tmp_path, [record])
    assert code == 3
    assert manifest["captured_complete"] == 0
    assert manifest["data_unassessable"] == 1
    assert manifest["data_unassessable_views"][0]["reason"] == payload_facts.CSV_TRANSPORT_CLOSE_DELIMITED
    assert _naive_numeric_consumer(tmp_path) == []


@pytest.mark.parametrize("content_length", ["", "abc", "-1"])
def test_an_invalid_content_length_does_not_count_as_csv_completeness_evidence(tmp_path, content_length):
    """An unusable length falls back to EOF-as-framing in Python, so it is not defended evidence."""
    body = "Region,Sales\r\nWest,10\r\n"
    session = CloseDelimitedFakeSession([(200, body, {"Content-Type": "text/csv", "Content-Length": content_length})])
    view = {"id": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa", "name": "Real Time Availability", "workbook": {"id": "wb"}}
    record = oracle.capture_view(session, view, tmp_path, frozenset(), None)

    data = record["data"]
    assert data["certification"] == payload_facts.CSV_TRANSPORT_INVALID_CONTENT_LENGTH
    assert data["response_framing"] == payload_facts.CSV_TRANSPORT_INVALID_CONTENT_LENGTH
    assert "row_count" not in data and "path" not in data


def test_loopback_exact_chunked_csv_is_decoded_and_counts_as_numeric_evidence(tmp_path):
    body = b"Region,Sales\r\nWest,10\r\n"
    wire = f"{len(body):X}\r\n".encode() + body + b"\r\n0\r\n\r\n"
    code, manifest = _loopback_capture(
        tmp_path,
        [("Content-Type", "text/csv"), ("Transfer-Encoding", "chunked")],
        wire,
    )

    data = manifest["views"][0]["data"]
    assert code == 0
    assert data["response_framing"] == "chunked"
    assert data["certification"] == payload_facts.CSV_CERTIFIED
    assert data["row_count"] == 1
    assert _naive_numeric_consumer(tmp_path)


def test_loopback_exact_content_length_csv_counts_as_numeric_evidence(tmp_path):
    body = b"Region,Sales\r\nWest,10\r\n"
    code, manifest = _loopback_capture(
        tmp_path,
        [("Content-Type", "text/csv"), ("Content-Length", str(len(body)))],
        body,
    )

    data = manifest["views"][0]["data"]
    assert code == 0
    assert data["response_framing"] == "content_length"
    assert data["certification"] == payload_facts.CSV_CERTIFIED
    assert data["row_count"] == 1
    assert _naive_numeric_consumer(tmp_path)


@pytest.mark.parametrize(
    ("headers", "wire_body", "reason"),
    [
        (
            [("Content-Type", "text/csv"), ("Transfer-Encoding", "gzip, chunked")],
            b"17\r\nRegion,Sales\r\nWest,10\r\n\r\n0\r\n\r\n",
            payload_facts.CSV_TRANSPORT_UNSUPPORTED_TRANSFER_ENCODING,
        ),
        (
            [("Content-Type", "text/csv"), ("Transfer-Encoding", "chunked, gzip")],
            b"17\r\nRegion,Sales\r\nWest,10\r\n\r\n0\r\n\r\n",
            payload_facts.CSV_TRANSPORT_UNSUPPORTED_TRANSFER_ENCODING,
        ),
        (
            [("Content-Type", "text/csv"), ("Transfer-Encoding", "chunked"), ("Transfer-Encoding", "chunked")],
            b"17\r\nRegion,Sales\r\nWest,10\r\n\r\n0\r\n\r\n",
            payload_facts.CSV_TRANSPORT_UNSUPPORTED_TRANSFER_ENCODING,
        ),
        (
            [
                ("Content-Type", "text/csv"),
                ("Content-Encoding", "gzip"),
                ("Content-Length", str(len(gzip.compress(b"Region,Sales\r\nWest,10\r\n")))),
            ],
            gzip.compress(b"Region,Sales\r\nWest,10\r\n"),
            payload_facts.CSV_TRANSPORT_UNSUPPORTED_CONTENT_ENCODING,
        ),
        (
            [("Content-Type", "text/csv"), ("Content-Length", "abc")],
            b"Region,Sales\r\nWest,10\r\n",
            payload_facts.CSV_TRANSPORT_INVALID_CONTENT_LENGTH,
        ),
        (
            [("Content-Type", "text/csv"), ("Content-Length", "5"), ("Content-Length", "23")],
            b"Region,Sales\r\nWest,10\r\n",
            payload_facts.CSV_TRANSPORT_CONFLICTING_CONTENT_LENGTH,
        ),
        (
            [("Content-Type", "text/csv")],
            b"Region,Sales\r\nWest,10\r\n",
            payload_facts.CSV_TRANSPORT_CLOSE_DELIMITED,
        ),
        (
            [("Content-Type", "text/csv")],
            b"Region,Sales\r\nWest",
            payload_facts.CSV_TRANSPORT_CLOSE_DELIMITED,
        ),
    ],
)
def test_loopback_unsupported_or_unknowable_csv_framing_is_retained_not_evidence(tmp_path, headers, wire_body, reason):
    code, manifest = _loopback_capture(tmp_path, headers, wire_body)

    data = manifest["views"][0]["data"]
    assert code == 3
    assert data["status"] == "ok"
    assert data["certification"] == reason
    assert "row_count" not in data and "path" not in data
    assert data[verdict.RETAINED_PATH_KEY].startswith(f"{verdict.RETAINED_DIR}/")
    assert manifest["captured_complete"] == 0
    assert manifest["data_unassessable"] == 1
    assert manifest["data_unassessable_views"][0]["reason"] == reason
    assert _naive_numeric_consumer(tmp_path) == []


@pytest.mark.parametrize("framing", ["Content-Length", "Transfer-Encoding: chunked"])
def test_an_early_eof_csv_capture_is_a_failed_manifest_not_unassessable_evidence(tmp_path, framing):
    """The transport catches truncation under defended framing before `_capture_data` can write bytes."""
    retry = oracle.RetryPolicy(max_attempts=1, budget_sec=1)
    session = FakeSession([(oracle.NETWORK_ERROR_STATUS, f"IncompleteRead from {framing}", {})], retry=retry)
    view = {"id": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa", "name": "Real Time Availability", "workbook": {"id": "wb"}}
    record = oracle.capture_view(session, view, tmp_path, frozenset(), None)
    record["workbook_name"] = "Network Ops"

    assert record["data"]["status"] == "transient"
    assert "path" not in record["data"] and not list((tmp_path / "data").glob("*.csv"))

    code, manifest = _named_manifest(tmp_path, [record])
    assert code == 3
    assert manifest["failed"] == 1
    assert manifest["captured_complete"] == 0


def test_a_200_with_no_Content_Type_is_kept_but_reported_UNASSESSABLE(tmp_path):
    """The seam between the two findings, and the honest answer to "we cannot certify this".

    A CSV carries no signature -- there is no byte sequence that PROVES a body is CSV the way
    `%PDF-` proves a PDF -- so with nothing declared, nothing establishes these bytes as data. The
    transport did succeed, so the bytes are kept and the status stays `ok`; what is refused is the
    EVIDENCE, so no row count is recorded, the verdict layer reports it rather than counting it, and
    the bytes are named `retained_path` OUTSIDE `data/` so no consumer can read them as numbers.
    """
    session = FakeSession([(200, "Region,Sales\r\nWest,10\r\n", {})])
    view = {"id": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa", "name": "Real Time Availability", "workbook": {"id": "wb"}}
    record = oracle.capture_view(session, view, tmp_path, frozenset(), None)
    record["workbook_name"] = "Network Ops"

    assert record["data"]["status"] == "ok", "the HTTP call succeeded -- that distinction is kept"
    assert record["data"]["certification"] == payload_facts.CSV_CONTENT_TYPE_ABSENT
    assert "row_count" not in record["data"]
    assert "path" not in record["data"], "an uncertified body must not be named where evidence is read"
    retained = record["data"][verdict.RETAINED_PATH_KEY]
    assert (tmp_path / retained).is_file(), "the bytes are the customer's; only the claim is refused"
    assert not retained.startswith("data/") and not retained.endswith(".csv")

    code, manifest = _named_manifest(tmp_path, [record])
    assert code == 3, "unassessable numeric evidence is a non-pass, even though the transport succeeded"
    assert manifest["data_ok"] == 1
    assert manifest["captured_complete"] == 0
    assert manifest["data_unassessable"] == 1
    assert manifest["data_unassessable_views"][0]["reason"] == payload_facts.CSV_CONTENT_TYPE_ABSENT
    assert manifest["views"][0]["flags"] == [verdict.FLAG_DATA_UNASSESSABLE, payload_facts.CSV_CONTENT_TYPE_ABSENT]


def test_an_error_page_with_the_Content_Type_STRIPPED_is_still_refused(tmp_path):
    """The header-stripping proxy, which is the one case Content-Type alone cannot answer.

    A body opening a tag is a document, not a table -- and `<html>` otherwise parses as a one-column
    CSV with two rows, which is exactly how the reviewer's first row was recorded as data.
    """
    session = FakeSession([(200, "<html>\n<body>Error</body>\n</html>\n", {})])
    view = {"id": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa", "name": "Real Time Availability", "workbook": {"id": "wb"}}
    record = oracle.capture_view(session, view, tmp_path, frozenset(), None)
    assert record["data"]["status"] == "format_mismatch"
    assert record["data"]["certification"] == payload_facts.CSV_NOT_TABULAR
    assert "row_count" not in record["data"]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"Region,Sales\r\nWest,10\r\n", payload_facts.CSV_CERTIFIED),
        (b"", payload_facts.CSV_CERTIFIED),
        (b"\r\n", payload_facts.CSV_CERTIFIED),
        (b'Region,Sales\r\nWest,"quoted, value"\r\n', payload_facts.CSV_CERTIFIED),
        (b'Region,Notes\r\nWest,"quoted\r\nmultiline value"\r\n', payload_facts.CSV_CERTIFIED),
        (b'Size\r\n"5"" pipe"\r\n', payload_facts.CSV_CERTIFIED),
        (b"<html><body>Error</body></html>", payload_facts.CSV_NOT_TABULAR),
        (b'{"error": {"code": "401002"}}', payload_facts.CSV_NOT_TABULAR),
        (b'Region,Sales\r\nWest,"unterminated', payload_facts.CSV_MALFORMED),
        (b"Region,Sales\r\nWest,10,EXTRA\r\n", payload_facts.CSV_RAGGED),
    ],
)
def test_certify_csv_verdicts(body, expected):
    """The certifier alone, over the shapes that decide it. `text/csv` throughout, so this isolates
    the STRUCTURAL half from the declaration half -- an empty body and a bare CRLF are the two real
    zero-row exports and must stay certifiable, or #471's own fixtures become unreachable."""
    assert payload_facts.certify_csv(body, "text/csv") == expected


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("text/csv", payload_facts.CSV_CERTIFIED),
        ("text/csv; charset=utf-8", payload_facts.CSV_CERTIFIED),
        ("TEXT/CSV", payload_facts.CSV_CERTIFIED),
        ("application/csv", payload_facts.CSV_CERTIFIED),
        ("text/plain", payload_facts.CSV_CONTENT_TYPE_UNSPECIFIC),
        ("TEXT/PLAIN; charset=utf-8", payload_facts.CSV_CONTENT_TYPE_UNSPECIFIC),
        (None, payload_facts.CSV_CONTENT_TYPE_ABSENT),
        ("", payload_facts.CSV_CONTENT_TYPE_ABSENT),
        ("text/html", payload_facts.CSV_CONTENT_TYPE_NOT_CSV),
        ("application/json", payload_facts.CSV_CONTENT_TYPE_NOT_CSV),
        ("application/octet-stream", payload_facts.CSV_CONTENT_TYPE_NOT_CSV),
        ("application/pdf", payload_facts.CSV_CONTENT_TYPE_NOT_CSV),
    ],
)
def test_certify_csv_reads_the_declaration_over_a_well_formed_body(declared, expected):
    """The declaration half, isolated: the SAME well-formed CSV body under every declaration.

    ⚠️ This is the opposite ordering from `format_matches`, deliberately. There the payload is
    decisive because a PNG/PDF/SVG carries a signature; here it cannot be, so a server saying
    `text/html` is the strongest evidence available and is believed.
    """
    assert payload_facts.certify_csv(b"Region,Sales\r\nWest,10\r\n", declared) == expected


def test_certify_csv_never_echoes_the_payload_or_the_header():
    """The verdict is a closed vocabulary this repo authors, which is what makes it loggable."""
    secret = "SYNTHETIC_SECRET_42"
    for declared in (f"text/{secret}", None):
        verdict_out = payload_facts.certify_csv(f"{secret}\n{secret}\n".encode(), declared)
        assert verdict_out in payload_facts.CSV_VERDICTS
        assert secret not in verdict_out


def test_a_view_name_is_redacted_before_the_empty_diagnostic_prints_it(tmp_path, caplog):
    """⚠️ A view NAME is response data -- a reflected session token has arrived as one.

    Both new console surfaces are covered: the per-view WARN and the run-end block. The manifest sink
    scrubs the file; the console is the third artifact and CI keeps its logs.
    """
    token = "tableau-session-token-value-that-must-not-print"
    session = oracle.TableauSession(
        oracle.SiteCredentials(
            base="https://x", site="s", pat_name="pat-name-here", pat_secret="pat-secret-here", version="3.29"
        )
    )
    session.token = token
    record = _record("ok", rows=0, columns=[])
    record["view_name"] = token
    record["workbook_name"] = token
    caplog.set_level(logging.INFO, logger="tableau-oracle")

    verdict.log_progress(1, 1, record, session.redact_text)
    env = {"TABLEAU_SERVER_URL": "https://x", "TABLEAU_SITE": "s", "TABLEAU_REST_API_VERSION": "3.29"}
    verdict.write_manifest([record], verdict.CaptureRun(session, env, tmp_path, 0.0))

    printed = "\n".join(r.getMessage() for r in caplog.records)
    assert "ZERO DATA ROWS" in printed, "the run-end block did not fire, so this proves nothing"
    assert "NO DATA" in printed, "the per-view line did not fire, so this proves nothing"
    assert token not in printed
    assert token not in (tmp_path / "oracle-manifest.json").read_text(encoding="utf-8")


# --- #480 round 2: an uncertified capture must be IMPOSSIBLE to consume, not merely flagged ----
#
# Round 1 named the state honestly -- `certification`, `flags`, no `row_count`, a counted-and-named
# list -- and left the bytes at `data/<luid>.csv` under the same `path` key a certified capture uses.
# The blind review then found the same fail-open at a FOURTH consumer and under an ACCEPTED MIME
# type, which is the signature of a fix that flags a bad state and trusts every consumer to check the
# flag. Measured on the PR tip, through the production export path:
#
#   absent Content-Type -> status ok, path data/view.csv, flags [data_unassessable, ...]
#                          build_reconcile_items.build() -> item_count 1, tableau_value 10.0
#   200 text/plain      -> certification CERTIFIED, columns ["Service unavailable"], row_count 1
#   200 text/plain      -> single line: row_count 0 and DIAGNOSED `empty_query_no_rows`
#
# So these tests pin the STRUCTURE, not another flag: uncertified bytes are never written under
# `data/`, never suffixed `.csv`, and never named by `path`. A consumer that wants numbers asks for
# `path` and finds nothing -- with no knowledge of certification, flags, or this module.


def _naive_numeric_consumer(oracle_dir: Path) -> list[dict]:
    """A consumer written the way the FIFTH one will be, and deliberately not import-coupled to us.

    ⚠️ This is the test that makes the fix durable rather than a fourth patch. It reads the manifest
    raw, keeps every leg that says `status: ok` and names a `path`, and reads that file as numbers --
    exactly what `build_reconcile_items.build()`, `check_unit._oracle_capture_oracles`,
    `package_unit._copy_leg` and `group_oracle_by_workbook.copy_view_files` all do, and exactly what
    someone writing a new one tomorrow will do. It knows nothing about `certification`, `flags`,
    `row_count` or `data_unassessable`, and it must STILL find no evidence in an uncertified capture.
    """
    manifest = json.loads((oracle_dir / "oracle-manifest.json").read_text(encoding="utf-8"))
    found = []
    for view in manifest.get("views", []):
        data = view.get("data") or {}
        if data.get("status") != "ok" or not data.get("path"):
            continue
        found.append({"view": view.get("view_name"), "rows": (oracle_dir / data["path"]).read_text(encoding="utf-8")})
    return found


def _capture_one(tmp_path: Path, body: str, headers: dict) -> dict:
    """One view captured through the production chain, written to a real manifest on disk."""
    session = FakeSession([(200, body, headers)])
    view = {"id": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa", "name": "Real Time Availability", "workbook": {"id": "wb"}}
    record = oracle.capture_view(session, view, tmp_path, frozenset(), None)
    record["workbook_name"] = "Network Ops"
    _code, manifest = _named_manifest(tmp_path, [record])
    return manifest


@pytest.mark.parametrize(
    ("headers", "certification"),
    [
        ({}, payload_facts.CSV_CONTENT_TYPE_ABSENT),
        ({"Content-Type": "text/plain"}, payload_facts.CSV_CONTENT_TYPE_UNSPECIFIC),
    ],
)
def test_an_uncertified_capture_is_unreadable_by_a_consumer_that_never_heard_of_certification(
    tmp_path, headers, certification
):
    """Finding 1, structurally: a naive numeric consumer finds NOTHING in an uncertified capture."""
    manifest = _capture_one(tmp_path, "Region,Sales\r\nWest,10\r\n", headers)

    data = manifest["views"][0]["data"]
    assert data["status"] == "ok", "the transport succeeded and that distinction survives"
    assert data["certification"] == certification
    assert "path" not in data, "an uncertified body named under `path` is the whole fail-open"
    assert data[verdict.RETAINED_PATH_KEY].startswith(f"{verdict.RETAINED_DIR}/")
    assert data[verdict.RETAINED_PATH_KEY].endswith(verdict.RETAINED_SUFFIX)
    assert (tmp_path / data[verdict.RETAINED_PATH_KEY]).is_file(), "the bytes are retained, not deleted"
    assert not list(tmp_path.rglob("*.csv")), "nothing uncertified may be discoverable as a CSV either"
    assert data[verdict.EVIDENCE_WITHHELD_KEY], "the manifest must SAY why it is not evidence"

    assert _naive_numeric_consumer(tmp_path) == [], "a consumer that only knows `path` must find nothing"


def test_a_record_assembled_ELSEWHERE_cannot_reach_the_manifest_with_uncertified_evidence(tmp_path):
    """`_capture_data` is not the only thing that builds a data leg, so the writer enforces it too.

    A re-scoped, hand-repaired or older record reaches `write_manifest` without ever passing through
    the capture path, and the structural rule has to hold for those as well -- otherwise the
    invariant is "whatever `_capture_data` happened to do", which is a convention, not a guarantee.
    """
    record = _record("ok")
    record["data"] = {"status": "ok", "path": "data/hand-built.csv"}
    _code, manifest = _named_manifest(tmp_path, [record])

    data = manifest["views"][0]["data"]
    assert "path" not in data, "the writer must not serialise uncertified bytes under the evidence key"
    assert data[verdict.RETAINED_PATH_KEY] == "data/hand-built.csv"
    assert data[verdict.EVIDENCE_WITHHELD_KEY] == verdict.RETAINED_DETAIL_DEFAULT
    assert data["status"] == "ok"
    assert manifest["data_unassessable"] == 1


def test_a_certified_capture_is_still_readable_by_that_same_consumer(tmp_path):
    """⚠️ The positive control. A rule that hides EVERY capture passes the test above and destroys
    the numeric oracle, and only this can tell the two apart."""
    manifest = _capture_one(tmp_path, "Region,Sales\r\nWest,10\r\n", {"Content-Type": "text/csv"})

    data = manifest["views"][0]["data"]
    assert data["certification"] == payload_facts.CSV_CERTIFIED
    assert data["path"] == "data/aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa.csv"
    assert verdict.RETAINED_PATH_KEY not in data
    assert [item["view"] for item in _naive_numeric_consumer(tmp_path)] == ["Real Time Availability"]


def test_the_reconcile_items_builder_emits_no_tableau_value_from_an_uncertified_capture(tmp_path):
    """The review's own reproduction of finding 1, at the consumer it was reported against.

    Before: `item_count 1, skipped_views [], tableau_value 10.0` from a record whose own flags said
    `data_unassessable`. The builder is unchanged in how it GATES -- `status == ok and path` -- and
    that is the point: the record no longer has a path to offer it.
    """
    _capture_one(tmp_path, "Region,Sales\r\nWest,10\r\n", {})
    roles = {"Network Ops": {"Region": "DIMENSION", "Sales": "MEASURE"}}

    result = build_reconcile_items.build(tmp_path, roles)
    assert result["item_count"] == 0, "a fidelity verdict built on an unassessable capture is unfounded"
    assert result["items"] == []
    assert [entry["view"] for entry in result["skipped_views"]] == ["Real Time Availability"]
    # The skip must say WHY, and must not report the transport status as if it were the reason. The
    # sentence is compared to the manifest's own by identity rather than quoted: asserting on its
    # wording would make an honest rewording read as a defect.
    manifest = json.loads((tmp_path / "oracle-manifest.json").read_text(encoding="utf-8"))
    assert result["skipped_views"][0]["reason"] == manifest["views"][0]["data"][verdict.EVIDENCE_WITHHELD_KEY]
    assert result["skipped_views"][0]["reason"] != "ok"


def test_the_reconcile_items_builder_still_maps_a_certified_capture(tmp_path):
    """The matched control: the builder must keep working on evidence that IS certified."""
    _capture_one(tmp_path, "Region,Sales\r\nWest,10\r\n", {"Content-Type": "text/csv"})
    roles = {"Network Ops": {"Region": "DIMENSION", "Sales": "MEASURE"}}

    result = build_reconcile_items.build(tmp_path, roles)
    assert result["item_count"] == 1
    assert result["items"][0]["tableau_value"] == 10.0
    assert result["skipped_views"] == []


def test_a_LEGACY_manifest_naming_uncertified_bytes_under_path_is_demoted_when_it_is_READ(tmp_path):
    """The review's second reproduction: a record from BEFORE any of this existed.

    `flags=[data_unassessable, row_count_unrecorded]`, no `row_count`, no `certification`, and
    `data/view.csv` sitting in `path` with real bytes beside it. Such a file cannot be rewritten
    retroactively, so the boundary that LOADS it restores the invariant -- and the naive consumer
    above, reading the same file raw, is what shows the residual honestly.
    """
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "view.csv").write_text("Region,Sales\r\nWest,10\r\n", encoding="utf-8")
    (tmp_path / "oracle-manifest.json").write_text(
        json.dumps(
            {
                "schema": "tableau-oracle/1",
                "views": [
                    {
                        "view_luid": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa",
                        "view_name": "Real Time Availability",
                        "workbook_name": "Network Ops",
                        "flags": ["data_unassessable", "row_count_unrecorded"],
                        "data": {"status": "ok", "path": "data/view.csv"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = verdict.read_manifest(tmp_path / "oracle-manifest.json")
    data = loaded["views"][0]["data"]
    assert "path" not in data
    assert data[verdict.RETAINED_PATH_KEY] == "data/view.csv", "the bytes are still findable, just not as evidence"
    assert data["status"] == "ok"

    result = build_reconcile_items.build(tmp_path, {"Network Ops": {"Region": "DIMENSION", "Sales": "MEASURE"}})
    assert result["item_count"] == 0
    assert result["items"] == []


def test_a_LEGACY_RECORD_WITH_A_ROW_COUNT_and_no_certification_is_not_evidence(tmp_path):
    """#480 round 3, finding 1 -- and the ONLY shape that exists on a customer's disk today.

    ⚠️ Round 2's gate returned early on any non-bool integer `row_count`, so it never fired on real
    data. `git show origin/master:scripts/capture_tableau_oracle.py` called `summarise_csv(payload)`
    on the body of EVERY HTTP 200 and wrote its `row_count`, recording no certification at all --
    so a pre-#480 manifest carries a number and no certificate, and a gate that reads the number as
    proof of measurement is a gate that has never once run.

    The number is not merely unbacked, it is misleading: `summarise_csv` counts LINES, so an HTML
    error page is one row and a plain-text outage banner is a header with no rows.
    """
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "view.csv").write_text("Region,Sales\r\nWest,10\r\n", encoding="utf-8")
    (tmp_path / "oracle-manifest.json").write_text(
        json.dumps(
            {
                "schema": "tableau-oracle/1",
                "views": [
                    {
                        "view_luid": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa",
                        "view_name": "Real Time Availability",
                        "workbook_name": "Network Ops",
                        # Exactly what `origin/master` wrote. No `certification` key at all.
                        "data": {
                            "status": "ok",
                            "path": "data/view.csv",
                            "row_count": 1,
                            "columns": ["Region", "Sales"],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    record = json.loads((tmp_path / "oracle-manifest.json").read_text(encoding="utf-8"))["views"][0]

    assert verdict.unassessable_reason(record) == verdict.UNASSESSABLE_NO_CERTIFICATION
    assert verdict.UNASSESSABLE_NO_CERTIFICATION in verdict.UNASSESSABLE_REASONS
    assert verdict.UNASSESSABLE_NO_CERTIFICATION != verdict.UNASSESSABLE_NO_ROW_COUNT, (
        "a record reading `row_count: 1` must not be told its row count is unrecorded"
    )

    data = verdict.read_manifest(tmp_path / "oracle-manifest.json")["views"][0]["data"]
    assert "path" not in data, "the legacy shape is the one that MUST be withheld, or this PR fixes nothing"
    assert data[verdict.RETAINED_PATH_KEY] == "data/view.csv"
    assert data["row_count"] == 1, "the recorded number is kept for forensics; what is denied is that it is evidence"
    assert data[verdict.EVIDENCE_WITHHELD_KEY] == verdict.RETAINED_DETAIL_NO_CERTIFICATION
    assert data[verdict.EVIDENCE_WITHHELD_KEY] != verdict.RETAINED_DETAIL_DEFAULT, (
        "the default sentence opens `records no row count`, which is false of this record"
    )

    # ⚠️ The residual, stated rather than hidden: a file already on disk cannot be rewritten
    # retroactively, so a consumer that opens `oracle-manifest.json` with `json.loads` still sees
    # `path`. `read_manifest` is the boundary that restores the invariant, and every in-repo consumer
    # goes through it -- `build_reconcile_items` below is the one the review filed this against.
    assert _naive_numeric_consumer(tmp_path), "the raw file is unchanged; the boundary that LOADS it is the gate"
    result = build_reconcile_items.build(tmp_path, {"Network Ops": {"Region": "DIMENSION", "Sales": "MEASURE"}})
    assert result["item_count"] == 0
    assert result["items"] == []
    assert [entry["view"] for entry in result["skipped_views"]] == ["Real Time Availability"]


def test_a_record_that_contradicts_itself_is_believed_on_its_REFUSAL_not_on_its_count(tmp_path):
    """#480 round 3, finding 1's second half: `content_type_absent` WITH a `row_count`.

    A record cannot be both uncertified and measured. Round 2 resolved the contradiction toward the
    number, which is the fail-open direction -- the count would then have come from the very body
    the certification says nothing established.
    """
    record = {
        "view_name": "V",
        "workbook_name": "W",
        "data": {
            "status": "ok",
            "certification": payload_facts.CSV_CONTENT_TYPE_ABSENT,
            "path": "data/v.csv",
            "row_count": 1,
            "columns": ["Region"],
        },
    }
    assert verdict.unassessable_reason(record) == payload_facts.CSV_CONTENT_TYPE_ABSENT, (
        "the record's own refusal is the more specific true statement, so it is the reason reported"
    )

    (demoted,) = verdict.withhold_uncertified_evidence([record])
    assert "path" not in demoted["data"]
    assert demoted["data"][verdict.RETAINED_PATH_KEY] == "data/v.csv"
    assert (
        demoted["data"][verdict.EVIDENCE_WITHHELD_KEY]
        == payload_facts.CSV_UNCERTIFIED_DETAIL[payload_facts.CSV_CONTENT_TYPE_ABSENT]
    )

    _code, manifest = _named_manifest(tmp_path, [record])
    assert manifest["data_unassessable"] == 1
    assert manifest["captured_complete"] == 0
    assert [entry["reason"] for entry in manifest["data_unassessable_views"]] == [payload_facts.CSV_CONTENT_TYPE_ABSENT]


def test_a_certification_this_module_does_not_recognise_is_unassessable_not_assessable(tmp_path):
    """A verdict from a NEWER capture, or a corrupted field. Either way nothing here established it.

    The fail-open reading is "unknown string, so fall through to whatever else the record says". A
    closed vocabulary only protects anything if a value outside it is refused rather than ignored.
    """
    record = {
        "view_name": "V",
        "workbook_name": "W",
        "data": {"status": "ok", "certification": "certified_by_a_future_release", "path": "d.csv", "row_count": 5},
    }
    assert record["data"]["certification"] not in payload_facts.CSV_VERDICTS, "the control must use an UNKNOWN value"
    assert verdict.unassessable_reason(record) == verdict.UNASSESSABLE_NO_CERTIFICATION

    _code, manifest = _named_manifest(tmp_path, [record])
    assert manifest["data_unassessable"] == 1
    assert "path" not in manifest["views"][0]["data"]


def test_a_zero_row_count_on_an_uncertified_body_is_never_reported_as_an_empty_QUERY(tmp_path):
    """#471's failure 2 with the number kept -- the shape the round-3 review found still surviving.

    `Service temporarily unavailable` exports HTTP 200, parses as one header and no data rows, and
    `origin/master` recorded `row_count: 0, columns: ["Service temporarily unavailable"]`. Read as
    `empty_query_no_rows` that sends an operator to look at a Tableau filter for a view whose server
    returned an error page, so `empty_classification` defers to `unassessable_reason` and the two
    predicates are mutually exclusive by construction.
    """
    record = {
        "view_name": "V",
        "workbook_name": "W",
        "data": {
            "status": "ok",
            "path": "data/error.csv",
            "row_count": 0,
            "columns": ["Service temporarily unavailable"],
        },
    }
    assert verdict.empty_classification(record) is None, "an uncertified zero is not a measured zero"
    assert verdict.unassessable_reason(record) == verdict.UNASSESSABLE_NO_CERTIFICATION

    _code, manifest = _named_manifest(tmp_path, [record])
    assert manifest["data_empty"] == 0
    assert manifest["data_empty_views"] == []
    assert manifest["data_unassessable"] == 1
    assert manifest["views"][0]["flags"] == [verdict.FLAG_DATA_UNASSESSABLE, verdict.UNASSESSABLE_NO_CERTIFICATION]


def test_a_CERTIFIED_zero_row_capture_is_still_reported_as_an_empty_query(tmp_path):
    """⚠️ The matched control for the test above, and it is load-bearing.

    Deferring to `unassessable_reason` could have been implemented as "never classify anything as
    empty", which passes every assertion above and silently deletes #471's whole feature. A capture
    that was certified AND measured zero rows is a real measurement and must still be named.
    """
    record = {
        "view_name": "V",
        "workbook_name": "W",
        "data": {
            "status": "ok",
            "certification": payload_facts.CSV_CERTIFIED,
            "path": "data/blank.csv",
            "row_count": 0,
            "columns": ["Region"],
        },
    }
    assert verdict.empty_classification(record) == verdict.EMPTY_QUERY_NO_ROWS
    assert verdict.unassessable_reason(record) is None

    _code, manifest = _named_manifest(tmp_path, [record])
    assert manifest["data_empty"] == 1
    assert [entry["view_name"] for entry in manifest["data_empty_views"]] == ["V"]
    assert manifest["data_unassessable"] == 0


def test_the_run_end_banner_names_the_certification_reason_too(tmp_path, caplog):
    """A reason an operator meets in the per-view list and never in the block that explains it reads
    as an internal code. The banner names all four, and this is the fourth."""
    caplog.set_level(logging.INFO, logger="tableau-oracle")
    record = {
        "view_name": "Metrics",
        "workbook_name": "W",
        "data": {"status": "ok", "path": "data/v.csv", "row_count": 900},
    }
    _named_manifest(tmp_path, [record])
    warnings = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "NOT BE ASSESSED" in warnings, "the block did not fire, so this proves nothing"
    assert verdict.UNASSESSABLE_NO_CERTIFICATION in warnings
    assert verdict.UNASSESSABLE_NO_ROW_COUNT in warnings, "the other three must not have been dropped"
    assert payload_facts.CSV_CONTENT_TYPE_ABSENT in warnings
    assert payload_facts.CSV_CONTENT_TYPE_UNSPECIFIC in warnings


def test_withholding_evidence_leaves_a_measured_capture_untouched_and_is_idempotent(tmp_path):
    """Two controls in one: the rule must not fire on a certified record, and applying it twice must
    not shuffle a `retained_path` back and forth or double-stamp anything."""
    good = {"data": {"status": "ok", "path": "data/x.csv", "row_count": 3, "certification": "certified"}}
    bad = {"data": {"status": "ok", "path": "data/y.csv", "certification": payload_facts.CSV_CONTENT_TYPE_ABSENT}}

    once = verdict.withhold_uncertified_evidence([good, bad])
    twice = verdict.withhold_uncertified_evidence(once)
    assert once[0] is good, "a certified record must not even be copied"
    assert once == twice
    assert once[1]["data"]["retained_path"] == "data/y.csv"
    assert "path" not in once[1]["data"]
    assert bad["data"]["path"] == "data/y.csv", "the caller's own records must not be mutated"


@pytest.mark.parametrize(
    ("body", "misdiagnosis"),
    [
        ("Service unavailable\r\nRetry later\r\n", "columns"),
        ("Service temporarily unavailable", "empty_query_no_rows"),
    ],
)
def test_text_plain_error_text_is_never_certified_as_CSV(tmp_path, body, misdiagnosis):
    """Finding 2, both measured variants, through the production export path.

    The two-line body was recorded `certified`, `columns: ["Service unavailable"]`, `row_count: 1`.
    The single-line variant is the worse one: `row_count: 0` and then the SPECIFIC diagnosis
    `empty_query_no_rows` -- "the query ran and returned nothing" about a maintenance banner, which
    is precisely the confidently-wrong claim #471 was filed for.
    """
    manifest = _capture_one(tmp_path, body, {"Content-Type": "text/plain"})
    data = manifest["views"][0]["data"]

    assert data["status"] == "ok", "the HTTP call did succeed; only the evidence claim is refused"
    assert data["certification"] == payload_facts.CSV_CONTENT_TYPE_UNSPECIFIC
    assert "row_count" not in data and "columns" not in data, f"the {misdiagnosis} claim must not be derivable"
    assert "path" not in data
    assert manifest["data_empty"] == 0 and manifest["data_empty_views"] == [], "no empty DIAGNOSIS may be made"
    assert manifest["data_unassessable"] == 1
    assert manifest["data_unassessable_views"][0]["reason"] == payload_facts.CSV_CONTENT_TYPE_UNSPECIFIC
    assert manifest["captured_complete"] == 0
    assert manifest["views"][0]["flags"] == [
        verdict.FLAG_DATA_UNASSESSABLE,
        payload_facts.CSV_CONTENT_TYPE_UNSPECIFIC,
    ]
    assert _naive_numeric_consumer(tmp_path) == []


def test_text_csv_and_application_csv_remain_explicit_declarations(tmp_path):
    """⚠️ The over-correction control for finding 2. Narrowing what counts as a CSV declaration must
    not narrow it to nothing: both real spellings still certify, with rows and columns recorded."""
    for declared in ("text/csv", "application/csv"):
        out = Path(tmp_path / declared.replace("/", "-"))
        out.mkdir()
        manifest = _capture_one(out, "Region,Sales\r\nWest,10\r\n", {"Content-Type": declared})
        data = manifest["views"][0]["data"]
        assert data["certification"] == payload_facts.CSV_CERTIFIED, declared
        assert data["row_count"] == 1 and data["columns"] == ["Region", "Sales"]
        assert data["path"].startswith("data/")
        assert manifest["captured_complete"] == 1


def test_a_text_plain_body_that_is_not_even_tabular_is_still_REFUSED_not_retained(tmp_path):
    """The structural checks still run under `text/plain`, so an HTML error page relabelled by a
    proxy is refused with no file written -- unspecific is a weaker declaration, not an amnesty."""
    session = FakeSession([(200, "<html>\n<body>Error</body>\n</html>\n", {"Content-Type": "text/plain"})])
    view = {"id": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa", "name": "Real Time Availability", "workbook": {"id": "wb"}}
    record = oracle.capture_view(session, view, tmp_path, frozenset(), None)

    assert record["data"]["status"] == "format_mismatch"
    assert record["data"]["certification"] == payload_facts.CSV_NOT_TABULAR
    assert not list(tmp_path.rglob("*.csv")) and not list(tmp_path.rglob(f"*{verdict.RETAINED_SUFFIX}"))


def test_every_uncertified_verdict_has_an_authored_reason_and_none_is_a_refusal():
    """The vocabulary must stay closed and the two halves must stay disjoint: a RETAINED verdict that
    is also a REFUSAL would mean bytes both written and not written, and a retained verdict with no
    sentence beside it puts a bare literal in the manifest for a human to guess at."""
    assert payload_facts.CSV_UNCERTIFIED.isdisjoint(payload_facts.CSV_REFUSALS)
    assert payload_facts.CSV_UNCERTIFIED <= payload_facts.CSV_VERDICTS
    assert set(payload_facts.CSV_UNCERTIFIED_DETAIL) == set(payload_facts.CSV_UNCERTIFIED)
    assert payload_facts.CSV_UNCERTIFIED <= verdict.UNASSESSABLE_REASONS, (
        "a retained-but-uncertified verdict that is not an unassessable reason reads as a clean capture"
    )


# --- #402: dashboard vs worksheet -------------------------------------------------------------
#
# Tableau REST returns both under `/views` with nothing to tell them apart, so a captured render
# could be a whole dashboard or a single chart and no consumer could say which. These pin the
# discriminator AND -- more importantly -- that every failure of it lands on `unknown` rather than
# on a guess. A wrong type would be believed; an absent one cannot be.


# ⚠️ These MUST be real UUIDs. An earlier revision used readable stand-ins (`D-1`, `W-1`), which the
# `_LUID_RE` shape check then refused -- so the happy-path test went red and, worse, every "fails
# closed" case below was refused by the SHAPE guard before reaching the branch it was written to
# cover. A fixture that cannot reach its own subject reads as coverage and is not.
DASH_LUID = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
SHEET_LUID = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
SHEET2_LUID = "cccccccc-3333-4333-8333-cccccccccccc"


def _wb(dashboards=(), sheets=()):
    return {"dashboards": [{"luid": x} for x in dashboards], "sheets": [{"luid": x} for x in sheets]}


def _graphql(payload):
    return FakeSession([(200, json.dumps(payload), {})])


def test_view_types_joins_dashboards_and_worksheets_by_luid():
    """The join is on LUID, not on name -- a dashboard and its principal sheet often share one."""
    session = _graphql({"data": {"workbooks": [_wb(dashboards=[DASH_LUID], sheets=[SHEET_LUID, SHEET2_LUID])]}})
    mapping, unavailable = view_types_mod.view_types(session)
    assert unavailable is None
    assert mapping == {DASH_LUID: "dashboard", SHEET_LUID: "worksheet", SHEET2_LUID: "worksheet"}


def test_view_types_asks_the_metadata_api_through_the_hardened_path():
    """`api='metadata'` + `/graphql` composes the unversioned endpoint via the ONE HTTP round trip.

    If this ever regresses to a second HTTP client, the credential-reflection rounds that produced
    `tableau_http` start again from scratch on a new surface.
    """
    session = _graphql({"data": {"workbooks": []}})
    view_types_mod.view_types(session)
    assert session.calls == ["/graphql"]


@pytest.mark.parametrize(
    "responses, why, guard",
    [
        # ⚠️ Carries BOTH `errors` AND usable `data`. A GraphQL 200 can return partial data beside an
        # error, and with an empty `data` the mapping would end up empty regardless -- so the errors
        # branch would be redundant and its mutation would survive. Measured: it did.
        (
            [
                (
                    200,
                    json.dumps(
                        {
                            "errors": [{"message": "Cannot query field 'luid' on type 'Dashboard'"}],
                            "data": {"workbooks": [_wb(dashboards=[DASH_LUID], sheets=[SHEET_LUID])]},
                        }
                    ),
                    {},
                )
            ],
            "FieldUndefined beside partial data",
            "graphql error(s); response refused",
        ),
        # ⚠️ The body here is a PERFECTLY VALID mapping payload, so the HTTP status is the only reason
        # to refuse. The earlier fixture sent `403, "forbidden"` -- unparseable, so the JSON guard
        # refused it too and deleting the status check left all 53 tests green. Measured by review.
        (
            [(403, json.dumps({"data": {"workbooks": [_wb(dashboards=[DASH_LUID])]}}), {})],
            "metadata api disabled, but answering with a well-formed body",
            "returned HTTP 403",
        ),
        (
            [(0, json.dumps({"data": {"workbooks": [_wb(dashboards=[DASH_LUID])]}}), {})],
            "network error status, with a body that would otherwise parse",
            "returned HTTP 0",
        ),
        ([(200, "not json at all", {})], "unparseable", "not usable JSON"),
        (
            [(200, json.dumps({"data": {"workbooks": []}}), {})],
            "no luids at all",
            "no dashboards or sheets carrying a luid",
        ),
        # --- one poisoned part BESIDE a valid one. Each WOULD yield a non-empty mapping if its guard
        # were removed, which is what makes the guard the single reason for the refusal. Both node
        # collections are present in every one of them, because the presence guard added in round 2
        # would otherwise refuse three of these before they reached their own subject.
        (
            [
                (
                    200,
                    json.dumps(
                        {"data": {"workbooks": [{"dashboards": [{"luid": DASH_LUID}, {"luid": 7}], "sheets": []}]}}
                    ),
                    {},
                )
            ],
            "a non-string luid beside a valid one",
            "where the schema declares String!",
        ),
        (
            [
                (
                    200,
                    json.dumps(
                        {"data": {"workbooks": [{"dashboards": [{"luid": DASH_LUID}, {"luid": "D-1"}], "sheets": []}]}}
                    ),
                    {},
                )
            ],
            "a non-uuid string luid beside a valid one",
            "non-empty value that is not a luid",
        ),
        (
            [
                (
                    200,
                    json.dumps({"data": {"workbooks": [{"dashboards": [{"luid": DASH_LUID}, "nope"], "sheets": []}]}}),
                    {},
                )
            ],
            "a non-dict node beside a valid one",
            "not an object; response refused",
        ),
        (
            [(200, json.dumps({"data": {"workbooks": [_wb(dashboards=[DASH_LUID]), ["not", "a", "dict"]]}}), {})],
            "a non-dict workbook beside a valid one",
            "a workbook node was list, not an object",
        ),
        (
            [
                (
                    200,
                    json.dumps({"data": {"workbooks": [{"sheets": [{"luid": SHEET_LUID}], "dashboards": "nope"}]}}),
                    {},
                )
            ],
            "a non-list `dashboards` beside a valid `sheets`",
            "`dashboards` was str, not a list",
        ),
        # --- shapes that used to RAISE rather than refuse. `pytest.raises` is deliberately not used:
        # an escaping exception fails the call outright, which is the point.
        ([(200, "null", {})], "a top-level null", "was NoneType, not an object"),
        ([(200, json.dumps([{"data": {"workbooks": []}}]), {})], "a top-level list", "was list, not an object"),
        ([(200, json.dumps("nope"), {})], "a top-level string", "was str, not an object"),
        ([(200, json.dumps({"data": None}), {})], "`data` is null", "`data` was NoneType, not an object"),
        ([(200, json.dumps({"data": []}), {})], "`data` is a list", "`data` was list, not an object"),
        (
            [(200, json.dumps({"data": {"workbooks": {"dashboards": []}}}), {})],
            "`workbooks` is a dict",
            "`workbooks` was dict, not a list",
        ),
        (
            [(200, json.dumps({"errors": "boom", "data": {"workbooks": []}}), {})],
            "`errors` is not a list",
            "`errors` was str, not a list",
        ),
        (
            [(200, json.dumps({"errors": {"message": "boom"}, "data": {"workbooks": []}}), {})],
            "`errors` is a dict",
            "`errors` was dict, not a list",
        ),
        ([(200, "", {})], "an empty body", "not usable JSON"),
        ([(200, b"\xff\xfe\x00bad", {})], "a body that is not valid utf-8", "not usable JSON"),
        # A LUID that names BOTH kinds is contradictory, not a last-wins tiebreak: overwriting would
        # silently take whichever the server happened to list second.
        (
            [
                (
                    200,
                    json.dumps(
                        {
                            "data": {
                                "workbooks": [{"dashboards": [{"luid": DASH_LUID}], "sheets": [{"luid": DASH_LUID}]}]
                            }
                        }
                    ),
                    {},
                )
            ],
            "one luid reported as both a dashboard and a worksheet",
            "both a dashboard and a worksheet",
        ),
        (
            [
                (
                    200,
                    json.dumps({"data": {"workbooks": [_wb(dashboards=[DASH_LUID]), _wb(sheets=[DASH_LUID])]}}),
                    {},
                )
            ],
            "the same contradiction split across two workbooks",
            "both a dashboard and a worksheet",
        ),
        # --- round 2: four measured FAIL-OPEN paths, each of which produced a NON-EMPTY mapping with
        # `unavailable=None`. Every fixture keeps a valid sibling that WOULD still be mapped if the
        # guard were removed -- otherwise the later "no luids" branch does the refusing and the
        # guard's mutation survives.
        (
            [(200, json.dumps({"errors": 0, "data": {"workbooks": [_wb(dashboards=[DASH_LUID])]}}), {})],
            "a FALSY-but-present `errors` beside usable data",
            "`errors` was int, not a list",
        ),
        (
            [
                (
                    200,
                    json.dumps({"data": {"workbooks": [{"dashboards": [{"luid": DASH_LUID}], "sheets": None}]}}),
                    {},
                )
            ],
            "`sheets` is null beside a valid dashboard",
            "`sheets` was NoneType, not a list",
        ),
        (
            [(200, json.dumps({"data": {"workbooks": [{"dashboards": [{"luid": DASH_LUID}]}]}}), {})],
            "`sheets` is absent entirely, beside a valid dashboard",
            "had no `sheets` field",
        ),
        (
            [(200, json.dumps({"data": {"workbooks": [{"sheets": [{"luid": SHEET_LUID}]}]}}), {})],
            "`dashboards` is absent entirely, beside a valid sheet",
            "had no `dashboards` field",
        ),
        (
            [
                (
                    200,
                    b'{"data": {"workbooks": [{"dashboards": [{"luid": "'
                    + DASH_LUID.encode()
                    + b'"}], "sheets": []}]},'
                    b' "n": "\xff\xfe"}',
                    {},
                )
            ],
            "invalid utf-8 INSIDE an otherwise valid body",
            "not usable JSON",
        ),
        (
            [
                (
                    200,
                    json.dumps(
                        {"data": {"workbooks": [{"dashboards": [{"luid": DASH_LUID}, {"luid": None}], "sheets": []}]}}
                    ),
                    {},
                )
            ],
            "`luid` is null, which the schema declares String!",
            "where the schema declares String!",
        ),
        # Bodies a server can send that made `json.loads` RAISE, aborting the whole capture before the
        # view loop. Neither is a `JSONDecodeError`, which is why an enumerated catch missed both.
        (
            [(200, '{"data": {"workbooks": []}, "pad": ' + "9" * 5000 + "}", {})],
            "a 5000-digit integer (CPython's int-conversion limit)",
            "not usable JSON: ValueError",
        ),
        (
            [(200, '{"data": ' + "[" * 200_000 + "]" * 200_000 + "}", {})],
            "deeply nested JSON, only 400 kB so no size ceiling catches it",
            "not usable JSON: RecursionError",
        ),
    ],
)
def test_view_types_fails_closed_and_never_guesses(responses, why, guard):
    """⚠️ The load-bearing property. EVERY failure yields an empty map plus a stated reason.

    There is deliberately no name-based fallback: matching on the view NAME is the exact join this
    replaces, and it is what let a worksheet stand in as evidence for a dashboard page.

    ⚠️ Three properties, not one. A malformed answer must be refused **whole** -- an earlier revision
    skipped bad nodes and trusted their valid siblings, producing a mapping that typed some views and
    left others `unknown`, which downstream is indistinguishable from a run where those views
    genuinely had no type. It must never RAISE: a top-level `null`, list or string each escaped as an
    uncaught `AttributeError`, `errors` as a dict escaped as a `KeyError`, and a 5000-digit integer
    escaped as a `ValueError` from CPython's int-conversion limit.

    ⚠️ And it must refuse for **the reason this fixture was written to provoke**. That third
    assertion is not decoration -- it is the structural fix for a vacuity that has now occurred TWICE
    on this file. Adding `_LUID_RE` made every fails-closed fixture refuse on luid SHAPE before
    reaching its own branch; adding the `dashboards`/`sheets` presence guard did it again to three
    more. Both times the suite stayed green while covering strictly less, because "it refused" was
    the whole assertion. `guard` makes a fixture that stops reaching its subject fail loudly.
    """
    mapping, unavailable = view_types_mod.view_types(FakeSession(responses))
    assert mapping == {}, f"{why} must not produce a mapping"
    assert unavailable, f"{why} must state why the type is unknown"
    assert guard in unavailable, (
        f"{why} refused, but for the WRONG reason: expected a refusal mentioning {guard!r}, got "
        f"{unavailable!r}. The fixture no longer reaches the guard it was written to cover."
    )


# --- #402 round 2: an EXPECTED non-joinable node must not disable the whole site ----------------
#
# ⚠️ The mirror of everything above, and the likelier failure in practice. This query scans EVERY
# workbook on the site and a refusal refuses the WHOLE response, so an over-strict rule does not
# degrade one view -- it turns typing off for every captured view on the site.
#
# Tableau documents `Sheet.luid: String!` as "Blank if worksheet is hidden in Workbook", and REST
# `/views` omits hidden sheets entirely. So a blank luid names NO capturable view: skipping it cannot
# leave any view mistyped, or `unknown` when it could have been typed. A NON-EMPTY malformed luid is
# the opposite -- it may be a real visible view whose identity we failed to read -- and still refuses.


@pytest.mark.parametrize(
    "workbooks, expected, why",
    [
        (
            [{"dashboards": [{"luid": DASH_LUID}], "sheets": [{"luid": ""}]}],
            {DASH_LUID: "dashboard"},
            "a hidden sheet beside the dashboard it belongs to",
        ),
        (
            [
                {"dashboards": [{"luid": DASH_LUID}], "sheets": [{"luid": SHEET_LUID}]},
                {"dashboards": [], "sheets": [{"luid": ""}]},
            ],
            {DASH_LUID: "dashboard", SHEET_LUID: "worksheet"},
            "a hidden sheet in a COMPLETELY UNRELATED workbook",
        ),
        (
            [{"dashboards": [{"luid": DASH_LUID}], "sheets": [{"luid": "   "}]}],
            {DASH_LUID: "dashboard"},
            "a blank luid spelled as whitespace",
        ),
        (
            [{"dashboards": [{"luid": ""}], "sheets": [{"luid": SHEET_LUID}]}],
            {SHEET_LUID: "worksheet"},
            "a blank DASHBOARD luid, not only a sheet",
        ),
    ],
)
def test_a_hidden_sheet_does_not_switch_typing_off_for_the_whole_site(workbooks, expected, why):
    """⚠️ Measured: before this, ONE hidden sheet made every captured view on the site `unknown`.

    Hidden sheets are ordinary in a real estate, so the feature would have been inert exactly where
    it was built to be used -- and inert SILENTLY, reported as a clean fail-closed run.

    ⚠️ **MEASURED, not merely documented.** Tableau's docs say ``Sheet.luid: String!`` is *"Blank if
    worksheet is hidden in Workbook"*, but nobody had confirmed a real site emits one. Measured
    against our Tableau Cloud trial on **2026-09-01**, REST **3.29** requested, one read-only
    Metadata API query (``scripts/tableau_luid_census.py``, re-runnable):

    ==========================================  =========
    workbooks returned                          48
    dashboard nodes / blank                     60 / **0**
    sheet nodes / blank                         416 / **116**
    workbooks holding at least one blank luid   **5**
    non-empty non-uuid luids                    0
    views typed by the SHIPPED parser           **360**
    views typed by the PRE-FIX rule             **0** (refused the whole response)
    ==========================================  =========

    So this is not "would break a site that has hidden sheets" -- **27.9% of sheets on the site this
    feature was built against carry a blank luid**, and the pre-fix rule typed nothing at all there.
    Re-measure with ``python scripts/tableau_luid_census.py``; the three verdicts are CONFIRMED,
    NOT-PRESENT and CANNOT-TELL, and it does not push toward any of them.
    """
    mapping, unavailable = view_types_mod.view_types(_graphql({"data": {"workbooks": workbooks}}))
    assert unavailable is None, f"{why} must not refuse the response"
    assert mapping == expected, f"{why} must leave every OTHER view typed"


def test_a_blank_luid_is_skipped_but_a_garbage_one_still_refuses_everything():
    """⚠️ The distinction IS the rule, so it is asserted as one fact rather than as two tests.

    Same workbook, same position, one character different: `""` is documented and non-joinable, and
    `"x"` is an identity we could not read. If these two ever collapse onto the same behaviour the
    feature is either inert (both refuse) or fail-open (both skip).
    """
    blank = _graphql({"data": {"workbooks": [{"dashboards": [{"luid": DASH_LUID}], "sheets": [{"luid": ""}]}]}})
    garbage = _graphql({"data": {"workbooks": [{"dashboards": [{"luid": DASH_LUID}], "sheets": [{"luid": "x"}]}]}})

    blank_mapping, blank_reason = view_types_mod.view_types(blank)
    garbage_mapping, garbage_reason = view_types_mod.view_types(garbage)

    assert (blank_mapping, blank_reason) == ({DASH_LUID: "dashboard"}, None)
    assert garbage_mapping == {}
    assert "non-empty value that is not a luid" in garbage_reason


def test_a_site_of_only_hidden_sheets_reports_that_it_typed_nothing():
    """Skipping every node must not read as a successful run that happened to type nothing."""
    session = _graphql({"data": {"workbooks": [{"dashboards": [], "sheets": [{"luid": ""}, {"luid": ""}]}]}})
    mapping, unavailable = view_types_mod.view_types(session)
    assert mapping == {}
    assert "no dashboards or sheets carrying a luid" in unavailable


@pytest.mark.parametrize("errors", [None, []])
def test_the_two_UNAMBIGUOUS_spellings_of_no_errors_are_accepted(errors):
    """⚠️ A deliberate deviation from "validate `errors` by presence and exact shape".

    The GraphQL spec forbids both spellings, so refusing them is defensible on paper. But neither is
    AMBIGUOUS -- there is no server for which `"errors": []` means errors occurred -- so refusing
    buys nothing on the safety axis and costs on the inertness axis, which is the very failure mode
    the hidden-sheet finding was about. `0`, `""`, `{}` and a string all still refuse, because none
    of those can be interpreted at all; `errors: 0` is in the fails-closed table above.
    """
    session = _graphql({"errors": errors, "data": {"workbooks": [_wb(dashboards=[DASH_LUID])]}})
    mapping, unavailable = view_types_mod.view_types(session)
    assert unavailable is None
    assert mapping == {DASH_LUID: "dashboard"}


def test_an_oversized_body_is_refused_before_it_is_decoded(monkeypatch):
    """The ceiling is the SOLE reason here: the body is a perfectly valid mapping payload.

    Patched down rather than sending 32 MiB, because a fixture that costs seconds gets deleted. What
    is pinned is that the check exists and fires before `decode`, not the constant's value.
    """
    monkeypatch.setattr(view_types_mod, "_MAX_BODY_BYTES", 10)
    session = _graphql({"data": {"workbooks": [_wb(dashboards=[DASH_LUID])]}})
    mapping, unavailable = view_types_mod.view_types(session)
    assert mapping == {}
    assert "byte ceiling" in unavailable


def test_the_body_ceiling_is_large_enough_for_a_real_estate():
    """A ceiling below a plausible site would be the inertness failure wearing a different hat.

    The query asks for `luid` and nothing else, so a node costs ~30 bytes on the wire.
    """
    assert view_types_mod._MAX_BODY_BYTES >= 100_000 * 30 * 2  # pylint: disable=protected-access


def test_a_repeated_luid_of_the_SAME_kind_is_tolerated_deliberately():
    """⚠️ A judgement call, pinned so it is a decision rather than an accident.

    Everything else malformed refuses the whole answer. A node repeated under the *same* kind is the
    one exception: it cannot produce a wrong type, and refusing would blank an entire estate's typing
    over a duplicate that changes nothing. The CONTRADICTORY case -- one luid under both kinds -- is
    refused, and is covered above.
    """
    session = _graphql(
        {"data": {"workbooks": [{"dashboards": [{"luid": DASH_LUID}, {"luid": DASH_LUID}], "sheets": []}]}}
    )
    mapping, unavailable = view_types_mod.view_types(session)
    assert unavailable is None
    assert mapping == {DASH_LUID: "dashboard"}


def test_a_luid_is_matched_case_insensitively_but_keeps_its_shape():
    """Tableau's REST `id` and the Metadata API's `luid` can differ in case for the same view."""
    session = _graphql({"data": {"workbooks": [_wb(dashboards=[DASH_LUID.upper()])]}})
    mapping, unavailable = view_types_mod.view_types(session)
    assert unavailable is None
    assert mapping == {DASH_LUID: "dashboard"}


# --- #402 finding 2: NO server-controlled string may reach a diagnostic -------------------------
#
# A GraphQL `errors[].message` is authored by the server, and a server that reflects the inbound
# `X-Tableau-Auth` header into it puts a LIVE SESSION TOKEN in our warning. Measured against a
# one-request localhost server: the pre-fix warning contained the token verbatim.
#
# The defence is not detection -- a credential FRAGMENT is not detectable -- it is to emit fewer
# server-controlled strings. Same deletion, same reason, as the HTTP reason phrase in
# `tableau_render_capability` (#405 round 8).

REFLECTION_SENTINEL = "SENTINEL_SESSION_TOKEN_FULL_PERMISSION"

#: Planted wherever the SERVER controls a string, so a branch that quotes it back is visible.
TAINT = "TAINT_SENTINEL"


class _Recorder:
    """Captures what a logging handler would actually emit, i.e. the message AFTER `%` formatting."""

    def __init__(self):
        self.warnings: list[str] = []

    def warning(self, fmt, *args):
        self.warnings.append(fmt % args if args else fmt)


def test_a_reflected_session_token_never_reaches_the_view_type_warning():
    """⚠️ The regression test for the leak, driven by a REAL one-request server.

    A `FakeSession` cannot prove this: the leak is about a body that travelled the real transport,
    and `tableau_http` passes response bodies through RAW **by design** (classification has to see
    the unmodified text). So the token is genuinely present in the bytes this module parses, and the
    only thing standing between it and the log is this module declining to quote the server.
    """

    class Reflector(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            payload = {
                "errors": [
                    {
                        "message": f"Reflected request context: X-Tableau-Auth={self.headers.get('X-Tableau-Auth')}",
                        "extensions": {"code": "FIELD_UNDEFINED"},
                    }
                ],
                # Beside usable data, so the refusal is the errors branch and not "no luids".
                "data": {"workbooks": [_wb(dashboards=[DASH_LUID])]},
            }
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Reflector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        session = oracle.TableauSession(
            oracle.SiteCredentials(
                base=f"http://127.0.0.1:{server.server_port}",
                site="site",
                pat_name="pat-name",
                pat_secret="PAT_SECRET_1234567890",
                version="3.29",
            ),
            oracle.RetryPolicy(max_attempts=1, budget_sec=1),
        )
        session.token, session.site_id = REFLECTION_SENTINEL, "site-id"

        log = _Recorder()
        views = [{"id": DASH_LUID, "name": "Revenue"}]
        unavailable = view_types_mod.resolve_and_stamp(session, views, log)
    finally:
        server.shutdown()
        server.server_close()

    # The server DID reflect it -- otherwise this test proves nothing about redaction.
    assert REFLECTION_SENTINEL in session.token
    assert unavailable, "an errors block must still be reported as a reason"
    assert REFLECTION_SENTINEL not in unavailable
    assert log.warnings, "a run that cannot discriminate must say so"
    assert REFLECTION_SENTINEL not in "\n".join(log.warnings)
    # ⚠️ Fail-closed, not fail-open: the partial `data` beside the error must NOT have been used.
    assert views[0][view_types_mod.VIEW_TYPE_KEY] == "unknown"


@pytest.mark.parametrize(
    "responses",
    [
        [(200, json.dumps({"errors": [{"message": TAINT}], "data": {"workbooks": []}}), {})],
        [(200, json.dumps({"errors": [{"message": "x"}, {"message": TAINT}]}), {})],
        [(200, f"{TAINT} not json", {})],
        [(418, TAINT, {})],
        [(200, json.dumps({"data": {"workbooks": [{"dashboards": [{"luid": TAINT}], "sheets": []}]}}), {})],
        [(200, json.dumps({"data": {"workbooks": [TAINT]}}), {})],
        [(200, json.dumps({"data": {"workbooks": [{"dashboards": TAINT}]}}), {})],
        [(200, json.dumps({"data": TAINT}), {})],
        [(200, json.dumps(TAINT), {})],
    ],
)
def test_no_branch_quotes_the_server_back_into_its_reason(responses):
    """⚠️ The GENERAL property, not just the one branch that leaked.

    A sentinel is planted in every server-controlled position the parser touches -- error messages,
    a bad luid, a bad node, a bad container, an unparseable body, an error-status body. No reason
    string may contain it. Reporting a *type name* or a *count* is fine; those are ours.
    """
    mapping, unavailable = view_types_mod.view_types(FakeSession(responses))
    assert mapping == {}
    assert unavailable
    assert TAINT not in unavailable


@pytest.mark.parametrize(
    "payload, guard",
    [
        (None, "response was NoneType, not an object"),
        ([], "response was list, not an object"),
        ("nope", "response was str, not an object"),
        (7, "response was int, not an object"),
        (3.5, "response was float, not an object"),
        (True, "response was bool, not an object"),
        ({"data": None}, "`data` was NoneType, not an object"),
        ({"data": []}, "`data` was list, not an object"),
        ({"data": "x"}, "`data` was str, not an object"),
        ({"data": {"workbooks": 1}}, "`workbooks` was int, not a list"),
    ],
)
def test_parse_payload_accepts_arbitrary_decoded_json(payload, guard):
    """⚠️ The shared seam is TOTAL, and that is a correctness requirement rather than politeness.

    It was written for a `dict`, which was true only because its one caller pre-validated in
    `_fetch_payload`. The moment a second caller appeared - `tableau_luid_census`, holding a body it
    had decoded itself - a top-level `null` escaped as `TypeError` and a list or string as
    `AttributeError`. A precondition living in one caller's path is not a precondition.
    """
    mapping, unavailable = view_types_mod.parse_payload(payload)
    assert mapping == {}
    assert unavailable
    assert TAINT not in unavailable
    # ⚠️ WHICH guard refused, not merely that one did. Several fail-closed guards satisfy the bare
    # assertion, so without this a later guard can quietly take over and the case stops covering
    # its own subject -- which has happened five times on this PR.
    assert guard in unavailable, f"expected a refusal mentioning {guard!r}, got {unavailable!r}"


def test_a_transport_exception_is_reported_by_TYPE_not_by_message():
    """`str(exc)` on a transport error can carry a reflected URL, and so a reflected credential."""

    class Boom:
        def _request(self, method, path, *, body=None, accept=None, authed=True, api=None, deadline=None):  # noqa: ARG002
            raise RuntimeError(f"connect failed to http://user:{TAINT}@host/api")

    mapping, unavailable = view_types_mod.view_types(Boom())
    assert mapping == {}
    assert "RuntimeError" in unavailable
    assert TAINT not in unavailable


def test_the_run_warns_when_it_cannot_discriminate_at_all():
    """A "cannot establish" that is merely RETURNED becomes an unexamined variable at the call site."""
    log = _Recorder()
    views = [{"id": DASH_LUID, "name": "Revenue"}]
    unavailable = view_types_mod.resolve_and_stamp(FakeSession([(403, "no", {})]), views, log)
    assert unavailable
    assert len(log.warnings) == 1
    assert "UNKNOWN" in log.warnings[0]
    assert views[0][view_types_mod.VIEW_TYPE_KEY] == "unknown"


def test_stamp_joins_on_the_view_ID_not_on_its_NAME():
    """⚠️ The whole point of #402, and a hole the review found: keying `stamp` on `view["name"]`
    instead of `view["id"]` SURVIVED all 53 tests.

    Nothing else in the suite could see it, because every fixture's `name` was absent from the
    mapping, so a name-keyed lookup fell through to `unknown` and merely looked conservative. Here
    the name IS a key -- of the OTHER kind -- so the two joins give different, both-plausible answers
    and only the identity join gives the right one.
    """
    view = {"id": DASH_LUID, "name": SHEET_LUID}
    view_types_mod.stamp([view], {DASH_LUID: "dashboard", SHEET_LUID: "worksheet"})
    assert view[view_types_mod.VIEW_TYPE_KEY] == "dashboard"


def test_an_unmapped_view_records_unknown_not_a_default_type(tmp_path):
    """`unknown` is a real value a consumer must handle, not a synonym for worksheet."""
    view = {"id": "11111111-2222-3333-4444-555555555555", "name": "Revenue"}
    view_types_mod.stamp([view], {"99999999-9999-9999-9999-999999999999": "dashboard"})
    record = oracle.capture_view(FakeSession([(200, "", {})]), view, tmp_path, frozenset(), None)
    assert record["view_type"] == "unknown"


def test_the_manifest_censuses_view_types_so_a_consumer_reads_it_once(tmp_path):
    """A zero here must be legible as 'none of that kind', distinct from 'we could not tell'."""
    _write(tmp_path, [dict(_record("ok"), view_type="dashboard"), dict(_record("ok"), view_type="unknown")])
    manifest = json.loads((tmp_path / "oracle-manifest.json").read_text(encoding="utf-8"))
    assert manifest["view_types"] == {"dashboard": 1, "worksheet": 0, "unknown": 1}


# --- #473: maxAge query parameter, validation, and manifest persistence -------------------------
#
# Tableau REST query cache can silently serve /data, /image, and /pdf from cache.
# The tests below pin:
# 1. validate_max_age accepts integers >= 1 and rejects 0, negatives, bools, strings, and floats.
# 2. CLI parser accepts --max-age <int >= 1> and rejects 0, negative, or non-integer arguments.
# 3. HTTP request paths for /data, /image (png, svg), and /pdf include maxAge=<minutes>.
# 4. Custom max-age propagates across all request paths, capability probe URLs, and manifest output.
# 5. Manifest records max_age_minutes at the top level, per view, per leg, and in render capability.
# 6. Server rejection remains a capture failure / cannot assess, never silently retrying without maxAge.


@pytest.mark.parametrize("valid_age", [1, 15, 360])
def test_validate_max_age_accepts_positive_integers(valid_age):
    assert oracle.validate_max_age(valid_age) == valid_age


@pytest.mark.parametrize(
    "invalid_input, exc_type",
    [
        (0, ValueError),
        (-1, ValueError),
        (-100, ValueError),
        (True, TypeError),
        (False, TypeError),
        (1.5, TypeError),
        ("15", TypeError),
        (None, TypeError),
        ([1], TypeError),
    ],
)
def test_validate_max_age_refuses_invalid_types_and_values_less_than_one(invalid_input, exc_type):
    with pytest.raises(exc_type):
        oracle.validate_max_age(invalid_input)


@pytest.mark.parametrize(
    "cli_args, expected_max_age",
    [
        (["--out", "o"], 1),
        (["--out", "o", "--max-age", "1"], 1),
        (["--out", "o", "--max-age", "15"], 15),
        (["--out", "o", "--max-age=120"], 120),
    ],
)
def test_cli_parser_accepts_valid_max_age(cli_args, expected_max_age):
    parser = oracle.build_parser()
    args = parser.parse_args(cli_args)
    assert args.max_age == expected_max_age


@pytest.mark.parametrize(
    "invalid_cli_arg",
    ["0", "-1", "-5", "abc", "1.5", "None"],
)
def test_cli_parser_refuses_invalid_max_age(invalid_cli_arg):
    parser = oracle.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--out", "o", "--max-age", invalid_cli_arg])


def test_oracle_requests_send_max_age_on_data_and_all_render_endpoints(tmp_path):
    """Positive test: /data, /image (PNG/SVG) and /pdf requests all include maxAge=<minutes> in URL."""
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
    svg_bytes = b'<?xml version="1.0"?><svg width="264.848mm" height="211.931mm"><text>x</text></svg>'
    pdf_bytes = b"%PDF-1.4\n/MediaBox [0 0 822 672]\n"
    csv_bytes = b"Region,Sales\r\nWest,10\r\n"

    session = FakeSession(
        [
            (200, csv_bytes, {"Content-Type": "text/csv"}),
            (200, png_bytes, {"Content-Type": "image/png"}),
            (200, svg_bytes, {"Content-Type": "image/svg+xml"}),
            (200, pdf_bytes, {"Content-Type": "application/pdf"}),
        ]
    )
    view = {"id": DASH_LUID, "name": "Revenue", "workbook": {"id": "wb-1"}}
    record = oracle.capture_view(
        session,
        view,
        tmp_path,
        frozenset({"png", "svg", "pdf"}),
        None,
        max_age=1,
    )

    # Check that each request URL carried the maxAge query parameter
    assert len(session.calls) == 4
    assert session.calls[0] == f"/sites/{session.site_id}/views/{DASH_LUID}/data?maxAge=1"
    assert session.calls[1] == f"/sites/{session.site_id}/views/{DASH_LUID}/image?resolution=high&maxAge=1"
    assert session.calls[2] == f"/sites/{session.site_id}/views/{DASH_LUID}/image?format=svg&maxAge=1"
    assert session.calls[3] == f"/sites/{session.site_id}/views/{DASH_LUID}/pdf?type=Unspecified&maxAge=1"

    # Check that record and per-leg records disclose max_age_minutes
    assert record["max_age_minutes"] == 1
    assert record["data"]["max_age_minutes"] == 1
    assert record["image"]["max_age_minutes"] == 1
    assert record["svg"]["max_age_minutes"] == 1
    assert record["pdf"]["max_age_minutes"] == 1


def test_oracle_custom_max_age_propagates_to_requests_and_manifest(tmp_path):
    """Custom max_age (e.g. 45 min) is passed to requests and persisted to manifest."""
    csv_bytes = b"Region,Sales\r\nEast,20\r\n"
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
    session = FakeSession(
        [
            (200, csv_bytes, {"Content-Type": "text/csv"}),
            (200, png_bytes, {"Content-Type": "image/png"}),
        ]
    )
    view = {"id": DASH_LUID, "name": "Profits", "workbook": {"id": "wb-1"}}
    record = oracle.capture_view(
        session,
        view,
        tmp_path,
        frozenset({"png"}),
        None,
        max_age=45,
    )
    record["workbook_name"] = "Finance"

    assert session.calls[0] == f"/sites/{session.site_id}/views/{DASH_LUID}/data?maxAge=45"
    assert session.calls[1] == f"/sites/{session.site_id}/views/{DASH_LUID}/image?resolution=high&maxAge=45"

    env = {"TABLEAU_SERVER_URL": "https://x", "TABLEAU_SITE": "s", "TABLEAU_REST_API_VERSION": "3.29"}
    oracle.write_manifest([record], oracle.CaptureRun(session, env, tmp_path, 0.0, max_age_minutes=45))

    manifest = json.loads((tmp_path / "oracle-manifest.json").read_text(encoding="utf-8"))
    assert manifest["max_age_minutes"] == 45
    assert manifest["views"][0]["max_age_minutes"] == 45
    assert manifest["views"][0]["data"]["max_age_minutes"] == 45
    assert manifest["views"][0]["image"]["max_age_minutes"] == 45


def test_max_age_is_persisted_in_leg_records_across_failure_and_skip_outcomes(tmp_path):
    """Max-age is disclosed on legs even when data fails or renders are skipped."""
    session = FakeSession([(401, FEDERATED_CREDENTIAL, {})])
    view = {"id": DASH_LUID, "name": "Orders", "workbook": {"id": "wb-1"}}
    record = oracle.capture_view(
        session,
        view,
        tmp_path,
        frozenset({"png", "svg"}),
        None,
        max_age=10,
    )

    assert record["max_age_minutes"] == 10
    assert record["data"]["max_age_minutes"] == 10
    assert record["data"]["status"] == "source_credential"
    # Render legs were skipped due to shared root cause (source_credential)
    assert record["image"]["max_age_minutes"] == 10
    assert record["image"]["attempted"] is False
    assert record["image"]["status"] == "source_credential"
    assert record["svg"]["max_age_minutes"] == 10
    assert record["svg"]["attempted"] is False
    assert record["svg"]["status"] == "source_credential"


def test_server_rejection_of_max_age_remains_a_failure_and_never_retries_without_parameter(tmp_path):
    """Invariant: if the server rejects a request with maxAge, it fails loudly and never strips the param."""
    server_rejection = "<error code='400000'><summary>Bad Request</summary><detail>Invalid parameter: maxAge</detail></error>"
    session = FakeSession(
        [
            (400, server_rejection, {}),
        ]
    )
    view = {"id": DASH_LUID, "name": "Sales", "workbook": {"id": "wb-1"}}
    record = oracle.capture_view(
        session,
        view,
        tmp_path,
        frozenset(),
        None,
        max_age=1,
    )

    assert record["data"]["status"] == "failed"
    # Ensure there was exactly 1 call and it had maxAge=1 (no second call without maxAge)
    assert len(session.calls) == 1
    assert "maxAge=1" in session.calls[0]

