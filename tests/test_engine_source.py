"""The conversion engine has ONE source: the installed plugin. These tests are issue #107's fix.

The defect was never "the code picked the wrong tree". It was that picking a tree at all was SILENT:
three modules each carried their own candidate list, the machine had two engines installed at
different versions, and a bundle could not say which one built it. So every test here asserts a
FAILURE mode - that the resolver refuses rather than substitutes - because a resolver that falls back
passes any test written about its happy path.
"""

from __future__ import annotations

import json
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


# ---------------------------------------------------------------------------
# No fallback. This is the whole point.
# ---------------------------------------------------------------------------


def test_a_missing_plugin_raises_instead_of_finding_another_copy(tmp_path: Path, monkeypatch) -> None:
    """The failure that mattered: a sibling clone standing in for an absent plugin, silently.

    A second tree exists here and is even on the alternatives list, so any fallback logic would find
    it. The resolver must still refuse.
    """
    sibling = _make_engine_tree(tmp_path / "sibling")
    monkeypatch.setattr(engine_source, "PLUGIN_ENGINE_ROOT", tmp_path / "not-installed")
    monkeypatch.setattr(engine_source, "ALTERNATIVE_ENGINE_ROOTS", (sibling,))

    with pytest.raises(engine_source.EngineNotFoundError):
        engine_source.engine_root()
    with pytest.raises(engine_source.EngineNotFoundError):
        engine_source.engine_scripts_dir()


def test_the_error_names_the_path_and_the_install_command(tmp_path: Path, monkeypatch) -> None:
    """An unactionable failure gets worked around; this one has to say what to run."""
    monkeypatch.setattr(engine_source, "PLUGIN_ENGINE_ROOT", tmp_path / "not-installed")
    with pytest.raises(engine_source.EngineNotFoundError) as caught:
        engine_source.engine_root()
    message = str(caught.value)
    assert str(tmp_path / "not-installed") in message
    assert "copilot plugin install tableau-fabric-skills@tableau-collection" in message


def test_the_plugin_resolves_when_it_is_installed(tmp_path: Path, monkeypatch) -> None:
    plugin = _make_engine_tree(tmp_path / "plugin", version="2.126.0")
    monkeypatch.setattr(engine_source, "PLUGIN_ENGINE_ROOT", plugin)
    assert engine_source.engine_root() == plugin
    assert engine_source.engine_scripts_dir() == plugin / engine_source.ENGINE_SKILL / "scripts"
    assert engine_source.engine_version() == "2.126.0"


# ---------------------------------------------------------------------------
# A second tree is the defect, so it has to be VISIBLE
# ---------------------------------------------------------------------------


def test_an_alternative_tree_is_reported(tmp_path: Path, monkeypatch) -> None:
    plugin = _make_engine_tree(tmp_path / "plugin", version="2.126.0")
    sibling = _make_engine_tree(tmp_path / "sibling", version="2.113.0")
    monkeypatch.setattr(engine_source, "PLUGIN_ENGINE_ROOT", plugin)
    monkeypatch.setattr(engine_source, "ALTERNATIVE_ENGINE_ROOTS", (sibling,))

    assert engine_source.alternative_engine_roots() == [sibling]
    assert engine_source.status()["ok"] is False


def test_a_clean_machine_reports_ok(tmp_path: Path, monkeypatch) -> None:
    plugin = _make_engine_tree(tmp_path / "plugin")
    monkeypatch.setattr(engine_source, "PLUGIN_ENGINE_ROOT", plugin)
    monkeypatch.setattr(engine_source, "ALTERNATIVE_ENGINE_ROOTS", (tmp_path / "absent",))

    verdict = engine_source.status()
    assert verdict["ok"] is True
    assert verdict["alternatives"] == []
    assert verdict["version"] == "2.126.0"


def test_the_plugin_is_never_reported_as_its_own_alternative(tmp_path: Path, monkeypatch) -> None:
    """A candidate path that happens to BE the plugin (symlink, duplicate entry) is not a second copy."""
    plugin = _make_engine_tree(tmp_path / "plugin")
    monkeypatch.setattr(engine_source, "PLUGIN_ENGINE_ROOT", plugin)
    monkeypatch.setattr(engine_source, "ALTERNATIVE_ENGINE_ROOTS", (plugin,))
    assert engine_source.alternative_engine_roots() == []


def test_an_empty_directory_is_not_an_engine(tmp_path: Path, monkeypatch) -> None:
    """`is_engine_tree` keys on the engine skill, not on the folder name."""
    decoy = tmp_path / "tableau-fabric-skills"
    decoy.mkdir()
    monkeypatch.setattr(engine_source, "ALTERNATIVE_ENGINE_ROOTS", (decoy,))
    assert engine_source.alternative_engine_roots() == []


def test_the_shipped_alternative_list_covers_the_trees_this_project_used() -> None:
    """The list is only as good as what is on it - an unlisted tree is one preflight cannot block.

    These two are the locations that actually held a second engine on a real machine: the sibling
    clone beside the repo, and `~/vscode-projects/tableau-fabric-skills`, which was `run_estate.py`'s
    argparse default and therefore the tree the whole estate ran on.
    """
    listed = {str(path).lower() for path in engine_source.ALTERNATIVE_ENGINE_ROOTS}
    assert str(engine_source.REPO_ROOT.parent / "tableau-fabric-skills").lower() in listed
    assert str(Path.home() / "vscode-projects" / "tableau-fabric-skills").lower() in listed


# ---------------------------------------------------------------------------
# An override must be deliberate, and it must be recorded
# ---------------------------------------------------------------------------


def test_a_noncanonical_engine_is_refused_by_default(tmp_path: Path, monkeypatch) -> None:
    plugin = _make_engine_tree(tmp_path / "plugin")
    other = _make_engine_tree(tmp_path / "other")
    monkeypatch.setattr(engine_source, "PLUGIN_ENGINE_ROOT", plugin)

    with pytest.raises(engine_source.NonCanonicalEngineError):
        engine_source.resolve_engine(other)
    assert engine_source.resolve_engine(other, allow_noncanonical=True) == other
    assert engine_source.resolve_engine(None) == plugin


def test_provenance_marks_an_override_as_noncanonical(tmp_path: Path, monkeypatch) -> None:
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
    assert override["canonical"] is False
    assert override["source"] == "override"
    assert override["version"] == "2.113.0"


def test_provenance_records_the_absence_rather_than_omitting_it(tmp_path: Path, monkeypatch) -> None:
    """A bundle built with no resolvable engine must still say so - never a missing key."""
    monkeypatch.setattr(engine_source, "PLUGIN_ENGINE_ROOT", tmp_path / "gone")
    provenance = engine_source.engine_provenance()
    assert provenance["source"] == "unresolved"
    assert provenance["version"] is None
    assert provenance["canonical"] is False


# ---------------------------------------------------------------------------
# The bundle answers "what built me?" on its own
# ---------------------------------------------------------------------------


def test_the_receipt_records_the_engine_path_and_version(tmp_path: Path, monkeypatch) -> None:
    """The acceptance criterion of #107, verbatim: traceable without asking the machine."""
    plugin = _make_engine_tree(tmp_path / "plugin", version="2.126.0")
    monkeypatch.setattr(engine_source, "PLUGIN_ENGINE_ROOT", plugin)

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "report.json").write_text("{}", encoding="utf-8")
    receipt = json.loads(migration_bundle.write_engine_receipt(bundle, plugin).read_text(encoding="utf-8"))

    assert receipt["engine"]["version"] == "2.126.0"
    assert receipt["engine"]["root"] == str(plugin)
    assert receipt["engine"]["canonical"] is True


def test_the_receipt_still_verifies_against_the_credential_gate(tmp_path: Path, monkeypatch) -> None:
    """Adding a key must not break the gate that consumes this file."""
    from credential_gate import _receipt_matches_bundle  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    plugin = _make_engine_tree(tmp_path / "plugin")
    monkeypatch.setattr(engine_source, "PLUGIN_ENGINE_ROOT", plugin)

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "report.json").write_text("{}", encoding="utf-8")
    (bundle / "input_manifest.json").write_text('{"inputs": []}', encoding="utf-8")
    receipt = json.loads(migration_bundle.write_engine_receipt(bundle, plugin).read_text(encoding="utf-8"))

    assert _receipt_matches_bundle(bundle, receipt)


# ---------------------------------------------------------------------------
# Every caller uses the one resolver
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    ["harvest_estate_assets.py", "run_estate.py", "dax_oracle_server.py", "transpile_tableau_calc.py"],
)
def test_no_script_carries_its_own_engine_path(script: str) -> None:
    """A second hard-coded path is a second resolution order - exactly what #107 was.

    Matching on the literal plugin/clone path fragments is the point: they are what each of these
    four files used to contain, and re-introducing one is how the divergence would come back.
    """
    text = (REPO_ROOT / "scripts" / script).read_text(encoding="utf-8")
    assert "installed-plugins" not in text, f"{script} hard-codes a plugin path; use engine_source"
    assert "vscode-projects" not in text, f"{script} hard-codes a clone path; use engine_source"
    assert "engine_source" in text, f"{script} must resolve the engine through engine_source"


def test_the_cli_exits_nonzero_when_the_engine_is_not_the_only_one(tmp_path: Path) -> None:
    """preflight reads this exit code path indirectly; a vacuous zero would make the gate useless."""
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
