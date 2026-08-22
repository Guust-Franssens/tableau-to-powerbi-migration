"""
purpose: shared discovery helpers for shipping Power BI artifacts in migration bundles.
usage:   import bundle_corpus; bundle_corpus.shipping_reports(Path("bundle"))

The check_* gates deliberately keep separate verdicts and exit codes, but they should not keep
separate copies of the same filesystem-discovery rules. This module is the single place for the
`pbip/`-first shipping-artifact convention.
"""

from __future__ import annotations

from pathlib import Path


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
