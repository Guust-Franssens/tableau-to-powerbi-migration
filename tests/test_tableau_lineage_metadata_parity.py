"""Metadata-client parity experiment for issue #554.

``assess_estate.py``'s ``Site.graphql()`` and ``tableau_lineage.py``'s ``fetch_lineage()`` both
target ``POST <server>/api/metadata/graphql`` with ``X-Tableau-Auth``, using the SAME `.env`
credential context. A frozen live run (repository ``b438381``, run 409) recorded the assessment's
GraphQL call succeeding while the lineage script's call to the same site failed with an immediate
HTTP 401 - the asymmetry this file investigates.

The bounded brief (issue #554 comment): under one injected transport, capture and compare both
clients' method, normalized URL, headers, body encoding and HTTP-error handling; add a positive
control; classify a 401 as a final verdict (never retried, never a partial-plan fallback) for both;
and add a discriminating control that tells a GraphQL-level (200-with-``errors``) refusal apart from
a transport-level 401.

**Finding, narrowed to what this file actually measures** (round 2 correction - an earlier revision
of this docstring over-claimed "transport-equivalent" without having captured timeouts or exercised
a nonempty 401 body):

* Sign-in and the metadata call itself ARE shape-identical for method, normalized path, headers and
  body encoding (tests 1-2 below), and a bare 401/403 (no recognized body) is a final, never-retried
  verdict for BOTH clients (test 3) - that part of the equivalence claim IS measured.
* The two clients do NOT use the same timeout value (test 3b records both inputs rather than
  asserting they match - ``assess_estate.py`` is independently configurable via
  ``--graphql-timeout``/``--rest-timeout``; ``tableau_lineage.py`` hard-codes 120s). That is an
  existing, independent difference this brief does not change (out of scope: "no broad retry
  framework or project-scope changes").
* The two clients do NOT handle a *recognized session-expiry* 401 (Tableau's own ``401002`` body)
  the same way: ``assess_estate.py`` re-authenticates once (bounded by ``MAX_REAUTH``) and retries
  the SAME request; ``tableau_lineage.py`` has no such classification and propagates any 401 -
  including a ``401002`` one - immediately as a final answer (test 3c). This is a REAL, measured
  behavioral gap, not a transport bug: reproducing ``assess_estate.py``'s narrow reauth-on-401002
  policy here would need either importing its ``classify``/``SESSION_LOST`` (a new cross-script
  dependency the round-2 brief asks to avoid unless "strictly smaller", and one that would also
  widen the tainted-parameter surface ``test_diagnostic_redaction.py`` enumerates for
  ``fetch_lineage``) or reimplementing it locally, neither of which is in scope for this bounded fix.
  **Per the issue's option (b): this gap is documented rather than silently claimed as parity, and
  #554 should stay open pending the live trial-site re-run (acceptance #4) even after this PR.** If
  a live 401 turns out to carry a ``401002`` body, that is very likely the actual explanation, and
  the fix would be adding the SAME bounded reauth this file demonstrates ``tableau_lineage.py``
  lacks - not a broader retry policy.
* The one drift this file DOES fix: ``tableau_lineage.py`` never read ``TABLEAU_REST_API_VERSION``
  from `.env` at all, hardcoding a REST API version for sign-in that could silently differ from the
  one ``assess_estate.py`` (and every other Tableau client in this repo) reads from the SAME `.env`
  file - independently worth fixing regardless of whether it explains the live 401.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

_spec = importlib.util.spec_from_file_location("assess_estate", SCRIPTS / "assess_estate.py")
assess_estate = importlib.util.module_from_spec(_spec)
sys.modules["assess_estate"] = assess_estate
_spec.loader.exec_module(assess_estate)

# ruff: noqa: E402  (the sys.path insert above must precede this import)
import tableau_lineage

ENV = {
    "TABLEAU_SERVER_URL": "https://tableau.invalid",
    "TABLEAU_SITE": "acme",
    "TABLEAU_PAT_NAME": "parity-pat",
    "TABLEAU_PAT_SECRET": "not-a-real-secret-0123456789",
    "TABLEAU_REST_API_VERSION": "3.29",
}

SIGNIN_BODY = json.dumps({"credentials": {"token": "session-token-abc", "site": {"id": "site-1"}}}).encode()
STRUCTURE_BODY = json.dumps({"data": {"workbooks": []}}).encode()
LINEAGE_BODY = json.dumps({"data": {"publishedDatasources": []}}).encode()
GRAPHQL_ERRORS_BODY = json.dumps(
    {"errors": [{"message": "field 'downstreamWorkbooks' requires Data Management"}]}
).encode()
SESSION_LOST_BODY = json.dumps(
    {"error": {"code": "401002", "summary": "Signed Out", "detail": "The session is not valid."}}
).encode()


class _Response:
    """The slice of ``http.client.HTTPResponse`` both clients' transport touches."""

    def __init__(self, status: int, payload: bytes) -> None:
        self.status = status
        self._payload = payload
        self.headers: dict[str, str] = {}

    def read(self) -> bytes:
        return self._payload

    def close(self) -> None:
        """``urllib.error.HTTPError`` closes the body it was handed."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False


class RecordingTransport:
    """One scripted, in-process stand-in for ``urllib.request.urlopen``.

    Both clients are pointed at the SAME instance so the comparison is apples to apples: identical
    scripted responses, one shared call log, no real network. ``metadata_status``/``metadata_body``
    covers the ONE endpoint under investigation; sign-in always succeeds so a metadata-call failure
    is never confused with an authentication failure.
    """

    def __init__(
        self,
        *,
        metadata_status: int = 200,
        metadata_body: bytes = STRUCTURE_BODY,
        metadata_error_body: bytes = b"",
    ) -> None:
        self.calls: list[urllib.request.Request] = []
        self.timeouts: list[float | None] = []
        self.metadata_status = metadata_status
        self.metadata_body = metadata_body
        self.metadata_error_body = metadata_error_body

    def __call__(self, request: urllib.request.Request, timeout: float | None = None) -> _Response:
        self.calls.append(request)
        self.timeouts.append(timeout)
        url = request.full_url
        if url.endswith("/auth/signin"):
            return _Response(200, SIGNIN_BODY)
        if "/metadata/graphql" in url:
            if self.metadata_status != 200:
                raise urllib.error.HTTPError(
                    url,
                    self.metadata_status,
                    "Unauthorized",
                    {},
                    _Response(self.metadata_status, self.metadata_error_body),
                )
            return _Response(200, self.metadata_body)
        raise AssertionError(f"unscripted call in the parity experiment: {url}")

    def calls_matching(self, fragment: str) -> list[urllib.request.Request]:
        return [call for call in self.calls if fragment in call.full_url]

    def timeouts_matching(self, fragment: str) -> list[float | None]:
        """Timeout inputs, in call order - paired positionally with ``calls_matching``."""
        return [t for call, t in zip(self.calls, self.timeouts) if fragment in call.full_url]


def _normalize(request: urllib.request.Request) -> dict:
    """Reduce a ``Request`` to exactly what the brief asks to compare - never the host, which is
    test-fixture noise, always the path, method, headers and body encoding."""
    return {
        "method": request.get_method(),
        "path": urlsplit(request.full_url).path,
        "auth_header": request.get_header("X-tableau-auth"),
        "content_type": request.get_header("Content-type"),
        "accept": request.get_header("Accept"),
        "body": json.loads(request.data.decode("utf-8")) if request.data else None,
    }


def _sign_in_both(transport: RecordingTransport, monkeypatch: pytest.MonkeyPatch):
    """Sign in through both clients against the SAME injected transport and env."""
    monkeypatch.setattr(assess_estate.urllib.request, "urlopen", transport)
    monkeypatch.setattr(tableau_lineage.urllib.request, "urlopen", transport)
    site = assess_estate.Site(ENV)
    site.sign_in()
    session = tableau_lineage.sign_in(
        ENV["TABLEAU_SERVER_URL"],
        ENV["TABLEAU_SITE"],
        ENV["TABLEAU_PAT_NAME"],
        ENV["TABLEAU_PAT_SECRET"],
        ENV["TABLEAU_REST_API_VERSION"],
    )
    return site, session


# --- 1. capture and compare: sign-in and the metadata call itself, normalized -----------------


def test_signin_requests_are_shape_identical_given_the_same_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same `.env`, same REST API version passed explicitly -> the two sign-in requests match."""
    transport = RecordingTransport()
    site, session = _sign_in_both(transport, monkeypatch)

    signin_calls = transport.calls_matching("/auth/signin")
    assert len(signin_calls) == 2
    first, second = (_normalize(call) for call in signin_calls)
    assert first["method"] == second["method"] == "POST"
    assert first["path"] == second["path"], "both clients must sign in against the same REST API version path"
    assert first["body"] == second["body"]
    assert first["content_type"] == second["content_type"] == "application/json"
    assert site.token == session.token == "session-token-abc"


def test_metadata_requests_are_shape_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    """The metadata call itself: same normalized URL, method, auth header and body encoding."""
    transport = RecordingTransport(metadata_body=STRUCTURE_BODY)
    site, session = _sign_in_both(transport, monkeypatch)

    site.graphql(assess_estate.STRUCTURE_QUERY)
    tableau_lineage.fetch_lineage(session)

    metadata_calls = transport.calls_matching("/metadata/graphql")
    assert len(metadata_calls) == 2
    first, second = (_normalize(call) for call in metadata_calls)
    assert first["method"] == second["method"] == "POST"
    assert first["path"] == second["path"] == "/api/metadata/graphql"
    assert first["accept"] == second["accept"] == "application/json"
    assert first["content_type"] == second["content_type"] == "application/json"
    assert first["auth_header"] == second["auth_header"] == "session-token-abc"
    # The query TEXT legitimately differs (structure vs. lineage) - the body SHAPE must not.
    assert set(first["body"]) == set(second["body"]) == {"query"}
    assert isinstance(first["body"]["query"], str) and isinstance(second["body"]["query"], str)


# --- 2. positive control -------------------------------------------------------------------------


def test_a_synthetic_success_response_is_accepted_by_both_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = RecordingTransport(metadata_status=200, metadata_body=STRUCTURE_BODY)
    site, session = _sign_in_both(transport, monkeypatch)

    payload, error = site.graphql(assess_estate.STRUCTURE_QUERY)
    assert error is None
    assert payload == {"data": {"workbooks": []}}

    transport.metadata_body = LINEAGE_BODY
    assert tableau_lineage.fetch_lineage(session) == []


# --- 3. the same 401 is a FINAL verdict for both, never retried, never a partial-plan fallback ----


def test_a_401_is_a_final_verdict_for_both_clients_with_no_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = RecordingTransport(metadata_status=401)
    site, session = _sign_in_both(transport, monkeypatch)

    payload, error = site.graphql(assess_estate.STRUCTURE_QUERY)
    assert payload == {}
    assert error is not None and error["status"] == 401
    assert assess_estate.classify(401, "") == "denied"  # a final answer, never "transient"

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        tableau_lineage.fetch_lineage(session)
    assert excinfo.value.code == 401

    # Exactly one metadata call each - neither client retried the 401.
    metadata_calls = transport.calls_matching("/metadata/graphql")
    assert len(metadata_calls) == 2


def test_a_401_never_falls_back_to_a_partial_or_site_wide_plan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """CLI-level: main() must exit non-zero and write NOTHING - never a degraded plan (issue #554 #5)."""
    transport = RecordingTransport(metadata_status=401)
    monkeypatch.setattr(tableau_lineage.urllib.request, "urlopen", transport)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(f"{key}={value}" for key, value in ENV.items()),
        encoding="utf-8",
    )
    save_json = tmp_path / "lineage-response.json"

    exit_code = tableau_lineage.main(["--plan", "--env", str(env_path), "--save-json", str(save_json)])

    assert exit_code == 1
    assert not save_json.exists()


def test_timeout_inputs_are_recorded_not_assumed_equal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Round-2 ask: RECORD the timeout each client actually passes to ``urlopen`` rather than
    asserting an unmeasured equivalence claim. ``assess_estate.py`` uses its configurable
    ``--graphql-timeout`` (default 300s); ``tableau_lineage.py`` hard-codes 120s. That is an
    existing, independent difference - out of scope for this bounded fix ("no broad retry/timeout
    framework")."""
    transport = RecordingTransport()
    site, session = _sign_in_both(transport, monkeypatch)

    site.graphql(assess_estate.STRUCTURE_QUERY)
    tableau_lineage.fetch_lineage(session)

    metadata_timeouts = transport.timeouts_matching("/metadata/graphql")
    assert len(metadata_timeouts) == 2
    site_timeout, lineage_timeout = metadata_timeouts
    assert site_timeout == assess_estate.DEFAULT_GRAPHQL_TIMEOUT_SEC == 300.0
    assert lineage_timeout == 120
    assert site_timeout != lineage_timeout  # recorded as-is, not papered over


def test_a_recognized_session_expiry_401_is_not_handled_the_same_way(monkeypatch: pytest.MonkeyPatch) -> None:
    """Round-2 ask #3: a nonempty 401 carrying Tableau's OWN session-expiry code (``401002``) is a
    RECOGNIZED, bounded reauth-and-retry case for ``assess_estate.py`` and an unrecognized,
    immediately-final one for ``tableau_lineage.py``. This is a REAL measured gap, documented here
    rather than papered over - see the module docstring for why reproducing it is out of scope for
    this bounded fix, and why #554 should stay open pending the live trial-site re-run."""
    transport = RecordingTransport(metadata_status=401, metadata_error_body=SESSION_LOST_BODY)
    site, session = _sign_in_both(transport, monkeypatch)
    signin_calls_before = len(transport.calls_matching("/auth/signin"))

    payload, error = site.graphql(assess_estate.STRUCTURE_QUERY)
    assert payload == {}
    assert error is not None and error["status"] == 401
    assert assess_estate.classify(401, str(SESSION_LOST_BODY)) == "session_lost"
    # assess_estate recognized 401002 and spent its bounded reauth budget re-signing-in.
    reauth_signins = len(transport.calls_matching("/auth/signin")) - signin_calls_before
    assert reauth_signins == assess_estate.MAX_REAUTH

    signin_calls_before_lineage = len(transport.calls_matching("/auth/signin"))
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        tableau_lineage.fetch_lineage(session)
    assert excinfo.value.code == 401
    # tableau_lineage has no such classification: zero additional sign-in attempts, no retry at all.
    assert len(transport.calls_matching("/auth/signin")) == signin_calls_before_lineage


# --- 4. discriminating control: a GraphQL-level refusal is NOT the same as a transport 401 --------


def test_a_query_level_graphql_error_is_distinct_from_a_transport_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 carrying an ``errors`` array (e.g. a Data Management license gate) is a DIFFERENT
    verdict from a 401: assess_estate reports ``ok`` transport with query errors in the payload;
    tableau_lineage raises ``RuntimeError``, never ``HTTPError`` - so a caller catching only
    ``HTTPError`` cannot mistake one for the other."""
    transport = RecordingTransport(metadata_status=200, metadata_body=GRAPHQL_ERRORS_BODY)
    site, session = _sign_in_both(transport, monkeypatch)

    payload, error = site.graphql(assess_estate.STRUCTURE_QUERY)
    assert error is None  # transport succeeded - this is a QUERY-level refusal, not a transport one
    assert payload.get("errors")

    with pytest.raises(RuntimeError, match="Metadata API returned errors"):
        tableau_lineage.fetch_lineage(session)


# --- 5. the .env-driven REST API version drift itself (the concrete parity bug this file found) --


def test_lineage_reads_the_same_rest_api_version_env_var_assess_estate_does(tmp_path: Path) -> None:
    """Before the fix, ``tableau_lineage.py`` never read ``TABLEAU_REST_API_VERSION`` at all and
    signed in with a hardcoded default regardless of `.env` - so the SAME `.env` could drive
    ``assess_estate.py`` and ``tableau_lineage.py`` to sign in at DIFFERENT REST API versions."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(f"{key}={value}" for key, value in ENV.items()),
        encoding="utf-8",
    )

    _server, _site_url, _pat_name, _pat_secret, env_api_version = tableau_lineage._env_config(  # pylint: disable=protected-access
        env_path
    )

    assert env_api_version == ENV["TABLEAU_REST_API_VERSION"]

    site = assess_estate.Site({**ENV})
    assert site.version == ENV["TABLEAU_REST_API_VERSION"]


def test_an_explicit_cli_api_version_still_overrides_the_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--api-version`` remains an explicit override, never silently shadowed by `.env`."""
    transport = RecordingTransport()
    monkeypatch.setattr(tableau_lineage.urllib.request, "urlopen", transport)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(f"{key}={value}" for key, value in ENV.items()),
        encoding="utf-8",
    )

    exit_code = tableau_lineage.main(["--plan", "--env", str(env_path), "--api-version", "3.4"])

    assert exit_code == 0
    signin_calls = transport.calls_matching("/auth/signin")
    assert len(signin_calls) == 1
    assert urlsplit(signin_calls[0].full_url).path == "/api/3.4/auth/signin"


# --- 6. version validation: blank is ABSENT, malformed is a CONFIGURATION error (round-2 ask #1) --


def test_blank_env_and_cli_api_versions_fall_back_to_the_default(tmp_path: Path) -> None:
    """Blank means ABSENT, never malformed - stripped whitespace-only input falls back to the
    documented default for both the env-sourced value and an explicit ``--api-version``."""
    for blank in ("", "   ", "\t"):
        env_path = tmp_path / f".env-blank-{len(blank)}"
        values = {**ENV, "TABLEAU_REST_API_VERSION": blank}
        env_path.write_text("\n".join(f"{key}={value}" for key, value in values.items()), encoding="utf-8")
        *_rest, env_api_version = tableau_lineage._env_config(env_path)  # pylint: disable=protected-access
        assert env_api_version == tableau_lineage.DEFAULT_API_VERSION

        assert (
            tableau_lineage._resolve_api_version(blank, "--api-version")  # pylint: disable=protected-access
            == tableau_lineage.DEFAULT_API_VERSION
        )


@pytest.mark.parametrize("bad_version", ["banana", "3.", ".21", "3", "3.21x", "v3.21", "3..21", "3.21/etc"])
def test_a_malformed_api_version_is_a_configuration_error_before_any_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad_version: str
) -> None:
    """A malformed ``.env`` value AND a malformed explicit ``--api-version`` must both fail as a
    usage/configuration error BEFORE sign-in - never reach the transport, never get silently
    coerced into the default (that would hide a typo'd version rather than reject it)."""
    transport = RecordingTransport()
    monkeypatch.setattr(tableau_lineage.urllib.request, "urlopen", transport)

    malformed_env = tmp_path / ".env-malformed"
    malformed_values = {**ENV, "TABLEAU_REST_API_VERSION": bad_version}
    malformed_env.write_text("\n".join(f"{key}={value}" for key, value in malformed_values.items()), encoding="utf-8")
    with pytest.raises(SystemExit):
        tableau_lineage._env_config(malformed_env)  # pylint: disable=protected-access
    assert transport.calls == []

    valid_env = tmp_path / ".env-valid"
    valid_env.write_text("\n".join(f"{key}={value}" for key, value in ENV.items()), encoding="utf-8")
    with pytest.raises(SystemExit):
        tableau_lineage.main(["--plan", "--env", str(valid_env), "--api-version", bad_version])
    assert transport.calls == []


# --- 7. main()'s ACTUAL selection, not just _env_config in isolation (round-2 ask #2) --------------


def test_main_selects_the_env_rest_api_version_with_no_cli_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CLI-level, not unit-level: fails if ``main()`` itself ever stops honoring the env value even
    while ``_env_config`` in isolation stays correct."""
    transport = RecordingTransport()
    monkeypatch.setattr(tableau_lineage.urllib.request, "urlopen", transport)
    env_path = tmp_path / ".env"
    env_path.write_text("\n".join(f"{key}={value}" for key, value in ENV.items()), encoding="utf-8")

    exit_code = tableau_lineage.main(["--plan", "--env", str(env_path)])

    assert exit_code == 0
    signin_calls = transport.calls_matching("/auth/signin")
    assert len(signin_calls) == 1
    assert urlsplit(signin_calls[0].full_url).path == "/api/3.29/auth/signin"


def test_main_falls_back_to_the_documented_default_with_no_env_or_cli_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Absent env AND no ``--api-version`` -> the literal documented default (``3.21``), reached by
    ``main()`` itself. A regression that reintroduces a hardcoded ``3.19`` inside ``main()`` - even
    if ``_env_config``/``_resolve_api_version`` stay correct - must fail THIS test: the literal is
    written out rather than read back from ``DEFAULT_API_VERSION``, which a hardcoded regression
    would not touch."""
    transport = RecordingTransport()
    monkeypatch.setattr(tableau_lineage.urllib.request, "urlopen", transport)
    values = {key: value for key, value in ENV.items() if key != "TABLEAU_REST_API_VERSION"}
    env_path = tmp_path / ".env"
    env_path.write_text("\n".join(f"{key}={value}" for key, value in values.items()), encoding="utf-8")

    exit_code = tableau_lineage.main(["--plan", "--env", str(env_path)])

    assert exit_code == 0
    signin_calls = transport.calls_matching("/auth/signin")
    assert len(signin_calls) == 1
    assert urlsplit(signin_calls[0].full_url).path == "/api/3.21/auth/signin"


# --- 8. reflected-credential redaction at the TOP-LEVEL error log (round-3 security fix) -----------
#
# `main()`'s top-level `except` handler used to redact ONLY `pat_secret` from the logged error text,
# even though an `HTTPError.reason` (which `str(exc)` includes) can echo the PAT NAME or the
# authenticated session TOKEN just as easily as the secret - an adversarial echo server reflects
# whatever the request sent, and all three travel in this request. A reflected name or token is
# exactly as sensitive as the secret itself; logging it verbatim persists it as durably as the secret
# leak `redact()`'s own docstring was written to prevent (see `tableau_env.redact`, issue #97/#381).


def _reflecting_transport(*, reflect_at: str, reflected_value: str):
    """A transport whose ``reason`` at one named hop echoes ``reflected_value`` verbatim, exactly
    how an adversarial echo server would - not something either client would ever construct itself.
    """

    def _urlopen(request: urllib.request.Request, timeout: float | None = None) -> _Response:
        url = request.full_url
        if url.endswith("/auth/signin"):
            if reflect_at == "signin":
                raise urllib.error.HTTPError(url, 401, f"Unauthorized: {reflected_value}", {}, _Response(401, b""))
            return _Response(200, SIGNIN_BODY)
        if "/metadata/graphql" in url:
            raise urllib.error.HTTPError(url, 401, f"Unauthorized: {reflected_value}", {}, _Response(401, b""))
        raise AssertionError(f"unscripted call in the parity experiment: {url}")

    return _urlopen


def test_a_reflected_pat_name_during_signin_failure_never_reaches_the_top_level_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A sign-in failure has no session/token yet - only ``pat_name``/``pat_secret`` can leak here,
    and the fix must not crash on the not-yet-assigned ``session`` (no unbound-local exception)."""
    reflected = ENV["TABLEAU_PAT_NAME"]
    transport = _reflecting_transport(reflect_at="signin", reflected_value=reflected)
    monkeypatch.setattr(tableau_lineage.urllib.request, "urlopen", transport)
    env_path = tmp_path / ".env"
    env_path.write_text("\n".join(f"{key}={value}" for key, value in ENV.items()), encoding="utf-8")

    with caplog.at_level("ERROR"):
        exit_code = tableau_lineage.main(["--plan", "--env", str(env_path)])

    assert exit_code == 1
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert reflected not in logged
    assert "[REDACTED]" in logged


def test_a_reflected_pat_secret_after_authentication_is_redacted_from_the_top_level_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The Metadata call fails AFTER a successful sign-in (a ``session`` now exists); its reflected
    PAT secret must still be redacted - the fix must not rely on the failure happening pre-session."""
    reflected = ENV["TABLEAU_PAT_SECRET"]
    transport = _reflecting_transport(reflect_at="metadata", reflected_value=reflected)
    monkeypatch.setattr(tableau_lineage.urllib.request, "urlopen", transport)
    env_path = tmp_path / ".env"
    env_path.write_text("\n".join(f"{key}={value}" for key, value in ENV.items()), encoding="utf-8")

    with caplog.at_level("ERROR"):
        exit_code = tableau_lineage.main(["--plan", "--env", str(env_path)])

    assert exit_code == 1
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert reflected not in logged
    assert "[REDACTED]" in logged


def test_a_reflected_session_token_after_metadata_failure_is_redacted_from_the_top_level_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The session TOKEN (only known once sign-in succeeds) must ALSO be redacted from this same
    log line - the pre-fix code redacted only ``pat_secret`` and never touched the token at all."""
    reflected = "session-token-abc"  # == SIGNIN_BODY's token, i.e. the real authenticated session
    transport = _reflecting_transport(reflect_at="metadata", reflected_value=reflected)
    monkeypatch.setattr(tableau_lineage.urllib.request, "urlopen", transport)
    env_path = tmp_path / ".env"
    env_path.write_text("\n".join(f"{key}={value}" for key, value in ENV.items()), encoding="utf-8")

    with caplog.at_level("ERROR"):
        exit_code = tableau_lineage.main(["--plan", "--env", str(env_path)])

    assert exit_code == 1
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert reflected not in logged
    assert "[REDACTED]" in logged
