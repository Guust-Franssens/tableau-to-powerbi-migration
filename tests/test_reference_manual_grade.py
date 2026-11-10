"""The `manual` reference provider must not silently claim `validation_grade`."""

from __future__ import annotations

import argparse
import contextlib
import io
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import capture_tableau_reference as ctr  # noqa: E402  # pylint: disable=wrong-import-position


def _args(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def test_a_dropped_screenshot_is_validation_grade_only_on_an_explicit_assertion():
    # pylint: disable=protected-access
    caps = ctr._manual_capabilities(_args(manual_validation_grade=False))
    # The oracle's REST images are default-state, so layout+text is their honest ceiling. A dropped
    # file must not be graded above that without an explicit human claim.
    assert caps == [ctr.CAP_LAYOUT, ctr.CAP_TEXT], "an un-provenanced file cannot claim the sign-off tier"
    # Fail closed even if a caller builds the namespace without the flag at all.
    assert ctr.CAP_VALIDATION not in ctr._manual_capabilities(_args())
    assert ctr.CAP_VALIDATION in ctr._manual_capabilities(_args(manual_validation_grade=True))


def test_the_flag_is_wired_into_the_cli_and_defaults_off():
    # A clean --help proves the option exists and is spelled the way callers will type it.
    # Matched with a boundary, NOT a substring: a plain `in` check also passes for
    # `--manual-validation-grade-typo`, which is exactly the regression this test must catch
    # (verified - a rename mutation survived the substring version).
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            ctr.main(["--help"])
        except SystemExit as exc:
            assert exc.code == 0
    assert re.search(r"--manual-validation-grade(?![\w-])", buf.getvalue())
