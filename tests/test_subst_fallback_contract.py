r"""Contract tests for the `subst` Desktop-open fallback documented for issue #566."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

AGENTS_MD = REPO_ROOT / "AGENTS.md"
DRY_RUN_OPERATOR_MD = REPO_ROOT / ".github" / "agents" / "dry-run-operator.agent.md"
OPERATOR_RUNBOOK_MD = REPO_ROOT / "docs" / "operator-runbook.md"
WINDOWS_PATH_LIMITS_MD = REPO_ROOT / "docs" / "windows-path-limits.md"
INDEX_MD = REPO_ROOT / "docs" / "INDEX.md"

#: The exact wording that was retired. It must not survive in the two contract documents that used
#: to carry it - an additive edit that leaves the absolute prohibition beside its own exception is
#: how an agent picks the shorter, more familiar rule and the correction never lands.
RETIRED_PROHIBITION = "never substitute a junction, `subst` or symlink"

#: document -> phrases the #566 contract requires (whitespace-normalized before matching).
REQUIRED: dict[str, tuple[Path, tuple[str, ...]]] = {
    "canonical-measurement": (
        WINDOWS_PATH_LIMITS_MD,
        (
            "| **same root through `subst R:`** | **253** |",
            "Measured 2026-09-08 (#566)",
        ),
    ),
    "canonical-boundary": (
        WINDOWS_PATH_LIMITS_MD,
        (
            "`check_path_ceiling.py` measures the **physical** tree",
            "Never use the alias to waive a failed `check_path_ceiling.py`, a `check_unit.py` path gate "
            "or a\n  promotion destination check",
            "Never record the alias as the run's canonical path",
            "Never point `python scripts/work_dirs.py --verify` at the alias spelling",
        ),
    ),
    "canonical-recipe": (
        WINDOWS_PATH_LIMITS_MD,
        (
            "if (-not (Test-Path -LiteralPath 'R:\\')) { throw 'R: did not appear - open nothing' }",
            "subst R: /d\nif ($LASTEXITCODE -ne 0 -or (Test-Path -LiteralPath 'R:\\')) { throw 'R: is still mapped' }",
        ),
    ),
    "runbook-branch": (
        OPERATOR_RUNBOOK_MD,
        (
            "### 4.13 Desktop refuses to open a project and names a file that is too long",
            "[`docs/windows-path-limits.md`](windows-path-limits.md) (§6",
        ),
    ),
    "navigation-row": (
        INDEX_MD,
        ("§6 the **measured `subst` fallback** for opening an over-ceiling PBIP in Desktop",),
    ),
}


def _normalized(path: Path) -> str:
    """Collapse every run of whitespace, so a phrase survives a re-wrapped paragraph."""
    return " ".join(path.read_text(encoding="utf-8").split())


def _missing(texts: dict[Path, str]) -> list[str]:
    return [
        f"{contract}: {phrase!r} missing from {path.name}"
        for contract, (path, phrases) in REQUIRED.items()
        for phrase in phrases
        if " ".join(phrase.split()) not in texts[path]
    ]


def _texts() -> dict[Path, str]:
    return {path: _normalized(path) for path in {doc for doc, _ in REQUIRED.values()}}


def test_the_documents_carry_the_whole_contract_as_committed() -> None:
    """Positive case: permission, boundary, recipe, branch and navigation row all present."""
    assert _missing(_texts()) == []


@pytest.mark.parametrize("doc", [AGENTS_MD, DRY_RUN_OPERATOR_MD])
def test_the_retired_absolute_prohibition_is_gone_from_the_contract_documents(doc: Path) -> None:
    """The correction replaces the old rule; it does not sit beside it."""
    assert RETIRED_PROHIBITION not in _normalized(doc)


def test_the_retired_prohibition_survives_only_as_a_quoted_correction() -> None:
    """The evidence page keeps the old wording ON PURPOSE - labelled as corrected, with its scope. (#566)"""
    text = _normalized(WINDOWS_PATH_LIMITS_MD)
    assert RETIRED_PROHIBITION in text
    assert "This section corrects an earlier unconditional prohibition." in text
    assert "The junction and symlink halves stand." in text


def test_the_root_routes_alias_recovery_to_the_canonical_path_limits() -> None:
    """The short root points to the detailed permission and boundary instead of duplicating them."""
    assert "[Path limits](docs/windows-path-limits.md) owns recovery." in _normalized(AGENTS_MD)


@pytest.mark.parametrize("doc", [DRY_RUN_OPERATOR_MD, OPERATOR_RUNBOOK_MD])
def test_every_document_that_permits_the_alias_also_states_its_boundary(doc: Path) -> None:
    """Fail-open control: no document may grant the permission without the limit beside it."""
    text = _normalized(doc)
    if "`subst`" not in text:
        pytest.fail(f"{doc.name} no longer mentions the fallback at all")
    assert "waives no gate" in text or "may claim path-safety because an alias opened it" in text
    assert (
        "never the recorded path" in text
        or "never the run's recorded path" in text
        or "never the canonical path" in text
    )


def test_the_alias_is_never_offered_as_a_replacement_for_the_short_physical_root() -> None:
    """The default must stay the default: a short PHYSICAL root, allocated before the engine runs."""
    limits = _normalized(WINDOWS_PATH_LIMITS_MD)
    assert "The default has not changed" in limits
    assert "python scripts/work_dirs.py <slug> --runs-parent <short-parent> --json" in limits

    runbook = _normalized(OPERATOR_RUNBOOK_MD)
    assert "The fix is a short physical root" in runbook


def test_the_evidence_page_cites_a_committed_fixture_and_guard_test() -> None:
    """The repro and the alias-resolution guard the page cites must exist, not be remembered paths."""
    fixture = REPO_ROOT / "fixtures" / "upstream-repros" / "issue-194-long-pbir-path"
    assert (fixture / "README.md").is_file()
    guard = (REPO_ROOT / "tests" / "test_harvest_output_guard.py").read_text(encoding="utf-8")
    assert "def test_the_canonical_probe_is_what_lets_a_substituted_drive_through" in guard
    limits = _normalized(WINDOWS_PATH_LIMITS_MD)
    assert "fixtures/upstream-repros/issue-194-long-pbir-path" in limits
    assert "test_the_canonical_probe_is_what_lets_a_substituted_drive_through" in limits


def test_the_physical_path_stays_the_authoritative_artifact_identity() -> None:
    """Invariant 1 of #566, guarded on its own so a deletion NAMES the identity rule."""
    text = _normalized(WINDOWS_PATH_LIMITS_MD)
    assert "The canonical physical path — and every hash taken over it — remains authoritative" in text, (
        "the canonical-identity rule is gone from the evidence page"
    )
    assert "It shortens the *spelling* Desktop sees. It does not shorten, move or repair the artifact." in text, (
        "the alias is no longer described as a spelling rather than a repair"
    )


def test_an_alias_open_never_makes_an_over_ceiling_tree_clean() -> None:
    """Invariant 2 of #566, guarded on its own so a deletion NAMES the shippability rule."""
    text = _normalized(WINDOWS_PATH_LIMITS_MD)
    assert "a physically over-ceiling tree stays non-clean" in text, (
        "the page no longer says an over-ceiling tree stays non-clean when an alias opens it"
    )
    assert "❌ **It does not make anything shippable.**" in text, (
        "the page no longer refuses shippability on the strength of an alias open"
    )


def test_the_ceiling_gate_still_scans_the_resolved_target() -> None:
    """Independent oracle for the load-bearing sentence: the gate's own source, not the prose."""
    source = " ".join((REPO_ROOT / "scripts" / "check_path_ceiling.py").read_text(encoding="utf-8").split())
    assert "scan( target.resolve()," in source or "scan(target.resolve()," in source


@pytest.mark.skipif(os.name != "nt", reason="`subst` is Windows-only")
def test_resolving_a_substituted_drive_returns_the_physical_path() -> None:
    """The measurement the page cites, re-run: `resolve()` sees through `subst`."""
    letter = next((candidate for candidate in "ZYXWVUT" if not Path(f"{candidate}:\\").exists()), None)
    if letter is None:
        pytest.skip("no free drive letter available for subst")
    target = REPO_ROOT
    made = subprocess.run(["subst", f"{letter}:", str(target)], capture_output=True, text=True, check=False, shell=True)
    if made.returncode != 0:
        pytest.skip(f"subst failed: {made.stdout.strip()} {made.stderr.strip()}")
    try:
        aliased = Path(f"{letter}:\\docs\\windows-path-limits.md")
        assert aliased.exists(), "fixture invariant: the alias must expose the tree"
        assert aliased.resolve() == (target / "docs" / "windows-path-limits.md").resolve()
    finally:
        subprocess.run(["subst", f"{letter}:", "/d"], capture_output=True, text=True, check=False, shell=True)
