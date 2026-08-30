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

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import capture_tableau_oracle as oracle  # noqa: E402  # pylint: disable=wrong-import-position

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

    def _request(self, method, path, *, body=None, accept=None, authed=True, api=None):  # noqa: ARG002
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
            {"id": "view-id-12345678", "name": "Echo", "workbook": {"id": "wb", "name": "Workbook"}},
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
    assert oracle.detect_format(values) == expected


def test_csv_summary_proves_a_capture_is_non_empty():
    summary = oracle.summarise_csv(b"Region,Profit Ratio\nWest,19.5%\nEast,-3.0%\n")
    assert summary["row_count"] == 2
    assert summary["columns"] == ["Region", "Profit Ratio"]
    assert summary["format_hints"] == {"Profit Ratio": "percent"}


def test_empty_csv_reports_zero_rows_rather_than_failing():
    assert oracle.summarise_csv(b"")["row_count"] == 0


def test_slug_is_filesystem_safe():
    assert "/" not in oracle.safe_slug("Superstore/sheets/Overview")


# --------------------------------------------------------------------------- manifest contract


def _record(status: str, rows: int = 1, image_status: str | None = None) -> dict:
    data = {"status": status}
    if status == "ok":
        data.update({"row_count": rows, "elapsed_sec": 1.0, "reauths": 0, "retries": 0})
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
