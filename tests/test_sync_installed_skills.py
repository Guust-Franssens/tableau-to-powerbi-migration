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
    repo = _fixture_repo(tmp_path, monkeypatch, with_origin_ref=False)
    plugin_root = _build_installed_from(repo, tmp_path)

    assert sync.main(["--check", "--plugin-root", str(plugin_root)]) == 5

    output = capsys.readouterr().out
    assert "cannot verify publication source 'origin/master'" in output
    assert "Refusing to fall back to the caller's working tree" in output


def test_env_plugin_root_override_is_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
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


def test_plugin_root_with_symlink_component_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _fixture_repo(tmp_path, monkeypatch)
    plugin_root = _build_installed_from(repo, tmp_path)
    link = tmp_path / "plugin-link"
    try:
        link.symlink_to(plugin_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this environment: {exc}")

    assert sync.main(["--check", "--plugin-root", str(link)]) == 6

    output = capsys.readouterr().out
    assert "unsafe plugin path" in output


def test_write_refuses_symlink_destination_that_escapes_installed_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _fixture_repo(tmp_path, monkeypatch)
    plugin_root = _build_installed_from(repo, tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("do not overwrite\n", encoding="utf-8")
    first_skill = build_plugin.SHIPPED_SKILLS[0]
    target = plugin_root / "skills" / first_skill / "SKILL.md"
    target.unlink()
    try:
        target.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this environment: {exc}")

    assert sync.main(["--plugin-root", str(plugin_root)]) == 6

    output = capsys.readouterr().out
    assert "unsafe destination path" in output
    assert outside.read_text(encoding="utf-8") == "do not overwrite\n"


def test_write_refuses_in_tree_symlink_destination_without_overwriting_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _fixture_repo(tmp_path, monkeypatch)
    plugin_root = _build_installed_from(repo, tmp_path)
    first_skill, second_skill = build_plugin.SHIPPED_SKILLS[:2]
    target = plugin_root / "skills" / first_skill / "SKILL.md"
    redirected = plugin_root / "skills" / second_skill / "SKILL.md"
    original_redirected = redirected.read_text(encoding="utf-8")
    target.unlink()
    try:
        target.symlink_to(redirected)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this environment: {exc}")

    assert sync.main(["--plugin-root", str(plugin_root)]) == 6

    output = capsys.readouterr().out
    assert "unsafe destination path" in output
    assert redirected.read_text(encoding="utf-8") == original_redirected


def test_write_refuses_symlink_parent_without_overwriting_redirected_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _fixture_repo(tmp_path, monkeypatch)
    plugin_root = _build_installed_from(repo, tmp_path)
    first_skill, second_skill = build_plugin.SHIPPED_SKILLS[:2]
    first_dir = plugin_root / "skills" / first_skill
    redirected = plugin_root / "skills" / second_skill / "SKILL.md"
    original_redirected = redirected.read_text(encoding="utf-8")
    shutil.rmtree(first_dir)
    try:
        first_dir.symlink_to(plugin_root / "skills" / second_skill, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this environment: {exc}")

    assert sync.main(["--plugin-root", str(plugin_root)]) == 6

    output = capsys.readouterr().out
    assert "unsafe destination path" in output
    assert redirected.read_text(encoding="utf-8") == original_redirected


def test_write_refuses_matching_symlinked_skill_directory_before_stale_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _fixture_repo(tmp_path, monkeypatch)
    plugin_root = _build_installed_from(repo, tmp_path)
    first_skill = build_plugin.SHIPPED_SKILLS[0]
    first_dir = plugin_root / "skills" / first_skill
    redirect_dir = plugin_root / "skills" / "redirect-target"
    shutil.copytree(first_dir, redirect_dir)
    shutil.rmtree(first_dir)
    try:
        first_dir.symlink_to(redirect_dir, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this environment: {exc}")

    assert sync.main(["--plugin-root", str(plugin_root)]) == 6

    output = capsys.readouterr().out
    assert "unsafe destination path" in output
    assert first_dir.is_symlink()
    assert redirect_dir.is_dir()


def test_write_refuses_stale_symlink_leaf_instead_of_unlinking_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _fixture_repo(tmp_path, monkeypatch)
    plugin_root = _build_installed_from(repo, tmp_path)
    target_dir = plugin_root / "skills" / "redirect-target"
    target_dir.mkdir()
    stale_link = plugin_root / "skills" / "stale-skill"
    try:
        stale_link.symlink_to(target_dir, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this environment: {exc}")

    assert sync.main(["--plugin-root", str(plugin_root)]) == 6

    output = capsys.readouterr().out
    assert "unsafe stale path" in output
    assert stale_link.is_symlink()


def test_from_worktree_is_explicit_and_reports_unmerged_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _fixture_repo(tmp_path, monkeypatch)
    plugin_root = _build_installed_from(repo, tmp_path)
    first_skill = build_plugin.SHIPPED_SKILLS[0]
    (repo / ".github" / "skills" / first_skill / "SKILL.md").write_text("unmerged feature\n", encoding="utf-8")

    assert sync.main(["--check", "--from-worktree", "--plugin-root", str(plugin_root)]) == 1

    output = capsys.readouterr().out
    assert "WORKTREE SOURCE" in output
    assert "WORKTREE" in output
    assert "DRIFT" in output
