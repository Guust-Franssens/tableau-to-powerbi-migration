"""`harvest_estate_assets.py --out` must be a path git ignores, and the rules must be prefix globs.

Issue #125. `.gitignore` reserved `/_harvest/` and `/_sweep/` as EXACT directory names while their
siblings (`/_assessment*/`, `/_bundle*/`, `/_estate*/`) were prefix globs. The natural move when
`_harvest/` already holds a previous run - `_harvest-2`, `_sweep-2026-08-13` - therefore staged a
real customer's `.twbx` in a PUBLIC repo. This repo has already had one customer-data incident that
needed a history rewrite, so the class is proven, not hypothetical.

Two independent guards, because either alone fails open:

1. the ignore rules themselves (`test_ignore_rules_*`), read out of the live repo via
   `git check-ignore`, so a future edit that narrows a glob back to an exact name fails here;
2. the script's own refusal (`test_guard_*`), because an operator can always invent a name no rule
   anticipated - and a download that has already happened cannot be un-leaked.

No customer data is used or created anywhere below: every fixture is an empty temp directory.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import harvest_estate_assets as h  # noqa: E402  # pylint: disable=wrong-import-position

# The exact names from issue #125, plus the `_sweep*` variants the fix now makes usable. Each was
# NOT IGNORED before the fix except the two bare names, and `git status --porcelain` listed the rest
# as untracked - i.e. one `git add -A` from a public commit.
CANDIDATE_OUTPUT_DIRS = (
    "_harvest",
    "_sweep",
    "_harvest-op",
    "_sweep2",
    "_harvest-2026-08-13",
    "_sweep-op",
    "_sweep-2026-08-13",
)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60, check=False)


def _is_ignored(path: Path, cwd: Path) -> bool:
    """Ask git the same question the guard asks: would a downloaded file under `path` be staged?

    Probed as a FILE inside the directory, not as `path` itself: a directory-only rule
    (`/_sweep*/`) can only be applied to a path git knows is a directory, which for a not-yet-created
    `--out` it does not. And never with a trailing slash - on git 2.55.0.windows.3 that reports EVERY
    path as ignored (empty matched pattern), so a trailing-slash probe would make this file pass
    vacuously.
    """
    probe = path / "assets" / "harvested-workbook.twbx"
    return _git(["check-ignore", "-q", "--", str(probe)], cwd).returncode == 0


@pytest.fixture(name="repo")
def repo_fixture(tmp_path: Path) -> Path:
    """A throwaway git repo carrying this repo's real harvest ignore rules."""
    _git(["init", "-q"], tmp_path)
    rules = [
        line
        for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.startswith(("/_harvest", "/_sweep", "/_assessment", "/_bundle", "/_estate"))
    ]
    assert rules, "no harvest-family rules found in .gitignore - this fixture would prove nothing"
    (tmp_path / ".gitignore").write_text("\n".join(rules) + "\n", encoding="utf-8")
    return tmp_path


# --------------------------------------------------------------------------- the ignore rules


@pytest.mark.parametrize("name", CANDIDATE_OUTPUT_DIRS)
def test_ignore_rules_cover_dated_and_suffixed_variants(repo: Path, name: str) -> None:
    """Every candidate name an operator might reach for must already be ignored."""
    assert _is_ignored(repo / name, repo), (
        f"{name}/ is NOT ignored - a harvest run into it would stage customer .twbx in a public repo"
    )


def test_ignore_rules_are_globs_in_the_live_repo() -> None:
    """The same claim against the real working tree, not just a reconstructed fixture."""
    if _git(["rev-parse", "--is-inside-work-tree"], REPO_ROOT).stdout.strip() != "true":
        pytest.skip("not a git work tree (exported source?)")
    for name in CANDIDATE_OUTPUT_DIRS:
        assert _is_ignored(REPO_ROOT / name, REPO_ROOT), f"{name}/ is not ignored in this repo"


def test_a_name_outside_the_convention_is_still_reported_unignored(repo: Path) -> None:
    """The control. Without it, an accidental `*` rule would make every assertion above vacuous."""
    assert not _is_ignored(repo / "sweep-without-underscore", repo)


def test_the_two_harvesters_do_not_share_one_documented_folder() -> None:
    """`.gitignore` must say WHICH tool writes where; one folder for two tools is the collision."""
    text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    harvest_block = text.split("/_harvest*/")[0].rsplit("\n\n", 1)[-1]
    sweep_block = text.split("/_sweep*/")[0].rsplit("\n\n", 1)[-1]
    assert "harvest_tableau_public.py" in harvest_block, "_harvest* rule does not name its owning tool"
    assert "harvest_estate_assets.py" in sweep_block, "_sweep* rule does not name its owning tool"
    assert "harvest_estate_assets.py" not in harvest_block, "both harvesters documented into _harvest*"


# --------------------------------------------------------------------------- the script's guard


def test_guard_refuses_an_unignored_path_inside_a_work_tree(repo: Path) -> None:
    assert h.unignored_output_paths(repo / "_leaky") == [repo / "_leaky" / artifact for artifact in h.OUTPUT_ARTIFACTS]
    assert h.refuse_unignored_output(repo / "_leaky", allow_unignored=False) is True


def test_guard_proceeds_for_an_ignored_path(repo: Path) -> None:
    assert h.unignored_output_paths(repo / "_sweep-2026-08-13") == []
    assert h.refuse_unignored_output(repo / "_sweep-2026-08-13", allow_unignored=False) is False


def test_guard_proceeds_when_the_directory_already_exists(repo: Path) -> None:
    """An existing `--out` (the `--skip-download` re-run) must be judged the same way."""
    (repo / "_sweep-again" / "assets").mkdir(parents=True)
    (repo / "_leaky-again" / "assets").mkdir(parents=True)
    assert h.refuse_unignored_output(repo / "_sweep-again", allow_unignored=False) is False
    assert h.refuse_unignored_output(repo / "_leaky-again", allow_unignored=False) is True


def test_guard_proceeds_outside_any_git_work_tree(tmp_path: Path) -> None:
    """Harvesting to a scratch disk outside the repo is a NORMAL run and must not be blocked."""
    if _git(["rev-parse", "--is-inside-work-tree"], tmp_path).stdout.strip() == "true":
        pytest.skip("pytest tmp_path is itself inside a git work tree on this machine")
    assert h.unignored_output_paths(tmp_path / "anything-at-all") == []
    assert h.refuse_unignored_output(tmp_path / "anything-at-all", allow_unignored=False) is False


def test_guard_covers_a_target_whose_parents_do_not_exist_yet(repo: Path) -> None:
    """The common case: `--out` is created by the run, so the guard must judge a missing path."""
    target = repo / "not" / "created" / "yet"
    assert not target.exists()
    assert h.refuse_unignored_output(target, allow_unignored=False) is True


def test_guard_checks_every_artifact_not_just_the_downloads(repo: Path) -> None:
    """A rule covering only `assets/` still leaks the sweep, which lists every workbook name."""
    (repo / ".gitignore").write_text("/_partial/assets/\n", encoding="utf-8")
    unignored = h.unignored_output_paths(repo / "_partial")
    assert [p.name for p in unignored] == ["parse-sweep.json", "parse-sweep.md"]
    assert h.refuse_unignored_output(repo / "_partial", allow_unignored=False) is True


def test_override_flag_downgrades_the_refusal_to_a_warning(repo: Path, caplog) -> None:
    with caplog.at_level("WARNING"):
        assert h.refuse_unignored_output(repo / "_leaky", allow_unignored=True) is False
    assert "--allow-unignored-out" in caplog.text


def test_an_unanswerable_git_is_treated_as_unsafe(repo: Path, monkeypatch) -> None:
    """A git that errors (128, a broken index, a permissions failure) proves nothing - so refuse."""
    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if "check-ignore" in cmd:
            return subprocess.CompletedProcess(cmd, 128, "", "fatal: something went wrong")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(h.subprocess, "run", fake_run)
    with pytest.raises(h.OutputPathNotIgnoredError):
        h.unignored_output_paths(repo / "_sweep")
    assert h.refuse_unignored_output(repo / "_sweep", allow_unignored=False) is True


def test_a_missing_git_binary_does_not_silently_disable_the_guard(repo: Path, monkeypatch) -> None:
    """No git binary is not evidence of no repository - the `.git` directory is still there."""
    monkeypatch.setattr(h.subprocess, "run", _raise_missing_git)
    with pytest.raises(h.OutputPathNotIgnoredError):
        h.unignored_output_paths(repo / "_sweep")
    assert h.refuse_unignored_output(repo / "_sweep", allow_unignored=False) is True


def test_a_missing_git_binary_outside_a_checkout_still_runs(tmp_path: Path, monkeypatch) -> None:
    """...but with no `.git` anywhere above it, there is nothing to leak into."""
    if any((parent / ".git").exists() for parent in (tmp_path, *tmp_path.parents)):
        pytest.skip("pytest tmp_path sits inside a checkout on this machine")
    monkeypatch.setattr(h.subprocess, "run", _raise_missing_git)
    assert h.refuse_unignored_output(tmp_path / "out", allow_unignored=False) is False


def _raise_missing_git(*_args, **_kwargs):
    raise FileNotFoundError("git")


# --------------------------------------------------------------------------- end to end


def test_cli_exits_nonzero_before_touching_anything(repo: Path) -> None:
    """The refusal must beat `--out` creation, the .env, the estate DB and every network call.

    Run with `--skip-download` on purpose: even the no-download path writes `parse-sweep.json`,
    which carries every workbook name, so the guard cannot be conditional on downloading.
    """
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "harvest_estate_assets.py"), "--out", "_leaky", "--skip-download"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0, f"the CLI accepted an unignored --out:\n{result.stdout}\n{result.stderr}"
    assert "REFUSING" in (result.stdout + result.stderr)
    assert not (repo / "_leaky").exists(), "the refusal ran too late - it already created --out"
