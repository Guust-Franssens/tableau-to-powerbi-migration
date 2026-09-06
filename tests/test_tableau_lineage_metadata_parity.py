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

**Finding**: the two clients are transport-equivalent (same URL, method, headers, body shape, and
identical single-attempt-then-final-answer handling of a 401) - EXCEPT that ``tableau_lineage.py``
never read ``TABLEAU_REST_API_VERSION`` from `.env` at all, hardcoding a REST API version for
sign-in that could silently differ from the one ``assess_estate.py`` (and every other Tableau client
in this repo) reads from the SAME `.env` file. That is fixed here. Whether THAT specific drift
explains the live 401 is not established by this file alone (acceptance #4, a live trial-site
re-run, is external to this sandbox) - see ``docs/`` / the PR description for that residual.
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
GRAPHQL_ERRORS_BODY = json.dumps({"errors": [{"message": "field 'downstreamWorkbooks' requires Data Management"}]}).encode()


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

    def __init__(self, *, metadata_status: int = 200, metadata_body: bytes = STRUCTURE_BODY) -> None:
        self.calls: list[urllib.request.Request] = []
        self.metadata_status = metadata_status
        self.metadata_body = metadata_body

    def __call__(self, request: urllib.request.Request, timeout: float | None = None) -> _Response:
        self.calls.append(request)
        url = request.full_url
        if url.endswith("/auth/signin"):
            return _Response(200, SIGNIN_BODY)
        if "/metadata/graphql" in url:
            if self.metadata_status != 200:
                raise urllib.error.HTTPError(url, self.metadata_status, "Unauthorized", {}, _Response(self.metadata_status, b""))
            return _Response(200, self.metadata_body)
        raise AssertionError(f"unscripted call in the parity experiment: {url}")

    def calls_matching(self, fragment: str) -> list[urllib.request.Request]:
        return [call for call in self.calls if fragment in call.full_url]


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
