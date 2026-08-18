"""The `manual` reference provider must not silently claim `validation_grade`.

Reported by a user running the toolkit against a real estate: any PNG dropped in `reference/` was
recorded as `validation_grade` - the tier `pbi-migration-validator` requires before it may sign off
visual fidelity - with nothing verifying resolution, filter-state pinning, or even that the image
came from the handed-over workbook rather than a newer published revision.

It failed OPEN, which is what makes it worth a regression test: the provider with the WEAKEST
provenance claimed the STRONGEST guarantee, by default, with no operator action. It also inverted
the grading against its own siblings - a live Tableau Server REST render is honestly graded
layout+text (captured in the view's default state, no `?vf_` pinning), so a random dropped file
outranked it.
"""

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


def test_a_dropped_screenshot_is_not_validation_grade_by_default():
    caps = ctr._manual_capabilities(_args(manual_validation_grade=False))  # pylint: disable=protected-access
    assert ctr.CAP_VALIDATION not in caps, "an un-provenanced file cannot claim the sign-off tier"
    assert caps == [ctr.CAP_LAYOUT, ctr.CAP_TEXT]


def test_an_absent_flag_attribute_still_defaults_to_not_validation_grade():
    # Fail closed even if a caller builds the namespace without the flag at all.
    caps = ctr._manual_capabilities(_args())  # pylint: disable=protected-access
    assert ctr.CAP_VALIDATION not in caps


def test_validation_grade_is_available_but_only_on_an_explicit_assertion():
    caps = ctr._manual_capabilities(_args(manual_validation_grade=True))  # pylint: disable=protected-access
    assert ctr.CAP_VALIDATION in caps


def test_the_manual_provider_does_not_outrank_a_live_server_render():
    # The oracle's REST images are default-state, so layout+text is their honest ceiling. A dropped
    # file must not be graded above that without an explicit human claim.
    default_manual = ctr._manual_capabilities(_args(manual_validation_grade=False))  # pylint: disable=protected-access
    assert set(default_manual) <= {ctr.CAP_LAYOUT, ctr.CAP_TEXT}


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
