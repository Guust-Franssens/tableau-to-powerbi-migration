"""Tests for scripts/tableau_env.py.

The motivating defect (issue: Tableau PAT secret has two different env var names across the tiers):
a ``.env`` written from OUR docs (``TABLEAU_PAT_SECRET``) authenticates fine against OUR scripts but
fails against the deterministic engine's own scripts, which read ``TABLEAU_PAT_VALUE`` instead. These
tests pin the tolerant read and the engine-child-env bridge that close that gap.
"""

from __future__ import annotations

import ast
import json
import re
import sys
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import tableau_env as te  # noqa: E402  # pylint: disable=wrong-import-position

# --------------------------------------------------------------------------- load_env


def test_load_env_reads_key_value_pairs(tmp_path):
    path = tmp_path / ".env"
    path.write_text("TABLEAU_SERVER_URL=https://x.online.tableau.com\n# a comment\nTABLEAU_SITE=site\n")
    env = te.load_env(path)
    assert env == {"TABLEAU_SERVER_URL": "https://x.online.tableau.com", "TABLEAU_SITE": "site"}


def test_load_env_missing_file_is_not_an_error(tmp_path):
    assert te.load_env(tmp_path / "does-not-exist.env") == {}


def test_load_env_ignores_blank_lines_and_comment_only_lines(tmp_path):
    path = tmp_path / ".env"
    path.write_text("\n# full comment line\nTABLEAU_SITE=site\n\n")
    assert te.load_env(path) == {"TABLEAU_SITE": "site"}


# --------------------------------------------------------------------------- pat_secret


def test_pat_secret_reads_our_documented_name():
    assert te.pat_secret({"TABLEAU_PAT_SECRET": "s3cr3t"}) == "s3cr3t"


def test_pat_secret_reads_the_engines_name_too():
    """A `.env` written from the ENGINE's docs must also work against OUR scripts."""
    assert te.pat_secret({"TABLEAU_PAT_VALUE": "s3cr3t"}) == "s3cr3t"


def test_pat_secret_prefers_our_name_when_both_are_set():
    assert te.pat_secret({"TABLEAU_PAT_SECRET": "ours", "TABLEAU_PAT_VALUE": "engine"}) == "ours"


def test_pat_secret_is_empty_string_not_keyerror_when_absent():
    assert te.pat_secret({}) == ""


# --------------------------------------------------------------------------- engine_child_env


def test_engine_child_env_bridges_our_name_to_the_engines_name():
    """This is the fix: promote harvest_estate_assets.py's local bridge to the shared rule."""
    child = te.engine_child_env({"TABLEAU_PAT_SECRET": "s3cr3t"}, base={})
    assert child["TABLEAU_PAT_VALUE"] == "s3cr3t"


def test_engine_child_env_passes_through_other_keys():
    child = te.engine_child_env({"TABLEAU_SERVER_URL": "https://x", "TABLEAU_PAT_SECRET": "s3cr3t"}, base={})
    assert child["TABLEAU_SERVER_URL"] == "https://x"


def test_engine_child_env_preserves_an_already_set_engine_name_when_our_name_is_absent():
    child = te.engine_child_env({}, base={"TABLEAU_PAT_VALUE": "already-there"})
    assert child["TABLEAU_PAT_VALUE"] == "already-there"


def test_engine_child_env_merges_over_the_base_os_environment():
    child = te.engine_child_env({"TABLEAU_SITE": "override"}, base={"TABLEAU_SITE": "base", "OTHER": "kept"})
    assert child["TABLEAU_SITE"] == "override"
    assert child["OTHER"] == "kept"


# --------------------------------------------------------------------------- server_url


def test_server_url_reads_the_canonical_name():
    """The name `.env.example` documents and every script but one already used."""
    assert te.server_url({"TABLEAU_SERVER_URL": "https://x.online.tableau.com"}) == "https://x.online.tableau.com"


def test_server_url_accepts_the_deprecated_alias_with_a_warning():
    """`tableau_lineage.py` shipped reading TABLEAU_SERVER; an existing .env must keep working."""
    with pytest.warns(DeprecationWarning):
        assert te.server_url({"TABLEAU_SERVER": "https://x.online.tableau.com"}) == "https://x.online.tableau.com"


def test_server_url_prefers_the_canonical_name_and_does_not_warn():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert te.server_url({"TABLEAU_SERVER_URL": "https://canonical", "TABLEAU_SERVER": "https://alias"}) == (
            "https://canonical"
        )


def test_server_url_strips_a_trailing_slash():
    """A trailing slash produces '//api/...' URLs that some proxies reject."""
    assert te.server_url({"TABLEAU_SERVER_URL": "https://x.online.tableau.com/"}) == "https://x.online.tableau.com"


def test_server_url_is_empty_string_not_keyerror_when_absent():
    assert te.server_url({}) == ""


# --------------------------------------------------------------------------- resolve_env


def test_resolve_env_reads_the_process_environment_when_there_is_no_dotenv(tmp_path):
    """The defect in assess_estate.py: exported variables raised KeyError because only the file was read."""
    env = te.resolve_env(tmp_path / "absent.env", environ={"TABLEAU_SERVER_URL": "https://from-shell"})
    assert env["TABLEAU_SERVER_URL"] == "https://from-shell"


def test_resolve_env_reads_the_dotenv_when_nothing_is_exported(tmp_path):
    """The defect in tableau_lineage.py: it read os.environ directly and ignored .env entirely."""
    path = tmp_path / ".env"
    path.write_text("TABLEAU_SERVER_URL=https://from-file\n", encoding="utf-8")
    assert te.resolve_env(path, environ={})["TABLEAU_SERVER_URL"] == "https://from-file"


def test_resolve_env_lets_an_exported_value_win_over_the_file(tmp_path):
    """Precedence: file supplies defaults, the environment overrides. See the rotation test below."""
    path = tmp_path / ".env"
    path.write_text("TABLEAU_SERVER_URL=https://from-file\n", encoding="utf-8")
    env = te.resolve_env(path, environ={"TABLEAU_SERVER_URL": "https://from-shell"})
    assert env["TABLEAU_SERVER_URL"] == "https://from-shell"


def test_resolve_env_normalises_the_deprecated_server_alias(tmp_path):
    """A caller reads the canonical name and never learns there was a second spelling."""
    path = tmp_path / ".env"
    path.write_text("TABLEAU_SERVER=https://aliased\n", encoding="utf-8")
    with pytest.warns(DeprecationWarning):
        env = te.resolve_env(path, environ={})
    assert env["TABLEAU_SERVER_URL"] == "https://aliased"


def test_resolve_env_normalises_the_engines_pat_secret_name(tmp_path):
    """A .env written from the ENGINE's docs must authenticate our scripts too."""
    path = tmp_path / ".env"
    path.write_text("TABLEAU_PAT_VALUE=s3cr3t\n", encoding="utf-8")
    assert te.resolve_env(path, environ={})["TABLEAU_PAT_SECRET"] == "s3cr3t"


# --------------------------------------------------------------------------- require


def test_require_names_every_missing_variable_not_just_the_first():
    """One run should reveal the whole gap; naming one at a time costs a round trip each."""
    with pytest.raises(SystemExit) as exc:
        te.require({"TABLEAU_SERVER_URL": "https://x"})
    message = str(exc.value)
    assert "TABLEAU_PAT_NAME" in message and "TABLEAU_PAT_SECRET" in message
    assert "TABLEAU_SERVER_URL" not in message.split("Missing Tableau credential(s):")[1].split("\n")[0]


def test_require_explains_that_a_pat_cannot_be_created_for_the_user():
    """Tableau answers HTTP 405 to create-PAT; a user must issue it, so the error has to say so."""
    with pytest.raises(SystemExit) as exc:
        te.require({})
    assert "405" in str(exc.value)


def test_require_is_silent_when_everything_is_present():
    te.require({"TABLEAU_SERVER_URL": "https://x", "TABLEAU_PAT_NAME": "n", "TABLEAU_PAT_SECRET": "s"})


# --------------------------------------------------------------------------- redaction


def test_redact_scrubs_reflected_tableau_session_header_without_losing_status():
    """Engine child stderr can echo an authenticated request into parse-sweep.json."""
    text = "HTTP 400 from proxy: X-Tableau-Auth: SENTINEL_SESSION_TOKEN_FULL_PERMISSION path=/api/3.29"
    redacted = te.redact(text, "PAT_SECRET_1234567890")
    assert "SENTINEL_SESSION_TOKEN_FULL_PERMISSION" not in redacted
    assert "HTTP 400" in redacted
    assert "X-Tableau-Auth: [REDACTED]" in redacted


# --------------------------------------------------------------------------- the guard against a fourth divergence


def _environ_aliases(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Names bound to `os.environ` and to `os.getenv` in one module.

    Catches `from os import environ as E`, `from os import getenv as G`, and `E = os.environ`. A
    reviewer defeated the previous version with exactly these, and they are what a developer writes
    by accident rather than to evade a test -- which is the population a guard has to cover.
    """
    environs, getenvs = {"environ"}, {"getenv"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name == "environ":
                    environs.add(alias.asname or alias.name)
                elif alias.name == "getenv":
                    getenvs.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign):
            value = node.value
            bound = (isinstance(value, ast.Attribute) and value.attr == "environ") or (
                isinstance(value, ast.Name) and value.id in environs
            )
            if bound:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        environs.add(target.id)
    return environs, getenvs


def _direct_environ_reads(tree: ast.AST) -> list[str]:
    """Every direct environment read in one module, including through ordinary aliases.

    Structural, not textual: it asks what the code DOES, so an f-string, a concatenated name or a
    `**kwargs` indirection cannot slip past by dodging a literal-name pattern. It reports the call
    site even when the key is not a literal, because an unresolvable key is exactly the case a
    name-allowlist can never prove safe.

    **It is not exhaustive, and must not be read as proof.** `os.environ.copy()`, `dict(os.environ)`,
    `getattr(os, "environ")` and anything reached dynamically will pass. Static analysis cannot close
    that, so the guard's job is to catch the ordinary mistake, not to defeat an adversary.
    """
    environs, getenvs = _environ_aliases(tree)

    def _is_environ(node: ast.AST) -> bool:
        return (isinstance(node, ast.Attribute) and node.attr == "environ") or (
            isinstance(node, ast.Name) and node.id in environs
        )

    def _is_getenv(func: ast.AST) -> bool:
        return (isinstance(func, ast.Attribute) and func.attr == "getenv") or (
            isinstance(func, ast.Name) and func.id in getenvs
        )

    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            is_environ_get = isinstance(func, ast.Attribute) and func.attr == "get" and _is_environ(func.value)
            if _is_getenv(func) or is_environ_get:
                arg = node.args[0] if node.args else None
                key = arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else "<dynamic>"
                found.append(key)
        elif isinstance(node, ast.Subscript) and _is_environ(node.value):
            key = node.slice.value if isinstance(node.slice, ast.Constant) else "<dynamic>"
            found.append(str(key))
    return found


def test_no_script_reads_a_tableau_credential_from_os_environ_directly():
    """Credential reads go through `tableau_env`, and this asserts it STRUCTURALLY.

    An earlier version of this guard matched variable NAMES with a regex. A reviewer defeated it with
    `os.getenv`, an aliased `environ`, an f-string and a concatenated name -- and, worse, it missed a
    real caller (`capture_tableau_reference.py` read `os.environ.get("TABLEAU_SERVER_URL")` directly,
    so a canonical `.env` was invisible to it) precisely BECAUSE that name was on the allowlist. A
    guard that checks names cannot see a script bypassing the resolver with an accepted name.

    So this checks the access pattern instead: no direct environment read of a `TABLEAU_*` key
    anywhere in `scripts/` except inside `tableau_env.py`, which is the one place allowed to do it.
    """
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    offenders: list[str] = []
    for py in sorted(scripts.glob("*.py")):
        if py.name == "tableau_env.py":
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for key in _direct_environ_reads(tree):
            if key.startswith("TABLEAU_"):
                offenders.append(f"{py.name}: {key}")
    assert not offenders, (
        f"{offenders} read a TABLEAU_* variable straight from the environment. Use "
        "tableau_env.resolve_env() so a .env is honoured and the naming aliases are normalised; "
        "reading os.environ directly is how capture_tableau_reference.py silently ignored a .env."
    )


def test_the_structural_guard_catches_what_a_name_regex_missed():
    """Pin the bypasses that defeated the previous name-matching version of this guard."""
    bypasses = [
        'os.getenv("TABLEAU_X")',
        'environ.get(f"TABLEAU_{suffix}")',
        'os.environ.get("TABLEAU_" + "Y")',
        'os.environ["TABLEAU_Z"]',
    ]
    for src in bypasses:
        assert _direct_environ_reads(ast.parse(src)), f"{src!r} slipped past the structural guard"


def test_every_tableau_env_var_used_in_scripts_is_an_accepted_name():
    """Three naming divergences have already reached main; this is the guard against the fourth.

    TABLEAU_PAT_VALUE vs TABLEAU_PAT_SECRET (#88), TABLEAU_SERVER vs TABLEAU_SERVER_URL (#97), and
    the four divergent per-script ``load_env`` copies. Any new TABLEAU_* name in scripts/ must be
    added to ``tableau_env`` deliberately rather than invented in a single script.

    This complements the structural guard above and does not replace it: this one catches a NEW NAME
    read through the shared resolver, that one catches ANY name read around it. Neither is sufficient
    alone, and a dynamically-built name cannot be proven safe by either -- do not read a pass here as
    proof that every access was seen.
    """
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    # Match environment ACCESS, not any uppercase identifier: `make_showcase.py` has RGB constants
    # named TABLEAU_STRIP/TABLEAU_ACCENT and `probe_lab.py` a Snowflake database TABLEAU_MIGRATION,
    # none of which are environment variables. This pattern caught a real one on its first run
    # (TABLEAU_PRODUCT_VERSION, read by stamp_tableau_provenance.py and absent from the canonical set).
    access = re.compile(r"""(?:\.get\(|\[)\s*["'](TABLEAU_[A-Z_]+)["']""")
    used: set[str] = set()
    for py in scripts.glob("*.py"):
        used |= set(access.findall(py.read_text(encoding="utf-8")))
    assert used, "no TABLEAU_* environment reads found - this guard now proves nothing"
    unexpected = sorted(used - te.ACCEPTED_ENV_KEYS)
    assert not unexpected, (
        f"{unexpected} are TABLEAU_* variables used in scripts/ but not accepted by tableau_env.py. "
        "Add the name to CANONICAL_ENV_KEYS/ACCEPTED_ENV_KEYS (and .env.example) rather than "
        "introducing a fourth spelling in one script."
    )


# --------------------------------------------------------------------------- redact


def test_redact_removes_a_secret_from_text_bound_for_a_persisted_artifact():
    """Measured leak: a reflected sign-in error put the PAT in parse-sweep.json on disk."""
    reflected = 'HTTPError 401: {"personalAccessTokenSecret": "s3cr3t-value-long"}'
    assert "s3cr3t-value-long" not in te.redact(reflected, "s3cr3t-value-long")
    assert "[REDACTED]" in te.redact(reflected, "s3cr3t-value-long")


def test_redact_removes_every_occurrence_not_just_the_first():
    text = "tok=abcdefghij again abcdefghij"
    assert te.redact(text, "abcdefghij").count("[REDACTED]") == 2


def test_redact_ignores_empty_and_short_values_that_would_scrub_unrelated_text():
    """A blank or 2-char 'secret' would redact half the message and hide the real error."""
    assert te.redact("no secret here", "") == "no secret here"
    assert te.redact("a error about ab", "ab") == "a error about ab"


def test_redact_tolerates_a_secret_that_is_absent():
    assert te.redact("clean text", "unrelated-secret") == "clean text"


# --------------------------------------------------------------------------- env_source


def test_env_source_says_environment_when_the_variable_is_exported(tmp_path):
    assert te.env_source("TABLEAU_PAT_SECRET", tmp_path / "absent.env", environ={"TABLEAU_PAT_SECRET": "s"}) == (
        "environment"
    )


def test_env_source_says_file_when_only_the_dotenv_has_it(tmp_path):
    path = tmp_path / ".env"
    path.write_text("TABLEAU_PAT_SECRET=s\n", encoding="utf-8")
    assert te.env_source("TABLEAU_PAT_SECRET", path, environ={}) == "file"


def test_env_source_says_unset_when_neither_has_it(tmp_path):
    assert te.env_source("TABLEAU_PAT_SECRET", tmp_path / "absent.env", environ={}) == "unset"


def test_env_source_never_returns_the_value_itself(tmp_path):
    """It is logged at sign-in, so it must name the source and never the secret."""
    assert te.env_source("TABLEAU_PAT_SECRET", tmp_path / "a.env", environ={"TABLEAU_PAT_SECRET": "s3cr3t"}) == (
        "environment"
    )


# --------------------------------------------------------------------------- precedence


def test_an_exported_variable_overrides_a_stale_dotenv(tmp_path):
    """Token rotation: exporting a freshly issued PAT must supersede a revoked one in .env.

    Both orders have a stale-source failure; the tie-break is recoverability. A shell export dies
    with the shell, a .env persists indefinitely -- and this also matches python-dotenv's default.
    """
    path = tmp_path / ".env"
    path.write_text("TABLEAU_PAT_SECRET=revoked-old-token\n", encoding="utf-8")
    env = te.resolve_env(path, environ={"TABLEAU_PAT_SECRET": "freshly-rotated-token"})
    assert env["TABLEAU_PAT_SECRET"] == "freshly-rotated-token"


def test_the_dotenv_still_supplies_values_the_environment_does_not_have(tmp_path):
    """Overriding must not mean ignoring: the file remains the documented mechanism."""
    path = tmp_path / ".env"
    path.write_text("TABLEAU_SITE=from-file\nTABLEAU_PAT_SECRET=from-file\n", encoding="utf-8")
    env = te.resolve_env(path, environ={"TABLEAU_PAT_SECRET": "from-shell"})
    assert env["TABLEAU_SITE"] == "from-file"
    assert env["TABLEAU_PAT_SECRET"] == "from-shell"


# --------------------------------------------------------------------------- the leak, at its boundary


def test_a_reflected_signin_error_cannot_persist_the_pat(monkeypatch, tmp_path):
    """Adversarial: an endpoint that echoes the request body must not put the PAT on disk.

    Found in review of #97 with a local echo server. We place the secret in the engine child's
    environment ourselves (`engine_child_env`), the engine raises with the first 500 characters of
    the response, and this wrapper captures that text into `parse-sweep.json` - so a reflecting
    proxy, WAF or debug endpoint would write a full-permission Tableau token to a durable artifact.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import harvest_estate_assets as h  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    secret = "SENTINEL_PAT_abcdefghijklmnop"
    env = {"TABLEAU_SERVER_URL": "https://x.invalid", "TABLEAU_PAT_NAME": "probe-name", "TABLEAU_PAT_SECRET": secret}

    class _Reflected:
        returncode = 1
        stdout = ""
        stderr = '401: {"credentials": {"personalAccessTokenSecret": "%s", "name": "probe-name"}}' % secret

    monkeypatch.setattr(h.subprocess, "run", lambda *a, **k: _Reflected())
    ok, detail = h.download("datasource", "luid-1", tmp_path / "out.tdsx", env, tmp_path)

    assert ok is False
    assert secret not in detail
    assert secret not in json.dumps([{"download_error": detail}]), "the PAT reached a persisted artifact"
    assert "401" in detail, "redaction must not destroy the diagnostic"


# --------------------------------------------------------------------------- round 2 of review


def test_redaction_happens_before_truncation(monkeypatch, tmp_path):
    """A secret straddling the 300-char cut must not leave its tail in the retained slice.

    Measured in review: truncating first produced full_secret=False but suffix_in_detail=True, and
    that suffix was both logged and persisted. The earlier test passed only because its sentinel
    happened to sit inside the window -- the order is the fix, not the scrub.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import harvest_estate_assets as h  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    secret = "SENTINEL" + ("x" * 40) + "TAILPIECE"
    env = {"TABLEAU_SERVER_URL": "https://x.invalid", "TABLEAU_PAT_NAME": "n", "TABLEAU_PAT_SECRET": secret}

    class _Straddling:
        returncode = 1
        stdout = ""
        # Place the secret so the 300-char tail slice cuts through the middle of it.
        stderr = ("A" * 500) + secret + ("B" * 270)

    monkeypatch.setattr(h.subprocess, "run", lambda *a, **k: _Straddling())
    _, detail = h.download("datasource", "luid", tmp_path / "o.tdsx", env, tmp_path)

    assert secret not in detail
    # Only suffixes long enough to matter: a 1-2 char tail matches inside "[REDACTED]" itself, which
    # would fail the assertion without any secret having survived.
    for cut in range(0, len(secret) - 8):
        assert secret[cut:] not in detail, f"a {len(secret) - cut}-char suffix of the PAT survived"


def test_resolve_env_precedence_holds_across_alias_spellings(tmp_path):
    """A fresh export under the ENGINE's name must still beat a stale `.env` under ours.

    Merging raw dicts and normalising afterwards let a canonical key in the losing source outrank an
    alias in the winning one, which silently defeated the rotation argument for process-wins.
    """
    path = tmp_path / ".env"
    path.write_text("TABLEAU_SERVER_URL=https://stale\nTABLEAU_PAT_SECRET=revoked-old-token\n", encoding="utf-8")
    env = te.resolve_env(path, environ={"TABLEAU_SERVER": "https://fresh", "TABLEAU_PAT_VALUE": "fresh-token"})
    assert env["TABLEAU_PAT_SECRET"] == "fresh-token"
    assert env["TABLEAU_SERVER_URL"] == "https://fresh"


def test_env_source_is_alias_aware(tmp_path):
    """It is logged at sign-in, so naming the wrong source is worse than naming none."""
    path = tmp_path / ".env"
    path.write_text("TABLEAU_PAT_SECRET=from-file\n", encoding="utf-8")
    assert te.env_source("TABLEAU_PAT_SECRET", path, environ={"TABLEAU_PAT_VALUE": "exported"}) == "environment"


def test_a_default_site_setup_does_not_raise_keyerror(tmp_path):
    """An empty site IS the documented Default site, so absence must resolve, not explode.

    `require()` deliberately does not demand TABLEAU_SITE, so two callers indexing it directly turned
    a valid Default-site setup into a raw KeyError *after* the friendly check had passed.
    """
    env = te.resolve_env(
        tmp_path / "absent.env",
        environ={"TABLEAU_SERVER_URL": "https://x", "TABLEAU_PAT_NAME": "n", "TABLEAU_PAT_SECRET": "s"},
    )
    te.require(env)
    assert env["TABLEAU_SITE"] == ""


def test_the_structural_guard_sees_ordinary_environ_aliases():
    """The bypasses that defeated round 2: aliased imports and a plain assignment."""
    aliased = [
        "from os import environ as E\nE.get('TABLEAU_X')",
        "from os import getenv as G\nG('TABLEAU_X')",
        "import os\nE = os.environ\nE['TABLEAU_X']",
        "from os import environ as E\nE['TABLEAU_X']",
    ]
    for src in aliased:
        assert _direct_environ_reads(ast.parse(src)), f"{src!r} slipped past the structural guard"
