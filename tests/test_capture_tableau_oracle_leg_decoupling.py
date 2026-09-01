"""The two capture legs are INDEPENDENT, and the request timeout is the operator's to set (#423).

Customer-reported, verified in source, reproduced offline before any fix. ``capture_view`` read:

    record["data"] = _capture_data(...)
    if record["data"]["status"] != "ok":
        return record          # the render loop below was NEVER reached

so one slow ``/data`` cost two pieces of evidence. The image was not "attempted and failed" -- it was
structurally skipped, and the record carried no ``image`` key at all, which is indistinguishable from
a capture where no render was requested.

The field evidence both halves of this file are built from:

* **Availability Summary by Tail** -- ``HTTP 0``, ``TimeoutError: read operation timed out``, three
  runs across two days (Aug 17 20:17, Aug 17 20:32, Aug 18 14:46). Three identical failures on two
  days is not a network blip; it reads as a view whose query cannot export within 180s server-side.
  No image was ever attempted, so it has no ``image`` key in any record -- an unrecoverable blind
  spot, and the reason ``--rest-timeout`` had to stop being a module constant.
* **Daily Monitoring** -- the recoverable case. Data failed twice (image never attempted), then on a
  third batch **both** succeeded: 905,098 bytes of PNG.

Why that matters more than a missing file: the one confirmed visual defect in that estate -- a
``columnChart`` stacking five airlines' percentages into a ~462% bar -- was only discoverable because
a reference image for that page happened to exist. Wherever a capture gap exists, an equivalent
fidelity bug is **structurally unfalsifiable**, not merely unverified.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import capture_tableau_oracle as oracle  # noqa: E402  # pylint: disable=wrong-import-position
import tableau_oracle_manifest as verdict  # noqa: E402  # pylint: disable=wrong-import-position

LUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
TIMEOUT_BODY = "TimeoutError: read operation timed out"
FEDERATED_CREDENTIAL = (
    "<error code='400081'><detail>com.tableausoftware.nativeapi.exceptions."
    "FederatedDataSourceException: one or more connections need attention</detail></error>"
)
SVG_GATE = f"<error code='400'><detail>{oracle.SVG_VERSION_MARKER} 3.29 or later</detail></error>"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + (800).to_bytes(4, "big") + (600).to_bytes(4, "big") + b"\x08\x02"
SVG = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>'
PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"


def _creds() -> oracle.SiteCredentials:
    return oracle.SiteCredentials(
        base="https://example.online.tableau.com",
        site="site",
        pat_name="a-long-enough-pat-name",
        pat_secret="a-long-enough-pat-secret",
        version="3.29",
    )


class FakeSession(oracle.TableauSession):
    """Routes a scripted ``(status, body, headers)`` per REQUEST PATH, not per call index.

    Keyed by path on purpose: these tests turn legs on and off, so an index-keyed script would have
    to be re-counted every time a leg is added -- and a mis-counted script fails for the wrong
    reason, which is the vacuity mode this suite is meant to avoid.
    """

    def __init__(self, routes: dict[str, list], retry=None):
        super().__init__(_creds(), retry)
        self.routes = {key: list(value) for key, value in routes.items()}
        self.calls: list[str] = []
        self.token, self.site_id = "tok", "sid"

    def _request(self, method, path, *, body=None, accept=None, authed=True, api=None):  # noqa: ARG002
        self.calls.append(path)
        for key, responses in self.routes.items():
            if key in path:
                status, payload, headers = responses[0] if len(responses) == 1 else responses.pop(0)
                return status, payload.encode() if isinstance(payload, str) else payload, headers
        raise AssertionError(f"no scripted response for {path}")

    def count(self, marker: str) -> int:
        """How many requests hit an endpoint -- the only way to see a retry that produced no record."""
        return sum(1 for call in self.calls if marker in call)

    def sign_in(self):
        self.token, self.site_id = "tok", "sid"


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Backoff must not actually sleep, but the delay is still computed."""
    monkeypatch.setattr(oracle.time, "sleep", lambda _seconds: None)


def _view() -> dict:
    return {"id": LUID, "name": "Availability Summary by Tail", "workbook": {"id": "wb-1"}}


def _capture(session, tmp_path, wants=frozenset({"png"})) -> dict:
    return oracle.capture_view(session, _view(), tmp_path, wants, None)


# ------------------------------------------------------- the defect: a skipped render, not a failed one


def test_a_failed_data_leg_no_longer_skips_the_render(tmp_path):
    """⚠️ THE anchor of this file. Restoring `if record["data"]["status"] != "ok": return record`
    must fail it -- that mutation is in the harness as `restore-the-early-return`.

    The scripted site is exactly the field signature: `/data` times out at the network level
    (`HTTP 0`) on every attempt, while `/image` would have answered. Before the fix `/image` was
    never requested, so the PNG on disk in the customer's third batch had no counterpart in the
    first two."""
    session = FakeSession(
        {"/data": [(0, TIMEOUT_BODY, {})], "/image": [(200, PNG, {"Content-Type": "image/png"})]},
        retry=oracle.RetryPolicy(max_attempts=2, budget_sec=1e6),
    )
    record = _capture(session, tmp_path)

    assert record["data"]["status"] == "transient"
    assert session.count("/image?resolution=high") == 1, "the render must be ATTEMPTED, not skipped"
    assert record["image"]["status"] == "ok"
    assert record["image"]["bytes"] == len(PNG)
    assert (tmp_path / "images" / f"{LUID}.png").is_file(), "the salvaged reference must reach disk"


def test_the_salvaged_render_is_a_real_status_not_a_placeholder(tmp_path):
    """A render attempted after a failed data leg can itself fail, and must say so with the same
    vocabulary as any other leg -- otherwise 'we tried' is indistinguishable from 'we did not'."""
    session = FakeSession(
        {"/data": [(0, TIMEOUT_BODY, {})], "/image": [(400, "ExportViewException: something else", {})]},
        retry=oracle.RetryPolicy(max_attempts=2, budget_sec=1e6),
    )
    record = _capture(session, tmp_path)

    assert record["image"]["status"] == "failed"
    assert record["image"].get("attempted") is not False, "this leg WAS attempted; do not mark it otherwise"
    assert session.count("/image?resolution=high") == 1


# ------------------------------------------------------------------------------- the cost of decoupling


def test_a_salvage_render_gets_one_attempt_and_no_retry_budget(tmp_path):
    """Decoupling must not double the wall clock on a view that is slow everywhere.

    The retry budget is PER-LEG, and a salvage leg's is zero: the data leg has already spent a full
    budget proving this view cannot answer, so re-asking is asking the same slow question again. A
    transient 503 that the session policy would retry four times is attempted exactly once here."""
    session = FakeSession(
        {"/data": [(0, TIMEOUT_BODY, {})], "/image": [(503, "gateway", {})]},
        retry=oracle.RetryPolicy(max_attempts=5, budget_sec=1e6),
    )
    record = _capture(session, tmp_path)

    assert record["image"]["status"] == "transient"
    assert session.count("/image?resolution=high") == 1, "a salvage leg must not retry"


def test_a_render_after_a_SUCCESSFUL_data_leg_keeps_the_full_session_policy(tmp_path):
    """The control that makes the test above mean something. Same 503, same session policy -- but the
    data leg succeeded, so nothing has been learned about this view being slow and the render keeps
    every retry it always had. A fix that simply capped all renders at one attempt would pass the
    test above and fail this one."""
    session = FakeSession(
        {"/data": [(200, "a,b\n1,2\n", {})], "/image": [(503, "gateway", {})]},
        retry=oracle.RetryPolicy(max_attempts=4, budget_sec=1e6),
    )
    record = _capture(session, tmp_path)

    assert record["data"]["status"] == "ok"
    assert session.count("/image?resolution=high") == 4, "a normal render keeps the session's attempts"


def test_the_first_failed_salvage_render_stops_the_rest_and_records_them(tmp_path):
    """All render routes come from the same VizQL render, so a second and third ask cost a metered
    call to learn the same thing. The remaining tiers are recorded `not_attempted` -- NOT omitted,
    and not stamped with a failure they never earned."""
    session = FakeSession(
        {"/data": [(0, TIMEOUT_BODY, {})], "/image": [(0, TIMEOUT_BODY, {})], "/pdf": [(200, PDF, {})]},
        retry=oracle.RetryPolicy(max_attempts=3, budget_sec=1e6),
    )
    record = _capture(session, tmp_path, wants=frozenset({"png", "svg", "pdf"}))

    assert record["image"]["status"] == "transient"
    assert record["svg"]["status"] == verdict.NOT_ATTEMPTED
    assert record["pdf"]["status"] == verdict.NOT_ATTEMPTED
    assert record["svg"]["attempted"] is False
    assert "image" in record["svg"]["reason"], "say WHICH leg blocked it, or the record is not actionable"
    assert session.count("/pdf") == 0, "the whole point is that the doomed tiers cost nothing"


def test_a_version_gate_does_not_stop_the_remaining_salvage_legs(tmp_path):
    """`unsupported_api_version` is a CONFIGURATION fault answered instantly with a 400 -- it is not
    evidence the view is unwell, so it must not short-circuit a tier that might still work.

    ⚠️ The tier order is fixed (`png`, `svg`, `pdf`), so the gated tier must be asked BEFORE the one
    that has to survive it, or this test asserts nothing. Measured: an earlier version of it requested
    `{"svg", "png"}`, PNG was therefore attempted first and succeeded, and the mutation that adds
    `unsupported_api_version` to `_VIEW_HEALTH_FAILURES` SURVIVED -- an assertion inside a branch the
    fixture never entered. `{"svg", "pdf"}` is what actually exercises the short-circuit.
    """
    session = FakeSession(
        {"/data": [(0, TIMEOUT_BODY, {})], "?format=svg": [(400, SVG_GATE, {})], "/pdf": [(200, PDF, {})]},
        retry=oracle.RetryPolicy(max_attempts=2, budget_sec=1e6),
    )
    record = oracle.capture_view(session, _view(), tmp_path, frozenset({"svg", "pdf"}), None)

    assert record["svg"]["status"] == "unsupported_api_version"
    assert session.count("/pdf") == 1, "the gated tier must not stop the one after it"
    assert record["pdf"]["status"] == "ok", "a version gate on one tier must not cost another tier"


# ------------------------------------------------------- the credential carve-out, and its exit code


def test_a_credential_block_still_skips_the_renders_and_they_inherit_its_status(tmp_path):
    """The one case decoupling must NOT change. All four routes fail identically on
    `data sources not connected`, so a render cannot get past it -- and inventing an independent
    failure per leg puts a purely credential-blocked view into `blocked` AND `failed` at once, where
    `failed` wins and the run exits 3 instead of the human-actionable 2."""
    session = FakeSession({"/data": [(400, FEDERATED_CREDENTIAL, {})]})
    record = oracle.capture_view(session, _view(), tmp_path, frozenset({"png", "svg"}), None)

    assert record["data"]["status"] == "source_credential"
    assert session.count("/image") == 0, "no render may be attempted once the source refused"
    assert record["image"]["status"] == "source_credential"
    assert record["svg"]["status"] == "source_credential"
    assert record["image"]["attempted"] is False


def test_a_credential_only_run_still_exits_2_after_decoupling(tmp_path):
    """The exit code is the thing an operator's shell reads, so the carve-out above is pinned end to
    end rather than only at the record."""
    session = FakeSession({"/data": [(400, FEDERATED_CREDENTIAL, {})]})
    record = oracle.capture_view(session, _view(), tmp_path, frozenset({"png"}), None)
    record["workbook_name"] = "wb"
    run = verdict.CaptureRun(
        session,
        {"TABLEAU_SERVER_URL": "https://example", "TABLEAU_SITE": "site"},
        tmp_path,
        0.0,
        frozenset({"png"}),
    )
    assert verdict.write_manifest([record], run) == 2


# ----------------------------------------------- UNESTABLISHED must not read the same as NOT REQUESTED


def test_a_requested_leg_is_never_absent_so_absent_means_not_requested(tmp_path):
    """The collapse this issue is really about. An absent `image` key used to mean EITHER 'no render
    was asked for' OR 'a render was asked for and never attempted', and nothing downstream could tell
    them apart -- so an unassessable view landed in the clean bucket."""
    blocked = FakeSession({"/data": [(400, FEDERATED_CREDENTIAL, {})]})
    asked = oracle.capture_view(blocked, _view(), tmp_path, frozenset({"png", "svg", "pdf"}), None)
    assert {"image", "svg", "pdf"} <= asked.keys(), "every REQUESTED leg carries a record"

    quiet = FakeSession({"/data": [(200, "a,b\n1,2\n", {})]})
    not_asked = oracle.capture_view(quiet, _view(), tmp_path, frozenset(), None)
    assert not {"image", "svg", "pdf"} & not_asked.keys(), "an absent leg now means exactly one thing"


def test_the_manifest_counts_and_names_the_views_with_no_establishable_render(tmp_path):
    """The count answers 'is my reference set complete'; the list answers 'on which pages can I not
    make a fidelity finding at all'. Both, because a consumer that has to re-derive it from three
    per-tier statuses will not."""
    session = FakeSession(
        {"/data": [(0, TIMEOUT_BODY, {})], "/image": [(0, TIMEOUT_BODY, {})]},
        retry=oracle.RetryPolicy(max_attempts=1, budget_sec=1e6),
    )
    record = _capture(session, tmp_path)
    record["workbook_name"] = "airborne services"
    run = verdict.CaptureRun(
        session,
        {"TABLEAU_SERVER_URL": "https://example", "TABLEAU_SITE": "site"},
        tmp_path,
        0.0,
        frozenset({"png"}),
    )
    verdict.write_manifest([record], run)
    import json  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    manifest = json.loads((tmp_path / "oracle-manifest.json").read_text(encoding="utf-8"))

    assert manifest["render_unestablished"] == 1
    named = manifest["render_unestablished_views"]
    assert named[0]["view_name"] == "Availability Summary by Tail"
    assert named[0]["renders"] == {"png": "transient"}


def test_a_view_whose_render_SUCCEEDED_is_not_unestablished():
    """Positive control: the census must be able to return zero, or `render_unestablished` is just a
    view count wearing a different name."""
    good = [{"view_luid": LUID, "view_name": "v", "image": {"status": "ok"}}]
    assert verdict.render_unestablished(good, frozenset({"png"})) == []


def test_no_render_requested_means_nothing_is_unestablished():
    """A data-only capture asked for no render, so it cannot be missing one. Without this the field
    would fire on every numeric-oracle run and be ignored within a week."""
    data_only = [{"view_luid": LUID, "view_name": "v", "data": {"status": "ok"}}]
    assert verdict.render_unestablished(data_only, frozenset()) == []


def test_one_ok_tier_is_enough_to_establish_a_reference():
    """SVG gated out but PNG obtained is a reference set, not a gap -- the tiers are alternatives."""
    mixed = [
        {
            "view_luid": LUID,
            "view_name": "v",
            "image": {"status": "ok"},
            "svg": {"status": "unsupported_api_version"},
        }
    ]
    assert verdict.render_unestablished(mixed, frozenset({"png", "svg"})) == []


# ------------------------------------------------------------------ --rest-timeout, and what moves with it


def test_the_request_timeout_reaches_the_transport(monkeypatch):
    """`REST_TIMEOUT_SEC` was a module constant, so an operator facing a genuinely slow view could
    not grant it more time without editing the script."""
    seen: list[float] = []

    def _fake_request(_req, *, timeout, redactor):  # noqa: ARG001
        seen.append(timeout)
        return 200, b'{"credentials": {"token": "t", "site": {"id": "s"}}}', {}

    monkeypatch.setattr(oracle, "_request", _fake_request)
    oracle.TableauSession(_creds(), timeout_sec=600.0).sign_in()
    assert seen == [600.0]


def test_the_default_timeout_is_unchanged_when_nothing_is_passed(monkeypatch):
    """The flag must not move the default out from under an existing runbook."""
    seen: list[float] = []

    def _fake_request(_req, *, timeout, redactor):  # noqa: ARG001
        seen.append(timeout)
        return 200, b'{"credentials": {"token": "t", "site": {"id": "s"}}}', {}

    monkeypatch.setattr(oracle, "_request", _fake_request)
    oracle.TableauSession(_creds()).sign_in()
    assert seen == [float(oracle.REST_TIMEOUT_SEC)]


def test_the_cli_accepts_a_raised_timeout_and_the_budget_follows_it():
    """⚠️ The interaction operators are surprised by, pinned. The budget is charged from BEFORE
    attempt 1, so a budget left at the default's 360s while the timeout rose to 600s would be spent
    the moment the first timeout returned -- ZERO retries at any --max-attempts, on exactly the slow
    failure the raised timeout was meant to survive."""
    args = oracle.build_parser().parse_args(["--out", "x", "--rest-timeout", "600"])
    assert args.rest_timeout == 600.0
    assert args.retry_budget is None, "an unset budget must stay unset, so it can track the timeout"

    policy = oracle.build_retry_policy(args.max_attempts, args.retry_budget, args.rest_timeout)
    assert policy.budget_sec == 1200.0
    assert policy.budget_sec > oracle.retry_admission_floor(600.0)


def test_an_explicit_budget_is_honoured_even_when_the_timeout_moves():
    """Tracking is a DEFAULT, not a clamp: a deliberately tight budget for fast-failing transients is
    a real, documented choice, and silently overriding it would defeat it."""
    args = oracle.build_parser().parse_args(["--out", "x", "--rest-timeout", "600", "--retry-budget", "45"])
    policy = oracle.build_retry_policy(args.max_attempts, args.retry_budget, args.rest_timeout)
    assert policy.budget_sec == 45.0


def test_the_floor_warning_names_the_timeout_actually_in_force(caplog):
    """Warning against the 180s constant while the operator runs at 600s sends them to the wrong
    number -- and the whole point of the warning is that the number is the fix."""
    import logging  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    with caplog.at_level(logging.WARNING, logger="tableau-oracle"):
        oracle.build_retry_policy(max_attempts=4, budget_sec=200.0, timeout_sec=600.0)
    assert "600s" in caplog.text
    assert "601s" in caplog.text, "the floor is one timeout plus the first backoff"
    assert "180" not in caplog.text, "the module default is not the timeout in force"


def test_the_floor_and_default_still_track_the_module_constant():
    """The named constants must remain this repo's two functions evaluated at the default, or the
    prose in the module header stops describing the code."""
    assert oracle.RETRY_ADMISSION_FLOOR_SEC == oracle.retry_admission_floor(oracle.REST_TIMEOUT_SEC)
    assert oracle.DEFAULT_RETRY_BUDGET_SEC == oracle.default_retry_budget(oracle.REST_TIMEOUT_SEC)
