"""Nothing derived from a Tableau response may reach a log, an exception or a manifest unredacted.

⚠️ **This is the fourth review round on the same leak**, and the first three fixes were each a
careful call site. They failed for one reason, and it is worth stating before any test below:

===== =============================================== ===========================================
round  where the secret escaped                        what ran before the redactor
===== =============================================== ===========================================
2      ``raw_get()`` error bodies                      *nothing* -- the redactor was simply absent
3      the HTTP-200 wrong-format diagnostic            the message was built and RETURNED first
3      ``format_matches`` Content-Type                 ``.lower()``
4      ``format_matches`` body head                    ``.lstrip()``, ``[:256]``, ``[:8]``
===== =============================================== ===========================================

Rounds 3 and 4 are one defect wearing three hats: **a transformation ran before redaction and
destroyed the needle the redactor searches for.** ``redact`` matches literals, so case-folding,
stripping, slicing and splitting all leave it hunting a string that is no longer in the haystack.

So this module gates the RULE rather than the four known sites:

1. :func:`tableau_env.redacted_note` is the chokepoint. It takes the value **untransformed**, redacts
   the whole of it, and only then truncates/strips/quotes. The wrong order is not expressible there.
2. ``test_every_diagnostic_site_survives_every_adversarial_secret_shape`` runs the whole inventory of
   diagnostic-producing sites against a battery of secret shapes -- one shape per historical escape,
   plus the ones nobody has tried yet.
3. ``test_no_interpolation_reaches_a_diagnostic_without_certification`` fails on ANY f-string
   interpolation in the two modules that is not on a hand-certified list. That is what makes a FIFTH
   site impossible to add silently: a new expression fails by default, and the author has to say why
   it cannot carry a credential.

Gate 3 is not decoration. Writing it found a fifth site nobody had reported: ``classify_probe``
extracted ``<detail>...</detail>`` from the RAW body, so a secret straddling ``</detail>`` was split
in two and neither half matched. It is fixed, and ``detail-tag`` below is that shape.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import capture_tableau_oracle as oracle  # noqa: E402  # pylint: disable=wrong-import-position
import tableau_render_capability as cap  # noqa: E402  # pylint: disable=wrong-import-position
from tableau_env import redacted_note  # noqa: E402  # pylint: disable=wrong-import-position

# --------------------------------------------------------------------------- the adversarial battery

# Every shape is >= NOISY_SECRET_LEN so the redactor's short-secret warning is not in play, and every
# one is a form a real PAT secret or session token could take. The first four are the measured
# escapes; the rest are the transformations nobody has tried on this path yet.
SHAPES: dict[str, str] = {
    "plain": "SYNTHETIC_SECRET_42",
    # round 4a: `.lstrip()` ran first, so the redactor's literal no longer matched.
    "leading-whitespace": " SYNTHETIC_SECRET_42",
    "trailing-whitespace": "SYNTHETIC_SECRET_42 ",
    # round 4b: a 256-byte window cut the literal in half.
    "longer-than-any-window": "S" + "YNTHETIC_SECRET_" * 20,
    # round 3b: `.lower()` ran first.
    "mixed-case": "SYNTHETIC_SeCReT_42",
    # found while writing gate 3: split by the `<detail>` capture group on the RAW body.
    "detail-tag": "SYNTHETIC</detail>_SECRET_42",
    # `repr(bytes)` escaped these, so the redactor was shown a form it had never matched.
    "quote-and-backslash": "SYNTHETIC_'SECRET\\42",
    # exercises the one transformation that MUST precede redaction: the utf-8 decode.
    "non-ascii": "SYNTHETIC_S\u00c9CRET_\u20ac42",
    # `.split(';')[0]` on a Content-Type would cut this one.
    "semicolon": "SYNTHETIC;SECRET;42",
}

MIN_RUN = 6  # a surviving run this long is a leak, not a coincidence

# A decoy per shape: identical STRUCTURE (whitespace, tags, punctuation, length) but a disjoint
# payload alphabet. Running each site twice and subtracting the decoy's output is what stops the
# detector crediting the scaffolding: the `detail-tag` secret embeds `</detail>`, which every Tableau
# error body contains anyway, so a naive substring search reported a leak that told an attacker
# nothing. A run counts only if it appears with the real secret and NOT with the decoy.
DECOYS: dict[str, str] = {
    "plain": "ZZQQWVVBBN_MMKKJ_77",
    "leading-whitespace": " ZZQQWVVBBN_MMKKJ_77",
    "trailing-whitespace": "ZZQQWVVBBN_MMKKJ_77 ",
    "longer-than-any-window": "Z" + "ZQQWVVBBN_MMKKJ_" * 20,
    "mixed-case": "ZZQQWVvBbN_MMKKJ_77",
    "detail-tag": "ZZQQWVVBB</detail>ZZMMKKJZ77",
    "quote-and-backslash": "ZZQQWVVBBN_'MMKKJ\\77",
    "non-ascii": "ZZQQWVVBB\u00c9N_MMKKJ\u20ac77",
    "semicolon": "ZZQQWVVBB;MMKKJZ;77",
}


def longest_surviving_run(secret: str, haystack: str, floor: int = MIN_RUN) -> str:
    """The longest contiguous slice of ``secret`` that reached ``haystack``. ``''`` means none did.

    Deliberately not `secret in haystack`: every historical escape was a FRAGMENT -- eight bytes, or
    sixteen, or a lowercased copy -- and an equality check would have passed for all four of them.
    """
    best = ""
    for start in range(len(secret)):
        for end in range(len(secret), start + floor, -1):
            if end - start > len(best) and secret[start:end] in haystack:
                best = secret[start:end]
                break
    return best


def leaked_run(secret: str, output: str, control: str) -> str:
    """The longest run of ``secret`` in ``output`` that a decoy run did NOT also produce."""
    for start in range(len(secret)):
        for end in range(len(secret), start + MIN_RUN, -1):
            run = secret[start:end]
            if run in output and run not in control:
                return run
    return ""


def test_the_battery_can_actually_detect_a_leak():
    """A detector that never fires would make every row below a false pass."""
    assert longest_surviving_run("SYNTHETIC_SECRET_42", "detail: SYNTHETIC_SECRET_42") == "SYNTHETIC_SECRET_42"
    assert longest_surviving_run("SYNTHETIC_SECRET_42", "got SYNTHETIC_SECRET") == "SYNTHETIC_SECRET"
    assert longest_surviving_run("SYNTHETIC_SECRET_42", "nothing here") == ""
    assert longest_surviving_run("SYNTHETIC_SECRET_42", "SYNTH") == "", "a run below the floor is not a leak"
    # ... and the differential form still fires when the decoy run did NOT produce the fragment.
    assert leaked_run("SYNTHETIC_SECRET_42", "got SYNTHETIC_SECRET", "got [REDACTED]") == "SYNTHETIC_SECRET"
    assert leaked_run("SYNTH</detail>ETIC", "x</detail>y", "x</detail>y") == "", "scaffolding is not a leak"


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_each_decoy_is_dissimilar_enough_to_be_a_valid_control(shape):
    """A decoy sharing a long run with its secret would SUBTRACT a real leak. Control of the control."""
    assert longest_surviving_run(SHAPES[shape], DECOYS[shape]) in ("", "</detail>")
    assert len(DECOYS[shape]) == len(SHAPES[shape])


# --------------------------------------------------------------------------- scripted plumbing

SVG_BODY = b'<?xml version="1.0"?><svg width="1mm" height="1mm"><text>x</text></svg>'
PNG_BODY = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
PDF_BODY = b"%PDF-1.4\n/MediaBox [0 0 822 672]\n"
VIEW = {"id": "eb00995d-1ff1-4a42-9ac9-28846f861d31", "name": "HR | Summary", "workbook": {"id": "wb"}}
ENV = {"TABLEAU_SERVER_URL": "https://s", "TABLEAU_SITE": "site", "TABLEAU_REST_API_VERSION": "3.29"}


class _Counter:
    reauth_count = 0
    retry_count = 0


class _Session(oracle.TableauSession):
    """A session whose PAT secret IS the planted value, so the real redactor is under test."""

    def __init__(self, secret: str, reply=(200, b"", {})):
        super().__init__(
            oracle.SiteCredentials(
                base="https://example.online.tableau.com",
                site="site",
                pat_name="a-long-enough-pat-name",
                pat_secret=secret,
                version="3.29",
            ),
            oracle.RetryPolicy(max_attempts=1, budget_sec=1),
        )
        self.reply = reply
        self.token, self.site_id = "tok", "sid"

    def _request(self, method, path, *, body=None, accept=None, authed=True, api=None):  # noqa: ARG002
        if path.endswith("/data"):
            return 200, b"a\n1\n", {}
        return self.reply


def _reflected(secret: str) -> bytes:
    """An error body of the shape a reflecting proxy or WAF actually produces."""
    return f"<error code='400000'><summary>Bad Request</summary><detail>echo {secret}</detail></error>".encode()


def _session_lost(secret: str) -> bytes:
    """The same reflection, but carrying 401002 -- the ONE class whose detail is empty by design."""
    return f"<error code='401002'><summary>Unauthorized</summary><detail>echo {secret}</detail></error>".encode()


def _r(secret: str):
    """The REAL redactor, wired exactly as production wires it."""
    return _Session(secret).redact_text


# --------------------------------------------------------------------------- the site inventory
#
# One entry per place a Tableau response can become text a human or a file sees. Each returns the
# string that would actually be printed, raised or persisted. Adding a diagnostic without adding it
# here is what gate 3 below refuses.


def site_raw_get_then_redact_text(secret, _tmp, _mp):
    """Round 2: `raw_get` returns RAW by contract; `redact_text` is the caller's obligation."""
    session = _Session(secret, (401, _reflected(secret), {}))
    _status, payload, _ctype = session.raw_get("/sites/sid/views/v/image?format=svg")
    return session.redact_text(payload.decode("utf-8", "replace"))


def site_classify_probe_wrong_format_body(secret, _tmp, _mp):
    """Round 4: the body head quote."""
    return cap.classify_probe(200, (secret + " <html>").encode(), kind="svg", redactor=_r(secret))[1]


def site_classify_probe_wrong_format_content_type(secret, _tmp, _mp):
    """Round 3b: the reflected Content-Type."""
    return cap.classify_probe(200, SVG_BODY, kind="svg", content_type=f"image/{secret}", redactor=_r(secret))[1]


def site_classify_probe_error_detail(secret, _tmp, _mp):
    """The fifth site: `<detail>` was extracted from the RAW body before redaction."""
    return cap.classify_probe(400, _reflected(secret), kind="svg", redactor=_r(secret))[1]


def site_format_matches_body(secret, _tmp, _mp):
    return cap.format_matches("svg", (secret + " <html>").encode(), None, redactor=_r(secret))[1]


def site_format_matches_content_type(secret, _tmp, _mp):
    return cap.format_matches("svg", SVG_BODY, f"image/{secret}", redactor=_r(secret))[1]


def site_classify_export_error(secret, _tmp, _mp):
    """All three detail branches: transient, credential and generic."""
    body = f"FederatedDataSourceException adb.example.net: Tableau needs {secret}"
    return " | ".join(
        oracle.classify_export_error(status, text, redactor=_r(secret))[1]
        for status, text in ((503, secret), (400, body), (404, secret))
    )


def site_sign_in_failure(secret, _tmp, _mp):
    """The sign-in POST carries the PAT, so a reflecting endpoint echoes it verbatim."""
    session = _Session(secret, (403, _reflected(secret), {}))
    with pytest.raises(RuntimeError) as excinfo:
        session.sign_in()
    return str(excinfo.value)


def site_get_json_failure(secret, _tmp, _mp):
    session = _Session(secret, (400, _reflected(secret), {}))
    with pytest.raises(RuntimeError) as excinfo:
        session.get_json("/sites/sid/views")
    return str(excinfo.value)


def site_export_failure(secret, _tmp, _mp):
    session = _Session(secret, (400, _reflected(secret), {}))
    with pytest.raises(oracle.ExportFailed) as excinfo:
        session.export("/sites/sid/views/v/image?format=svg")
    return f"{excinfo.value} :: {excinfo.value.detail}"


def site_export_session_lost_fallback(secret, _tmp, _mp):
    """`export`'s `detail or redacted_note(payload, ...)` fallback.

    ⚠️ Added because a mutation SURVIVED. Slicing the raw payload there leaked, and nothing noticed:
    the fallback is reached only when the detail is empty, which happens for exactly one classification
    -- ``session_lost`` -- and only after re-auth is exhausted. Every other site in this inventory
    produces a non-empty detail, so the `or` branch was dead to the whole battery.
    """

    class _LostSession(_Session):
        def __init__(self):
            super().__init__(secret, (401, _session_lost(secret), {}))
            self.retry = oracle.RetryPolicy(max_attempts=3, budget_sec=1)

        def sign_in(self):  # a re-auth that "succeeds" and still meets the same dead session
            self.token, self.site_id = "tok", "sid"

    with pytest.raises(oracle.ExportFailed) as excinfo:
        _LostSession().export("/sites/sid/views/v/image?format=svg")
    assert excinfo.value.detail, "the fallback branch produced nothing -- this site is not exercising it"
    return f"{excinfo.value} :: {excinfo.value.detail}"


def site_capture_render_record(secret, tmp_path, _mp):
    session = _Session(secret, (200, (secret + " and then some html").encode(), {}))
    return json.dumps(oracle.capture_view(session, VIEW, tmp_path, frozenset({"svg"})))


def site_written_manifest(secret, tmp_path, _mp):
    """THE level the leak keeps escaping at: the bytes of `oracle-manifest.json` on disk."""
    session = _Session(secret, (200, (secret + " and then some html").encode(), {}))
    record = oracle.capture_view(session, VIEW, tmp_path, frozenset({"svg"}))
    record["workbook_name"] = "W"
    oracle.write_manifest([record], oracle.CaptureRun(_Counter(), ENV, tmp_path, 0.0, frozenset({"svg"})))
    return (tmp_path / "oracle-manifest.json").read_text(encoding="utf-8")


def site_written_manifest_from_a_failed_data_leg(secret, tmp_path, _mp):
    """The other manifest route: `_capture_data`'s ExportFailed detail."""
    session = _Session(secret, (400, _reflected(secret), {}))
    record = oracle.capture_view(session, VIEW, tmp_path, frozenset({"svg"}))
    record["workbook_name"] = "W"
    oracle.write_manifest([record], oracle.CaptureRun(_Counter(), ENV, tmp_path, 0.0, frozenset({"svg"})))
    return (tmp_path / "oracle-manifest.json").read_text(encoding="utf-8")


def site_capability_report(secret, _tmp, monkeypatch):
    """`render_capability` is serialised into the manifest, warnings and per-tier details included."""
    monkeypatch.setattr(cap, "server_info", lambda *_a, **_k: {"rest_api_version": "3.30"})
    session = _Session(secret, (400, _reflected(secret), {}))
    report = cap.probe_render_capability(session, ENV, [{"id": "v1", "name": "View 1"}])
    return json.dumps(report)


def site_pin_warning(secret, _tmp, _mp):
    """`_add_pin_warnings` quotes a floor re-probe's detail back into a warning."""
    gated = f"<error><detail>requires API version 3.29 or later {secret}</detail></error>".encode()

    def fetch(_endpoint, _query, api=None):
        return (400, gated if api is None else _reflected(secret), None)

    report = cap.detect(fetch, "l", cap.ApiVersions("3.21", "3.30"), redactor=_r(secret))
    return json.dumps(report)


SITES = {
    "raw_get->redact_text": site_raw_get_then_redact_text,
    "classify_probe/body": site_classify_probe_wrong_format_body,
    "classify_probe/content-type": site_classify_probe_wrong_format_content_type,
    "classify_probe/<detail>": site_classify_probe_error_detail,
    "format_matches/body": site_format_matches_body,
    "format_matches/content-type": site_format_matches_content_type,
    "classify_export_error": site_classify_export_error,
    "sign_in": site_sign_in_failure,
    "get_json": site_get_json_failure,
    "export": site_export_failure,
    "export/session-lost fallback": site_export_session_lost_fallback,
    "capture_view record": site_capture_render_record,
    "written manifest (render)": site_written_manifest,
    "written manifest (data)": site_written_manifest_from_a_failed_data_leg,
    "render_capability report": site_capability_report,
    "pin warning": site_pin_warning,
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("site", sorted(SITES))
def test_every_diagnostic_site_survives_every_adversarial_secret_shape(site, shape, tmp_path, monkeypatch):
    """The whole matrix. A single cell failing is a credential on disk.

    Each cell runs the site TWICE -- once with the secret, once with a structurally identical decoy --
    and only counts a fragment the decoy run did not also produce, so the harness's own markup can
    never be scored as a leak.
    """
    secret, decoy = SHAPES[shape], DECOYS[shape]
    output = SITES[site](secret, tmp_path / "real", monkeypatch)
    control = SITES[site](decoy, tmp_path / "decoy", monkeypatch)
    leaked = leaked_run(secret, output, control)
    assert not leaked, f"{site} leaked {leaked!r} of the {shape} secret into: {output[:400]!r}"


@pytest.mark.parametrize("site", sorted(SITES))
def test_every_site_still_produces_a_usable_diagnostic(site, tmp_path, monkeypatch):
    """Redaction that empties the message is not a fix -- it destroys the reason the message exists."""
    output = SITES[site](SHAPES["plain"], tmp_path, monkeypatch)
    assert len(output.strip()) > 10, f"{site} produced nothing diagnosable: {output!r}"


# --------------------------------------------------------------------------- gate 3: certification

# Every f-string interpolation in the two modules, with the reason it cannot carry a credential.
# A new one FAILS BY DEFAULT: a fifth site cannot be added silently, only certified deliberately.
_SAFE_BY_CONSTRUCTION = "redacted through tableau_env.redacted_note, or derived from something that was"
_NOT_RESPONSE_DATA = "our own request/config data -- never derived from a Tableau response body"
_FIXED_VOCABULARY = "resolves to one of a fixed set of literals in this module"
_CLIENT_EXCEPTION = "an http/urllib exception's own text; carries the transport reason, not our credential"

CERTIFIED: dict[str, dict[str, str]] = {
    "scripts/capture_tableau_oracle.py": {
        "', '.join(marks)": _FIXED_VOCABULARY,
        "DEFAULT_MAX_ATTEMPTS": _NOT_RESPONSE_DATA,
        "DEFAULT_RETRY_BUDGET_SEC": _NOT_RESPONSE_DATA,
        "REST_TIMEOUT_SEC": _NOT_RESPONSE_DATA,
        "SVG_MIN_API_VERSION": _NOT_RESPONSE_DATA,
        "_RENDER_EXTENSIONS[kind]": _FIXED_VOCABULARY,
        "api or self._creds.version": _NOT_RESPONSE_DATA,
        "data['reauths']": "an integer counter",
        "data['retries']": "an integer counter",
        "endpoint": _FIXED_VOCABULARY,
        "exc": _CLIENT_EXCEPTION,
        "label": _FIXED_VOCABULARY,
        "last": _SAFE_BY_CONSTRUCTION,
        "match.group(1)": _SAFE_BY_CONSTRUCTION + " (the regex runs on `safe`, not on `text`)",
        "match.group(2).strip()": _SAFE_BY_CONSTRUCTION + " (the regex runs on `safe`, not on `text`)",
        "path": _NOT_RESPONSE_DATA,
        "query": _FIXED_VOCABULARY,
        "read_exc": _CLIENT_EXCEPTION,
        "redacted_note(payload, self._redact_response, limit=200)": _SAFE_BY_CONSTRUCTION,
        "safe[:150]": _SAFE_BY_CONSTRUCTION,
        "safe[:200]": _SAFE_BY_CONSTRUCTION,
        "safe_slug(view.get('name', ''))": _NOT_RESPONSE_DATA + " (a view name, filesystem-sanitised)",
        "self._creds.base.rstrip('/')": _NOT_RESPONSE_DATA,
        "self.retry.max_attempts": _NOT_RESPONSE_DATA,
        "session.site_id": _NOT_RESPONSE_DATA + " (a site LUID, not a credential)",
        "status": "an HTTP status integer",
        "stem": _NOT_RESPONSE_DATA,
        "type(exc).__name__": _CLIENT_EXCEPTION,
        "type(read_exc).__name__": _CLIENT_EXCEPTION,
        "view_luid": _NOT_RESPONSE_DATA,
        "view_luid[:8]": _NOT_RESPONSE_DATA,
    },
    "scripts/tableau_render_capability.py": {
        "', '.join(blocked)": _FIXED_VOCABULARY + " (tier names)",
        "SERVERINFO_PROBE_VERSION": _NOT_RESPONSE_DATA,
        "_identify(head)": _FIXED_VOCABULARY + " -- `_identify` returns one of four literals",
        "api": _NOT_RESPONSE_DATA,
        "api_version": _NOT_RESPONSE_DATA,
        "base.rstrip('/')": _NOT_RESPONSE_DATA,
        "chosen": _FIXED_VOCABULARY + " (a tier name)",
        "endpoint": _FIXED_VOCABULARY,
        "exc": _CLIENT_EXCEPTION,
        "expected": _FIXED_VOCABULARY + " (a MIME type from CONTENT_TYPES)",
        "kind": _FIXED_VOCABULARY,
        "note": _SAFE_BY_CONSTRUCTION,
        "query": _FIXED_VOCABULARY,
        "received": _SAFE_BY_CONSTRUCTION,
        "reprobe['detail'][:80]": _SAFE_BY_CONSTRUCTION + " (a classify_probe detail, truncated after)",
        "reprobe['verdict']": _FIXED_VOCABULARY,
        "session.site_id": _NOT_RESPONSE_DATA,
        "site_id": _NOT_RESPONSE_DATA,
        "status": "an HTTP status integer",
        "tier.min_api": _NOT_RESPONSE_DATA,
        "tier.name": _FIXED_VOCABULARY,
        "type(exc).__name__": _CLIENT_EXCEPTION,
        "version": _NOT_RESPONSE_DATA,
        "versions.advertised": "the server's own version string from /serverinfo, an unauthenticated call",
        "versions.configured": _NOT_RESPONSE_DATA,
        "view_luid": _NOT_RESPONSE_DATA,
        "why": _SAFE_BY_CONSTRUCTION + " (format_matches' return, whose parts are redacted_note'd)",
    },
}


def _interpolations(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.FormattedValue):
                    found.add(ast.unparse(part.value))
    return found


@pytest.mark.parametrize("module", sorted(CERTIFIED))
def test_no_interpolation_reaches_a_diagnostic_without_certification(module):
    """A FIFTH site cannot be added silently -- only certified deliberately.

    Certification is by EXPRESSION, not by variable name. `_identify(head)` is certified; a bare
    `{head}` is a different expression and fails, which is the whole point: the four historical
    escapes were all a familiar name wearing a new transformation.
    """
    actual = _interpolations(REPO / module)
    uncertified = sorted(actual - set(CERTIFIED[module]))
    assert not uncertified, (
        f"{module} interpolates {uncertified} into an f-string with no certification. Either route the "
        f"value through tableau_env.redacted_note(), or add the exact expression to CERTIFIED with a "
        f"one-line reason it cannot carry a Tableau credential."
    )


@pytest.mark.parametrize("module", sorted(CERTIFIED))
def test_the_certification_list_has_no_stale_entries(module):
    """A certification for an expression that no longer exists is a claim about nothing."""
    stale = sorted(set(CERTIFIED[module]) - _interpolations(REPO / module))
    assert not stale, f"{module}: certified expressions that are no longer in the source: {stale}"


def test_every_certification_carries_a_reason():
    for module, entries in CERTIFIED.items():
        for expression, reason in entries.items():
            assert len(reason) > 15, f"{module}: {expression!r} is certified with no real reason"


# ------------------------------------------------- the reviewer's two round-4 reproductions, named


def test_a_secret_with_leading_whitespace_never_reaches_the_written_manifest(tmp_path):
    """Measured before the fix: `.lstrip()` ran first, so the literal redactor no longer matched and
    `oracle-manifest.json` carried `expected an <svg> root, got unrecognised bytes ('SYNTHETIC_SECRET')`."""
    secret = " SYNTHETIC_SECRET_42"
    manifest = site_written_manifest(secret, tmp_path, None)
    assert "SYNTHETIC_SECRET" not in manifest
    assert "[REDACTED]" in manifest
    assert json.loads(manifest)["views"][0]["svg"]["status"] == "format_mismatch"


def test_a_secret_longer_than_any_window_never_reaches_the_written_manifest(tmp_path):
    """Measured before the fix: a 321-character secret was cut by the 256-byte window, so the
    redactor's literal was absent and the first 16 characters persisted verbatim."""
    secret = "S" + "YNTHETIC_SECRET_" * 20
    manifest = site_written_manifest(secret, tmp_path, None)
    assert longest_surviving_run(secret, manifest) == ""
    assert "[REDACTED]" in manifest


# --------------------------------------------------------------------------- the chokepoint itself


def test_redacted_note_redacts_before_it_truncates():
    """The single property every fix in four rounds was trying to have."""
    secret = "S" + "E" * 400
    assert redacted_note(secret + "tail", lambda t: t.replace(secret, "[R]"), limit=8) == "[R]tail"


def test_redacted_note_redacts_before_it_strips():
    secret = "  SYNTHETIC_SECRET_42"
    assert "SYNTHETIC" not in redacted_note(secret + "!", lambda t: t.replace(secret, "[R]"), limit=80)


def test_redacted_note_quotes_after_redacting_not_before():
    """`repr(bytes)` escapes quotes and non-ASCII, hiding the literal from the redactor."""
    secret = "tok'en\\42x"
    out = redacted_note((secret + "rest").encode(), lambda t: t.replace(secret, "[R]"), limit=40, quote=True)
    assert out == "'[R]rest'"


def test_redacted_note_output_is_ascii_safe_for_a_cp1252_console():
    redacted_note(PNG_BODY, None, limit=16, quote=True).encode("cp1252")


def test_redacted_note_without_a_redactor_does_not_pretend_to_redact():
    """Pins the opt-in, so a call site that forgets one is caught by the battery, not masked here."""
    assert "SYNTHETIC" in redacted_note("SYNTHETIC_SECRET_42", None, limit=80)
