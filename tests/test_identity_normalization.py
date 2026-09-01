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
# Every route to the guarded function is caught
# --------------------------------------------------------------------------------------------


def test_a_module_alias_call_is_caught() -> None:
    """`import object_identity as oid` then `oid.normalize(...)`."""
    found = violations("import object_identity as oid\n\n\ndef f(a, b):\n    return oid.normalize(a) == b\n")
    assert [item.line for item in found] == [5]
    assert found[0].expression == "oid.normalize()"


def test_an_unaliased_module_call_is_caught() -> None:
    """`import object_identity` then `object_identity.normalize(...)`."""
    found = violations("import object_identity\n\n\ndef f(a):\n    return object_identity.normalize(a)\n")
    assert [item.line for item in found] == [5]


def test_a_bare_imported_name_is_caught() -> None:
    """`from object_identity import normalize` then `normalize(...)` - no attribute to match on."""
    found = violations("from object_identity import normalize\n\n\ndef f(a):\n    return normalize(a)\n")
    assert [item.line for item in found] == [5]
    assert found[0].expression == "normalize()"


def test_a_renamed_import_is_caught() -> None:
    """`from object_identity import normalize as squash` - the alias is resolved, not the spelling."""
    found = violations("from object_identity import normalize as squash\n\n\ndef f(a):\n    return squash(a)\n")
    assert [item.line for item in found] == [5]


def test_every_call_is_reported_not_just_the_first() -> None:
    """A file that reintroduces the join in three places must show all three."""
    source = (
        "import object_identity as oid\n\n\n"
        "def f(a, b, c):\n"
        "    x = oid.normalize(a)\n"
        "    y = oid.normalize(b)\n"
        "    return x, y, oid.normalize(c)\n"
    )
    assert [item.line for item in violations(source)] == [5, 6, 7]


# --------------------------------------------------------------------------------------------
# False-positive controls - a rule that cries wolf gets disabled and protects nothing
# --------------------------------------------------------------------------------------------


def test_another_modules_own_normalize_is_not_reported() -> None:
    """`group_oracle_by_workbook.py` has its own `normalize()`, and it is a different function."""
    source = "import group_oracle_by_workbook as grp\n\n\ndef f(a):\n    return grp.normalize(a)\n"
    assert violations(source) == []


def test_the_stdlib_normalize_is_not_reported() -> None:
    """`work_dirs.py` calls `unicodedata.normalize`, which is unrelated to identity."""
    source = 'import unicodedata\n\n\ndef f(a):\n    return unicodedata.normalize("NFKD", a)\n'
    assert violations(source) == []


def test_a_local_function_called_normalize_is_not_reported() -> None:
    """A file may define and call its own helper of the same name without importing the owner."""
    source = "def normalize(text):\n    return text.strip()\n\n\ndef f(a):\n    return normalize(a)\n"
    assert violations(source) == []


def test_an_occurrence_inside_a_string_literal_is_not_reported() -> None:
    """Deliberate: the mutation harness carries `oid.normalize` in mutation SOURCE strings.

    Those exist to reintroduce the defect and prove the suite catches it, so reporting them would
    make the rule fight the very tests that defend the invariant.
    """
    source = 'import object_identity as oid\n\nMUTATION = """\noid.normalize(x) == oid.normalize(y)\n"""\n'
    assert violations(source) == []


def test_a_file_that_never_imports_the_owner_is_not_scanned_for_calls() -> None:
    """No import means no route, whatever the file happens to spell."""
    assert violations("def f(a):\n    return a.normalize()\n") == []


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
