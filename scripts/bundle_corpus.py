"""
purpose: shared discovery helpers for shipping Power BI artifacts in migration bundles.
usage:   import bundle_corpus; bundle_corpus.shipping_reports(Path("bundle"))

The check_* gates deliberately keep separate verdicts and exit codes, but they should not keep
separate copies of the same filesystem-discovery rules. This module is the single place for the
`pbip/`-first shipping-artifact convention.
"""

from __future__ import annotations

from pathlib import Path

#: A self-contained handover package writes this beside the unit (`scripts/package_unit.py`, #446).
PACKAGE_MARKER = "package-manifest.json"


def is_self_contained(target: Path) -> bool:
    """Whether ``target`` declares that it carries its own evidence and must inherit none.

    ⚠️ **This is what stops an evidence walk-up, in BOTH gates, and it has to be one rule.** Every
    gate here looks for `reference/`/`oracle/` beside the target AND beside its ancestors, and
    **unions** the hits - which is right for an un-packaged unit under `<bundle>/pbip/<Unit>/`, whose
    capture lives further up. `package_unit.py` writes a unit-scoped `oracle/oracle-manifest.json`
    holding THIS unit's views with rewritten paths, so a package assembled INSIDE a run directory
    matches every view twice and both gates then refuse the pair as an ambiguity:

    * `check_reference_readiness` reports *"2 records share this name once normalized"* and takes
      every page from ready to **unverifiable** (issue #451);
    * `check_unit` reports *"2 producer records are named X"* and reports **0 visual coverage** -
      measured on a synthetic package, the same defect one gate along.

    Both are silent, and both make packaging strictly WORSE than not packaging. The marker is the
    package's own declaration that it is complete, so it is taken at its word.

    ⚠️ Deliberately NOT "stop when the target has its own copy of this directory": a package that
    OMITTED a render because it could not attribute it would then pick that render up from the
    ancestor, undoing a fail-closed packaging decision at the consumer.
    """
    return (target / PACKAGE_MARKER).is_file()


def shipping_reports(root: Path) -> list[Path]:
    """Return `.Report` folders that ship under ``root``.

    Engine bundles carry the editable/shipping copy under ``pbip/`` and the pristine engine baseline
    under ``reports/``. When ``pbip/`` exists, scan only it. Passing a `.Report` folder directly is an
    explicit override for targeted checks.
    """
    root = root.resolve()
    if root.name.endswith(".Report"):
        return [root]
    base = root / "pbip" if (root / "pbip").is_dir() else root
    return sorted({path.resolve() for path in base.rglob("*.Report") if path.is_dir()}, key=str)


def shipping_models(root: Path, *, include_standalone: bool = False) -> list[Path]:
    """Return `.SemanticModel` folders that ship under ``root``.

    Most artifact gates scan ``pbip/`` only when it exists, because ``semantic_models/`` is then the
    engine baseline. ``check_empty_model`` passes ``include_standalone=True`` because datasource-only
    migrations can legitimately ship a standalone model there.
    """
    root = root.resolve()
    if root.name.endswith(".SemanticModel"):
        return [root]
    pbip = root / "pbip"
    if not pbip.is_dir():
        return sorted({path.resolve() for path in root.rglob("*.SemanticModel") if path.is_dir()}, key=str)
    models = {path.resolve() for path in pbip.rglob("*.SemanticModel") if path.is_dir()}
    if include_standalone:
        standalone = root / "semantic_models"
        if standalone.is_dir():
            models.update(path.resolve() for path in standalone.rglob("*.SemanticModel") if path.is_dir())
    return sorted(models, key=str)
