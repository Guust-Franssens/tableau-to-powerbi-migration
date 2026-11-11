"""The conversion engine has ONE source: the installed plugin (#107)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import engine_source  # noqa: E402  # pylint: disable=wrong-import-position
import migration_bundle  # noqa: E402  # pylint: disable=wrong-import-position


def _make_engine_tree(root: Path, version: str = "2.126.0") -> Path:
    """A minimal but structurally real `tableau-fabric-skills` tree."""
    skill = root / engine_source.ENGINE_SKILL
    (skill / "scripts").mkdir(parents=True, exist_ok=True)
    (skill / "VERSION").write_text(version + "\n", encoding="utf-8")
    (skill / "scripts" / "migrate_estate.py").write_text("# engine\n", encoding="utf-8")
    return root


def test_jsonc_removes_comment_only_lines_without_touching_strings(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        '// before\n{"url": "https://example.invalid//path",\n// between\n"value": 1}\n',
        encoding="utf-8",
    )

    assert engine_source._read_json(config, leading_comments=True) == {  # pylint: disable=protected-access
        "url": "https://example.invalid//path",
        "value": 1,
    }


@pytest.mark.parametrize("configured", [None, "", "relocated", "missing"])
def test_process_start_copilot_home(tmp_path: Path, configured: str | None) -> None:
    """Resolve after setting the subprocess environment, never the parent import's hook."""
    user = tmp_path / "user"
    suffix = Path("installed-plugins/tableau-collection/tableau-fabric-skills")
    default = _make_engine_tree(user / ".copilot" / suffix)
    relocated = tmp_path / "relocated"
    _make_engine_tree(relocated / suffix)
    selected = tmp_path / configured if configured else user / ".copilot"
    env = os.environ.copy()
    env.update(HOME=str(user), USERPROFILE=str(user), APPDATA=str(user / "appdata"))  # #727: VS Code is a host
    env.pop(engine_source.SIMULATE_ENGINE_ABSENT_ENV, None)
    env.pop(engine_source.PLUGIN_HOST_ENV, None)
    if configured is None:
        env.pop("COPILOT_HOME", None)
    else:
        env["COPILOT_HOME"] = str(selected) if configured else ""
    code = """
import json
import engine_source as engine
try:
    root = str(engine.engine_root())
    error = None
except engine.EngineNotFoundError as exc:
    root = None
    error = str(exc)
print(json.dumps({
    "root": root, "error": error,
    "default": str(engine.DEFAULT_PLUGIN_ENGINE_ROOT),
    "hook": str(engine.PLUGIN_ENGINE_ROOT),
    "alternatives": [str(path) for path in engine.ALTERNATIVE_ENGINE_ROOTS],
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT / "scripts", env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    verdict = json.loads(result.stdout)
    assert verdict["default"] == str(selected / suffix)
    assert verdict["hook"] == str(selected / suffix)
    assert str(default) not in verdict["alternatives"]
    if configured == "missing":
        assert verdict["root"] is None, "a missing configured home must refuse, not use the default install"
        assert str(selected / suffix) in verdict["error"]
    else:
        assert verdict["root"] == str(selected / suffix)
        assert verdict["error"] is None


def test_a_missing_plugin_raises_and_names_the_fix(tmp_path: Path, monkeypatch) -> None:
    """A sibling tree on the alternatives list must never stand in for an absent plugin."""
    sibling = _make_engine_tree(tmp_path / "sibling")
    monkeypatch.setattr(engine_source, "PLUGIN_ENGINE_ROOT", tmp_path / "not-installed")
    monkeypatch.setattr(engine_source, "ALTERNATIVE_ENGINE_ROOTS", (sibling,))

    with pytest.raises(engine_source.EngineNotFoundError) as caught:
        engine_source.engine_root()
    message = str(caught.value)
    assert str(tmp_path / "not-installed") in message
    assert "copilot plugin install tableau-fabric-skills@tableau-collection" in message
    with pytest.raises(engine_source.EngineNotFoundError):
        engine_source.engine_scripts_dir()


def test_the_plugin_resolves_and_a_clean_machine_reports_ok(tmp_path: Path, monkeypatch) -> None:
    plugin = _make_engine_tree(tmp_path / "plugin", version="2.126.0")
    monkeypatch.setattr(engine_source, "PLUGIN_ENGINE_ROOT", plugin)
    monkeypatch.setattr(engine_source, "ALTERNATIVE_ENGINE_ROOTS", (tmp_path / "absent",))
    assert engine_source.engine_root() == plugin
    assert engine_source.engine_scripts_dir() == plugin / engine_source.ENGINE_SKILL / "scripts"
    verdict = engine_source.status()
    assert (verdict["ok"], verdict["alternatives"], verdict["version"]) == (True, [], "2.126.0")


def test_only_a_real_second_tree_is_reported_and_never_selected(tmp_path: Path, monkeypatch) -> None:
    """#727: a sibling engine is advisory; the plugin itself and an empty decoy folder are not alternatives."""
    plugin = _make_engine_tree(tmp_path / "plugin", version="2.126.0")
    sibling = _make_engine_tree(tmp_path / "sibling", version="2.113.0")
    decoy = tmp_path / "tableau-fabric-skills"
    decoy.mkdir()
    monkeypatch.setattr(engine_source, "PLUGIN_ENGINE_ROOT", plugin)
    monkeypatch.setattr(engine_source, "ALTERNATIVE_ENGINE_ROOTS", (plugin, decoy, sibling))

    assert engine_source.alternative_engine_roots() == [sibling]
    verdict = engine_source.status()
    assert verdict["alternatives"] == [str(sibling)]
    assert verdict["root"] == str(plugin)
    assert verdict["ok"] is True


def test_a_noncanonical_engine_is_refused_by_default(tmp_path: Path, monkeypatch) -> None:
    plugin = _make_engine_tree(tmp_path / "plugin")
    other = _make_engine_tree(tmp_path / "other")
    monkeypatch.setattr(engine_source, "PLUGIN_ENGINE_ROOT", plugin)

    with pytest.raises(engine_source.NonCanonicalEngineError):
        engine_source.resolve_engine(other)
    assert engine_source.resolve_engine(other, allow_noncanonical=True) == other
    assert engine_source.resolve_engine(None) == plugin


def test_provenance_marks_overrides_and_absence(tmp_path: Path, monkeypatch) -> None:
    plugin = _make_engine_tree(tmp_path / "plugin", version="2.126.0")
    other = _make_engine_tree(tmp_path / "other", version="2.113.0")
    monkeypatch.setattr(engine_source, "PLUGIN_ENGINE_ROOT", plugin)

    assert engine_source.engine_provenance(plugin) == {
        "root": str(plugin),
        "version": "2.126.0",
        "canonical": True,
        "source": "plugin",
        "plugin_root": str(plugin),
    }
    override = engine_source.engine_provenance(other)
    assert (override["canonical"], override["source"], override["version"]) == (False, "override", "2.113.0")

    monkeypatch.setattr(engine_source, "PLUGIN_ENGINE_ROOT", tmp_path / "gone")
    absent = engine_source.engine_provenance()
    assert (absent["source"], absent["version"], absent["canonical"]) == ("unresolved", None, False)


def test_the_receipt_records_the_engine_and_still_matches_its_bundle(tmp_path: Path, monkeypatch) -> None:
    from migration_bundle import _receipt_matches_bundle  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    plugin = _make_engine_tree(tmp_path / "plugin", version="2.126.0")
    monkeypatch.setattr(engine_source, "PLUGIN_ENGINE_ROOT", plugin)

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "report.json").write_text("{}", encoding="utf-8")
    (bundle / "input_manifest.json").write_text('{"inputs": []}', encoding="utf-8")
    receipt = json.loads(migration_bundle.write_engine_receipt(bundle, plugin).read_text(encoding="utf-8"))

    assert receipt["engine"]["version"] == "2.126.0"
    assert receipt["engine"]["root"] == str(plugin)
    assert receipt["engine"]["canonical"] is True
    assert _receipt_matches_bundle(bundle, receipt)


def test_no_script_carries_its_own_engine_path() -> None:
    """A second hard-coded path is a second resolution order - exactly what #107 was."""
    for script in ("harvest_estate_assets.py", "run_estate.py", "dax_oracle_server.py", "transpile_tableau_calc.py"):
        text = (REPO_ROOT / "scripts" / script).read_text(encoding="utf-8")
        assert "installed-plugins" not in text, f"{script} hard-codes a plugin path; use engine_source"
        assert "vscode-projects" not in text, f"{script} hard-codes a clone path; use engine_source"
        assert "engine_source" in text, f"{script} must resolve the engine through engine_source"


def test_the_cli_exits_nonzero_when_the_engine_is_not_the_only_one() -> None:
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "engine_source.py"), "--json"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    verdict = json.loads(proc.stdout)
    assert proc.returncode == (0 if verdict["ok"] else 1)
    assert set(verdict) >= {"root", "present", "version", "alternatives", "upstream_version_url", "ok"}
