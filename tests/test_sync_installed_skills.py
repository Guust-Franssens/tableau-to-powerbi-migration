"""Regression tests for `scripts/sync_installed_skills.py` (issue #410).

The defect this file guards is not "the installed bundle went stale" - that one was always caught.
It is the opposite sign: the script published whatever WORKING TREE ran it, so with several
worktrees carrying unmerged edits to the same bundle, the installed copy - the one every newly
spawned subagent reads - could hold content on no merged branch, and "in sync" became a race that
thrashed three times in one day.

So every test here fixes one of the two directions:

* a genuinely stale published bundle is still caught (`--check` exits 1);
* an unmerged edit in THIS working tree is NOT a failure, only a NOTE, because the published copy is
  correctly the merged one and `preflight.ps1` blocks on the failure.

The fixtures build real git repositories, because every interesting behaviour here is a git one:
ref resolution order, detached HEAD, a missing `origin`, a shallow clone, and whether the network is
touched at all.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_plugin  # noqa: E402  (needs the sys.path insert above)
import sync_installed_skills as sync  # noqa: E402
from skill_plugin_source import discover_skill_plugin, write_owner_marker  # noqa: E402

PREFLIGHT = REPO_ROOT / "scripts" / "preflight.ps1"
BUNDLES = tuple(build_plugin.SHIPPED_SKILLS)
FIRST_BUNDLE = BUNDLES[0]


def _git(repo: Path, *args: str) -> str:
    """Run git in `repo`, failing the test loudly on a non-zero exit."""
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _rev_ok(repo: Path, ref: str) -> bool:
    """Whether `ref` resolves locally - used to prove a fixture really offers a guessable fallback."""
    done = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode == 0


def _write_bundles(root: Path, marker: str) -> None:
    """Give every shipped bundle a one-line SKILL.md carrying `marker`."""
    for name in BUNDLES:
        skill = root / ".github" / "skills" / name / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text(f"# {name}\n{marker}\n", encoding="utf-8")


def _read_installed(plugin_root: Path, bundle: str = FIRST_BUNDLE) -> str:
    """Installed bundle text with newlines normalised, so CRLF checkouts do not fail assertions."""
    return (plugin_root / "skills" / bundle / "SKILL.md").read_text(encoding="utf-8").replace("\r\n", "\n")


def _add_branch_only_bundle(estate: Estate) -> None:
    """Add a bundle to the WORKING TREE only - both its content and its `SHIPPED_SKILLS` entry."""
    generator = estate.clone / "scripts" / "build_plugin.py"
    generator.write_text(
        generator.read_text(encoding="utf-8").replace(
            "SHIPPED_SKILLS = (", 'SHIPPED_SKILLS = (\n    "branch-only-bundle",'
        ),
        encoding="utf-8",
    )
    bundle = estate.clone / ".github" / "skills" / "branch-only-bundle"
    bundle.mkdir(parents=True)
    (bundle / "SKILL.md").write_text("# branch-only-bundle\nnot merged\n", encoding="utf-8")


@dataclass
class Estate:
    """A bare `origin`, the seed clone that pushes to it, a working clone, and a plugin root."""

    origin: Path
    seed: Path
    clone: Path
    plugin: Path


@pytest.fixture(name="estate")
def _estate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Estate:
    """A bare origin holding `merged`, a clone, and an empty installed plugin root.

    `sync.REPO` is redirected at the clone, which is the only global the module reads at call time -
    deliberately, so a test can stand it up somewhere other than this checkout.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=master", str(origin)], check=True, capture_output=True)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--initial-branch=master")
    _git(seed, "config", "user.email", "t@example.invalid")
    _git(seed, "config", "user.name", "T")
    (seed / "scripts").mkdir()
    (seed / "scripts" / "build_plugin.py").write_bytes((REPO_ROOT / "scripts" / "build_plugin.py").read_bytes())
    _write_bundles(seed, "merged")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "merged")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-u", "origin", "master")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(origin), str(clone)], check=True, capture_output=True)
    _git(clone, "config", "user.email", "t@example.invalid")
    _git(clone, "config", "user.name", "T")

    plugin = tmp_path / "plugin"
    (plugin / "skills").mkdir(parents=True)

    monkeypatch.setattr(sync, "REPO", clone)
    return Estate(origin=origin, seed=seed, clone=clone, plugin=plugin)


def _run(estate: Estate, *argv: str) -> int:
    return sync.main(["--plugin-root", str(estate.plugin), *argv])


# --------------------------------------------------------------------------------------------
# Direction 1: a genuinely stale published bundle must still be caught.
# --------------------------------------------------------------------------------------------


def test_stale_installed_copy_is_still_caught_as_drift(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """The protection that already existed must survive the change of authority."""
    stale = estate.plugin / "skills" / FIRST_BUNDLE
    stale.mkdir(parents=True)
    (stale / "SKILL.md").write_text("# stale\nold\n", encoding="utf-8")

    code = _run(estate, "--check")
    out = capsys.readouterr().out

    assert code == sync.EXIT_DRIFT
    assert out.strip(), "a verdict must be printed - exit 1 alone is also what a crash looks like"
    assert "SYNC: DRIFT" in out
    assert f"differs: {FIRST_BUNDLE}/SKILL.md" in out


def test_missing_installed_bundle_is_drift_then_published(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """An empty install is drift, and a plain run publishes the merged content into it."""
    assert _run(estate, "--check") == sync.EXIT_DRIFT
    capsys.readouterr()

    assert _run(estate) == sync.EXIT_OK
    assert "SYNC: UPDATED" in capsys.readouterr().out
    assert _read_installed(estate.plugin) == f"# {FIRST_BUNDLE}\nmerged\n"
    assert _run(estate, "--check") == sync.EXIT_OK


def test_file_absent_from_the_build_is_removed_from_the_install(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """A bundle file deleted upstream must not linger in the install, shadowing nothing forever."""
    _run(estate)
    capsys.readouterr()
    orphan = estate.plugin / "skills" / FIRST_BUNDLE / "LEFTOVER.md"
    orphan.write_text("gone upstream\n", encoding="utf-8")

    assert _run(estate, "--check") == sync.EXIT_DRIFT
    assert f"stale (not in build): {FIRST_BUNDLE}/LEFTOVER.md" in capsys.readouterr().out

    assert _run(estate) == sync.EXIT_OK
    assert not orphan.exists()


# --------------------------------------------------------------------------------------------
# Direction 2: an unmerged edit in THIS working tree is not a failure.
# --------------------------------------------------------------------------------------------


def test_unmerged_local_edit_does_not_fail_the_check(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """The whole point of #410: `preflight.ps1` blocks on this, so a branch edit must not fail it."""
    _run(estate)
    capsys.readouterr()

    _git(estate.clone, "checkout", "-b", "feat/skill-edit")
    _write_bundles(estate.clone, "unmerged branch content")
    _git(estate.clone, "commit", "-am", "edit a shipped skill")

    code = _run(estate, "--check")
    out = capsys.readouterr().out

    assert code == sync.EXIT_OK, "an unmerged skill edit must not fail the gate that blocks migrations"
    assert "SYNC: IN_SYNC" in out
    assert "SYNC: NOTE" in out, "it must still be REPORTED - silence would hide that edits are not live"
    assert f".github/skills/{FIRST_BUNDLE}/SKILL.md" in out
    assert "--from-worktree" in out


def test_uncommitted_local_edit_is_also_reported_but_not_fatal(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """Uncommitted is the commoner case; `git diff <ref>` must see it too."""
    _run(estate)
    capsys.readouterr()
    _write_bundles(estate.clone, "dirty working tree")

    assert _run(estate, "--check") == sync.EXIT_OK
    assert "SYNC: NOTE" in capsys.readouterr().out


def test_publishing_takes_the_merged_content_not_the_worktree(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """The publish itself must be branch-independent, or the race simply moves."""
    _write_bundles(estate.clone, "unmerged branch content")

    assert _run(estate) == sync.EXIT_OK
    capsys.readouterr()
    assert _read_installed(estate.plugin) == f"# {FIRST_BUNDLE}\nmerged\n"
    assert "unmerged" not in _read_installed(estate.plugin)


def test_the_generator_also_comes_from_the_ref(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """WHAT ships is `build_plugin.py`'s decision, so it must be read from the ref as well.

    A branch that adds a bundle to `SHIPPED_SKILLS` must not publish it before it merges; taking
    content from the ref but the file list from the worktree would be a third, hybrid answer to
    "what ships", which is the ambiguity this issue is about.
    """
    _add_branch_only_bundle(estate)

    assert _run(estate) == sync.EXIT_OK
    capsys.readouterr()
    assert not (estate.plugin / "skills" / "branch-only-bundle").exists()


# --------------------------------------------------------------------------------------------
# The deliberate override.
# --------------------------------------------------------------------------------------------


def test_from_worktree_publishes_the_worktree_and_says_so_loudly(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """Serving unreviewed guidance on purpose is allowed - silently is not."""
    _write_bundles(estate.clone, "unmerged branch content")

    assert _run(estate, "--from-worktree") == sync.EXIT_OK
    out = capsys.readouterr().out

    assert "unmerged branch content" in _read_installed(estate.plugin)
    assert "--from-worktree" in out
    assert "UNMERGED" in out
    assert "python scripts/sync_installed_skills.py" in out, "it must name the command that restores the merged copy"


def test_plain_check_after_a_from_worktree_publish_reports_drift(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """The override is temporary by construction: the default check pulls it back to the merged copy."""
    _write_bundles(estate.clone, "unmerged branch content")
    _run(estate, "--from-worktree")
    capsys.readouterr()

    assert _run(estate, "--check") == sync.EXIT_DRIFT
    assert "--from-worktree" in capsys.readouterr().out, "the hint must name how it got into this state"


# --------------------------------------------------------------------------------------------
# Ref resolution: order, detached HEAD, no origin, shallow clone.
# --------------------------------------------------------------------------------------------


def test_ref_resolution_asks_the_remote_for_its_advertised_default(estate: Estate) -> None:
    """The remote's own answer is the only one that cannot be stale, so it is the one asked for."""
    source = sync.resolve_publish_ref(repo=estate.clone)
    assert source.ref == "refs/remotes/origin/master"
    assert source.commit == _git(estate.clone, "rev-parse", "origin/master")
    assert source.default_verified is True
    assert source.default_proof == "remote"


def test_a_missing_local_origin_head_is_no_obstacle(estate: Estate) -> None:
    """Plenty of clones never get a symbolic `origin/HEAD`; the remote still knows its default."""
    (estate.clone / ".git" / "refs" / "remotes" / "origin" / "HEAD").unlink()
    source = sync.resolve_publish_ref(repo=estate.clone)
    assert source.ref == "refs/remotes/origin/master"
    assert source.default_verified is True


def test_explicit_ref_overrides_the_candidates(estate: Estate) -> None:
    """`--ref` must win outright, including over a resolvable `origin/HEAD`."""
    _git(estate.clone, "checkout", "-b", "release")
    _write_bundles(estate.clone, "release content")
    _git(estate.clone, "commit", "-am", "release")
    _git(estate.clone, "push", "origin", "release")

    source = sync.resolve_publish_ref("refs/remotes/origin/release", repo=estate.clone)
    assert source.ref == "refs/remotes/origin/release"


def test_detached_head_resolves_and_checks_normally(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """HEAD is never read, so the verdict is identical from a detached checkout."""
    _run(estate)
    capsys.readouterr()
    _git(estate.clone, "checkout", "--detach")
    head = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"], cwd=estate.clone, capture_output=True, text=True, check=False
    )
    assert head.returncode != 0, "the fixture must actually be in a detached HEAD state"

    assert _run(estate, "--check") == sync.EXIT_OK
    assert "SYNC: IN_SYNC" in capsys.readouterr().out


def test_no_origin_refuses_rather_than_falling_back_to_the_worktree(
    estate: Estate, capsys: pytest.CaptureFixture
) -> None:
    """A silent fallback is the bug. Refuse, name the override, and change nothing."""
    _git(estate.clone, "remote", "remove", "origin")
    _write_bundles(estate.clone, "unmerged branch content")

    code = _run(estate)
    out = capsys.readouterr().out

    assert code == sync.EXIT_NO_REF
    assert out.strip(), "exit 5 must come with an explanation, not a bare non-zero"
    assert "--from-worktree" in out
    assert not (estate.plugin / "skills" / FIRST_BUNDLE).exists(), "a refused run must publish nothing"


def test_shallow_clone_is_supported(tmp_path: Path, estate: Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the tree at the ref is needed, never history, so `--depth 1` is fine."""
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "--depth", "1", estate.origin.as_uri(), str(shallow)], check=True, capture_output=True
    )
    assert (shallow / ".git" / "shallow").exists(), "the fixture must actually be shallow"

    monkeypatch.setattr(sync, "REPO", shallow)
    assert sync.main(["--plugin-root", str(estate.plugin)]) == sync.EXIT_OK
    assert _read_installed(estate.plugin) == f"# {FIRST_BUNDLE}\nmerged\n"


# --------------------------------------------------------------------------------------------
# Network policy.
# --------------------------------------------------------------------------------------------


def _spy_subprocess(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []
    real = subprocess.run

    def spy(cmd, *args, **kwargs):
        calls.append([str(part) for part in cmd])
        return real(cmd, *args, **kwargs)

    monkeypatch.setattr(sync.subprocess, "run", spy)
    return calls


def test_default_run_asks_the_remote_but_never_fetches(estate: Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one deliberate network call, and no more.

    `preflight.ps1` runs this on every migration start, so a full `git fetch` there would tax every
    run. But there is no OFFLINE way to notice that the remote renamed its default branch, and the
    consequence measured in review was publishing the old branch while reporting `in_sync`. So the
    default run asks `ls-remote --symref` (bounded, one round trip) and still never fetches.
    """
    calls = _spy_subprocess(monkeypatch)
    assert _run(estate, "--check") in (sync.EXIT_OK, sync.EXIT_DRIFT)

    assert any(call[:2] == ["git", "archive"] for call in calls), "the spy saw no git call - it did not apply"
    assert any(call[:3] == ["git", "ls-remote", "--symref"] for call in calls), (
        f"the advertised default must be ASKED for, not guessed: {calls}"
    )
    assert not any(call[:2] == ["git", "fetch"] for call in calls), f"default run must not fetch: {calls}"


def test_fetch_flag_actually_fetches(estate: Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    """The opt-in must do the thing it opts into."""
    calls = _spy_subprocess(monkeypatch)
    _run(estate, "--check", "--fetch")
    assert any("fetch" in call for call in calls), f"--fetch must invoke git fetch: {calls}"


def test_fetch_failure_is_advisory_not_fatal(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """Offline must never block the gate; the local ref still names a real merged commit."""
    _run(estate)
    capsys.readouterr()
    _git(estate.clone, "remote", "set-url", "origin", str(estate.origin.parent / "does-not-exist.git"))

    code = _run(estate, "--check", "--fetch")
    out = capsys.readouterr().out

    assert code == sync.EXIT_OK
    assert "fetch origin - FAILED" in out
    assert "SYNC: IN_SYNC" in out


def test_fetch_does_not_break_the_json_verdict(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """One stray human line ahead of the JSON reads to preflight as "did not report" - a false green."""
    _run(estate)
    capsys.readouterr()

    code = _run(estate, "--check", "--json", "--fetch")
    verdict = json.loads(capsys.readouterr().out)

    assert code == sync.EXIT_OK
    assert verdict["fetch"] == {"ok": True, "detail": "ok"}


# --------------------------------------------------------------------------------------------
# The machine-readable verdict preflight consumes.
# --------------------------------------------------------------------------------------------


def test_json_verdict_carries_what_preflight_needs(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """`--json` must be the ONLY thing on stdout, and must carry the drift list and the ref."""
    stale = estate.plugin / "skills" / FIRST_BUNDLE
    stale.mkdir(parents=True)
    (stale / "SKILL.md").write_text("# stale\nold\n", encoding="utf-8")

    code = _run(estate, "--check", "--json")
    verdict = json.loads(capsys.readouterr().out)

    assert code == sync.EXIT_DRIFT
    assert verdict["exit_code"] == sync.EXIT_DRIFT
    assert verdict["status"] == "drift"
    assert verdict["source"] == "ref"
    assert verdict["ref"] == "refs/remotes/origin/master"
    assert verdict["commit"] == _git(estate.clone, "rev-parse", "origin/master")
    assert verdict["default_verified"] is True
    assert f"{FIRST_BUNDLE}/SKILL.md" in verdict["changed"]
    assert verdict["local_unmerged"] == []


def test_json_verdict_reports_local_unmerged_without_failing(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """preflight has to be able to say "your edits are not live" without failing the run."""
    _run(estate)
    capsys.readouterr()
    _write_bundles(estate.clone, "dirty working tree")

    code = _run(estate, "--check", "--json")
    verdict = json.loads(capsys.readouterr().out)

    assert code == sync.EXIT_OK
    assert verdict["status"] == "in_sync"
    assert verdict["local_unmerged"] == [f".github/skills/{name}/SKILL.md" for name in sorted(BUNDLES)]


def test_json_is_emitted_even_when_the_ref_cannot_be_resolved(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """preflight parses stdout; an unparseable failure path would read as "did not report"."""
    _git(estate.clone, "remote", "remove", "origin")
    code = _run(estate, "--check", "--json")
    verdict = json.loads(capsys.readouterr().out)

    assert code == sync.EXIT_NO_REF
    assert verdict["status"] == "no_ref"


# --------------------------------------------------------------------------------------------
# preflight.ps1 is the gate that actually blocks, so its wiring is part of this fix.
# --------------------------------------------------------------------------------------------


def _preflight_source() -> str:
    return PREFLIGHT.read_text(encoding="utf-8")


def test_preflight_asks_the_script_rather_than_hashing_the_working_tree() -> None:
    """preflight carried the SAME defect independently, comparing `$repoRoot\\.github\\skills` itself.

    Two notions of "which version is authoritative" is the bug, not a redundancy, so the gate must
    consume this script's verdict instead of computing a second one - including the bundle INVENTORY
    and the destination, which review found were still branch-derived via `skill_plugin_source.py`.
    """
    source = _preflight_source()
    assert "sync_installed_skills.py') --check --json" in source
    assert ".github\\skills\\$name\\SKILL.md" not in source, "preflight must not re-derive authority from the worktree"
    assert "shipped_skills" not in source, "the inventory must come from the merged ref, not the worktree"
    assert "skill_plugin_source.py')" not in source, "one authority: the sync verdict already discovers the plugin"


def test_preflight_keeps_the_stale_bundle_check_critical() -> None:
    """A stale install silently runs different bytes than the repo shows; that must still block."""
    source = _preflight_source()
    found = re.findall(r"Add-Check\s+'skill bundles match published plugin'\s+'(\w+)'", source)
    assert found, "preflight must still emit the stale-bundle check"
    assert all(tier == "critical" for tier in found), found


def test_preflight_surfaces_unmerged_local_edits_without_failing() -> None:
    """The NOTE has to reach the operator, but as information - failing it is what blocked migrations."""
    source = _preflight_source()
    assert "local_unmerged" in source, "preflight must surface the unmerged-edit note"
    assert "Add-Check 'skill bundles: local edits vs merged' 'optional'" in source


def test_preflight_separates_an_unresolvable_ref_from_being_in_sync() -> None:
    """No merged ref means UNVERIFIED, not stale; reporting "in sync with <blank>" while failing is worse."""
    source = _preflight_source()
    assert "$Sync.status -eq 'no_ref'" in source
    assert "cannot resolve the merged ref" in source


# --------------------------------------------------------------------------------------------
# ... and EXECUTED, because a source string cannot show which way a comparison points. Review
# mutated the inline `($syncCode -eq 0)` to `-ne` and it survived the whole file: nothing proved
# that stale blocks and in-sync passes, which is the entire purpose of the gate.
# --------------------------------------------------------------------------------------------

_PS = shutil.which("pwsh") or shutil.which("powershell")

# Dot-source ONLY the verdict function, by AST, so running these never executes preflight itself
# (which probes npm, Power BI Desktop and the plugin cache).
_EXTRACT = """
$ErrorActionPreference = 'Stop'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:PREFLIGHT_PS1, [ref]$null, [ref]$null)
$fn = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true) |
      Where-Object { $_.Name -eq 'Get-SkillBundleVerdict' } | Select-Object -First 1
if (-not $fn) { Write-Output 'FUNCTION-NOT-FOUND'; exit 3 }
. ([scriptblock]::Create($fn.Extent.Text))
$sync = if ($env:SYNC_JSON) { $env:SYNC_JSON | ConvertFrom-Json } else { $null }
$v = Get-SkillBundleVerdict $sync
"""


def _skill_verdict(sync_json: str | None, expression: str) -> str:
    """Evaluate `expression` against a real sync verdict inside preflight's own function."""
    if not _PS:
        pytest.skip("no PowerShell on PATH")
    script = _EXTRACT + f"Write-Output ('<<' + ({expression}) + '>>')\n"
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    done = subprocess.run(
        [_PS, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PREFLIGHT_PS1": str(PREFLIGHT), "SYNC_JSON": sync_json or ""},
    )
    assert "FUNCTION-NOT-FOUND" not in done.stdout, "Get-SkillBundleVerdict must exist to be tested"
    match = re.search(r"<<(.*?)>>", done.stdout, re.DOTALL)
    assert match, f"no value emitted; stdout={done.stdout!r} stderr={done.stderr!r}"
    return match.group(1).strip()


def _sync_json(estate: Estate, capsys: pytest.CaptureFixture) -> str:
    _run(estate, "--check", "--json")
    return capsys.readouterr().out


def test_preflight_passes_an_in_sync_install(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """The executed half of the gate: a correct install must not be reported as a critical miss."""
    _run(estate)
    capsys.readouterr()
    verdict = _sync_json(estate, capsys)

    assert json.loads(verdict)["status"] == "in_sync"
    assert _skill_verdict(verdict, "$v.Mode") == "compared"
    assert _skill_verdict(verdict, "$v.Merged.Ok") == "True"
    assert _skill_verdict(verdict, "$v.Installed.Ok") == "True"


def test_preflight_fails_a_stale_install(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """Change one published byte and the critical row must go red - the whole point of the gate."""
    _run(estate)
    capsys.readouterr()
    stale = estate.plugin / "skills" / FIRST_BUNDLE / "SKILL.md"
    stale.write_text("# tampered\n", encoding="utf-8")
    verdict = _sync_json(estate, capsys)

    assert json.loads(verdict)["status"] == "drift"
    assert _skill_verdict(verdict, "$v.Merged.Ok") == "False"
    assert FIRST_BUNDLE in _skill_verdict(verdict, "$v.Merged.Detail")


def test_preflight_fails_a_partially_installed_plugin(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """A bundle absent from the install is a separate critical row, driven by the MERGED inventory."""
    _run(estate)
    capsys.readouterr()
    shutil.rmtree(estate.plugin / "skills" / FIRST_BUNDLE)
    verdict = _sync_json(estate, capsys)

    assert _skill_verdict(verdict, "$v.Installed.Ok") == "False"
    assert FIRST_BUNDLE in _skill_verdict(verdict, "$v.Installed.Detail")


def test_preflight_keeps_local_edits_non_blocking_when_executed(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """Executed proof of the decision the issue turns on: divergence is reported, never a blocker."""
    _run(estate)
    capsys.readouterr()
    _write_bundles(estate.clone, "dirty working tree")
    verdict = _sync_json(estate, capsys)

    assert _skill_verdict(verdict, "$v.Merged.Ok") == "True", "an unmerged edit must not fail the critical row"
    assert _skill_verdict(verdict, "$v.LocalEdits.Ok") == "False", "...but it must be reported"


def test_preflight_treats_an_unreported_verdict_as_unverified(estate: Estate) -> None:
    """No JSON at all is not "fine" - the shadowing bundles are simply unchecked."""
    assert _skill_verdict(None, "$v.Mode") == "unreported"
    assert _skill_verdict(None, "$v.Merged.Ok") == "False"


def test_preflight_treats_an_unresolvable_ref_as_unverified(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """Executed counterpart to the source contract above."""
    _git(estate.clone, "remote", "remove", "origin")
    verdict = _sync_json(estate, capsys)

    assert json.loads(verdict)["status"] == "no_ref"
    assert _skill_verdict(verdict, "$v.Mode") == "noref"
    assert _skill_verdict(verdict, "$v.Merged.Ok") == "False"


def test_preflight_keeps_a_missing_plugin_non_blocking(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """With no installed copy nothing shadows .github/skills, so this stays a warning, not a blocker."""
    empty = estate.plugin.parent / "no-such-plugin"
    sync.main(["--plugin-root", str(empty), "--check", "--json"])
    verdict = capsys.readouterr().out

    assert json.loads(verdict)["status"] == "no_plugin"
    assert _skill_verdict(verdict, "$v.Mode") == "missing"


def test_preflight_emits_every_skill_plugin_row_as_recommended() -> None:
    """EVERY emission, not one of them: a sibling branch's identical string hid a downgrade.

    Measured here - mutating the `missing` branch's tier to `critical` SURVIVED an assertion that
    merely looked for the `'recommended'` string, because the `compared` branch emits the same row
    with the same tier. That is the trap `test_preflight_contract.py::_assert_add_check_tier`
    documents, reproduced in a test written the same afternoon.
    """
    source = _preflight_source()
    tiers = re.findall(r'Add-Check\s+"plugin: \$\(\$skills\.Identity\)"\s+\'(\w+)\'', source)
    assert len(tiers) >= 2, f"expected the plugin row from both the missing and compared paths: {tiers}"
    assert all(tier == "recommended" for tier in tiers), tiers
    assert "elseif ($skills.Mode -eq 'missing') {" in source


def test_preflight_wires_the_function_verdict_into_every_skill_row() -> None:
    """A perfect function is worthless if ANY row is computed from something else."""
    source = _preflight_source()
    assert "$skills = Get-SkillBundleVerdict $sync" in source
    for row, field in (
        ("skill bundles installed", "$skills.Installed.Ok"),
        ("skill bundles match published plugin", "$skills.Merged.Ok"),
        ("skill bundles: local edits vs merged", "$skills.LocalEdits.Ok"),
    ):
        emitted = re.findall(rf"Add-Check '{re.escape(row)}'\s+'\w+'\s+(\S+)", source)
        assert emitted, f"{row} must be emitted"
        assert all(arg == field for arg in emitted), f"{row} must take EVERY verdict from {field}, saw {emitted}"


# --------------------------------------------------------------------------------------------
# Review finding 3 - the resolved COMMIT must be what is used, not the ref name that can move.
# --------------------------------------------------------------------------------------------


def test_export_and_diff_are_given_the_commit_not_the_ref_name(estate: Estate, monkeypatch) -> None:
    """A ref moves. Resolving by name then exporting by name reads two trees and reports the first."""
    seen: dict[str, str] = {}
    real_export = sync.export_ref_tree

    def export_spy(ref, dest, **kwargs):
        seen["export"] = ref
        return real_export(ref, dest, **kwargs)

    monkeypatch.setattr(sync, "export_ref_tree", export_spy)
    _run(estate, "--check")

    assert seen["export"] == _git(estate.clone, "rev-parse", "origin/master"), "export must use the commit"


def test_a_ref_that_advances_mid_run_does_not_change_what_is_published(
    estate: Estate, capsys: pytest.CaptureFixture, monkeypatch
) -> None:
    """`origin/master` advanced during this PR's own review; the publish must still be the pinned tree."""
    real = sync.resolve_publish_ref

    def resolve_then_move(*args, **kwargs):
        source = real(*args, **kwargs)
        _write_bundles(estate.seed, "moved after resolution")
        _git(estate.seed, "commit", "-am", "advance master")
        _git(estate.seed, "push", "origin", "master")
        _git(estate.clone, "fetch", "origin")
        return source

    monkeypatch.setattr(sync, "resolve_publish_ref", resolve_then_move)

    assert _run(estate) == sync.EXIT_OK
    capsys.readouterr()
    installed = _read_installed(estate.plugin)
    assert "moved after resolution" not in installed
    assert installed == f"# {FIRST_BUNDLE}\nmerged\n"


# --------------------------------------------------------------------------------------------
# Review finding 1 - `refs/remotes/origin/HEAD` is a LOCAL marker `git fetch` never refreshes.
# --------------------------------------------------------------------------------------------


def _rename_remote_default(estate: Estate, marker: str) -> None:
    """Move the bare remote's default branch to `main`, with different skill content on it."""
    _git(estate.seed, "checkout", "-b", "main")
    _write_bundles(estate.seed, marker)
    _git(estate.seed, "commit", "-am", "content for the new default branch")
    _git(estate.seed, "push", "origin", "main")
    # `--git-dir` rather than `-C`: `safe.bareRepository=explicit` refuses to treat a bare repo as
    # the working repository otherwise, and the fixture would silently not reproduce the rename.
    subprocess.run(
        ["git", "--git-dir", str(estate.origin), "symbolic-ref", "HEAD", "refs/heads/main"],
        capture_output=True,
        text=True,
        check=True,
    )
    _git(estate.clone, "fetch", "origin")


def test_a_renamed_remote_default_is_followed_without_being_asked(
    estate: Estate, capsys: pytest.CaptureFixture
) -> None:
    """`git fetch` alone leaves `origin/HEAD` stale, so every run ASKS the remote - not just --fetch.

    Reproduced in review by exit code: after the rename, a plain sync exited 0 having installed
    MASTER, and `--check` reported `in_sync` with `origin/main` listed only as an "alternative".
    """
    _rename_remote_default(estate, "new default branch content")
    assert _git(estate.clone, "symbolic-ref", "refs/remotes/origin/HEAD") == "refs/remotes/origin/master", (
        "the fixture must reproduce the stale local marker, or this test proves nothing"
    )

    assert _run(estate) == sync.EXIT_OK
    capsys.readouterr()
    assert "new default branch content" in _read_installed(estate.plugin)

    _run(estate, "--check", "--json")
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["ref"] == "refs/remotes/origin/main"
    assert verdict["default_verified"] is True


def test_a_single_branch_clone_fetches_the_ADVERTISED_ref(
    tmp_path: Path, estate: Estate, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single-branch clone's refspec never brings down the advertised default.

    Reproduced in review: `--fetch` printed "ok", exit 0, published `origin/master`, and emitted no
    alternative warning at all, because `refs/remotes/origin/main` simply did not exist locally.
    """
    _rename_remote_default(estate, "new default branch content")
    narrow = tmp_path / "single-branch"
    subprocess.run(
        ["git", "clone", "--single-branch", "--branch", "master", str(estate.origin), str(narrow)],
        check=True,
        capture_output=True,
    )
    assert not (narrow / ".git" / "refs" / "remotes" / "origin" / "main").exists()
    monkeypatch.setattr(sync, "REPO", narrow)

    assert sync.main(["--plugin-root", str(estate.plugin), "--check", "--json"]) == sync.EXIT_DRIFT
    verdict = json.loads(capsys.readouterr().out)

    assert verdict["ref"] == "refs/remotes/origin/main"
    assert verdict["default_verified"] is True


def test_an_unreachable_remote_uses_the_default_a_previous_run_verified(
    estate: Estate, capsys: pytest.CaptureFixture
) -> None:
    """Offline is not a licence to guess - it is a licence to reuse what was once confirmed."""
    _rename_remote_default(estate, "new default branch content")
    _run(estate)
    capsys.readouterr()
    _git(estate.clone, "remote", "set-url", "origin", str(estate.origin.parent / "gone.git"))

    code = _run(estate, "--check", "--json")
    verdict = json.loads(capsys.readouterr().out)

    assert code == sync.EXIT_OK
    assert verdict["ref"] == "refs/remotes/origin/main", "the RECORDED default, not the local marker"
    assert verdict["default_proof"] == "recorded"
    assert verdict["default_verified_at"]


def test_an_unreachable_remote_with_nothing_recorded_refuses(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """With no evidence at all the verdict must be UNVERIFIED, and must publish nothing."""
    _git(estate.clone, "remote", "set-url", "origin", str(estate.origin.parent / "gone.git"))

    code = _run(estate, "--json")
    verdict = json.loads(capsys.readouterr().out)

    assert code == sync.EXIT_UNVERIFIED_DEFAULT
    assert verdict["status"] == "unverified_default"
    assert verdict["default_verified"] is False
    assert not (estate.plugin / "skills" / FIRST_BUNDLE).exists(), "a refused run must publish nothing"


def test_the_default_is_never_guessed_from_local_branch_names(estate: Estate) -> None:
    """`origin/master` -> `origin/main` was an ordered GUESS; a guess is what published the wrong one."""
    _git(estate.clone, "remote", "set-url", "origin", str(estate.origin.parent / "gone.git"))
    assert _rev_ok(estate.clone, "refs/remotes/origin/master"), "the guessable ref must exist locally"

    with pytest.raises(sync.UnverifiedDefaultError):
        sync.resolve_publish_ref(repo=estate.clone)


def test_a_normal_clone_raises_no_stale_default_noise(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """Only DIFFERING refs count, or the note would fire on every ordinary repository."""
    _run(estate, "--check", "--json")
    assert json.loads(capsys.readouterr().out)["default_alternatives"] == []


def test_an_explicit_ref_counts_as_verified(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """`--ref` is the operator naming it outright; nothing about it can be stale."""
    _rename_remote_default(estate, "new default branch content")
    _run(estate, "--check", "--json", "--ref", "refs/remotes/origin/main")
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["default_verified"] is True
    assert verdict["default_proof"] == "explicit"


# --------------------------------------------------------------------------------------------
# Review finding 2 - the worktree must not choose the DESTINATION or the FILE LIST either.
# --------------------------------------------------------------------------------------------


def test_discovery_is_handed_the_merged_inventory_not_the_branch_one(
    estate: Estate, capsys: pytest.CaptureFixture, monkeypatch
) -> None:
    """Reproduced in review: a branch-added bundle steered discovery at an unrelated plugin."""
    _add_branch_only_bundle(estate)
    seen: dict[str, object] = {}
    real = sync.discover_skill_plugin

    def spy(**kwargs):
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(sync, "discover_skill_plugin", spy)
    _run(estate, "--check")
    capsys.readouterr()

    assert seen.get("bundles") == sorted(BUNDLES)
    assert "branch-only-bundle" not in (seen.get("bundles") or [])
    assert f"{OURS[1]}@{OURS[0]}" in (seen.get("identities") or ()), (
        "the ownership evidence must be handed over too, or discovery re-derives it from this branch"
    )


def test_a_branch_added_bundle_cannot_hijack_and_wipe_an_unrelated_plugin(
    estate: Estate, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """End-to-end repro of round-1 finding 2, re-measured after round 2 tightened ownership.

    Two installed plugins under one scan root: ours (half-installed, so content discovery cannot
    even see it), and a stranger's carrying only a bundle this branch has invented. Round 2
    reproduced the hijack through `--from-worktree`; a PLAIN sync must land in OUR plugin and leave
    the stranger untouched, byte for byte.
    """
    root = tmp_path / "installed-plugins"
    ours = _install(root, *OURS, FIRST_BUNDLE, "# stale\nold\n").parent
    stranger = _install(root, "someone-else", "their-plugin", "branch-only-bundle", "a different project's bundle\n")
    (stranger / "private.txt").write_text("their data\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in sorted(stranger.parent.rglob("*")) if path.is_file()}

    _add_branch_only_bundle(estate)
    code = sync.main(["--installed-plugins-root", str(root)])
    capsys.readouterr()

    assert code == sync.EXIT_OK
    assert _read_installed(ours.parent) == f"# {FIRST_BUNDLE}\nmerged\n", "the merged bundles land in OUR plugin"
    after = {path: path.read_bytes() for path in sorted(stranger.parent.rglob("*")) if path.is_file()}
    assert after == before, "nothing in the stranger's plugin may be written, overwritten or deleted"


def test_a_branch_added_bundle_is_reported_as_an_unmerged_edit(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """It must not be invisible either - review measured `local_unmerged=[]` while preflight went red."""
    _run(estate)
    capsys.readouterr()
    _add_branch_only_bundle(estate)
    _git(estate.clone, "add", "-A", ".github/skills", "scripts")
    _git(estate.clone, "commit", "-m", "add a bundle on the branch")

    code = _run(estate, "--check", "--json")
    verdict = json.loads(capsys.readouterr().out)

    assert code == sync.EXIT_OK, "an unmerged bundle is divergence, not drift in the published copy"
    assert any("branch-only-bundle" in path for path in verdict["local_unmerged"]), verdict["local_unmerged"]


def test_a_bundle_the_merged_tree_does_not_own_is_never_deleted(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """The data-loss half: `extra` used to sweep the WHOLE destination, including someone else's bundle."""
    _run(estate)
    capsys.readouterr()
    foreign = estate.plugin / "skills" / "someone-elses-bundle" / "SKILL.md"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("not ours to delete\n", encoding="utf-8")

    assert _run(estate, "--check") == sync.EXIT_OK
    assert _run(estate) == sync.EXIT_OK
    capsys.readouterr()
    assert foreign.exists(), "a sync must never delete a bundle the merged tree does not own"
    assert foreign.read_text(encoding="utf-8") == "not ours to delete\n"


def test_missing_bundles_are_reported_per_bundle(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """preflight's critical inventory row now reads this instead of the worktree's SHIPPED_SKILLS."""
    _run(estate)
    capsys.readouterr()
    shutil.rmtree(estate.plugin / "skills" / FIRST_BUNDLE)

    _run(estate, "--check", "--json")
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["missing"] == [FIRST_BUNDLE]
    assert verdict["bundles"] == sorted(BUNDLES)


def test_no_plugin_verdict_still_carries_the_merged_context(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """Discovery now runs AFTER the export, so its failure payload must still name the ref it used."""
    empty = estate.plugin.parent / "no-such-plugin"
    code = sync.main(["--plugin-root", str(empty), "--check", "--json"])
    verdict = json.loads(capsys.readouterr().out)

    assert code == sync.EXIT_NO_PLUGIN
    assert verdict["status"] == "no_plugin"
    assert verdict["commit"] == _git(estate.clone, "rev-parse", "origin/master")
    assert verdict["bundles"] == sorted(BUNDLES)


# --------------------------------------------------------------------------------------------
# Round-2 finding 1 - the DESTINATION must be PROVED, never inferred from content.
#
# Reproduced before the fix, in throw-away plugin roots (`_build/repro410/`), by exit code:
#   plain sync, a foreign plugin sharing ONE current bundle name
#       origin/master  -> exit 0, its SKILL.md overwritten, its private.txt DELETED
#       this branch    -> exit 0, its SKILL.md overwritten
#   --from-worktree, a bundle invented on the branch, our own plugin half-installed
#       this branch    -> exit 0, the STRANGER selected, overwritten, its private.txt DELETED
# --------------------------------------------------------------------------------------------

OURS = ("powerbi-playbook-collection", "powerbi-playbook")  # build_plugin's declared identity


def _install(root: Path, marketplace: str, plugin: str, bundle: str, text: str) -> Path:
    """Create `<root>/<marketplace>/<plugin>/skills/<bundle>/SKILL.md` and return the bundle dir."""
    bundle_dir = root / marketplace / plugin / "skills" / bundle
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "SKILL.md").write_text(text, encoding="utf-8")
    return bundle_dir


def test_a_shared_bundle_name_is_not_ownership(tmp_path: Path) -> None:
    """The core of the finding: content is what an attacker or a feature branch controls."""
    root = tmp_path / "installed-plugins"
    _install(root, "someone-else", "their-plugin", FIRST_BUNDLE, "their own guidance\n")

    verdict = discover_skill_plugin(installed_plugins_root=root, bundles=[FIRST_BUNDLE])

    assert verdict.status == "unproven", verdict
    assert verdict.plugin_root is None, "an unproven verdict must not name a destination at all"
    assert not verdict.ok


def test_a_declared_identity_is_ownership(tmp_path: Path) -> None:
    """The CLI's own `<marketplace>/<plugin>` layout is provenance; bundle names are not."""
    root = tmp_path / "installed-plugins"
    _install(root, *OURS, FIRST_BUNDLE, "ours\n")
    _install(root, "someone-else", "their-plugin", FIRST_BUNDLE, "theirs\n")

    verdict = discover_skill_plugin(installed_plugins_root=root, bundles=[FIRST_BUNDLE])

    assert verdict.ok and verdict.proof == "identity"
    assert verdict.plugin_root == root / OURS[0] / OURS[1]


def test_the_cli_registry_can_prove_ownership_after_a_rename(tmp_path: Path) -> None:
    """The plugin WAS renamed once, so identity must survive a directory this repo cannot predict."""
    root = tmp_path / "installed-plugins"
    _install(root, "renamed-collection", "renamed-plugin", FIRST_BUNDLE, "ours\n")
    registry = tmp_path / "config.json"
    registry.write_text(
        json.dumps(
            {
                "installedPlugins": [
                    {
                        "name": "renamed-plugin",
                        "marketplace": "renamed-collection",
                        "cache_path": str(root / "renamed-collection" / "renamed-plugin"),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    unknown = discover_skill_plugin(installed_plugins_root=root, bundles=[FIRST_BUNDLE], registry=registry)
    assert unknown.status == "unproven", "an identity nobody declared is still not ownership"

    known = discover_skill_plugin(
        installed_plugins_root=root,
        bundles=[FIRST_BUNDLE],
        identities=["renamed-plugin@renamed-collection"],
        registry=registry,
    )
    assert known.ok and known.proof == "identity"


def test_the_owner_marker_proves_ownership_on_its_own(tmp_path: Path) -> None:
    """A publish stamps the plugin, so the next run needs neither a name match nor an operator."""
    root = tmp_path / "installed-plugins"
    _install(root, "unpredictable", "directory-name", FIRST_BUNDLE, "ours\n")
    plugin_root = root / "unpredictable" / "directory-name"
    write_owner_marker(plugin_root, publish_repo="https://example.invalid/repo", bundles=[FIRST_BUNDLE])

    wrong = discover_skill_plugin(
        installed_plugins_root=root, bundles=[FIRST_BUNDLE], publish_repo="https://example.invalid/other"
    )
    assert wrong.status == "unproven", "a marker naming a DIFFERENT repo is not ownership"

    right = discover_skill_plugin(
        installed_plugins_root=root, bundles=[FIRST_BUNDLE], publish_repo="https://example.invalid/repo"
    )
    assert right.ok and right.proof == "marker"


def test_a_foreign_plugin_is_never_written_to_overwritten_or_emptied(
    estate: Estate, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The destructive-scope proof: ours is published, the stranger is byte-for-byte unchanged."""
    root = tmp_path / "installed-plugins"
    ours = _install(root, *OURS, FIRST_BUNDLE, "# stale\nold\n").parent
    theirs = _install(root, "someone-else", "their-plugin", FIRST_BUNDLE, "their own guidance\n")
    (theirs / "private.txt").write_text("their data\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in sorted(theirs.parent.rglob("*")) if path.is_file()}

    code = sync.main(["--installed-plugins-root", str(root)])
    capsys.readouterr()

    assert code == sync.EXIT_OK
    assert _read_installed(ours.parent) == f"# {FIRST_BUNDLE}\nmerged\n", "the merged bundles land in OUR plugin"
    after = {path: path.read_bytes() for path in sorted(theirs.parent.rglob("*")) if path.is_file()}
    assert after == before, "no file in an unowned plugin may be added, overwritten or deleted"


def test_an_unowned_lookalike_stops_the_run_instead_of_being_written_to(
    estate: Estate, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """With no provable destination it must refuse - exit non-zero, having written nothing."""
    root = tmp_path / "installed-plugins"
    theirs = _install(root, "someone-else", "their-plugin", FIRST_BUNDLE, "their own guidance\n")
    (theirs / "private.txt").write_text("their data\n", encoding="utf-8")

    code = sync.main(["--installed-plugins-root", str(root)])
    out = capsys.readouterr().out

    assert code == sync.EXIT_UNPROVEN_PLUGIN
    assert "UNPROVED" in out.upper() or "PROVED" in out.upper(), out
    assert (theirs / "SKILL.md").read_text(encoding="utf-8") == "their own guidance\n"
    assert (theirs / "private.txt").exists()
    assert sorted(p.name for p in theirs.parent.iterdir()) == [FIRST_BUNDLE]


def test_from_worktree_refuses_without_an_explicit_destination(
    estate: Estate, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Unmerged content must never also choose where it lands - the whole of review finding 1."""
    root = tmp_path / "installed-plugins"
    stranger = _install(root, "someone-else", "their-plugin", "branch-only-bundle", "their bundle\n")
    (stranger / "private.txt").write_text("their data\n", encoding="utf-8")
    _add_branch_only_bundle(estate)

    code = sync.main(["--installed-plugins-root", str(root), "--from-worktree"])
    out = capsys.readouterr().out

    assert code == sync.EXIT_UNPROVEN_PLUGIN
    assert "--plugin-root" in out
    assert (stranger / "SKILL.md").read_text(encoding="utf-8") == "their bundle\n"
    assert (stranger / "private.txt").exists(), "a refused run must delete nothing"
    assert sorted(p.name for p in stranger.parent.iterdir()) == ["branch-only-bundle"]


def test_from_worktree_with_an_explicit_root_publishes_and_is_reversible(
    estate: Estate, capsys: pytest.CaptureFixture
) -> None:
    """The capability survives; a plain sync then removes what the branch put there."""
    _run(estate)
    capsys.readouterr()
    _add_branch_only_bundle(estate)

    assert _run(estate, "--from-worktree") == sync.EXIT_OK
    capsys.readouterr()
    branch_bundle = estate.plugin / "skills" / "branch-only-bundle"
    assert branch_bundle.is_dir(), "the explicit destination must still be publishable"

    assert _run(estate) == sync.EXIT_OK
    capsys.readouterr()
    assert not branch_bundle.exists(), "restoring the merged copy must remove what the branch added"
    assert _run(estate, "--check") == sync.EXIT_OK


def test_the_pinned_identity_is_read_from_the_ref_not_the_worktree(estate: Estate, tmp_path: Path) -> None:
    """A branch that renames the plugin cannot redirect a plain sync at another install."""
    (estate.clone / "scripts" / "build_plugin.py").write_text(
        (estate.clone / "scripts" / "build_plugin.py")
        .read_text(encoding="utf-8")
        .replace('PLUGIN_NAME = "powerbi-playbook"', 'PLUGIN_NAME = "their-plugin"')
        .replace('MARKETPLACE_NAME = "powerbi-playbook-collection"', 'MARKETPLACE_NAME = "someone-else"'),
        encoding="utf-8",
    )
    merged_identities = sync.pinned_identity(
        sync.export_ref_tree(_git(estate.clone, "rev-parse", "origin/master"), tmp_path / "export", repo=estate.clone)
    ).identities
    assert f"{OURS[1]}@{OURS[0]}" in merged_identities
    assert "their-plugin@someone-else" not in merged_identities
    assert "their-plugin@someone-else" in sync.pinned_identity(estate.clone).identities, (
        "the fixture must actually have renamed the plugin on the branch, or this proves nothing"
    )


# --------------------------------------------------------------------------------------------
# Round-2 finding 3 - a bundle REMOVED from SHIPPED_SKILLS stayed installed forever.
#
# Reproduced before the fix, by exit code: publish 3 bundles, advance the merged ref with one
# removed from SHIPPED_SKILLS, then `--check --json` -> exit 0, status in_sync, extra [], and the
# retired bundle's SKILL.md still on disk. The real machine-wide install carries exactly this:
# `pbip-model-refresh`, held back at v0.3.0, still served to every subagent.
# --------------------------------------------------------------------------------------------


def _retire_a_bundle(estate: Estate, name: str) -> None:
    """Remove `name` from SHIPPED_SKILLS and delete its bundle, on the MERGED ref."""
    generator = estate.seed / "scripts" / "build_plugin.py"
    generator.write_text(generator.read_text(encoding="utf-8").replace(f'    "{name}",\n', ""), encoding="utf-8")
    shutil.rmtree(estate.seed / ".github" / "skills" / name)
    _git(estate.seed, "add", "-A")
    _git(estate.seed, "commit", "-m", f"retire {name}")
    _git(estate.seed, "push", "origin", "master")
    _git(estate.clone, "fetch", "origin")


def test_a_retired_bundle_is_reported_as_drift_and_then_removed(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """The whole of finding 3, by exit code: silent in_sync becomes drift, and the publish cleans up."""
    assert _run(estate) == sync.EXIT_OK
    capsys.readouterr()
    retired = BUNDLES[-1]
    installed = estate.plugin / "skills" / retired / "SKILL.md"
    assert installed.is_file(), "the fixture must actually have installed the bundle it then retires"

    _retire_a_bundle(estate, retired)

    code = _run(estate, "--check", "--json")
    verdict = json.loads(capsys.readouterr().out)
    assert code == sync.EXIT_DRIFT, "a bundle we installed and no longer ship is DRIFT, not in_sync"
    assert verdict["status"] == "drift"
    assert verdict["formerly_owned"] == [retired]
    assert f"{retired}/SKILL.md" in verdict["extra"]
    assert installed.is_file(), "--check must change nothing"

    assert _run(estate) == sync.EXIT_OK
    capsys.readouterr()
    assert not installed.exists(), "the publish must retire it"
    assert not (estate.plugin / "skills" / retired).exists(), "and leave no empty shell behind"
    assert _run(estate, "--check") == sync.EXIT_OK


def test_a_bundle_this_tool_never_installed_is_left_alone_when_it_is_retired(
    estate: Estate, capsys: pytest.CaptureFixture
) -> None:
    """The distinction finding 1 makes load-bearing: unknown foreign bundles are never removed."""
    assert _run(estate) == sync.EXIT_OK
    capsys.readouterr()
    foreign = estate.plugin / "skills" / "someone-elses-bundle" / "SKILL.md"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("not ours to delete\n", encoding="utf-8")

    assert _run(estate, "--check") == sync.EXIT_OK
    assert _run(estate) == sync.EXIT_OK
    capsys.readouterr()
    assert foreign.read_text(encoding="utf-8") == "not ours to delete\n"


def test_the_marker_records_the_inventory_that_was_published(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """Without the record there is no previously-owned inventory, so a retirement is invisible."""
    assert _run(estate) == sync.EXIT_OK
    capsys.readouterr()

    marker = json.loads((estate.plugin / ".skill-sync-owner.json").read_text(encoding="utf-8"))
    assert marker["bundles"] == sorted(BUNDLES)
    assert marker["publish_repo"] == build_plugin.PUBLISH_REPO
    assert marker["commit"] == _git(estate.clone, "rev-parse", "origin/master")


def test_a_check_run_never_writes_the_marker(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """`--check` changes nothing, including the provenance record."""
    _run(estate, "--check")
    capsys.readouterr()
    assert not (estate.plugin / ".skill-sync-owner.json").exists()


# --------------------------------------------------------------------------------------------
# The preflight rows for the new refusals, EXECUTED - a source string cannot show which way a
# comparison points, and review measured exactly that hole in round 1.
# --------------------------------------------------------------------------------------------


def test_preflight_blocks_an_unverified_default_branch(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """Offline with nothing recorded must reach preflight as UNVERIFIED, never as in_sync."""
    _git(estate.clone, "remote", "set-url", "origin", str(estate.origin.parent / "gone.git"))
    _run(estate, "--check", "--json")
    verdict = capsys.readouterr().out

    assert json.loads(verdict)["status"] == "unverified_default"
    assert _skill_verdict(verdict, "$v.Mode") == "unverified"
    assert _skill_verdict(verdict, "$v.Merged.Ok") == "False"


def test_preflight_blocks_an_unproven_destination(
    estate: Estate, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A refusal to write must halt preflight, not pass quietly as "nothing to compare"."""
    root = tmp_path / "installed-plugins"
    _install(root, "someone-else", "their-plugin", FIRST_BUNDLE, "their own guidance\n")
    sync.main(["--installed-plugins-root", str(root), "--check", "--json"])
    verdict = capsys.readouterr().out

    assert json.loads(verdict)["status"] == "unproven_plugin"
    assert _skill_verdict(verdict, "$v.Mode") == "unproven"
    assert _skill_verdict(verdict, "$v.Plugin.Ok") == "False"


def test_preflight_refuses_in_sync_against_an_unverified_default() -> None:
    """`default_verified` is part of the DECISION: in sync with the WRONG branch is not in sync."""
    unverified = json.dumps(
        {
            "status": "in_sync",
            "described": "refs/remotes/origin/master @ deadbeef",
            "identity": "p@m",
            "plugin_root": "C:/x",
            "bundles": list(BUNDLES),
            "changed": [],
            "extra": [],
            "missing": [],
            "local_unmerged": [],
            "default_verified": False,
        }
    )
    assert _skill_verdict(unverified, "$v.Mode") == "compared"
    assert _skill_verdict(unverified, "$v.Merged.Ok") == "False"
    assert (
        _skill_verdict(unverified.replace('"default_verified": false', '"default_verified": true'), "$v.Merged.Ok")
        == "True"
    )
