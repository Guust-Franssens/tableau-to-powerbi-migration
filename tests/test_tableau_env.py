"""Tests for scripts/tableau_env.py.

The motivating defect (issue: Tableau PAT secret has two different env var names across the tiers):
a ``.env`` written from OUR docs (``TABLEAU_PAT_SECRET``) authenticates fine against OUR scripts but
fails against the deterministic engine's own scripts, which read ``TABLEAU_PAT_VALUE`` instead. These
tests pin the tolerant read and the engine-child-env bridge that close that gap.
"""

from __future__ import annotations

import sys
from pathlib import Path

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
