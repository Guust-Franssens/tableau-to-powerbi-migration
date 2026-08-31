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
    """A `run.session` stand-in. It carries a REAL redactor on purpose.

    `write_manifest` scrubs the whole manifest through `run.session.redact_text` immediately before
    serialising, so a double with a pass-through redactor would silently switch the sink off in every
    test that uses it -- and the sink is the thing under test here.
    """

    reauth_count = 0
    retry_count = 0

    def __init__(self, secret: str = ""):
        self._redact = _Session(secret).redact_text if secret else (lambda text: text)

    def redact_text(self, text: str) -> str:
        return self._redact(text)


class _Session(oracle.TableauSession):
    """A session whose PAT secret IS the planted value, so the real redactor is under test."""

    def __init__(self, secret: str, reply=(200, b"", {}), *, data_reply=None, pat_name="a-long-enough-pat-name"):
        super().__init__(
            oracle.SiteCredentials(
                base="https://example.online.tableau.com",
                site="site",
                pat_name=pat_name,
                pat_secret=secret,
                version="3.29",
            ),
            oracle.RetryPolicy(max_attempts=1, budget_sec=1),
        )
        self.reply = reply if len(reply) == 3 else (*reply, {})
        # A clean `/data` by default, so a render-leg test is not masked by its own prerequisite.
        self.data_reply = (*data_reply, {}) if data_reply and len(data_reply) == 2 else data_reply
        self.token, self.site_id = "tok", "sid"

    def _request(self, method, path, *, body=None, accept=None, authed=True, api=None):  # noqa: ARG002
        if path.endswith("/data"):
            return self.data_reply or (200, b"a\n1\n", {})
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


def _capture_and_read_everything(secret, tmp_path, reply, wants, data_reply=None, session=None):
    """Run one capture and return the manifest text PLUS the bytes of every file it wrote.

    ⚠️ Reading the FILES, not only the manifest, is the round-5 lesson. A successful `/data` body is
    written verbatim to `data/<view>.csv`, and a successful `?format=svg` body to `images/<view>.svg`,
    both BEFORE any manifest exists -- so a manifest-only assertion is blind to the larger artifact,
    and a manifest-boundary scrub could never have reached it either.
    """
    session = session or _Session(secret, reply, data_reply=data_reply)
    record = oracle.capture_view(session, VIEW, tmp_path, wants)
    record["workbook_name"] = "W"
    counter = _Counter()
    counter._redact = session.redact_text  # pylint: disable=protected-access
    oracle.write_manifest([record], oracle.CaptureRun(counter, ENV, tmp_path, 0.0, wants))
    written = [
        path.read_bytes().decode("utf-8", "replace")
        for path in sorted(tmp_path.rglob("*"))
        if path.is_file() and path.name != "oracle-manifest.json"
    ]
    return "\n".join([(tmp_path / "oracle-manifest.json").read_text(encoding="utf-8"), *written])


def site_written_manifest(secret, tmp_path, _mp):
    """A 200 in the WRONG format: the diagnostic route, plus whatever it wrote."""
    return _capture_and_read_everything(
        secret, tmp_path, (200, (secret + " and then some html").encode()), frozenset({"svg"})
    )


def site_written_manifest_from_a_failed_data_leg(secret, tmp_path, _mp):
    """The other manifest route: `_capture_data`'s ExportFailed detail."""
    return _capture_and_read_everything(secret, tmp_path, (400, _reflected(secret)), frozenset({"svg"}))


def site_successful_csv(secret, tmp_path, _mp):
    """⚠️ ROUND 5. A perfectly successful `/data`, HTTP 200, whose body echoes the credential.

    `summarise_csv` copied its first row into `data.columns` and `_capture_data` wrote the bytes to
    `data/<view>.csv`. Neither is a diagnostic, so four rounds of source-side diagnostic rules could
    not see it, and 334 tests passed. This is the route that must never leave the battery again.
    """
    return _capture_and_read_everything(
        secret, tmp_path, (200, b""), frozenset(), data_reply=(200, f"{secret}\nv\n".encode())
    )


def site_successful_svg(secret, tmp_path, _mp):
    """The same shape one leg over: a VALID svg whose text content echoes the credential."""
    body = f'<?xml version="1.0"?><svg width="1mm" height="1mm"><text>{secret}</text></svg>'.encode()
    return _capture_and_read_everything(secret, tmp_path, (200, body), frozenset({"svg"}))


def site_successful_csv_reflecting_the_session_token(secret, tmp_path, _mp):
    """The seam must cover BOTH authenticating halves, not just the PAT secret.

    ⚠️ Added because a mutation SURVIVED: dropping the session-token arm of `reflected_credential`
    broke nothing, since every other site in this inventory plants its value as the PAT *secret*. The
    live token authorises the same session and is the half a reflecting proxy is most likely to echo,
    because it travels in a header on every single request.
    """
    session = _Session("an-unrelated-long-pat-secret", (200, b""), data_reply=(200, f"{secret}\nv\n".encode()))
    session.token = secret
    return _capture_and_read_everything(secret, tmp_path, (200, b""), frozenset(), session=session)


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
    "written artifacts (successful /data)": site_successful_csv,
    "written artifacts (successful svg)": site_successful_svg,
    "written artifacts (session token reflected)": site_successful_csv_reflecting_the_session_token,
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


# ------------------------------------------------ gate 3: PROVENANCE, per occurrence, all sinks
#
# ⚠️ Rewritten after round 5 found two holes in the first version, which keyed certification on
# `ast.unparse()` text GLOBALLY and looked only at f-strings:
#
#   * a name certified in one function certified the same name everywhere, so
#     `def leak(body): why = body.decode(...); return f"{why}"` passed with an EMPTY uncertified set;
#   * non-f-string sinks were invisible entirely -- including dict values, which is exactly how a
#     successful CSV's header row reached `data.columns`.
#
# So this version tracks PROVENANCE rather than spelling. Response-derived data is tainted at the
# parameters it arrives on, propagated through assignments to a fixpoint, cleared only by
# `redacted_note(...)`, and every sink -- f-string, dict value, dict `**` unpack, log/exception
# argument, `%` operand -- is checked against the tainted set of ITS OWN function.

RESPONSE_PARAMS = {"body", "payload", "content_type", "raw_type", "text", "raw"}
UNTAINTING = {"redacted_note"}
TAINTING_CALLS = {"_request", "export", "raw_get", "read", "decode"}
LOG_AND_RAISE = {"info", "warning", "error", "debug", "exception", "ExportFailed", "RuntimeError", "format"}

# A certification must state WHICH justification applies. Free prose lets "it's fine" stand in for an
# argument; a category has to be one of these, and each is independently checkable by a reader.
CATEGORIES = (
    "NOT-A-STRING:",  # an int/float/bool -- cannot carry a credential at all
    "REDACTED-UPSTREAM:",  # already through the redactor; every transform here happens after
    "REFUSED-AT-SEAM:",  # response-derived text, but `export()` refuses a 200 carrying a credential
    "DERIVED-IRREVERSIBLY:",  # a one-way digest of the payload, not the payload
    "FIXED-VOCABULARY:",  # resolves to one of a fixed set of literals in this module
)

CERTIFIED: dict[tuple[str, str], dict[str, str]] = {
    ("scripts/capture_tableau_oracle.py", "classify_export_error"): {
        "match.group(1)": "REDACTED-UPSTREAM: the regex runs on `safe`, the redacted copy, never on `text`",
        "match.group(2).strip()": "REDACTED-UPSTREAM: same match, and `.strip()` runs after redaction",
        "safe[:150]": "REDACTED-UPSTREAM: `safe` is `redactor(text)`; the slice is after",
        "safe[:200]": "REDACTED-UPSTREAM: `safe` is `redactor(text)`; the slice is after",
    },
    ("scripts/capture_tableau_oracle.py", "summarise_csv"): {
        # ⚠️ THE ROUND-5 LEAK, now certified rather than invisible. A CSV header row IS response text
        # and this gate must keep saying so; what makes it safe is the seam, not this function.
        "header": (
            "REFUSED-AT-SEAM: a successful body carrying the PAT secret or session token never reaches "
            "here -- `export()` raises `credential_reflected` before anything is parsed or written -- "
            "and the manifest sink scrubs the residual PAT-name case"
        ),
        "len(body)": "NOT-A-STRING: an integer byte count",
    },
    ("scripts/capture_tableau_oracle.py", "png_dimensions"): {
        "int.from_bytes(payload[16:20], 'big')": "NOT-A-STRING: an integer read from the IHDR",
        "int.from_bytes(payload[20:24], 'big')": "NOT-A-STRING: an integer read from the IHDR",
    },
    ("scripts/capture_tableau_oracle.py", "svg_facts"): {
        "text.count('<text')": "NOT-A-STRING: an integer element count",
        "text.count('<path')": "NOT-A-STRING: an integer element count",
        "text.count('<image')": "NOT-A-STRING: an integer element count",
        "len([h for h in _SVG_HREF.findall(text) if not h.startswith(('data:', '#'))])": (
            "NOT-A-STRING: an integer count of external references"
        ),
    },
    ("scripts/capture_tableau_oracle.py", "pdf_facts"): {
        "len(re.findall(b'/FontFile\\\\d?', payload))": "NOT-A-STRING: an integer count of embedded fonts",
        "len(re.findall(b'/Subtype\\\\s*/Image', payload))": "NOT-A-STRING: an integer count of image XObjects",
        "round(float(parts[2]))": "NOT-A-STRING: an integer page width in points",
        "round(float(parts[3]))": "NOT-A-STRING: an integer page height in points",
    },
    ("scripts/capture_tableau_oracle.py", "_capture_data"): {
        "summarise_csv(payload)": (
            "REFUSED-AT-SEAM: this unpack is what put a credential in `data.columns`; `export()` now "
            "refuses a 200 carrying one before `_capture_data` sees the payload at all"
        ),
        "stats": "REDACTED-UPSTREAM: counters, plus `retry_reasons` whose entries are redacted details",
        "hashlib.sha256(payload).hexdigest()": "DERIVED-IRREVERSIBLY: a one-way digest, not the payload",
        "len(payload)": "NOT-A-STRING: an integer byte count",
        "round(elapsed, 2)": "NOT-A-STRING: a float duration in seconds",
    },
    ("scripts/capture_tableau_oracle.py", "_capture_render"): {
        "why": "REDACTED-UPSTREAM: `format_matches` builds it entirely from `redacted_note` output",
        "stats": "REDACTED-UPSTREAM: counters, plus `retry_reasons` whose entries are redacted details",
        "hashlib.sha256(payload).hexdigest()": "DERIVED-IRREVERSIBLY: a one-way digest, not the payload",
        "len(payload)": "NOT-A-STRING: an integer byte count",
        "round(elapsed, 2)": "NOT-A-STRING: a float duration in seconds",
    },
    ("scripts/capture_tableau_oracle.py", "sign_in"): {
        "status": "NOT-A-STRING: an HTTP status integer",
    },
    ("scripts/capture_tableau_oracle.py", "get_json"): {
        "status": "NOT-A-STRING: an HTTP status integer",
    },
    ("scripts/capture_tableau_oracle.py", "export"): {
        "status": "NOT-A-STRING: an HTTP status integer",
        "delay": "NOT-A-STRING: a float backoff delay",
        "kind": "FIXED-VOCABULARY: one of classify_export_error's five status literals",
        "reflected": "FIXED-VOCABULARY: one of the two literal labels reflected_credential can return",
        "detail": "REDACTED-UPSTREAM: classify_export_error builds it from `safe`",
        "detail[:60]": "REDACTED-UPSTREAM: classify_export_error builds it from `safe`; the slice is after",
        "detail or redacted_note(payload, self._redact_response, limit=200)": (
            "REDACTED-UPSTREAM: both arms are redacted -- `detail` from `safe`, the fallback by the chokepoint"
        ),
    },
    ("scripts/tableau_render_capability.py", "format_matches"): {
        "_identify(head)": "FIXED-VOCABULARY: `_identify` returns one of four literals and never quotes bytes",
    },
    ("scripts/tableau_render_capability.py", "classify_probe"): {
        "why": "REDACTED-UPSTREAM: `format_matches` builds it entirely from `redacted_note` output",
    },
}


def _roots(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _called(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    return node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)


def tainted_names(func: ast.AST) -> set[str]:
    """Names in ``func`` holding data that came off the wire. Fixpoint over assignments."""
    args = func.args
    tainted = {a.arg for a in args.args + args.kwonlyargs if a.arg in RESPONSE_PARAMS}
    for _ in range(8):
        before = set(tainted)
        for node in ast.walk(func):
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            if _called(value) in UNTAINTING:
                continue  # the chokepoint is the ONE thing that clears taint
            if not (_roots(value) & tainted or _called(value) in TAINTING_CALLS):
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    tainted.add(target.id)
                elif isinstance(target, (ast.Tuple, ast.List)):
                    tainted.update(e.id for e in target.elts if isinstance(e, ast.Name))
        if tainted == before:
            break
    return tainted


def sink_expressions(func: ast.AST) -> list[tuple[str, ast.AST]]:
    """Every place a value in ``func`` becomes text that is printed, raised or persisted."""
    out: list[tuple[str, ast.AST]] = []
    for node in ast.walk(func):
        if isinstance(node, ast.JoinedStr):
            out += [("f-string", p.value) for p in node.values if isinstance(p, ast.FormattedValue)]
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if not isinstance(value, ast.Constant):
                    out.append(("dict-**" if key is None else "dict-value", value))
        elif isinstance(node, ast.Call) and _called(node) in LOG_AND_RAISE:
            out += [("call-arg", a) for a in node.args if not isinstance(a, (ast.Constant, ast.JoinedStr))]
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            out.append(("percent", node.right))
    return out


def uncertified_sinks(source: str, module: str) -> list[str]:
    """Response-derived expressions reaching a sink with no certification for THAT function."""
    findings = []
    for func in [n for n in ast.walk(ast.parse(source)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        tainted = tainted_names(func)
        if not tainted:
            continue
        certified = CERTIFIED.get((module, func.name), {})
        for kind, expr in sink_expressions(func):
            if _called(expr) in UNTAINTING or not (_roots(expr) & tainted):
                continue
            text = ast.unparse(expr)
            if text not in certified:
                findings.append(f"{func.name}() {kind}: {text}")
    return sorted(set(findings))


@pytest.mark.parametrize("module", sorted({m for m, _ in CERTIFIED}))
def test_no_response_derived_value_reaches_a_sink_without_certification(module):
    """A SIXTH escape cannot be added silently -- only certified deliberately, per function."""
    uncertified = uncertified_sinks((REPO / module).read_text(encoding="utf-8"), module)
    assert not uncertified, (
        f"{module} lets response-derived data reach a sink with no certification:\n  "
        + "\n  ".join(uncertified)
        + "\nEither route it through tableau_env.redacted_note(), or certify the exact expression under "
        f"CERTIFIED[({module!r}, '<function>')] with a reason starting with one of {CATEGORIES}."
    )


def test_the_gate_catches_the_reviewers_own_counterexample():
    """The hole that made this rewrite necessary: a certified NAME reused in a different function.

    `why` is legitimately certified inside `classify_probe`. Under the old global, expression-keyed
    gate that certified `why` EVERYWHERE, so this leak produced an empty uncertified set.
    """
    leak = 'def reachable_leak(body):\n    why = body.decode("utf-8", "replace")\n    return f"diagnostic: {why}"\n'
    findings = uncertified_sinks(leak, "scripts/tableau_render_capability.py")
    assert findings == ["reachable_leak() f-string: why"], findings


def test_the_gate_sees_a_non_f_string_sink():
    """The other hole: round 5 escaped through a DICT VALUE, which the old gate never looked at."""
    leak = "def build(payload):\n    columns = payload.decode().split(',')\n    return {'columns': columns}\n"
    assert uncertified_sinks(leak, "scripts/capture_tableau_oracle.py") == ["build() dict-value: columns"]


def test_the_chokepoint_is_the_only_thing_that_clears_taint():
    """Otherwise a certification could be earned by laundering a value through any helper."""
    clean = 'def f(body):\n    note = redacted_note(body, None, limit=8)\n    return f"{note}"\n'
    assert uncertified_sinks(clean, "scripts/tableau_render_capability.py") == []
    laundered = 'def f(body):\n    note = str(body)\n    return f"{note}"\n'
    assert uncertified_sinks(laundered, "scripts/tableau_render_capability.py") == ["f() f-string: note"]


@pytest.mark.parametrize("module", sorted({m for m, _ in CERTIFIED}))
def test_the_certification_list_has_no_stale_entries(module):
    """A certification for an expression that no longer reaches a sink is a claim about nothing."""
    source = (REPO / module).read_text(encoding="utf-8")
    live: set[tuple[str, str]] = set()
    for func in [n for n in ast.walk(ast.parse(source)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        tainted = tainted_names(func)
        for _kind, expr in sink_expressions(func):
            if _roots(expr) & tainted:
                live.add((func.name, ast.unparse(expr)))
    stale = sorted(
        f"{name}(): {expr}"
        for (mod, name), entries in CERTIFIED.items()
        if mod == module
        for expr in entries
        if (name, expr) not in live
    )
    assert not stale, f"{module}: certified expressions no longer reaching a sink: {stale}"


def test_every_certification_names_a_category_rather_than_arguing_in_prose():
    for (module, func), entries in CERTIFIED.items():
        for expression, reason in entries.items():
            assert reason.startswith(CATEGORIES), f"{module}:{func} {expression!r} -> {reason!r}"
            assert len(reason) > 30, f"{module}:{func} {expression!r} is certified with no real reason"


# ---------------------------------------------- the manifest SINK, on the one path that reaches it


def _pat_name_capture(planted, tmp_path, wants=frozenset()):
    """A capture whose PAT NAME appears in a SUCCESSFUL CSV. Returns (manifest dict, csv text)."""
    session = _Session("an-unrelated-long-pat-secret", (200, b""), pat_name=planted)
    session.data_reply = (200, f"{planted}\nvalue\n".encode(), {})
    record = oracle.capture_view(session, VIEW, tmp_path, wants)
    record["workbook_name"] = "W"
    counter = _Counter()
    counter._redact = session.redact_text  # pylint: disable=protected-access
    oracle.write_manifest([record], oracle.CaptureRun(counter, ENV, tmp_path, 0.0, wants))
    manifest = json.loads((tmp_path / "oracle-manifest.json").read_text(encoding="utf-8"))
    csv_text = next(iter(tmp_path.glob("data/*.csv"))).read_text(encoding="utf-8")
    return manifest, csv_text


def test_the_manifest_sink_scrubs_the_one_credential_the_seam_lets_through(tmp_path):
    """⚠️ Written because FOUR mutations survived: nothing drove a value through the sink at all.

    A backstop with no test is the "unkillable defence in depth" this review already rejected once.
    The path that reaches it is real and reachable: the PAT **name** is deliberately redacted rather
    than refused (`reflected_credential`), so a successful CSV whose header matches it produces
    `data.columns` containing it -- through a dict, through the `views` LIST, and through the
    `columns` LIST -- and the sink is the only thing standing between that and the manifest.
    """
    planted = "SYNTHETIC_PAT_NAME_42"
    manifest, _csv = _pat_name_capture(planted, tmp_path)
    assert manifest["views"][0]["data"]["status"] == "ok", "the seam must NOT refuse a mere PAT name"
    assert manifest["views"][0]["data"]["columns"] == ["[REDACTED]"]
    assert planted not in json.dumps(manifest)


def test_the_sink_reports_which_field_it_had_to_scrub(tmp_path):
    """Scrubbing silently is how a source defect survives: the artifact looks perfect either way."""
    manifest, _csv = _pat_name_capture("SYNTHETIC_PAT_NAME_42", tmp_path)
    assert manifest["credential_scrubbed_at_sink"] == ["views[0].data.columns[0]"]


def test_a_clean_capture_reports_no_sink_redactions(tmp_path):
    """Otherwise the report above would be noise rather than a signal."""
    _capture_and_read_everything("an-unrelated-long-pat-secret", tmp_path, (200, b""), frozenset())
    manifest = json.loads((tmp_path / "oracle-manifest.json").read_text(encoding="utf-8"))
    assert manifest["credential_scrubbed_at_sink"] == []


def test_the_pat_name_is_KNOWN_to_survive_in_the_csv_on_disk(tmp_path):
    """The deliberate residual, pinned so it is a decision rather than an oversight.

    The PAT name is not refused at the seam (a human-chosen name like `Migration` would refuse a
    legitimate estate), so the bytes are written. It does not authenticate on its own and is visible
    in Tableau's own UI. If that trade ever stops being acceptable the fix is in
    `reflected_credential`, and this test is the thing that will fail and say so.
    """
    planted = "SYNTHETIC_PAT_NAME_42"
    _manifest, csv_text = _pat_name_capture(planted, tmp_path)
    assert planted in csv_text


# ------------------------------------------------- the reviewer's two round-4 reproductions, named


def test_a_secret_with_leading_whitespace_never_reaches_the_written_manifest(tmp_path):
    """Round 4's reproduction, re-verified after the round-5 seam. `.lstrip()` used to run before the
    redactor, so the manifest carried `got unrecognised bytes ('SYNTHETIC_SECRET')`.

    The outcome is now STRONGER than redaction: a successful body echoing the PAT secret is refused
    outright, so nothing is written at all. The chokepoint that fixed round 4 is still what protects
    the value the seam deliberately allows through -- see the PAT-name test below."""
    secret = " SYNTHETIC_SECRET_42"
    out = site_written_manifest(secret, tmp_path, None)
    assert longest_surviving_run(secret, out) == ""
    assert '"credential_reflected"' in out


def test_a_secret_longer_than_any_window_never_reaches_the_written_manifest(tmp_path):
    """Round 4's other reproduction: a 321-character secret was cut by the 256-byte window, so the
    redactor's literal was absent and the first 16 characters persisted verbatim."""
    secret = "S" + "YNTHETIC_SECRET_" * 20
    out = site_written_manifest(secret, tmp_path, None)
    assert longest_surviving_run(secret, out) == ""
    assert '"credential_reflected"' in out


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_diagnostic_chokepoint_still_covers_what_the_seam_deliberately_allows(shape, tmp_path):
    """The PAT NAME is redacted, never refused -- so round 4's diagnostic path is still LIVE, and this
    is the test that keeps it proven now that the authenticating halves never reach it.

    Without this, the round-5 seam would have silently retired every end-to-end check of the round-4
    fix: refusing the payload makes the diagnostic unreachable for a secret, and a fix whose only
    coverage has become unreachable is a fix nobody is testing.
    """
    planted = SHAPES[shape]
    session = _Session("a-different-long-pat-secret", (200, (planted + " and then html").encode()), pat_name=planted)
    record = oracle.capture_view(session, VIEW, tmp_path, frozenset({"svg"}))
    record["workbook_name"] = "W"
    counter = _Counter()
    counter._redact = session.redact_text  # pylint: disable=protected-access
    oracle.write_manifest([record], oracle.CaptureRun(counter, ENV, tmp_path, 0.0, frozenset({"svg"})))
    out = (tmp_path / "oracle-manifest.json").read_text(encoding="utf-8")
    control = "ZZQQWVVBBN_MMKKJ_77"
    assert leaked_run(planted, out, control) == ""
    assert record["svg"]["status"] == "format_mismatch", "the diagnostic route must still be reached"
    assert "[REDACTED]" in out


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
