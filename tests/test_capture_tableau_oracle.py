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
        return status, payload.encode() if isinstance(payload, str) else payload, headers

    def sign_in(self):
        self.signin_count += 1
        self.token, self.site_id = "tok", "sid"


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
        data.update({"row_count": rows, "elapsed_sec": 1.0, "reauths": 0, "retries": 0})
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
    """
    old = {"view_name": "V", "workbook_name": "W", "data": {"status": "ok"}}
    code, manifest = _manifest(tmp_path, [old])
    assert code == 0
    assert manifest["data_empty"] == 0
    assert manifest["data_empty_views"] == []
    assert "flags" not in manifest["views"][0]


def test_a_failed_data_leg_is_not_ALSO_reported_as_empty(tmp_path):
    """One root cause, counted once. A failed export's emptiness is explained by the failure."""
    _code, manifest = _manifest(tmp_path, [_record("failed"), _record("source_credential")])
    assert manifest["data_empty"] == 0
    assert manifest["data_empty_views"] == []


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
