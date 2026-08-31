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
import logging
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import capture_tableau_oracle as oracle  # noqa: E402  # pylint: disable=wrong-import-position
import tableau_render_capability as cap  # noqa: E402  # pylint: disable=wrong-import-position
from tableau_env import redacted_note, scrub_tree  # noqa: E402  # pylint: disable=wrong-import-position

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


def site_reflected_view_name(secret, tmp_path, _mp):
    """⚠️ ROUND 6, finding 1. A view NAME comes from an authenticated `get_json`, which the export
    seam never sees. It was slugged and truncated into the artifact FILENAME, so the prefix on disk was
    no longer the literal any redactor could match -- and the same prefix reached the manifest.

    Covers all three artifacts at once: the filename, the manifest, and (via caplog in the dedicated
    test below) the console line, which also truncated the name before redacting it.
    """
    session = _Session("an-unrelated-long-pat-secret", (200, b""), data_reply=(200, b"a\n1\n"))
    session.token = secret
    view = {"id": "eb00995d-1ff1-4a42-9ac9-28846f861d31", "name": secret, "workbook": {"id": "wb"}}
    record = oracle.capture_view(session, view, tmp_path, frozenset())
    record["workbook_name"] = secret
    counter = _Counter()
    counter._redact = session.redact_text  # pylint: disable=protected-access
    oracle.write_manifest([record], oracle.CaptureRun(counter, ENV, tmp_path, 0.0, frozenset()))
    names = "\n".join(str(p) for p in sorted(tmp_path.rglob("*")))
    return "\n".join([(tmp_path / "oracle-manifest.json").read_text(encoding="utf-8"), names])


def site_reflected_csv_column(secret, tmp_path, _mp):
    """⚠️ ROUND 6, finding 2. A CSV column becomes a dict KEY in `format_hints` once the column has a
    detected format, and a values-only sink walk left it in the manifest while redacting the identical
    string one field over in `columns`.

    ⚠️ Scoped to the MANIFEST on purpose, unlike its sibling sites, which also read the files. The
    planted value here is the PAT **name**, and its presence in `data/<luid>.csv` is the residual this
    project deliberately accepts -- refusing a capture because a column heading matches a human-chosen
    PAT name would kill legitimate estates. That residual is pinned by
    `test_the_pat_name_is_KNOWN_to_survive_in_the_csv_on_disk`, so it is a stated decision with its own
    failing test rather than a hole this site quietly steps around.
    """
    session = _Session("an-unrelated-long-pat-secret", (200, b""), pat_name=secret)
    session.data_reply = (200, f"{secret}\n19.5%\n".encode(), {})
    record = oracle.capture_view(session, VIEW, tmp_path, frozenset())
    record["workbook_name"] = "W"
    counter = _Counter()
    counter._redact = session.redact_text  # pylint: disable=protected-access
    oracle.write_manifest([record], oracle.CaptureRun(counter, ENV, tmp_path, 0.0, frozenset()))
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
    "written artifacts (successful /data)": site_successful_csv,
    "written artifacts (successful svg)": site_successful_svg,
    "written artifacts (session token reflected)": site_successful_csv_reflecting_the_session_token,
    "written artifacts (reflected view NAME)": site_reflected_view_name,
    "written artifacts (reflected CSV column)": site_reflected_csv_column,
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


# ------------------------------------ gate 3: INTER-procedural taint, declared seeds, all exits
#
# ⚠️ Third rewrite, because round 6 found four bypasses in the second one. It keyed taint off six
# parameter NAMES and looked at f-strings, dict values and log/raise args. All four of these produced
# an empty finding list:
#
#     def f(payload):  return {payload.decode(): "percent"}   # a dict KEY
#     def f(body):     return "diagnostic: " + body.decode()  # CONCATENATION
#     def f(response): return f"{response}"                   # a renamed parameter
#     def f(payload):  out = {}; out["k"] = payload.decode()  # a SUBSCRIPT store
#
# Two changes fix the class rather than the four instances:
#
#   * taint is SEEDED per (module, function) from a declared table and then PROPAGATED ACROSS CALLS
#     within the module, so a parameter's spelling is irrelevant -- `def f(response)` is tainted
#     because something taints it at a call site, not because of what it is called;
#   * the sink list is derived from the EXITS a value can leave through, which the language closes:
#     write_bytes/write_text (content AND the path they are called on), LOG.*, print, raise, and every
#     construction that can carry a value into one -- f-strings, dict values, dict KEYS, `**` unpacks,
#     `+`, `%`, subscript and attribute stores, and `/` path joins.
#
# That is the answer to "why will round 8 differ": rounds 2-7 enumerated SOURCES, which is an open set
# nobody can finish. Exits are closed by Python itself.

MODULES = (
    "scripts/capture_tableau_oracle.py",
    "scripts/tableau_render_capability.py",
    "scripts/tableau_payload_facts.py",
)

# Where response bytes ENTER, declared per function rather than inferred from a parameter's spelling.
# ⚠️ Deliberately SMALL, and it shrank in round 7 rather than growing. `capture_view`'s `view` and
# `log_progress`'s `record` were hand-seeded when the gate was found blind to the round-6 path defect;
# both are now DERIVED, because taint propagates through return values -- `list_views` returns what
# `get_json` gave it, `select_views` returns what `list_views` gave it, `main` loops over that, and the
# value arrives at `capture_view` inter-procedurally. Round 7's lesson is that a hand-maintained list
# nobody is forced to update is the shape that keeps generating findings, so the answer was to need
# fewer entries, not to remember more. What remains is genuinely irreducible: the HTTP origin, the two
# redactors, and the cross-module entry points, which propagation cannot see across a module boundary.
TAINT_SEEDS: dict[tuple[str, str], set[str]] = {
    ("scripts/capture_tableau_oracle.py", "_request"): set(),  # the origin: its RETURN is tainted
    ("scripts/capture_tableau_oracle.py", "classify_export_error"): {"text"},
    ("scripts/capture_tableau_oracle.py", "reflected_credential"): {"payload"},
    ("scripts/capture_tableau_oracle.py", "redact_text"): {"text"},
    ("scripts/capture_tableau_oracle.py", "_redact_response"): {"text"},
    ("scripts/tableau_render_capability.py", "format_matches"): {"body", "content_type"},
    ("scripts/tableau_render_capability.py", "classify_probe"): {"body", "content_type"},
    ("scripts/tableau_render_capability.py", "looks_like_svg"): {"body"},
    ("scripts/tableau_render_capability.py", "_identify"): {"head"},
    # Cross-module entry points, flagged by `test_every_cross_module_call_carrying_tainted_data_...`.
    # Propagation cannot see across a module boundary, so these are irreducible.
    ("scripts/tableau_render_capability.py", "probe_render_capability"): {"views"},
    ("scripts/tableau_render_capability.py", "apply_selected_tier"): {"report"},
    ("scripts/tableau_payload_facts.py", "detect_format"): {"values"},
    ("scripts/tableau_payload_facts.py", "summarise_csv"): {"payload"},
    ("scripts/tableau_payload_facts.py", "png_dimensions"): {"payload"},
    ("scripts/tableau_payload_facts.py", "svg_facts"): {"payload"},
    ("scripts/tableau_payload_facts.py", "pdf_facts"): {"payload"},
}

# Calls whose RESULT is response data wherever they appear.
TAINTING_CALLS = {"_request", "export", "raw_get", "get_json", "read", "decode", "loads"}
# The ONE thing that clears taint. Not "any helper" -- see test_the_chokepoint_is_the_only_...
UNTAINTING = {"redacted_note", "scrub_tree", "artifact_stem"}
LOG_AND_RAISE = {"info", "warning", "error", "debug", "exception", "ExportFailed", "RuntimeError", "ValueError"}
WRITE_CALLS = {"write_bytes", "write_text", "print", "dumps"}
# Container stores: `record.update(svg_facts(payload))` puts response-derived data into a manifest
# record just as surely as a dict literal does.
CONTAINER_STORES = {"update", "append", "extend", "setdefault"}

CATEGORIES = (
    "NOT-A-STRING:",
    "REDACTED-UPSTREAM:",
    "REFUSED-AT-SEAM:",
    "DERIVED-IRREVERSIBLY:",
    "FIXED-VOCABULARY:",
    "SCRUBBED-AT-SINK:",
    "OUTBOUND:",  # our own request, travelling to Tableau -- not a response coming back
    "SHAPE-VERIFIED:",  # response-derived, but constrained to a shape that cannot carry a credential
    "UNAUTHENTICATED-SOURCE:",  # came from a request that never carried a credential, so cannot reflect one
)

_SERVERINFO = (
    "UNAUTHENTICATED-SOURCE: from `/serverinfo`, which `server_info` calls with no auth header and no "
    "PAT in the body -- it never received a credential, so it cannot reflect one. It still reaches the "
    "manifest, where `scrub_tree` covers it anyway"
)
_INTO_THE_MANIFEST_AGGREGATE = (
    "SCRUBBED-AT-SINK: an aggregate on its way to the manifest; `scrub_tree` walks it whole, values "
    "and keys, immediately before serialisation, and reports anything it had to redact"
)
_PROBE_VERDICT = (
    "FIXED-VOCABULARY: one of classify_probe's three verdict literals, a ladder tier name, or an "
    "api-version string this module itself chose"
)

_LUID_OK = (
    "SHAPE-VERIFIED: the view LUID, anchored to a full UUID regex by `artifact_stem` and additionally "
    "refused if it equals one of our own credentials, so it is the one response-derived string allowed "
    "to reach a filename"
)
_INTO_THE_MANIFEST = (
    "SCRUBBED-AT-SINK: Tableau metadata copied into the manifest record; `scrub_tree` covers it, "
    "values and keys, immediately before serialisation -- and it never reaches a path or a raw log line"
)

CERTIFIED: dict[tuple[str, str], dict[str, str]] = {
    ("scripts/capture_tableau_oracle.py", "classify_export_error"): {
        "match.group(1)": "REDACTED-UPSTREAM: the regex runs on `safe`, the redacted copy, never on `text`",
        "match.group(2).strip()": "REDACTED-UPSTREAM: same match, and `.strip()` runs after redaction",
        "safe[:150]": "REDACTED-UPSTREAM: `safe` is `redactor(text)`; the slice is after",
        "safe[:200]": "REDACTED-UPSTREAM: `safe` is `redactor(text)`; the slice is after",
        "label": "FIXED-VOCABULARY: either 'network error' or an f-string of the HTTP status integer",
        "status": "NOT-A-STRING: an HTTP status integer",
    },
    ("scripts/capture_tableau_oracle.py", "_request"): {
        "body": "OUTBOUND: the request entity WE send to Tableau, serialised on its way out",
        "path": "OUTBOUND: the REST path we are requesting, built from the site id and a verified LUID",
    },
    ("scripts/capture_tableau_oracle.py", "capture_view"): {
        "view_luid": _LUID_OK,
        "view.get('name')": _INTO_THE_MANIFEST,
        "view.get('viewUrlName')": _INTO_THE_MANIFEST,
        "view.get('contentUrl')": _INTO_THE_MANIFEST,
        "view.get('updatedAt')": _INTO_THE_MANIFEST,
        "(view.get('project') or {}).get('name')": _INTO_THE_MANIFEST,
        "workbook.get('id')": _INTO_THE_MANIFEST,
        "_capture_data(session, view_luid, out_dir / 'data' / f'{stem}.csv', out_dir)": (
            "SCRUBBED-AT-SINK: the returned leg record, whose own fields are certified in _capture_data; "
            "the PATH argument is built from `stem`, which comes only from `artifact_stem`"
        ),
        "_capture_render(session, view_luid, out_dir / 'images' / f'{stem}.{_RENDER_EXTENSIONS[kind]}', kind, api=(api_overrides or {}).get(kind))": (
            "SCRUBBED-AT-SINK: the returned leg record, whose own fields are certified in _capture_render; "
            "the PATH argument is built from `stem`, which comes only from `artifact_stem`"
        ),
    },
    ("scripts/capture_tableau_oracle.py", "artifact_stem"): {
        "len(view_luid or '')": "NOT-A-STRING: a character count, reported instead of the rejected identifier",
    },
    ("scripts/capture_tableau_oracle.py", "log_progress"): {
        "index": "NOT-A-STRING: an integer position in the loop",
        "total": "NOT-A-STRING: an integer view count",
        "status": "FIXED-VOCABULARY: one of classify_export_error's status literals",
        "data.get('detail')": "REDACTED-UPSTREAM: classify_export_error builds every detail from `safe`",
        "data['row_count']": "NOT-A-STRING: an integer row count",
        "data['elapsed_sec']": "NOT-A-STRING: a float duration in seconds",
        "data['reauths']": "NOT-A-STRING: an integer counter",
        "data['retries']": "NOT-A-STRING: an integer counter",
    },
    ("scripts/capture_tableau_oracle.py", "_capture_data"): {
        "view_luid": _LUID_OK,
        "payload": (
            "REFUSED-AT-SEAM: writing the customer's own CSV IS the capture. What must never be in it "
            "is OUR credential, and `export()` refuses a 200 carrying the PAT secret or session token "
            "before this line runs -- the file is the artifact a manifest scrub could never reach"
        ),
        "summarise_csv(payload)": (
            "REFUSED-AT-SEAM: this unpack is what put a credential in `data.columns`; `export()` "
            "refuses a 200 carrying one before `_capture_data` sees the payload, and the sink scrubs "
            "the residual PAT-name case in both the values and the `format_hints` KEYS"
        ),
        "stats": "REDACTED-UPSTREAM: counters, plus `retry_reasons` whose entries are redacted details",
        "hashlib.sha256(payload).hexdigest()": "DERIVED-IRREVERSIBLY: a one-way digest, not the payload",
        "len(payload)": "NOT-A-STRING: an integer byte count",
        "round(elapsed, 2)": "NOT-A-STRING: a float duration in seconds",
    },
    ("scripts/capture_tableau_oracle.py", "_capture_render"): {
        "view_luid": _LUID_OK,
        "payload": (
            "REFUSED-AT-SEAM: writing the rendered reference IS the capture; the seam refuses a 200 "
            "echoing the PAT secret or session token before these bytes reach a file"
        ),
        "why": "REDACTED-UPSTREAM: `format_matches` builds it entirely from `redacted_note` output",
        "stats": "REDACTED-UPSTREAM: counters, plus `retry_reasons` whose entries are redacted details",
        "hashlib.sha256(payload).hexdigest()": "DERIVED-IRREVERSIBLY: a one-way digest, not the payload",
        "len(payload)": "NOT-A-STRING: an integer byte count",
        "round(elapsed, 2)": "NOT-A-STRING: a float duration in seconds",
        "dimensions": "NOT-A-STRING: the two integers png_dimensions read from the IHDR chunk",
        "svg_facts(payload)": "NOT-A-STRING: element counts and millimetre geometry, no payload text",
        "pdf_facts(payload)": "NOT-A-STRING: page geometry and font/image counts, no payload text",
    },
    ("scripts/capture_tableau_oracle.py", "sign_in"): {
        "status": "NOT-A-STRING: an HTTP status integer",
        "delay": "NOT-A-STRING: a float backoff delay",
    },
    ("scripts/capture_tableau_oracle.py", "get_json"): {
        "status": "NOT-A-STRING: an HTTP status integer",
    },
    ("scripts/capture_tableau_oracle.py", "export"): {
        "path": "OUTBOUND: the REST path we are requesting, built from the site id and a verified LUID",
        "status": "NOT-A-STRING: an HTTP status integer",
        "delay": "NOT-A-STRING: a float backoff delay",
        "kind": "FIXED-VOCABULARY: one of classify_export_error's five status literals",
        "reflected": "FIXED-VOCABULARY: one of the two literal labels reflected_credential returns",
        "detail": "REDACTED-UPSTREAM: classify_export_error builds it from `safe`",
        "detail[:60]": "REDACTED-UPSTREAM: classify_export_error builds it from `safe`; the slice is after",
        "detail[:80]": "REDACTED-UPSTREAM: classify_export_error builds it from `safe`; the slice is after",
        "detail or redacted_note(payload, self._redact_response, limit=200)": (
            "REDACTED-UPSTREAM: both arms are redacted -- `detail` from `safe`, the fallback by the chokepoint"
        ),
    },
    ("scripts/tableau_render_capability.py", "format_matches"): {
        "_identify(head)": "FIXED-VOCABULARY: `_identify` returns one of four literals and never quotes bytes",
    },
    ("scripts/tableau_render_capability.py", "classify_probe"): {
        "why": "REDACTED-UPSTREAM: `format_matches` builds it entirely from `redacted_note` output",
        "status": "NOT-A-STRING: an HTTP status integer",
    },
    ("scripts/tableau_payload_facts.py", "summarise_csv"): {
        "header": (
            "SCRUBBED-AT-SINK: a CSV header row IS response text and this gate must keep saying so. "
            "The seam refuses a 200 carrying the PAT secret or session token, and `scrub_tree` cleans "
            "the residual PAT-name case out of the manifest -- values AND keys -- before it is written"
        ),
        "hints": (
            "SCRUBBED-AT-SINK: keyed by column name, which is why `scrub_tree` scrubs dict KEYS; a "
            "values-only walk put the PAT name in `format_hints` while redacting it in `columns`"
        ),
        "name": (
            "SCRUBBED-AT-SINK: THE round-6 finding-2 site -- a column name becoming a dict KEY. It is "
            "left as data on purpose (a column heading is the customer's, not ours) and scrubbed at "
            "the manifest boundary, keys included, with collisions disambiguated rather than dropped"
        ),
        "fmt": "FIXED-VOCABULARY: one of 'percent', 'currency', 'thousands_separated' or None",
        "len(body)": "NOT-A-STRING: an integer row count",
    },
    ("scripts/tableau_render_capability.py", "_walk_one_tier"): {
        "verdict": _PROBE_VERDICT,
        "floor_verdict": _PROBE_VERDICT,
        "detail": "REDACTED-UPSTREAM: classify_probe builds every detail through `redacted_note`",
        "floor_detail": "REDACTED-UPSTREAM: classify_probe builds every detail through `redacted_note`",
        "None if verdict == 'available' else None": "NOT-A-STRING: both arms are literally None",
        "{'api': tier.min_api, 'verdict': floor_verdict, 'detail': floor_detail}": _INTO_THE_MANIFEST_AGGREGATE,
    },
    ("scripts/tableau_render_capability.py", "_walk_ladder"): {
        "entry": _INTO_THE_MANIFEST_AGGREGATE,
    },
    ("scripts/tableau_render_capability.py", "detect"): {
        "view_luid": _LUID_OK,
        "verdicts": _INTO_THE_MANIFEST_AGGREGATE,
        "chosen": _PROBE_VERDICT,
        "chosen_api": _PROBE_VERDICT,
        "', '.join(blocked)": _PROBE_VERDICT,
        "versions.configured": "OUTBOUND: the api-version WE send, read from .env, never from a response",
        "versions.advertised": _SERVERINFO,
        "bool(chosen and unknown_above)": "NOT-A-STRING: a boolean provisional flag",
        "bool(chosen and (not unknown_above)) or definite_no": "NOT-A-STRING: a boolean completeness flag",
    },
    ("scripts/tableau_render_capability.py", "_add_pin_warnings"): {
        "reprobe['verdict']": _PROBE_VERDICT,
        "reprobe['detail'][:80]": "REDACTED-UPSTREAM: a classify_probe detail; the slice runs after redaction",
        "versions.configured": "OUTBOUND: the api-version WE send, read from .env, never from a response",
        "versions.advertised": _SERVERINFO,
    },
    ("scripts/tableau_render_capability.py", "release_for"): {"api_version": _SERVERINFO},
    ("scripts/tableau_render_capability.py", "_log_versions"): {
        "info.get('product_version')": _SERVERINFO,
        "info.get('build')": _SERVERINFO,
        "info.get('rest_api_version')": _SERVERINFO,
        "release_for(info.get('rest_api_version') or '')": _SERVERINFO,
    },
    ("scripts/tableau_render_capability.py", "_build_report"): {"info": _SERVERINFO},
    ("scripts/tableau_render_capability.py", "fetcher"): {"view_luid": _LUID_OK},
    ("scripts/tableau_render_capability.py", "apply_selected_tier"): {
        "kind": "FIXED-VOCABULARY: one of the three ladder tier names, mapped through a literal dict",
        "api_overrides[kind]": _PROBE_VERDICT,
        "report['selected_api_version']": _PROBE_VERDICT,
    },
    ("scripts/tableau_render_capability.py", "probe_render_capability"): {
        "view['id']": _LUID_OK,
        "view.get('name')": _INTO_THE_MANIFEST,
        "info": _SERVERINFO,
        "info.get('product_version')": _SERVERINFO,
        "info.get('build')": _SERVERINFO,
        "advertised": _SERVERINFO,
        "best.get('selected_tier') or 'UNDETERMINED'": _PROBE_VERDICT,
        "' (PROVISIONAL)' if best.get('provisional') else ''": "FIXED-VOCABULARY: one of two literals",
    },
    ("scripts/tableau_render_capability.py", "main"): {
        "report": _INTO_THE_MANIFEST_AGGREGATE,
        "json.dumps(report, indent=2)": _INTO_THE_MANIFEST_AGGREGATE,
        "warning": _PROBE_VERDICT,
    },
    ("scripts/capture_tableau_oracle.py", "main"): {
        "record": _INTO_THE_MANIFEST_AGGREGATE,
        "len(views)": "NOT-A-STRING: an integer view count",
        "workbook_names.get(record['workbook_luid'])": _INTO_THE_MANIFEST,
    },
    ("scripts/capture_tableau_oracle.py", "write_manifest"): {
        "manifest": _INTO_THE_MANIFEST_AGGREGATE,
        "capability_report": _INTO_THE_MANIFEST_AGGREGATE,
        "json.dumps(manifest, indent=2)": (
            "SCRUBBED-AT-SINK: `manifest` is the ALREADY-scrubbed tree -- `scrub_tree` runs on the line "
            "above this one, so the serialisation sees only redacted values and keys"
        ),
        "json.dumps(manifest, indent=2) + '\\n'": (
            "SCRUBBED-AT-SINK: `manifest` is the ALREADY-scrubbed tree; the concatenation is a newline"
        ),
        "manifest['elapsed_sec']": "NOT-A-STRING: a float duration in seconds",
    },
    ("scripts/capture_tableau_oracle.py", "_log_blocked_and_stale"): {"warning": _PROBE_VERDICT},
    ("scripts/tableau_payload_facts.py", "png_dimensions"): {
        "int.from_bytes(payload[16:20], 'big')": "NOT-A-STRING: an integer read from the IHDR",
        "int.from_bytes(payload[20:24], 'big')": "NOT-A-STRING: an integer read from the IHDR",
    },
    ("scripts/tableau_payload_facts.py", "svg_facts"): {
        "text.count('<text')": "NOT-A-STRING: an integer element count",
        "text.count('<path')": "NOT-A-STRING: an integer element count",
        "text.count('<image')": "NOT-A-STRING: an integer element count",
        "len([h for h in _SVG_HREF.findall(text) if not h.startswith(('data:', '#'))])": (
            "NOT-A-STRING: an integer count of external references"
        ),
        "round(float(match.group(1)) * 96 / 25.4)": "NOT-A-STRING: an integer pixel width",
        "round(float(match.group(2)) * 96 / 25.4)": "NOT-A-STRING: an integer pixel height",
    },
    ("scripts/tableau_payload_facts.py", "pdf_facts"): {
        "len(re.findall(b'/FontFile\\\\d?', payload))": "NOT-A-STRING: an integer count of embedded fonts",
        "len(re.findall(b'/Subtype\\\\s*/Image', payload))": "NOT-A-STRING: an integer count of image XObjects",
        "round(float(parts[2]))": "NOT-A-STRING: an integer page width in points",
        "round(float(parts[3]))": "NOT-A-STRING: an integer page height in points",
        "{'width': round(float(parts[2])), 'height': round(float(parts[3]))}": (
            "NOT-A-STRING: a two-integer page geometry, stored under a constant key"
        ),
    },
}


def _roots(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _called(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    return node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)


def _functions(tree: ast.AST) -> list[ast.AST]:
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _param_names(func: ast.AST) -> list[str]:
    return [a.arg for a in func.args.posonlyargs + func.args.args + func.args.kwonlyargs]


def _assignments(func: ast.AST):
    """(targets, value) for every binding form that can carry taint, INCLUDING subscript stores."""
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            yield node.targets, node.value
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
            yield [node.target], node.value
        elif isinstance(node, ast.For):
            yield [node.target], node.iter
        elif isinstance(node, (ast.comprehension,)):
            yield [node.target], node.iter


def _bind(targets, tainted: set[str]) -> None:
    for target in targets:
        if isinstance(target, ast.Name):
            tainted.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                if isinstance(element, ast.Name):
                    tainted.add(element.id)
        elif isinstance(target, (ast.Subscript, ast.Attribute)):
            # `out["k"] = payload.decode()` taints the CONTAINER, not a new name.
            base = target
            while isinstance(base, (ast.Subscript, ast.Attribute)):
                base = base.value
            if isinstance(base, ast.Name):
                tainted.add(base.id)


def taint_module(source: str, module: str) -> dict[str, set[str]]:
    """Tainted names per function, propagated ACROSS calls within the module to a fixpoint."""
    tree = ast.parse(source)
    functions = {f.name: f for f in _functions(tree)}
    tainted = {name: set(TAINT_SEEDS.get((module, name), set())) for name in functions}
    # Functions whose RETURN value is response-derived. Discovered, not declared: `list_views` returns
    # what `get_json` gave it, `select_views` returns what `list_views` gave it, and `main` then loops
    # over the result. Without this the chain died at the first hop and `capture_view`'s `view`
    # parameter had to be hand-seeded -- which is the hand-maintained list round 7 identified as the
    # shape that keeps generating findings. `_request` is the origin and is seeded here.
    returns_taint = {"_request"}
    for _ in range(12):
        before = ({k: set(v) for k, v in tainted.items()}, set(returns_taint))
        for name, func in functions.items():
            local = tainted[name]
            for targets, value in _assignments(func):
                if _called(value) in UNTAINTING:
                    continue
                if _roots(value) & local or _called(value) in TAINTING_CALLS or _called(value) in returns_taint:
                    _bind(targets, local)
            for node in ast.walk(func):
                if (
                    isinstance(node, ast.Return)
                    and node.value is not None
                    and _called(node.value) not in UNTAINTING
                    and (_roots(node.value) & local or _called(node.value) in TAINTING_CALLS)
                ):
                    returns_taint.add(name)
            # Inter-procedural: a call passing a tainted argument taints the callee's parameter.
            for node in ast.walk(func):
                if not isinstance(node, ast.Call):
                    continue
                callee = functions.get(_called(node) or "")
                if callee is None:
                    continue
                params = _param_names(callee)
                # A bound method call passes `self` implicitly, so argument 0 lands on parameter 1.
                # Without this the analyser mapped a tainted argument onto `self` and then treated the
                # whole receiver as response data -- which over-taints loudly (measured: `attempt`,
                # `self.retry.max_attempts` and friends all flagged) and, worse, silently shifts every
                # later argument by one, so a genuinely tainted third argument was checked against the
                # second parameter's name.
                if isinstance(node.func, ast.Attribute) and params and params[0] == "self":
                    params = params[1:]
                for index, arg in enumerate(node.args):
                    if _roots(arg) & local and index < len(params):
                        tainted[callee.name].add(params[index])
                for keyword in node.keywords:
                    if keyword.arg and _roots(keyword.value) & local:
                        tainted[callee.name].add(keyword.arg)
        if (tainted, returns_taint) == before:
            break
    return tainted


def sink_expressions(func: ast.AST) -> list[tuple[str, ast.AST]]:
    """Every place a value can leave this function as persisted or emitted text.

    Derived from the EXITS -- write/log/print/raise -- and from every construction that can carry a
    value into one. `+` and dict KEYS are here because round 6 escaped through exactly those.
    """
    out: list[tuple[str, ast.AST]] = []
    for node in ast.walk(func):
        if isinstance(node, ast.JoinedStr):
            out += [("f-string", p.value) for p in node.values if isinstance(p, ast.FormattedValue)]
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if key is not None and not isinstance(key, ast.Constant):
                    out.append(("dict-key", key))
                if not isinstance(value, ast.Constant):
                    out.append(("dict-**" if key is None else "dict-value", value))
        elif isinstance(node, ast.Call) and _called(node) in LOG_AND_RAISE | WRITE_CALLS | CONTAINER_STORES:
            kind = "container-store" if _called(node) in CONTAINER_STORES else "call-arg"
            out += [(kind, a) for a in node.args if not isinstance(a, (ast.Constant, ast.JoinedStr))]
            if _called(node) in WRITE_CALLS and isinstance(node.func, ast.Attribute):
                out.append(("write-path", node.func.value))
        elif isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.Mod):
                out.append(("percent", node.right))
            elif isinstance(node.op, ast.Add):
                out += [("concat", node.left), ("concat", node.right)]
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and not isinstance(node.value, ast.Constant):
                    out.append(("subscript-store", node.value))
                    if not isinstance(target.slice, ast.Constant):
                        out.append(("subscript-key", target.slice))
    return [(kind, expr) for kind, expr in out if not isinstance(expr, ast.Constant)]


def uncertified_sinks(source: str, module: str) -> list[str]:
    """Response-derived expressions reaching an exit with no certification for THAT function."""
    tainted = taint_module(source, module)
    findings = []
    for func in _functions(ast.parse(source)):
        local = tainted.get(func.name, set())
        if not local:
            continue
        certified = CERTIFIED.get((module, func.name), {})
        for kind, expr in sink_expressions(func):
            if _called(expr) in UNTAINTING or not (_roots(expr) & local):
                continue
            text = ast.unparse(expr)
            if text not in certified:
                findings.append(f"{func.name}() {kind}: {text}")
    return sorted(set(findings))


@pytest.mark.parametrize("module", MODULES)
def test_no_response_derived_value_reaches_an_exit_without_certification(module):
    """A SEVENTH escape cannot be added silently -- only certified deliberately, per function."""
    uncertified = uncertified_sinks((REPO / module).read_text(encoding="utf-8"), module)
    assert not uncertified, (
        f"{module} lets response-derived data reach an exit with no certification:\n  "
        + "\n  ".join(uncertified)
        + "\nEither route it through tableau_env.redacted_note(), build it from a verified identifier "
        f"(artifact_stem), or certify the exact expression with a reason starting with one of {CATEGORIES}."
    )


@pytest.mark.parametrize(
    ("label", "source", "expected"),
    [
        (
            "dict-key",
            'def summarise_csv(payload):\n    return {payload.decode(): "percent"}\n',
            ["summarise_csv() dict-key: payload.decode()"],
        ),
        (
            "concatenation",
            'def summarise_csv(payload):\n    return "diagnostic: " + payload.decode()\n',
            ["summarise_csv() concat: payload.decode()"],
        ),
        (
            "subscript store",
            'def summarise_csv(payload):\n    out = {}\n    out["k"] = payload.decode()\n    return out\n',
            ["summarise_csv() subscript-store: payload.decode()"],
        ),
        (
            "a write path built from response text",
            'def summarise_csv(payload, out_dir):\n    (out_dir / payload.decode()).write_bytes(b"x")\n',
            ["summarise_csv() write-path: out_dir / payload.decode()"],
        ),
    ],
)
def test_the_gate_catches_each_bypass_round_6_reported(label, source, expected):
    """All four produced an EMPTY finding list against the round-5 gate.

    The fourth is round 6's finding 1 in miniature: a filename built from response text. It is caught
    at the `write-path` exit -- the receiver of `write_bytes` -- rather than by inspecting `/` operands,
    because `/` is also arithmetic and flagging it produced false positives on `mm * 96 / 25.4`.
    """
    assert uncertified_sinks(source, "scripts/tableau_payload_facts.py") == sorted(expected), label


def test_a_renamed_parameter_is_tainted_by_its_CALLER_not_by_its_spelling():
    """The round-5 gate keyed off six parameter names, so `def f(response)` was invisible."""
    source = (
        "def leak(response):\n"
        '    return f"{response}"\n'
        "def summarise_csv(payload):\n"
        "    return leak(payload.decode())\n"
    )
    assert uncertified_sinks(source, "scripts/tableau_payload_facts.py") == ["leak() f-string: response"]


def test_the_chokepoint_is_the_only_thing_that_clears_taint():
    """Otherwise a certification could be earned by laundering a value through any helper."""
    clean = 'def summarise_csv(payload):\n    note = redacted_note(payload, None, limit=8)\n    return f"{note}"\n'
    assert uncertified_sinks(clean, "scripts/tableau_payload_facts.py") == []
    laundered = 'def summarise_csv(payload):\n    note = str(payload)\n    return f"{note}"\n'
    assert uncertified_sinks(laundered, "scripts/tableau_payload_facts.py") == ["summarise_csv() f-string: note"]


def test_taint_reaches_capture_view_without_a_hand_written_seed():
    """Round 7's structural half: the seed list must SHRINK, not grow.

    `capture_view`'s `view` was hand-seeded in round 6 after the gate was found blind to the path
    defect. It is now derived -- `get_json` -> `list_views` -> `select_views` -> `main`'s loop ->
    `capture_view` -- because taint propagates through RETURN values. This test fails if that chain
    breaks, which is what would silently push the burden back onto the hand-written table.
    """
    module = "scripts/capture_tableau_oracle.py"
    assert (module, "capture_view") not in TAINT_SEEDS, "re-seeding by hand would hide a broken chain"
    tainted = taint_module((REPO / module).read_text(encoding="utf-8"), module)
    assert "view" in tainted["capture_view"]
    assert "record" in tainted["log_progress"]


# ---- module coverage: a credential-handling module may not sit OUTSIDE the gate ------------------
#
# ⚠️ Round 5 disclosed this gap in prose -- "a diagnostic added in a third file is outside it" -- and
# round 7 is that third file: the standalone `sign_in()` in `tableau_render_capability.py` let an
# `HTTPError` escape with the PAT in its reason phrase. A gap that was documented and then fired is
# worth closing structurally. This fails CLOSED: a new credential-handling script is a hard failure
# until someone either brings it under the gate or waives it with a stated reason.

_HTTP_MARKERS = ("urlopen(", "http.client", "requests.")
_CREDENTIAL_MARKERS = ("pat_secret", "X-Tableau-Auth", "TABLEAU_PAT", "personalAccessTokenSecret")

# Scripts that touch a credentialed request but are deliberately NOT under the taint gate, each with
# the reason. A waiver is a decision on the record; absence from both lists is a test failure.
GATE_WAIVERS: dict[str, str] = {
    "scripts/tableau_env.py": "IS the redactor and the chokepoint; gating it against itself is circular",
    "scripts/assess_estate.py": "reads Tableau metadata but persists no response text -- counts and IDs only",
    "scripts/harvest_estate_assets.py": "downloads assets to disk by LUID and already redacts engine stderr",
    "scripts/tableau_lineage.py": "GraphQL lineage to a fixed schema; no response text reaches a path or a log",
    "scripts/provision_tableau_estate.py": "publishes TO Tableau; its credentials are outbound, not reflected",
    "scripts/capture_tableau_reference.py": "no Tableau REST call is wired -- its server provider is a stub (#194)",
    "scripts/capture_tableau_oracle.py": "under the gate as a MODULE entry, listed here only for completeness",
    "scripts/tableau_render_capability.py": "under the gate as a MODULE entry, listed here only for completeness",
    "scripts/run_engine_survey.py": "hands credentials to an engine child process; persists nothing itself",
    "scripts/stamp_tableau_provenance.py": "stamps provenance from a local spec; no live response text",
}


def _credential_handling_scripts() -> list[str]:
    """Scripts that make an HTTP call AND reference a Tableau credential."""
    found = []
    for path in sorted((REPO / "scripts").glob("*.py")):
        source = path.read_text(encoding="utf-8", errors="replace")
        if any(h in source for h in _HTTP_MARKERS) and any(c in source for c in _CREDENTIAL_MARKERS):
            found.append(f"scripts/{path.name}")
    return found


def test_the_credential_handling_detector_actually_detects():
    """Positive control: an assertion that cannot fire is not coverage (round 6's lesson)."""
    scripts = _credential_handling_scripts()
    assert "scripts/tableau_render_capability.py" in scripts, scripts
    assert "scripts/capture_tableau_oracle.py" in scripts, scripts
    assert "scripts/check_unit.py" not in scripts, "the detector is matching modules it should not"


def test_no_credential_handling_script_sits_outside_the_gate_unwaived():
    """The class round 7 identified: a fourth module could be added tomorrow and be silently outside."""
    covered = set(MODULES) | set(GATE_WAIVERS)
    orphans = sorted(set(_credential_handling_scripts()) - covered)
    assert not orphans, (
        f"{orphans} make a credentialed HTTP request but are neither under the taint gate (MODULES) "
        "nor waived. Add them to MODULES and certify their findings, or add a GATE_WAIVERS entry "
        "saying why response text cannot reach a path, a log line or a persisted artifact from there."
    )


def test_every_waiver_names_a_reason_and_a_file_that_exists():
    for script, reason in GATE_WAIVERS.items():
        assert (REPO / script).is_file(), f"waiver for a script that no longer exists: {script}"
        assert len(reason) > 25, f"{script} is waived with no real reason: {reason!r}"


@pytest.mark.parametrize("module", MODULES)
def test_the_declared_seeds_all_exist(module):
    """A seed naming a function or parameter that is gone is a claim about nothing."""
    tree = ast.parse((REPO / module).read_text(encoding="utf-8"))
    functions = {f.name: set(_param_names(f)) for f in _functions(tree)}
    for (mod, func), params in TAINT_SEEDS.items():
        if mod != module:
            continue
        assert func in functions, f"{module}: declared taint seed for missing function {func}()"
        missing = params - functions[func]
        assert not missing, f"{module}:{func}() declared seed parameters that do not exist: {missing}"


def _star_arg_functions(tree: ast.AST) -> list[str]:
    """Functions the taint analyser cannot follow into, because it maps args onto named parameters."""
    return [f.name for f in _functions(tree) if f.args.vararg or f.args.kwarg]


def test_the_star_arg_detector_actually_detects():
    """⚠️ Positive control, because the gate below SURVIVED a mutation that disabled it.

    It survived for an honest reason -- the guarded modules contain no `*args`, so switching the check
    off changes nothing today. But an assertion that cannot fail is not coverage, and this project has
    already shipped one unkillable guard. So the DETECTOR is tested here against a module that does use
    the construct, and the gate below is left as the forward-looking guard it is.
    """
    assert _star_arg_functions(ast.parse("def f(*args):\n    pass\n")) == ["f"]
    assert _star_arg_functions(ast.parse("def f(**kw):\n    pass\n")) == ["f"]
    assert _star_arg_functions(ast.parse("def f(a, b=1, *, c=2):\n    pass\n")) == []


@pytest.mark.parametrize("module", MODULES)
def test_the_analyser_cannot_follow_star_args_so_the_guarded_modules_may_not_use_them(module):
    """The boundary, enforced rather than documented.

    `taint_module` maps a call's positional arguments onto the callee's declared parameters, so a
    `*args`/`**kwargs` forwarder is a hole it cannot see through (measured: a `def helper(*args)`
    forwarder produces an empty finding list). Rather than teach the analyser a construct these
    modules do not use, the construct is forbidden here -- the same closed-allowlist move that
    replaced `safe_slug`.
    """
    offenders = _star_arg_functions(ast.parse((REPO / module).read_text(encoding="utf-8")))
    assert not offenders, (
        f"{module}: {offenders} use *args/**kwargs, which the taint analyser cannot follow. "
        "Name the parameters, or extend `taint_module` and delete this test."
    )


def _guarded_imports(tree: ast.AST) -> dict[str, str]:
    """name-used-in-this-module -> the guarded module it came from (aliased or `from X import y`)."""
    stems = {Path(m).stem: m for m in MODULES}
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in stems:
                    found[alias.asname or alias.name] = stems[alias.name]
        elif isinstance(node, ast.ImportFrom) and node.module in stems:
            for alias in node.names:
                found[alias.asname or alias.name] = stems[node.module]
    return found


@pytest.mark.parametrize("module", MODULES)
def test_every_cross_module_call_carrying_tainted_data_lands_on_a_declared_seed(module):
    """Taint propagation is INTRA-module, so a call into another guarded module is the boundary.

    That boundary is crossed exactly once today -- `capability.format_matches(kind, payload, ...)` --
    and `format_matches` declares `{"body", "content_type"}` in TAINT_SEEDS, so the value is re-seeded
    on arrival. This test is what stops the next such call being added without one, which would let
    tainted data enter a module with no seed and therefore no findings at all.

    ⚠️ It covers calls whose callee is IMPORTED from a guarded module, in either spelling
    (`capability.format_matches(...)` and a bare `summarise_csv(...)`). It cannot see a callee reached
    through a variable or a dispatch table; no such call exists in these modules.
    """
    source = (REPO / module).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = _guarded_imports(tree)
    tainted = taint_module(source, module)
    targets = {m: ast.parse((REPO / m).read_text(encoding="utf-8")) for m in set(imported.values())}
    undeclared = []
    for func in _functions(tree):
        local = tainted.get(func.name, set())
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            owner = None
            if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                owner = imported.get(node.func.value.id)
            elif isinstance(node.func, ast.Name):
                owner = imported.get(node.func.id)
            if owner is None or name is None:
                continue
            callee = next((f for f in _functions(targets[owner]) if f.name == name), None)
            if callee is None:
                continue
            params = _param_names(callee)
            declared = TAINT_SEEDS.get((owner, name), set())
            for index, arg in enumerate(node.args):
                if _roots(arg) & local and index < len(params) and params[index] not in declared:
                    undeclared.append(f"{func.name}() -> {owner}:{name}() parameter {params[index]!r}")
            for keyword in node.keywords:
                if keyword.arg and _roots(keyword.value) & local and keyword.arg not in declared:
                    undeclared.append(f"{func.name}() -> {owner}:{name}() keyword {keyword.arg!r}")
    assert not sorted(set(undeclared)), (
        f"{module} passes response-derived data across a module boundary into a parameter with no "
        f"declared taint seed, so it arrives untracked: {sorted(set(undeclared))}. Add it to TAINT_SEEDS."
    )


def test_the_static_gate_would_now_catch_the_round_6_PATH_defect(tmp_path):
    """⚠️ Round 6 was caught only by the RUNTIME battery. The static gate was blind to it.

    Measured before `view` was seeded: reintroducing `safe_slug(view["name"])` as the artifact stem
    produced **zero** findings. That is a gate that could not see the escape it exists to prevent, so
    `view` -- a dict straight off `list_views()` -> `get_json` -- is now a declared taint seed, and the
    same regression reaches the `write-path` exit at both write sites.
    """
    _ = tmp_path
    module = "scripts/capture_tableau_oracle.py"
    source = (REPO / module).read_text(encoding="utf-8")
    anchor = "        stem = artifact_stem(view_luid)"
    assert source.count(anchor) == 1, "the anchor moved; this test would otherwise mutate nothing"
    regressed = source.replace(
        anchor, '        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", view.get("name", "")).strip("_")[:60]'
    )
    new = set(uncertified_sinks(regressed, module)) - set(uncertified_sinks(source, module))
    assert {"_capture_data() write-path: path", "_capture_render() write-path: path"} <= new, sorted(new)


def test_a_bound_method_call_maps_argument_0_onto_parameter_1_not_onto_self():
    """Found by over-seeding: `x.m(tainted)` was mapping onto the callee's `self`.

    It over-taints loudly -- the whole receiver becomes response data -- but the dangerous half is
    silent: every later argument shifts by one, so a genuinely tainted third argument was checked
    against the second parameter's name and could pass unflagged.
    """
    source = (
        "class S:\n"
        "    def sink(self, first, second):\n"
        '        return f"{second}"\n'
        "    def summarise_csv(self, payload):\n"
        "        return self.sink('constant', payload)\n"
    )
    assert uncertified_sinks(source, "scripts/tableau_payload_facts.py") == ["sink() f-string: second"]


@pytest.mark.parametrize("module", MODULES)
def test_the_certification_list_has_no_stale_entries(module):
    """A certification for an expression that no longer reaches an exit is a claim about nothing."""
    source = (REPO / module).read_text(encoding="utf-8")
    tainted = taint_module(source, module)
    live: set[tuple[str, str]] = set()
    for func in _functions(ast.parse(source)):
        local = tainted.get(func.name, set())
        for _kind, expr in sink_expressions(func):
            if _roots(expr) & local:
                live.add((func.name, ast.unparse(expr)))
    stale = sorted(
        f"{name}(): {expr}"
        for (mod, name), entries in CERTIFIED.items()
        if mod == module
        for expr in entries
        if (name, expr) not in live
    )
    assert not stale, f"{module}: certified expressions no longer reaching an exit: {stale}"


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


# ---------------------------------------------------------- round 6: the CONSOLE is the third artifact


def test_the_progress_line_redacts_a_view_name_before_it_truncates_it(caplog):
    """`log_progress` sliced the name to 34 characters first, which is round 4's defect at a boundary
    round 4 never considered. CI keeps its logs, so "only the terminal" is not a mitigation."""
    token = "SYNTHETIC_SESSION_TOKEN_42_LONG_ENOUGH_TO_BE_TRUNCATED"
    session = _Session("an-unrelated-long-pat-secret")
    session.token = token
    record = {"view_name": token, "data": {"status": "ok", "row_count": 1, "elapsed_sec": 0.1}}
    with caplog.at_level(logging.INFO, logger="tableau-oracle"):
        oracle.log_progress(1, 1, record, session.redact_text)
    assert longest_surviving_run(token, caplog.text) == ""
    assert "[REDACTED]" in caplog.text


def test_the_blocked_list_redacts_the_names_it_prints(caplog):
    """`_log_blocked_and_stale` runs on the UNSCRUBBED records -- `scrub_tree` returns a copy -- so the
    console would otherwise print exactly what the manifest was careful not to."""
    token = "SYNTHETIC_SESSION_TOKEN_42_LONG_ENOUGH"
    session = _Session("an-unrelated-long-pat-secret")
    session.token = token
    blocked = [{"view_name": token, "workbook_name": token, "data": {"status": "source_credential", "detail": token}}]
    with caplog.at_level(logging.WARNING, logger="tableau-oracle"):
        oracle._log_blocked_and_stale(blocked, blocked, None, session.redact_text)  # pylint: disable=protected-access
    assert longest_surviving_run(token, caplog.text) == ""
    assert "[REDACTED]" in caplog.text


# --------------------------------------------- round 6: key scrubbing, collisions, and hit paths


def test_scrub_tree_scrubs_dict_KEYS_not_only_values():
    tree = {"format_hints": {"SECRET_COLUMN_42": "percent"}}
    scrubbed, hits = scrub_tree(tree, lambda t: t.replace("SECRET_COLUMN_42", "[R]"))
    assert scrubbed == {"format_hints": {"[R]": "percent"}}
    assert hits == ["format_hints.[R] (key)"]


def test_a_redaction_induced_key_collision_is_disambiguated_never_dropped():
    """Two distinct keys can scrub to the same string; `dict` would keep the last and lose the rest,
    turning a redaction into silent data loss."""
    tree = {"SECRET_A": 1, "SECRET_B": 2, "kept": 3}
    scrubbed, hits = scrub_tree(tree, lambda t: re.sub(r"SECRET_[AB]", "[R]", t))
    assert scrubbed == {"[R]": 1, "[R]#2": 2, "kept": 3}, "no field may vanish into a collision"
    assert len(hits) == 2


def test_a_recorded_hit_path_never_contains_the_unsanitised_key():
    """Otherwise the guard re-emits, in `credential_scrubbed_at_sink`, exactly what it just caught."""
    tree = {"views": [{"format_hints": {"SECRET_COLUMN_42": "percent"}}]}
    _scrubbed, hits = scrub_tree(tree, lambda t: t.replace("SECRET_COLUMN_42", "[R]"))
    assert hits == ["views[0].format_hints.[R] (key)"]
    assert "SECRET_COLUMN_42" not in " ".join(hits)


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


# ---------------------------------------------------------- round 7: the HTTP REASON PHRASE
#
# ⚠️ A surface distinct from a reflecting BODY, which the battery above already covers, and the ONE
# leak in this PR that master does not have: `capture_tableau_oracle` has always caught `HTTPError`
# inside `_request`, but the standalone `sign_in()` added for the #403 probe let it escape. Its
# message carries the server-controlled reason phrase, and that request contains the PAT.


def _one_request_server(status: int, reason: str, body: bytes):
    """A local server that answers exactly one POST. No live site, no .env, no credential on disk."""

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.send_response(status, reason)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("planted", ["pat_secret", "pat_name"])
def test_the_signin_reason_phrase_never_carries_a_credential(shape, planted):
    """Measured before the fix: `HTTPError: HTTP Error 403: SYNTHETIC_PAT_SECRET_REASON_42`.

    ⚠️ Catches BaseException rather than using `pytest.raises(RuntimeError)`, and the mutation harness
    is why. With `pytest.raises`, deleting the `except` clause let the raw `HTTPError` propagate --
    a genuine kill -- but the test then died of an unexpected EXCEPTION rather than an assertion, and
    a harness that (correctly) refuses to score crashes as kills reported INVALID. That is a false
    INVALID: the mirror of the false CAUGHT the rule exists to prevent, and it hid a working
    regression test behind a scoring artefact.

    The deeper point is that the question was wrong. This test is not "does a RuntimeError come out";
    it is "can a credential escape by ANY path". Asking the second makes the leak an assertion
    failure whatever the exception type, and the intended type is still pinned below.
    """
    secret = SHAPES[shape].replace("\n", " ").strip() or "SYNTHETIC_SECRET_42"
    # An HTTP reason phrase is a single header line; CR/LF and non-latin-1 cannot travel in one.
    reason = "".join(ch for ch in secret if ch.isprintable() and ord(ch) < 256) or "SYNTHETIC_SECRET_42"
    pat_secret_value = reason if planted == "pat_secret" else "an-unrelated-long-pat-secret"
    pat_name = reason if planted == "pat_name" else "an-unrelated-long-pat-name"
    server = _one_request_server(403, reason, b"")
    raised: BaseException | None = None
    try:
        cap.sign_in(f"http://127.0.0.1:{server.server_port}", "site", pat_name, pat_secret_value, "3.29")
    except BaseException as exc:  # noqa: BLE001  # ANY escape is in scope -- that is the finding
        raised = exc
    finally:
        server.shutdown()
        server.server_close()
    assert raised is not None, "sign_in must not report success for an HTTP 403"
    message = f"{type(raised).__name__}: {raised}"
    assert longest_surviving_run(reason, message) == "", message
    assert "403" in message, "the status must survive; redaction that destroys the diagnostic is not a fix"
    assert isinstance(raised, RuntimeError), f"the sanitised failure must be a RuntimeError, got {message}"


def test_the_signin_error_BODY_is_redacted_too_and_the_reason_still_reads():
    """Both surfaces, and the non-secret half of each must still be legible."""
    secret = "SYNTHETIC_PAT_SECRET_REASON_42"
    server = _one_request_server(403, "Forbidden", secret.encode())
    raised: BaseException | None = None
    try:
        cap.sign_in(f"http://127.0.0.1:{server.server_port}", "site", "a-long-pat-name", secret, "3.29")
    except BaseException as exc:  # noqa: BLE001
        raised = exc
    finally:
        server.shutdown()
        server.server_close()
    message = f"{type(raised).__name__}: {raised}"
    assert secret not in message, message
    assert "Forbidden" in message and "403" in message
    assert "[REDACTED]" in message
    assert isinstance(raised, RuntimeError), message


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
