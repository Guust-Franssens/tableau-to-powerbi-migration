"""Tests for `scripts/sync_agent_conventions.py`'s bundle-path gate.

Every test here pins a boundary that has already MOVED once, so each one names the failure it exists
to prevent rather than merely exercising a branch:

* Issue #123 - `<bundle>/out/pbip/` was documented in AGENTS.md and all four personas for weeks. Every
  copy agreed, and agreement was the only thing checked. That defect must keep failing, forever.
* The widened scan (persona bodies, not just the generated block) then rejected a CORRECT document:
  `<bundle>/summary.md` is a real bundle-root file, and the directory rule read it as a directory
  named `summary.md`, reporting "there is no `out/` level" about a file that has nothing to do with
  `out/`. `docs/operator-runbook.md` already cites 6 distinct files that way, so the cheapest fix
  available to an author was to DELETE the citation.
* The `--bundle` on-disk resolution likewise must not demand what a legitimate bundle omits:
  `data/` (no flat-file extracts) or a conditionally written root file (`empty-model-check.json`).

The through-line: a fix that stops rejecting files by also stopping rejecting `out/` is the worst
outcome, so the negative case is asserted beside every positive one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import sync_agent_conventions as sac  # noqa: E402  # pylint: disable=wrong-import-position

BLOCK = """<!-- BEGIN:shared-conventions -->
- **Three locations.**
  | stage | location |
  |---|---|
  | engine truth | `<bundle>/reports/` |
  | working copy | `<bundle>/pbip/` |

  A bundle is `<bundle>/{pbip,reports,semantic_models,handover,data}`.
<!-- END:shared-conventions -->
"""

PERSONA = """---
name: probe
description: a probe persona
---

# Probe — Subagent

## Gotchas

{body}
"""


@pytest.fixture(name="repo")
def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A miniature repo: one AGENTS.md carrying the block, one persona whose body we vary."""
    agents_dir = tmp_path / ".github" / "agents"
    agents_dir.mkdir(parents=True)
    source = tmp_path / "AGENTS.md"
    source.write_text(BLOCK, encoding="utf-8")
    monkeypatch.setattr(sac, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(sac, "SOURCE", source)
    monkeypatch.setattr(sac, "AGENTS_DIR", agents_dir)

    def write_persona(body: str) -> Path:
        path = agents_dir / "probe.agent.md"
        path.write_text(PERSONA.format(body=body), encoding="utf-8")
        return path

    def write_block(text: str) -> None:
        source.write_text(text, encoding="utf-8")

    return write_persona, write_block


def _bundle(root: Path, dirs: tuple[str, ...], files: tuple[str, ...] = ()) -> Path:
    for name in dirs:
        (root / name).mkdir(parents=True, exist_ok=True)
    for name in files:
        (root / name).write_text("{}", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# The correct document must pass (the regression this suite was added for).
# ---------------------------------------------------------------------------


def test_bundle_root_file_citation_is_not_a_layout_claim(repo) -> None:
    """`<bundle>/summary.md` is a citation, not a claim that a directory named `summary.md` exists."""
    write_persona, _ = repo
    write_persona("Read `<bundle>/summary.md` first, then `<bundle>/empty-model-check.json`.")
    assert sac.check_bundle_paths(None) == []


@pytest.mark.parametrize(
    "cited",
    ["summary.md", "report.json", "phase-timings.json", "deploy-journal.jsonl", "deploy-estate-id.txt"],
)
def test_every_root_file_style_the_runbook_uses_is_accepted(repo, cited: str) -> None:
    """docs/operator-runbook.md writes all of these; the gate must not make an author delete them."""
    write_persona, _ = repo
    write_persona(f"See `<bundle>/{cited}` for the verdict.")
    assert sac.check_bundle_paths(None) == []


# ---------------------------------------------------------------------------
# ...and the original defect must still fail. Both halves, always together.
# ---------------------------------------------------------------------------


def test_issue_123_out_level_still_fails_in_the_block(repo) -> None:
    """The exact #123 shape: an `out/` level in the generated conventions block."""
    write_persona, write_block = repo
    write_persona("Nothing to see here.")
    write_block(BLOCK.replace("`<bundle>/reports/`", "`<bundle>/out/reports/`"))
    problems = sac.check_bundle_paths(None)
    assert len(problems) == 1
    assert "`out` is not a bundle directory" in problems[0]
    assert "the shared-conventions block" in problems[0]


def test_issue_123_out_level_still_fails_in_a_persona_body(repo) -> None:
    """Same defect outside the fence - invisible before the scan was widened."""
    write_persona, _ = repo
    write_persona("The model lives at `<bundle>/out/pbip/<wb>/<Name>.SemanticModel`.")
    problems = sac.check_bundle_paths(None)
    assert len(problems) == 1
    assert "`out` is not a bundle directory" in problems[0]
    assert "probe.agent.md" in problems[0]


def test_a_file_citation_does_not_launder_an_out_level(repo) -> None:
    """The dotted-name exemption must not become a bypass: `out` is still `out`."""
    write_persona, _ = repo
    write_persona("Read `<bundle>/summary.md`, but the model is in `<bundle>/out/pbip/`.")
    problems = sac.check_bundle_paths(None)
    assert len(problems) == 1
    assert "`out` is not a bundle directory" in problems[0]


def test_an_unknown_directory_still_fails(repo) -> None:
    """A plausible-but-wrong directory (no dot) is exactly what BUNDLE_DIRS is for."""
    write_persona, _ = repo
    write_persona("Artifacts land in `<bundle>/output/`.")
    assert any("`output` is not a bundle directory" in p for p in sac.check_bundle_paths(None))


# ---------------------------------------------------------------------------
# --bundle: resolve real locations, tolerate what a legitimate bundle omits.
# ---------------------------------------------------------------------------


def test_bundle_resolution_passes_a_complete_bundle(repo, tmp_path: Path) -> None:
    write_persona, _ = repo
    write_persona("Read `<bundle>/summary.md`.")
    bundle = _bundle(tmp_path / "full", ("pbip", "reports", "semantic_models", "handover", "data"), ("summary.md",))
    assert sac.check_bundle_paths(bundle) == []


def test_bundle_missing_a_documented_location_fails(repo, tmp_path: Path) -> None:
    """`fakeB`: no `reports/`, which the block documents as a location."""
    write_persona, _ = repo
    write_persona("Nothing to see here.")
    bundle = _bundle(tmp_path / "fakeB", ("pbip", "semantic_models", "handover", "data"))
    problems = sac.check_bundle_paths(bundle)
    assert any("<bundle>/reports/" in p and "does not exist" in p for p in problems)


def test_bundle_with_only_pbip_fails(repo, tmp_path: Path) -> None:
    """`fakeC`: `pbip/` alone - the shape a half-written bundle has."""
    write_persona, _ = repo
    write_persona("Nothing to see here.")
    bundle = _bundle(tmp_path / "fakeC", ("pbip",))
    assert any("<bundle>/reports/" in p and "does not exist" in p for p in sac.check_bundle_paths(bundle))


def test_data_absent_is_tolerated_because_the_brace_form_is_vocabulary(repo, tmp_path: Path) -> None:
    """An estate with no flat-file extracts has no `data/`; the enumeration describes the layout."""
    write_persona, _ = repo
    write_persona("Nothing to see here.")
    bundle = _bundle(tmp_path / "nodata", ("pbip", "reports", "semantic_models", "handover"))
    assert sac.check_bundle_paths(bundle) == []


def test_a_conditionally_written_root_file_warns_but_does_not_fail(repo, tmp_path: Path) -> None:
    """`empty-model-check.json` is absent from a real completed bundle - a warning, not a verdict."""
    write_persona, _ = repo
    write_persona("Read `<bundle>/empty-model-check.json` before sign-off.")
    bundle = _bundle(tmp_path / "cond", ("pbip", "reports", "semantic_models", "handover", "data"))
    assert sac.check_bundle_paths(bundle) == []


# ---------------------------------------------------------------------------
# The repo's own documents, checked as documents rather than as fixtures.
# ---------------------------------------------------------------------------


def test_this_repo_passes_its_own_gate() -> None:
    """No monkeypatching: the shipped AGENTS.md and personas must satisfy the rule they publish."""
    assert sac.check_bundle_paths(None) == []
