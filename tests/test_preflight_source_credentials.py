"""Regression tests for `scripts/preflight_source_credentials.classify_source`.

**Why this file exists.** `classify_source` used to hold a second, independently-maintained opinion
about which Tableau connection classes need a Power BI credential, and it disagreed with
`connection_target.powerbi_target` - the module whose docstring calls that mapping "the single most
consequential mapping decision in a migration". Both disagreements failed OPEN, which is the only
direction that matters for a gate:

1. `mode == "extract"` short-circuited to `no-creds` *before* the class was examined, so a Snowflake
   or Databricks extract was reported as needing no credential. A packaged `.hyper` is Tableau's
   CACHE of an upstream system; migrating onto it yields a model that can never refresh.
2. Liveness came from a DENY-list of database classes, so any class not on it fell through to
   "review". Measured 2026-08-04: `azure_sqldb` appeared nowhere in the repo, so a workbook joining
   Azure SQL + Snowflake + Databricks printed "No live sources: all extract/flat" and the credential
   gate never armed.

The fix was to delete the duplicate policy, not to extend the list - a deny-list of live systems is
incomplete by construction and every omission is a silent gate failure. These tests exist to keep it
deleted: `test_classify_source_agrees_with_connection_target` fails the moment a second opinion
reappears.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# ruff: noqa: E402  (the sys.path insert above must precede these imports)
# pylint: disable=wrong-import-position,import-error
from connection_target import FLAT_FILE, LIVE_SOURCE, powerbi_target
from preflight_source_credentials import classify_source

# (class, mode, expected verdict). The cases that used to be wrong are marked.
CASES = [
    # --- live systems, live mode -------------------------------------------------------------
    ("snowflake", "live", "needs-credential"),
    ("databricks", "live", "needs-credential"),
    # `azure_sqldb` is the real class Tableau writes for Azure SQL Database. It was absent from the
    # old deny-list, so this returned "review" and the gate stayed disarmed on a 100%-live workbook.
    ("azure_sqldb", "live", "needs-credential"),
    # --- live systems packaged AS AN EXTRACT: the case that looked like a file and isn't ---------
    # All three used to return "no-creds" because `mode == "extract"` was tested first.
    ("snowflake", "extract", "needs-credential"),
    ("databricks", "extract", "needs-credential"),
    ("azure_sqldb", "extract", "needs-credential"),
    # --- genuine flat files stay credential-free, in either mode ---------------------------------
    ("excel-direct", "extract", "no-creds"),
    ("textscan", "live", "no-creds"),
]


@pytest.mark.parametrize(("klass", "mode", "expected"), CASES)
def test_classify_source_verdicts(klass: str, mode: str, expected: str) -> None:
    """The eight cases that pin the corrected behaviour. Six of these used to be wrong."""
    verdict, reason = classify_source({"class": klass, "mode": mode, "server": "host.example"})
    assert verdict == expected, f"{klass}/{mode}: expected {expected}, got {verdict} ({reason})"
    assert reason, "a verdict must always carry a reason"


@pytest.mark.parametrize(("klass", "mode", "_expected"), CASES)
def test_classify_source_agrees_with_connection_target(klass: str, mode: str, _expected: str) -> None:
    """The two modules must never diverge again.

    This is the structural guard: `classify_source` is required to be a thin translation of
    `powerbi_target`, so re-introducing any independent class policy in the preflight fails here
    rather than silently in a customer migration.
    """
    verdict, _ = classify_source({"class": klass, "mode": mode, "server": "host.example"})
    target, _ = powerbi_target(klass, mode)
    mapping = {LIVE_SOURCE: "needs-credential", FLAT_FILE: "no-creds"}
    assert verdict == mapping.get(target, "review"), (
        f"{klass}/{mode}: preflight says {verdict!r} but connection_target says {target!r} - "
        "the preflight must not hold a second opinion"
    )


def test_a_stamped_target_is_preferred_over_recomputing() -> None:
    """The parser stamps `powerbi_target` onto every connection; honour it.

    Recomputing would silently ignore a decision the parser may have made with more context than the
    class string alone.
    """
    verdict, reason = classify_source(
        {
            "class": "some-future-connector",
            "mode": "live",
            "server": "host.example",
            "powerbi_target": LIVE_SOURCE,
            "powerbi_target_reason": "stamped by the parser",
        }
    )
    assert verdict == "needs-credential"
    assert "stamped by the parser" in reason


def test_an_unknown_class_is_never_silently_cleared() -> None:
    """An unrecognised class must never come back as `no-creds`.

    Under-connecting is far worse than over-asking for a credential: the model refreshes once from
    stale cached rows and then never again.
    """
    verdict, _ = classify_source({"class": "brand-new-warehouse-2031", "mode": "live"})
    assert verdict != "no-creds"
