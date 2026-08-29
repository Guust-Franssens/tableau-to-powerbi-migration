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

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

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
    """A throwaway git repo carrying this repo's REAL .gitignore, byte for byte.

    Copied whole rather than filtered down to the harvest rules: a reduced fixture answered
    `check-ignore` differently from the real file (see
    `test_the_trailing_slash_trap_is_real_and_the_guard_avoids_it`), so a subset would have proved
    something about a file nobody has.
    """
    return _init_repo(tmp_path)


def _init_repo(path: Path) -> Path:
    """`path` as a git checkout carrying this repo's REAL .gitignore, byte for byte."""
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], path)
    ignore_text = (REPO_ROOT / ".gitignore").read_bytes()
    assert b"/_sweep" in ignore_text and b"/_harvest" in ignore_text, "no harvest rules - fixture proves nothing"
    (path / ".gitignore").write_bytes(ignore_text)
    return path


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
#
# ⚠️ The unignored fixture is called `leaky`, with NO leading underscore, and must stay that way.
# `.gitignore` now carries a root-anchored `/_*`, so EVERY `_*` path at the repo root is ignored by
# construction - which means the old `_leaky` name was silently ignored and this negative case
# asserted nothing. Renaming it back "for consistency with `_sweep`" would re-break it in a way that
# still passes locally and fails only here.
#
# The rename also sharpens what these tests cover. Post-`/_*`, an underscore-prefixed output path is
# safe whether or not the guard fires, so the guard's remaining value is for paths that do NOT start
# with `_` - exactly the `ses-prep/` shape from issue #322, where a deliverable naming real customer
# servers landed unprefixed and unignored.


def test_guard_refuses_an_unignored_path_inside_a_work_tree(repo: Path) -> None:
    assert h.unignored_output_paths(repo / "leaky") == [repo / "leaky" / artifact for artifact in h.OUTPUT_ARTIFACTS]
    assert h.refuse_unignored_output(repo / "leaky", allow_unignored=False) is True


def test_guard_proceeds_for_an_ignored_path(repo: Path) -> None:
    assert h.unignored_output_paths(repo / "_sweep-2026-08-13") == []
    assert h.refuse_unignored_output(repo / "_sweep-2026-08-13", allow_unignored=False) is False


def test_the_trailing_slash_trap_is_real_and_the_guard_avoids_it(repo: Path) -> None:
    """Appending a slash to make a directory rule match turns the guard into a rubber stamp.

    Measured on git 2.55.0.windows.3 against a realistic `.gitignore`: `check-ignore -- 'x/'` exits
    0 for ANY path, reporting an EMPTY matched pattern - so a guard that probes with a trailing
    slash reports every target as safely ignored, including the one this test refuses. The
    workaround for the directory-rule problem is a path COMPONENT under `--out`, never a slash.
    """
    stamp = _git(["check-ignore", "-q", "--", f"{repo / 'definitely-not-ignored'}/"], repo)
    if stamp.returncode != 0:
        pytest.skip("this git no longer reports every trailing-slash path as ignored")
    assert h.refuse_unignored_output(repo / "leaky", allow_unignored=False) is True


def test_guard_proceeds_when_the_directory_already_exists(repo: Path) -> None:
    """An existing `--out` (the `--skip-download` re-run) must be judged the same way."""
    (repo / "_sweep-again" / "assets").mkdir(parents=True)
    (repo / "leaky-again" / "assets").mkdir(parents=True)
    assert h.refuse_unignored_output(repo / "_sweep-again", allow_unignored=False) is False
    assert h.refuse_unignored_output(repo / "leaky-again", allow_unignored=False) is True


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
        assert h.refuse_unignored_output(repo / "leaky", allow_unignored=True) is False
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
        [sys.executable, str(REPO_ROOT / "scripts" / "harvest_estate_assets.py"), "--out", "leaky", "--skip-download"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0, f"the CLI accepted an unignored --out:\n{result.stdout}\n{result.stderr}"
    assert "REFUSING" in (result.stdout + result.stderr)
    assert not (repo / "leaky").exists(), "the refusal ran too late - it already created --out"


# --------------------------------------------------------------------------- issue #374
#
# The guard judged ONE form of `--out` (`expanduser().resolve()`) while `main` wrote to the RAW
# argument, and it anchored git at the nearest EXISTING ancestor of that resolved path - which a
# junction can move outside the checkout entirely. Two consequences, both measured before the fix:
#
#   --out ~/sweep       judged as %USERPROFILE%\sweep (outside any work tree -> allowed),
#                       written to <checkout>\~\sweep  -- unignored, stageable
#   --out linkdir\x     (linkdir is a junction to somewhere outside) judged from the junction
#                       TARGET, where `git rev-parse` exits 128 -> "outside any work tree" -> allowed,
#                       while `git add -A` at the checkout staged 'linkdir/x/assets/...' (Windows;
#                       on POSIX git stages the symlink entry instead - see the control below)
#
# Every test below therefore asserts TWICE: first that the question the OLD guard asked returns
# "safe" for this fixture, then that the guard refuses anyway. Without the first assertion a test
# that never exercised the defect would still pass - the failure shape called out in the review of
# PR #362, where harvest's default artifact names happened to be unignored under an unignored --out.


def _link_dir(link: Path, target: Path) -> None:
    """A directory link needing no elevation: an NTFS junction on Windows, a symlink elsewhere.

    A junction, not a symlink, on Windows deliberately: `New-Item -ItemType SymbolicLink` needs
    elevation (or Developer Mode) while `mklink /J` never does, so the Windows half of this coverage
    would otherwise silently skip on most machines - and Windows is where the defect was found.
    """
    if sys.platform == "win32":
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True, check=False
        )
        if made.returncode != 0:
            pytest.skip(f"could not create an NTFS junction: {made.stdout.strip()} {made.stderr.strip()}")
    else:
        os.symlink(target, link, target_is_directory=True)


def _unlink_dir(link: Path) -> None:
    """Remove the LINK, never what it points at - a leaked link into a checkout is the very harm."""
    if not os.path.lexists(link):
        return
    try:
        os.rmdir(link) if sys.platform == "win32" else link.unlink()
    except OSError:  # pragma: no cover - best effort teardown
        pass


@pytest.fixture(name="lab")
def lab_fixture(tmp_path: Path):
    """A checkout, a directory outside it, and three links between them.

    * `repo`        - a checkout with the REAL `.gitignore` (acceptance criterion 4)
    * `outside`     - a plain directory, in no work tree
    * `junction_out`- `<repo>/linkdir` -> `outside`:      inside the checkout, resolves out of it
    * `junction_in` - `<outside>/backdoor` -> `<repo>/leaky`: outside the checkout, resolves into it
    * `linked_repo` - `<tmp>/linkrepo` -> `<repo>`:       the checkout itself reached through a link
    """
    if _git(["rev-parse", "--is-inside-work-tree"], tmp_path).stdout.strip() == "true":
        pytest.skip("pytest tmp_path is itself inside a git work tree on this machine")
    repo = _init_repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / "leaky").mkdir()
    links = SimpleNamespace(
        junction_out=repo / "linkdir", junction_in=outside / "backdoor", linked_repo=tmp_path / "linkrepo"
    )
    _link_dir(links.junction_out, outside)
    _link_dir(links.junction_in, repo / "leaky")
    _link_dir(links.linked_repo, repo)
    yield SimpleNamespace(repo=repo, outside=outside, **vars(links))
    for link in vars(links).values():
        _unlink_dir(link)


def test_the_link_leak_is_real_on_this_platform(lab) -> None:
    """The control the rest of this section rests on: what does `git add -A` actually do with it?

    The two platforms differ and the difference is the point. An NTFS junction is a directory to
    git's working-tree walk, so it descends and stages the customer `.twbx` itself - that is the
    leak issue #374 is about. A POSIX symlink is staged as a link object; git does not walk into it,
    so the workbooks stay out of the index while the link entry - which names the path - still lands.
    Asserted, not assumed, so a change in either platform's behaviour shows up here rather than
    quietly making the refusals below pointless.
    """
    landed = lab.junction_out / "sweepout" / "assets"
    landed.mkdir(parents=True)
    (landed / "harvested-workbook.twbx").write_text("customer content", encoding="utf-8")
    assert (lab.outside / "sweepout" / "assets" / "harvested-workbook.twbx").exists(), "the link is not a link"

    staged = _git(["add", "-A", "--dry-run"], lab.repo).stdout.replace("\\", "/")
    if sys.platform == "win32":
        assert "linkdir/sweepout/assets/harvested-workbook.twbx" in staged, (
            f"git no longer walks into an NTFS junction; the leak may be gone:\n{staged}"
        )
    else:
        # Measured on git 2.43.0 (Ubuntu): the whole output is `add '.gitignore'` + `add 'linkdir'`.
        assert "linkdir" in staged, f"git staged nothing for the symlink at all:\n{staged}"
        assert "linkdir/sweepout" not in staged, f"git now walks into a POSIX symlink:\n{staged}"


def test_a_junction_leading_out_of_the_checkout_is_refused(lab) -> None:
    """`--out` inside the checkout whose link resolves outside it. Acceptance criterion 2."""
    out = lab.junction_out / "sweepout"

    old_question = h.unignored_output_paths(out.expanduser().resolve())
    assert old_question == [], (
        "fixture proves nothing: the OLD guard's single resolved form already refused this, so this "
        f"test would pass without the fix ({old_question})"
    )
    assert h.refuse_unignored_output(out, allow_unignored=False) is True


def test_a_junction_leading_out_is_refused_even_from_outside_the_checkout(lab, monkeypatch) -> None:
    """Anchoring at `Path.cwd()` fixes the common case; the lexical-ancestor walk fixes all of them.

    An operator can perfectly well run this script from anywhere - `python <repo>/scripts/... --out
    <repo>/linkdir/x` - and a cwd-derived anchor then has nothing to say.
    """
    monkeypatch.chdir(lab.outside)
    out = lab.junction_out / "sweepout"
    assert h.unignored_output_paths(out.expanduser().resolve()) == [], "fixture proves nothing"
    assert h.refuse_unignored_output(out, allow_unignored=False) is True


def test_a_junction_leading_INTO_the_checkout_is_refused(lab) -> None:
    """The other direction: `--out` looks external, the link puts the bytes inside the checkout.

    This one already passed BEFORE the fix, precisely because the guard resolved the path - which is
    why the resolved form is kept alongside the lexical one rather than replaced by it. Dropping it
    would trade one hole for another and this test is what says so.
    """
    out = lab.junction_in / "sweepin"
    assert h.unignored_output_paths(out.absolute()) == [], "fixture proves nothing: the lexical form already refuses"
    assert h.refuse_unignored_output(out, allow_unignored=False) is True


def test_a_checkout_reached_through_a_junction_is_not_falsely_refused(lab) -> None:
    """The false-refusal control. A guard that refuses everything is not a guard, it is an outage.

    `<tmp>/linkrepo` -> `<repo>`, so the lexical form names a path the checkout's own toplevel does
    not contain. Asking that repo about it anyway yields "outside repository" (exit 128), which is
    an unprovable answer and would therefore refuse an `--out` that is plainly ignored.
    """
    out = lab.linked_repo / "_sweep-hosted"
    assert h.refuse_unignored_output(out, allow_unignored=False) is False


def test_the_junction_cli_refuses_and_writes_nothing(lab) -> None:
    """End to end, by exit code: 2, and not one byte through the link."""
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "harvest_estate_assets.py"),
            "--out",
            str(Path("linkdir") / "sweepout"),
            "--skip-download",
        ],
        cwd=lab.repo,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 2, f"the CLI accepted a junctioned --out:\n{result.stdout}\n{result.stderr}"
    assert "REFUSING" in (result.stdout + result.stderr)
    assert not (lab.outside / "sweepout").exists(), "the refusal ran too late - it already wrote through the link"


def test_a_literal_tilde_is_judged_where_it_would_actually_land(repo: Path, monkeypatch) -> None:
    """Acceptance criterion 1, at the guard. `~` reaches argv unexpanded from cmd.exe, from a quoted
    PowerShell argument, and from any programmatic call; `Path("~/x")` is a plain relative path."""
    monkeypatch.chdir(repo)
    home_form = Path("~/harvest-374-sweep").expanduser().resolve()

    old_question = h.unignored_output_paths(home_form)
    if old_question:
        pytest.skip("the home directory is itself an unignored path inside a checkout on this machine")
    assert old_question == [], "the OLD guard judged the home directory, and called it safe"
    assert h.refuse_unignored_output(Path("~/harvest-374-sweep"), allow_unignored=False) is True


def test_the_tilde_directory_is_never_created_inside_the_checkout(repo: Path) -> None:
    """Acceptance criterion 1, end to end: refuse, and leave no literal `~` behind."""
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "harvest_estate_assets.py"),
            "--out",
            "~/harvest-374-sweep",
            "--skip-download",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 2, f"the CLI accepted a literal ~ --out:\n{result.stdout}\n{result.stderr}"
    assert not (repo / "~").exists(), "a literal `~` directory was created inside the checkout"


def test_the_run_writes_to_the_form_the_guard_passed(tmp_path: Path, monkeypatch) -> None:
    """The other half of the asymmetry: checking one form and writing another proves nothing.

    Runs `main` in-process with a fake HOME so no real home directory is touched, from a cwd in no
    work tree so the guard legitimately allows the run. The output must land on the RESOLVED form.
    `sqlite3` then fails on the empty estate DB (exit 1) - by design: the write happens first, and
    stubbing the engine keeps this independent of whether the conversion plugin is installed.
    """
    home = tmp_path / "home"
    home.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    if _git(["rev-parse", "--is-inside-work-tree"], workdir).stdout.strip() == "true":
        pytest.skip("pytest tmp_path is itself inside a git work tree on this machine")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workdir)
    monkeypatch.setattr(h, "engine_scripts_dir", lambda: tmp_path)
    monkeypatch.setattr(h, "resolve_env", lambda _path: {})
    monkeypatch.setattr(sys, "argv", ["harvest", "--out", "~/sweep", "--skip-download", "--db", str(tmp_path / "e.db")])

    assert h.main() == 1  # the empty estate DB, reached only AFTER --out is created
    assert (home / "sweep" / "assets").is_dir(), "the run did not write to the resolved form it checked"
    assert not (workdir / "~").exists(), "the run wrote to the raw argument, not the checked form"


def test_an_out_that_cannot_be_resolved_fails_closed(repo: Path, monkeypatch) -> None:
    """A form we cannot compute is a question we cannot answer, and unanswerable means unsafe."""

    def explode(_path: Path) -> Path:
        raise OSError("WinError 1921: the name of the file cannot be resolved by the system")

    monkeypatch.setattr(h, "_resolved", explode)
    with pytest.raises(h.OutputPathNotIgnoredError):
        h.output_path_forms(repo / "_sweep")
    assert h.refuse_unignored_output(repo / "_sweep", allow_unignored=False) is True


def test_both_path_forms_are_offered_and_neither_is_dropped(repo: Path, monkeypatch) -> None:
    """The contract `refuse_unignored_output` depends on, asserted where it is cheap to read."""
    monkeypatch.chdir(repo)
    forms = h.output_path_forms(Path("~/harvest-374-sweep"))
    assert forms == [repo / "~" / "harvest-374-sweep", Path("~/harvest-374-sweep").expanduser().resolve()]
    assert h.output_path_forms(repo / "_sweep") == [repo / "_sweep"], "identical forms must collapse to one probe"


def test_the_out_of_checkout_remedy_still_works(lab) -> None:
    """Acceptance criterion 3. The remedy the refusal recommends must not itself be refused.

    Asserted at the guard AND by exit code. The CLI cannot reach 0 here - there is no estate DB, and
    on a machine without the conversion plugin it stops earlier still - so the claim is that it is
    not stopped by the GUARD (exit 2), which is the thing under test.
    """
    assert h.refuse_unignored_output(lab.outside / "sweep", allow_unignored=False) is False
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "harvest_estate_assets.py"),
            "--out",
            str(lab.outside / "sweep"),
            "--skip-download",
        ],
        cwd=lab.repo,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 2, f"the guard blocked its own recommended remedy:\n{result.stdout}{result.stderr}"
    assert "REFUSING" not in (result.stdout + result.stderr)
