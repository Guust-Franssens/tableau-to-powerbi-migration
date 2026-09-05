"""Direct tests for installed skill bundle sync source selection."""
# pylint: disable=wrong-import-position

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_plugin  # noqa: E402
import sync_installed_skills as sync  # noqa: E402


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _git_stdout(repo: Path, *args: str, input_text: str | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input_text,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_repo(repo: Path, marker: str) -> None:
    (repo / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(SCRIPTS / "build_plugin.py", repo / "scripts" / "build_plugin.py")
    for skill in build_plugin.SHIPPED_SKILLS:
        skill_dir = repo / ".github" / "skills" / skill
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(f"# {skill}\n\n{marker}\n", encoding="utf-8")


def _fixture_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, with_origin_ref: bool = True) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init")
    _run_git(repo, "config", "user.email", "test@example.invalid")
    _run_git(repo, "config", "user.name", "Test User")
    _write_repo(repo, "master")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "master skills")
    if with_origin_ref:
        _run_git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")

    monkeypatch.setattr(sync, "REPO", repo)
    monkeypatch.setattr(sync, "REFERENCE_BUILD", tmp_path / "reference-build")
    return repo


def _build_installed_from(repo: Path, destination: Path) -> Path:
    out = destination / "marketplace"
    subprocess.run(
        [sys.executable, str(repo / "scripts" / "build_plugin.py"), "--out", str(out)],
        check=True,
        capture_output=True,
        text=True,
    )
    plugin_root = destination / "installed" / "collection" / build_plugin.PLUGIN_NAME
    shutil.copytree(out / "plugins" / build_plugin.PLUGIN_NAME / "skills", plugin_root / "skills")
    return plugin_root


def test_default_check_uses_origin_master_not_dirty_feature_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Default checks compare installed skills with merged origin/master bytes."""
    repo = _fixture_repo(tmp_path, monkeypatch)
    plugin_root = _build_installed_from(repo, tmp_path)

    _run_git(repo, "checkout", "-b", "feature")
    first_skill = build_plugin.SHIPPED_SKILLS[0]
    (repo / ".github" / "skills" / first_skill / "SKILL.md").write_text("unmerged feature\n", encoding="utf-8")

    assert sync.main(["--check", "--plugin-root", str(plugin_root)]) == 0

    output = capsys.readouterr().out
    assert "refs/remotes/origin/master" in output
    assert "IN_SYNC" in output


def test_default_check_uses_origin_master_build_metadata_not_worktree_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Build metadata comes from the selected merged ref, not local edits."""
    repo = _fixture_repo(tmp_path, monkeypatch)
    plugin_root = _build_installed_from(repo, tmp_path)

    build_script = repo / "scripts" / "build_plugin.py"
    build_script.write_text(
        build_script.read_text(encoding="utf-8").replace('PLUGIN_NAME = "powerbi-playbook"', 'PLUGIN_NAME = "evil"'),
        encoding="utf-8",
    )

    assert sync.main(["--check", "--plugin-root", str(plugin_root)]) == 0

    output = capsys.readouterr().out
    assert "refs/remotes/origin/master" in output
    assert "IN_SYNC" in output


def test_missing_origin_ref_refuses_without_worktree_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing merged ref refuses instead of falling back to worktree bytes."""
    repo = _fixture_repo(tmp_path, monkeypatch, with_origin_ref=False)
    plugin_root = _build_installed_from(repo, tmp_path)

    assert sync.main(["--check", "--plugin-root", str(plugin_root)]) == 5

    output = capsys.readouterr().out
    assert "cannot verify publication source 'origin/master'" in output
    assert "Refusing to fall back to the caller's working tree" in output


def test_env_plugin_root_override_is_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The documented plugin-root environment override remains effective."""
    repo = _fixture_repo(tmp_path, monkeypatch)
    plugin_root = _build_installed_from(repo, tmp_path)
    monkeypatch.setenv(sync.PLUGIN_ROOT_ENV, str(plugin_root))

    assert sync.main(["--check"]) == 0

    output = capsys.readouterr().out
    assert "IN_SYNC" in output
    assert str(plugin_root / "skills") in output


def test_source_ref_must_be_proven_merged_into_origin_master(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A source ref outside origin/master ancestry is rejected."""
    repo = _fixture_repo(tmp_path, monkeypatch)
    plugin_root = _build_installed_from(repo, tmp_path)
    _run_git(repo, "checkout", "-b", "feature")
    first_skill = build_plugin.SHIPPED_SKILLS[0]
    (repo / ".github" / "skills" / first_skill / "SKILL.md").write_text("unmerged feature\n", encoding="utf-8")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "unmerged feature")
    _run_git(repo, "update-ref", "refs/remotes/origin/feature", "HEAD")

    assert sync.main(["--check", "--source-ref", "origin/feature", "--plugin-root", str(plugin_root)]) == 5

    output = capsys.readouterr().out
    assert "'origin/feature' is not proven merged into 'origin/master'" in output
    assert "Refusing to fall back to the caller's working tree" in output


def test_each_invocation_uses_a_private_reference_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Concurrent invocations cannot overwrite one shared reference directory."""
    plugin_root = tmp_path / "installed" / "collection" / "plugin"
    skill_dir = plugin_root / "skills" / "sample"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# sample\n", encoding="utf-8")
    seen: list[Path] = []

    def fake_build(workdir: Path, *, source_ref: str, from_worktree: bool) -> sync.ReferenceCopy:
        del source_ref, from_worktree
        seen.append(workdir)
        ref_skill = workdir / "reference" / "plugins" / "plugin" / "skills" / "sample"
        ref_skill.mkdir(parents=True)
        (ref_skill / "SKILL.md").write_text("# sample\n", encoding="utf-8")
        return sync.ReferenceCopy(ref_skill.parent, f"fake {len(seen)}")

    monkeypatch.setattr(sync, "REFERENCE_BUILD", tmp_path / "reference-build")
    monkeypatch.setattr(sync, "build_reference_copy", fake_build)

    assert sync.main(["--check", "--plugin-root", str(plugin_root)]) == 0
    assert sync.main(["--check", "--plugin-root", str(plugin_root)]) == 0

    capsys.readouterr()
    assert len(seen) == 2
    assert seen[0] != seen[1]
    assert all(not path.exists() for path in seen)


def test_source_ref_archive_symlink_entry_is_refused_without_filesystem_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Tracked archive links are rejected without privileged filesystem setup."""
    repo = _fixture_repo(tmp_path, monkeypatch)
    first_skill = build_plugin.SHIPPED_SKILLS[0]
    blob = _git_stdout(repo, "hash-object", "-w", "--stdin", input_text="SKILL.md")
    link_path = f".github/skills/{first_skill}/LINK.md"
    _run_git(repo, "update-index", "--add", "--cacheinfo", f"120000,{blob},{link_path}")
    assert _git_stdout(repo, "ls-files", "--stage", link_path).startswith(f"120000 {blob} ")
    _run_git(repo, "commit", "-m", "track symlink entry")
    _run_git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")

    assert sync.main(["--check", "--plugin-root", str(tmp_path / "missing-plugin")]) == 5

    output = capsys.readouterr().out
    assert "unsupported mode 120000" in output
    assert "Refusing to fall back to the caller's working tree" in output


def test_source_ref_gitlink_entry_is_refused_without_omitting_empty_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _fixture_repo(tmp_path, monkeypatch)
    first_skill = build_plugin.SHIPPED_SKILLS[0]
    _run_git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        "160000",
        "0123456789012345678901234567890123456789",
        f".github/skills/{first_skill}/submodule",
    )
    _run_git(repo, "commit", "-m", "track gitlink entry")
    _run_git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")

    assert sync.main(["--check", "--plugin-root", str(tmp_path / "missing-plugin")]) == 5

    output = capsys.readouterr().out
    assert "unsupported mode 160000" in output
    assert "Refusing to fall back to the caller's working tree" in output


def test_from_worktree_is_explicit_and_reports_unmerged_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The explicit worktree mode reports drift from unmerged local content."""
    repo = _fixture_repo(tmp_path, monkeypatch)
    plugin_root = _build_installed_from(repo, tmp_path)
    first_skill = build_plugin.SHIPPED_SKILLS[0]
    (repo / ".github" / "skills" / first_skill / "SKILL.md").write_text("unmerged feature\n", encoding="utf-8")

    assert sync.main(["--check", "--from-worktree", "--plugin-root", str(plugin_root)]) == 1

    output = capsys.readouterr().out
    assert "WORKTREE SOURCE" in output
    assert "WORKTREE" in output
    assert "DRIFT" in output
