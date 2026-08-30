"""
purpose: render one engine-gap harvest report - the console view and the upstream-fileable markdown.
usage:   imported by scripts/harvest_engine_gaps.py; not a user-facing CLI

Split out because rendering consumes only the finished report dict: it needs no bundle, no hashes and
no filesystem, so it can be exercised on a literal payload. The seam also keeps `harvest_engine_gaps`
under pylint's `max-module-lines` after the round-3 review added reconciliation and named
incompleteness reasons.

Every table here carries its DENOMINATOR, and every non-claim is printed rather than implied - the
report is meant to be pasted into an upstream issue, where an unqualified count is worse than none.
"""

from __future__ import annotations

from typing import Any

# ⚠️ This module imports NOTHING from `harvest_engine_gaps`, deliberately. It reads the vocabulary
# out of the payload instead - the provenance names from `report["provenance"]` (whose insertion
# order IS the canonical order) and the baseline roots from `report["baseline_roots"]`. That keeps
# the dependency one-way and lets these renderers be exercised on a literal dict.
DEFAULT_TOP = 12


def _provenance_names(report: dict[str, Any]) -> list[str]:
    """The provenance vocabulary, in the order the harvest wrote it."""
    return [name for name in report["provenance"] if name != "differing_files"]


def _pct(part: int, whole: int) -> str:
    return f"{part}/{whole} ({round(100 * part / whole):d}%)" if whole else f"{part}/0 (n/a)"


def _layer_lines(report: dict[str, Any]) -> list[str]:
    lines = []
    for layer, summary in report["layers"].items():
        if not summary["artifacts"]:
            continue
        lines.append(
            f"  {layer + ' layer':<14}: {_pct(summary['pairs_assessed'], summary['artifacts'])} assessed"
            f" | identical {summary['identical']}, differs {summary['differs']}"
            f" | no baseline {summary['unpaired_no_baseline']},"
            f" no working copy {summary['unpaired_no_working']},"
            f" unassessable {summary['unassessable']}"
        )
        lines.append(
            f"  {'':<14}  files: {summary['files_changed']} changed,"
            f" {summary['files_added']} added, {summary['files_removed']} removed,"
            f" {summary['files_post_engine_only']} post-engine only"
        )
        if summary["baseline_reference_checked"]:
            lines.append(
                f"  {'':<14}  baseline dataset reference resolves:"
                f" {_pct(summary['baseline_reference_resolves'], summary['baseline_reference_checked'])}"
            )
    return lines


def _finding_lines(report: dict[str, Any], top: int) -> list[str]:
    """The sections that only appear when there is something to say."""
    return _evidence_lines(report, top) + _integrity_lines(report, top) + _coverage_lines(report, top)


def _evidence_lines(report: dict[str, Any], top: int) -> list[str]:
    """What changed, and who changed it."""
    lines: list[str] = []
    if report["shapes"]:
        lines.append(f"  shapes (top {top})       :")
        for row in report["shapes"][:top]:
            share = f"{100 * row['share_of_differing_files']:.0f}%" if row["share_of_differing_files"] else "n/a"
            lines.append(f"      {row['files']:>5} files / {row['artifacts']:>3} artifacts  {share:>5}  {row['shape']}")
    if report["tier_edits"]:
        lines.append(f"  TIER EDITS            : {len(report['tier_edits'])} file(s) changed after the engine ran")
        for record in report["tier_edits"][:top]:
            declared = record["declared_by"] or "UNDECLARED"
            lines.append(
                f"      [{record['unit'] or record['artifact']}] {record['path']} {record['shapes']} <- {declared}"
            )
    if report["unpaired_drift_records"]:
        lines.append(
            f"  UNPAIRED TIER EDITS   : {report['unpaired_drift_records']} adjudicated path(s) belong to no"
            " reports/-vs-pbip/ pair (e.g. a `.pbip` project file) and are reported from the inventory alone"
        )
    return lines


def _integrity_lines(report: dict[str, Any], top: int) -> list[str]:
    """Why the delta might not be readable as engine behaviour at all."""
    lines: list[str] = []
    if report["baseline_drift"]:
        lines.append(
            f"  BASELINE DRIFT        : {len(report['baseline_drift'])} engine-baseline path(s) under"
            f" {'/'.join(r.rstrip('/') for r in report['baseline_roots'])} moved after the engine ran."
            " Those trees are never edited by anyone, so this delta cannot be read as engine behaviour."
        )
        for entry in report["baseline_drift"][:top]:
            lines.append(f"      {entry['kind']:<8} {entry['target']}  ({entry['declared_by'] or 'undeclared'})")
    if report["baseline_tampered"]:
        lines.append(
            f"  BASELINE TAMPERED     : {len(report['baseline_tampered'])} compared file(s) whose baseline side drifted"
        )
        for record in report["baseline_tampered"][:top]:
            lines.append(f"      [{record['unit'] or record['artifact']}] {record['path']}")
    return lines


def _coverage_lines(report: dict[str, Any], top: int) -> list[str]:
    """What this run could not see - never folded into the counts above."""
    lines: list[str] = []
    if report["incomplete_reasons"]:
        lines.append("  NOT COMPLETE because  :")
        for reason in report["incomplete_reasons"]:
            lines.append(f"      - {reason}")
    if report["unassessable"]:
        lines.append(f"  UNASSESSABLE (not passed): {len(report['unassessable'])} path(s) could not be read")
        for record in report["unassessable"][:top]:
            lines.append(f"      [{record.get('scope', 'content')}] {record['reason']}  {record['path']}")
    if report["unreconciled_drift"]:
        lines.append(
            f"  UNRECONCILED DRIFT    : {len(report['unreconciled_drift'])} adjudicated path(s) this module"
            " could not place - reported rather than dropped, and the run cannot be `complete`"
        )
        for entry in report["unreconciled_drift"][:top]:
            lines.append(f"      {entry['kind']:<8} {entry['target']}")
    blind = report["git_blind_spot"]
    if blind["count"]:
        lines.append(
            f"  git blind spot        : {blind['count']} pair(s) exceed {blind['path_max']} characters -"
            " the AGENTS.md `git diff --no-index` form returns exit 1 with NO stat line for these."
            " Assessed here anyway."
        )
        for record in blind["pairs"][:top]:
            lines.append(f"      {record['longest_path']:>4}  [{record['layer']}] {record['unit']}")
    return lines


def render(report: dict[str, Any], top: int = DEFAULT_TOP) -> str:
    """Human-readable console report."""
    provenance = report["provenance"]
    total = provenance["differing_files"]
    coverage = report["attribution"]["coverage"]
    lines = [
        f"{report['status'].upper()}: {report['bundle']}",
        f"  engine                : {report['engine'].get('version') or 'unknown'}"
        f" (canonical={report['engine'].get('canonical')})",
        f"  attribution           : {'hash-attributed' if report['attribution']['usable'] else 'NOT AVAILABLE'}"
        f" from {report['attribution']['files_recorded']} recorded artifacts",
    ]
    lines += [f"      note              : {note}" for note in report["attribution"]["notes"]]
    lines.append(
        f"  attribution coverage  : {_pct(coverage['paths_attributed'], coverage['paths_compared'])}"
        f" of compared paths{'' if coverage['complete'] else '  <- NOT complete; status cannot be `complete`'}"
    )
    lines.extend(_layer_lines(report))
    lines.append(f"  differing files       : {total}")
    lines += [f"      {name:<18}: {_pct(provenance[name], total)}" for name in _provenance_names(report)]
    lines.append(
        "      -> only `tier_edit` answers 'what did the engine get wrong?'."
        " `engine_internal` is the engine's own reference-vs-bound difference."
    )
    lines.extend(_finding_lines(report, top))
    return "\n".join(lines)


def _markdown_shape_table(report: dict[str, Any], top: int) -> list[str]:
    lines = ["| shape | files | artifacts | share of differing files |", "|---|---:|---:|---:|"]
    for row in report["shapes"][:top]:
        share = f"{100 * row['share_of_differing_files']:.0f}%" if row["share_of_differing_files"] else "n/a"
        lines.append(f"| `{row['shape']}` | {row['files']} | {row['artifacts']} | {share} |")
    return lines


def render_markdown(report: dict[str, Any], top: int = DEFAULT_TOP) -> str:
    """An upstream-fileable summary: frequencies with denominators, and explicit non-claims."""
    provenance = report["provenance"]
    total = provenance["differing_files"]
    lines = [
        "# Engine-gap harvest",
        "",
        f"- bundle: `{report['bundle']}`",
        f"- engine: **{report['engine'].get('version') or 'unknown'}**"
        f" (canonical: {report['engine'].get('canonical')})",
        f"- harvested: {report['generated_at']}",
        f"- status: **{report['status']}**",
        f"- attribution: {'hash-attributed' if report['attribution']['usable'] else '**unavailable**'}"
        f" from {report['attribution']['files_recorded']} recorded artifacts, adjudicated by"
        " `check_migration_progress.adjudicate_generated_drift` (the `--tamper` gate's own machinery)",
        f"- attribution coverage: {report['attribution']['coverage']['paths_attributed']}"
        f"/{report['attribution']['coverage']['paths_compared']} compared paths"
        f"{'' if report['attribution']['coverage']['complete'] else ' - **not complete**'}",
        "",
        "## Coverage, per layer",
        "",
        "| layer | artifacts | assessed | identical | differs | no baseline | no working copy | unassessable |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for layer, summary in report["layers"].items():
        lines.append(
            f"| {layer} | {summary['artifacts']} | {summary['pairs_assessed']} | {summary['identical']} |"
            f" {summary['differs']} | {summary['unpaired_no_baseline']} |"
            f" {summary['unpaired_no_working']} | {summary['unassessable']} |"
        )
    lines += [
        "",
        "## Who wrote the difference",
        "",
        "| provenance | files | share |",
        "|---|---:|---:|",
    ]
    for name in _provenance_names(report):
        share = f"{round(100 * provenance[name] / total)}%" if total else "n/a"
        lines.append(f"| `{name}` | {provenance[name]} | {share} |")
    lines += [
        "",
        "> `engine_internal` means the engine wrote **both** sides - its reference-only emission and"
        " its bound working copy. That is a by-design difference and is **not** evidence of an engine"
        ' defect. Only `tier_edit` answers *"what did a human or agent have to change?"*.',
        "",
        "## What changed",
        "",
    ]
    lines += _markdown_shape_table(report, top)
    if report["tier_edits"]:
        lines += ["", "## Tier edits (the engine-gap evidence)", ""]
        lines += ["| unit | layer | file | shapes | declared by |", "|---|---|---|---|---|"]
        for record in report["tier_edits"][:top]:
            lines.append(
                f"| {record['unit']} | {record['layer']} | `{record['path']}` |"
                f" {', '.join(record['shapes'])} | {record['declared_by'] or '**undeclared**'} |"
            )
    else:
        lines += [
            "",
            "## Tier edits (the engine-gap evidence)",
            "",
            "**None.** Every differing byte in this bundle is still hash-identical to what the engine"
            " itself recorded, so nothing here shows work a human or agent had to do. A bundle with no"
            " fix pass cannot answer issue #274's question, and this report does not pretend it can.",
        ]
    if report["baseline_drift"]:
        lines += ["", "## Engine baseline drift (why this report is untrustworthy)", ""]
        lines += ["| kind | path | declared by |", "|---|---|---|"]
        for entry in report["baseline_drift"][:top]:
            lines.append(f"| {entry['kind']} | `{entry['target']}` | {entry['declared_by'] or '**undeclared**'} |")
        lines += [
            "",
            "> `reports/` and `semantic_models/` are the engine's pristine reference emission and are"
            " **never edited, by anyone** (AGENTS.md). A declaration makes such an edit visible, not"
            " legitimate, so drift here is refused whether declared or not.",
        ]
    lines += ["", "## What this does not say", ""]
    lines += [
        "- **Not effort.** File and line counts are not hours; a reformat and a fidelity fix count the same.",
        "- **Not why.** Provenance says who, shape says what; the reason lives in the handover and"
        " `limitations_encountered`.",
        "- **Not a defect list.** `engine_internal` differences are by construction not defect evidence.",
    ]
    if report["unreconciled_drift"]:
        lines.append(
            f"- **{len(report['unreconciled_drift'])} adjudicated path(s) could not be placed** by this module and"
            " are listed in `unreconciled_drift`; the run is not `complete` while any remain."
        )
    if report["unassessable"]:
        lines.append(
            f"- **{len(report['unassessable'])} path(s) could not be read** and are excluded from every count above."
        )
    return "\n".join(lines) + "\n"
