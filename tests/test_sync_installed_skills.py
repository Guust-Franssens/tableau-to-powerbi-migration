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

import json
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
from skill_plugin_source import discover_skill_plugin  # noqa: E402

PREFLIGHT = REPO_ROOT / "scripts" / "preflight.ps1"
BUNDLES = tuple(build_plugin.SHIPPED_SKILLS)
FIRST_BUNDLE = BUNDLES[0]


def _git(repo: Path, *args: str) -> str:
    """Run git in `repo`, failing the test loudly on a non-zero exit."""
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return done.stdout.strip()


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


def test_ref_resolution_prefers_origin_head(estate: Estate) -> None:
    """`origin/HEAD` is the remote's own declared default, so a branch rename cannot strand this."""
    source = sync.resolve_publish_ref(repo=estate.clone)
    assert source.ref == "refs/remotes/origin/HEAD"
    assert source.commit == _git(estate.clone, "rev-parse", "origin/master")


def test_ref_resolution_falls_back_when_origin_head_is_absent(estate: Estate) -> None:
    """Plenty of clones never get a symbolic `origin/HEAD`; those must still resolve."""
    (estate.clone / ".git" / "refs" / "remotes" / "origin" / "HEAD").unlink()
    source = sync.resolve_publish_ref(repo=estate.clone)
    assert source.ref == "refs/remotes/origin/master"


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


def test_no_network_round_trip_by_default(estate: Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    """`preflight.ps1` runs this on every migration start; a mandatory fetch would tax every run."""
    calls = _spy_subprocess(monkeypatch)
    assert _run(estate, "--check") in (sync.EXIT_OK, sync.EXIT_DRIFT)

    assert any(call[:2] == ["git", "archive"] for call in calls), "the spy saw no git call - it did not apply"
    assert not any("fetch" in call for call in calls), f"default run must not touch the network: {calls}"


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
    assert verdict["ref"] == "refs/remotes/origin/HEAD"
    assert verdict["commit"] == _git(estate.clone, "rev-parse", "origin/master")
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
    consume this script's verdict instead of computing a second one.
    """
    source = _preflight_source()
    assert "sync_installed_skills.py') --check --json" in source
    assert ".github\\skills\\$name\\SKILL.md" not in source, "preflight must not re-derive authority from the worktree"


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
    assert "$sync.status -eq 'no_ref'" in source
    assert "cannot resolve the merged ref" in source


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


def test_fetch_follows_a_renamed_remote_default(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """`git fetch` alone leaves `origin/HEAD` stale, so `--fetch` must ASK the remote."""
    _rename_remote_default(estate, "new default branch content")
    assert _git(estate.clone, "symbolic-ref", "refs/remotes/origin/HEAD") == "refs/remotes/origin/master", (
        "the fixture must reproduce the stale local marker, or this test proves nothing"
    )

    assert _run(estate, "--fetch") == sync.EXIT_OK
    capsys.readouterr()
    assert "new default branch content" in _read_installed(estate.plugin)


def test_fetch_records_that_the_default_was_verified(estate: Estate, capsys: pytest.CaptureFixture) -> None:
    """The verdict must say whether the default branch was confirmed with the remote or merely assumed."""
    _run(estate, "--check", "--json")
    assert json.loads(capsys.readouterr().out)["default_verified"] is False

    _run(estate, "--check", "--json", "--fetch")
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["default_verified"] is True
    assert verdict["ref"] == "refs/remotes/origin/master"


def test_an_offline_run_reports_that_the_local_default_marker_may_be_stale(
    estate: Estate, capsys: pytest.CaptureFixture
) -> None:
    """Offline it cannot be resolved - but staying silent is what let the rename go unnoticed."""
    _rename_remote_default(estate, "new default branch content")

    code = _run(estate, "--check", "--json")
    verdict = json.loads(capsys.readouterr().out)

    assert code in (sync.EXIT_OK, sync.EXIT_DRIFT)
    assert verdict["default_verified"] is False
    assert "refs/remotes/origin/main" in verdict["default_alternatives"]

    _run(estate, "--check")
    assert "LOCAL marker" in capsys.readouterr().out


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
    assert verdict["default_alternatives"] == []


# --------------------------------------------------------------------------------------------
# Review finding 2 - the worktree must not choose the DESTINATION or the FILE LIST either.
# --------------------------------------------------------------------------------------------


def test_discovery_selects_by_the_inventory_it_is_given(tmp_path: Path) -> None:
    """The inventory IS the selector, so handing it a branch-only name selects an unrelated plugin."""
    root = tmp_path / "installed-plugins"
    ours = root / "mkt-a" / "plug-a" / "skills" / FIRST_BUNDLE
    ours.mkdir(parents=True)
    (ours / "SKILL.md").write_text("ours\n", encoding="utf-8")
    theirs = root / "mkt-b" / "plug-b" / "skills" / "branch-only-bundle"
    theirs.mkdir(parents=True)
    (theirs / "SKILL.md").write_text("someone else's\n", encoding="utf-8")

    merged = discover_skill_plugin(installed_plugins_root=root, bundles=[FIRST_BUNDLE])
    assert merged.ok and merged.plugin_root is not None and merged.plugin_root.name == "plug-a"

    branchy = discover_skill_plugin(installed_plugins_root=root, bundles=["branch-only-bundle"])
    assert branchy.ok and branchy.plugin_root is not None and branchy.plugin_root.name == "plug-b"


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
