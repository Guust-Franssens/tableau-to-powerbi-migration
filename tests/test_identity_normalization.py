"""Tests for the identity-normalization quarantine rule (issue #421, round 4).

`object_identity.py` makes an ambiguous identity unrepresentable *inside* `IdentityIndex`. That is a
real guarantee, but only for joins that go through it - a plain `dict` keyed on `normalize(name)` is
still one line away, and this defect has defeated a convention at FIVE successive layers of PR #428.
So the residual risk is closed by a rule that fails the build rather than by a guideline.

Two halves matter equally here and both are tested:

* the rule CATCHES every way the guarded function can be reached, and
* the rule does NOT fire on unrelated code. A lint rule that cries wolf gets switched off, at which
  point it protects nothing - so the false-positive controls are load-bearing, not decoration.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_identity_normalization as rule  # noqa: E402  # pylint: disable=wrong-import-position

REPO_ROOT = Path(__file__).resolve().parents[1]


def violations(source: str, *, name: str = "some_module.py") -> list[rule.Violation]:
    """Scan a synthetic source file."""
    return rule.scan_source(REPO_ROOT / "scripts" / name, source)


# --------------------------------------------------------------------------------------------
# The invariant itself
# --------------------------------------------------------------------------------------------


def test_the_repository_has_no_lossy_join_outside_the_identity_module() -> None:
    """The actual invariant. This is what fails the build if a sixth layer is ever written."""
    found = rule.scan(REPO_ROOT)
    assert not found, "\n".join(str(item) for item in found)
    assert rule.main(["--quiet"]) == 0


def test_the_owner_module_is_allowed_to_call_its_own_function() -> None:
    """The quarantine is a boundary, not a ban - the lossy comparison has to live somewhere."""
    source = "def normalize(text):\n    return text\n\n\ndef f(x):\n    return normalize(x)\n"
    assert violations(source, name=rule.OWNER_FILE) == []


# --------------------------------------------------------------------------------------------
# Every route to the guarded function is caught, and nothing else is
# --------------------------------------------------------------------------------------------


def test_every_import_route_to_the_guarded_function_is_caught_and_every_call_reported() -> None:
    """Module alias, plain module, bare import and renamed import (the alias is resolved, not the
    spelling); a file that reintroduces the join in three places must show all three."""
    routes = [
        ("import object_identity as oid", "oid.normalize", "oid.normalize()"),
        ("import object_identity", "object_identity.normalize", "object_identity.normalize()"),
        ("from object_identity import normalize", "normalize", "normalize()"),
        ("from object_identity import normalize as squash", "squash", None),
    ]
    for import_line, call, expression in routes:
        found = violations(f"{import_line}\n\n\ndef f(a):\n    return {call}(a)\n")
        assert [item.line for item in found] == [5], import_line
        assert expression is None or found[0].expression == expression
    source = (
        "import object_identity as oid\n\n\n"
        "def f(a, b, c):\n"
        "    x = oid.normalize(a)\n"
        "    y = oid.normalize(b)\n"
        "    return x, y, oid.normalize(c)\n"
    )
    assert [item.line for item in violations(source)] == [5, 6, 7]


def test_unrelated_normalize_calls_are_not_reported() -> None:
    """A rule that cries wolf gets disabled and protects nothing. Another module's `normalize()`
    (`group_oracle_by_workbook.py`), `unicodedata.normalize` (`work_dirs.py`), a local helper, a
    mutation SOURCE string (the harness that proves the invariant), and a file that never imports
    the owner are all clean."""
    clean = [
        "import group_oracle_by_workbook as grp\n\n\ndef f(a):\n    return grp.normalize(a)\n",
        'import unicodedata\n\n\ndef f(a):\n    return unicodedata.normalize("NFKD", a)\n',
        "def normalize(text):\n    return text.strip()\n\n\ndef f(a):\n    return normalize(a)\n",
        'import object_identity as oid\n\nMUTATION = """\noid.normalize(x) == oid.normalize(y)\n"""\n',
        "def f(a):\n    return a.normalize()\n",
    ]
    for source in clean:
        assert violations(source) == [], source


# --------------------------------------------------------------------------------------------
# Unassessable input must not read as clean - the dominant defect class in this repo
# --------------------------------------------------------------------------------------------


def test_an_unparseable_file_is_reported_rather_than_skipped() -> None:
    """A file the rule cannot read is a finding, not a pass.

    Silently skipping it would let a violation hide behind a syntax error - the same
    "unassessable collapses into the clean bucket" shape every round of this review has found.
    """
    found = violations("import object_identity as oid\n\ndef f(:\n")
    assert len(found) == 1
    assert "UNPARSEABLE" in found[0].expression


def test_the_rendered_verdict_names_the_fix_not_just_the_refusal() -> None:
    """A rule that only refuses teaches nothing; this one has to say what to use instead."""
    rendered = rule.render(violations("import object_identity as oid\n\n\ndef f(a):\n    return oid.normalize(a)\n"))
    assert "IdentityIndex" in rendered
    assert "shares_name" in rendered
    assert "some_module.py:5" in rendered
