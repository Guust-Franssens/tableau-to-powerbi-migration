"""
purpose: group a flat `capture_tableau_oracle.py` capture into per-workbook reference folders
usage:   python scripts/group_oracle_by_workbook.py --oracle _oracle [--migrations migrations/workbooks]
                                                    [--dry-run]

`capture_tableau_oracle.py` writes every view flat into `<oracle>/data/` and `<oracle>/images/`,
with workbook association living only in `oracle-manifest.json`. That is deliberate and is NOT
changed here: a LUID-keyed flat layout survives a workbook or view rename, which a folder-per-
workbook layout cannot, so the capture stays the authoritative artifact.

This script is the browse-time convenience on top of it. It COPIES (never moves) each view's files
into `migrations/workbooks/<slug>/reference/{images,data}/`, which is the layout the rest of this
toolkit already uses, and writes a per-workbook `oracle-manifest.json` subset beside them.

Why a separate script rather than a `--group-by-workbook` flag on the capture:

* it re-runs against an EXISTING capture, costing no REST calls. Tableau's `/views/.../data` and
  `/image` endpoints are metered (100 calls/hr/Creator), so re-capturing merely to change the
  on-disk layout is the expensive way to get the same bytes.
* the capture can therefore stay a pure "talk to the API" step, and this a pure "arrange local
  files" step, which is testable with no network at all.

MATCHING IS AGAINST FOLDERS THAT ALREADY EXIST, and never by slugifying a name into a path.
Both sides are normalized (lowercased, non-alphanumerics dropped) and compared; a workbook whose
folder is absent is REPORTED, not created, and a name that normalizes onto two folders is reported
as ambiguous rather than resolved by picking one.

That normalizer is deliberately narrow, and its limits are known: dropping non-alphanumerics
collapses punctuation and case but never words, so a name carrying Tableau's cross-project
disambiguation suffix (`"Sales | Project : Finance"`) does NOT match a `sales` folder. It is
reported as unmatched -- which is the honest outcome, and the reason this script's exit code
distinguishes "grouped everything" from "grouped what it could".
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tableau_oracle_manifest

LOG = logging.getLogger("group-oracle")

MANIFEST_NAME = "oracle-manifest.json"
UNMATCHED_REPORT = "oracle-grouping-report.json"


@dataclass(frozen=True)
class _Context:
    """The per-run state `_group_one` reads; everything here is constant across workbooks."""

    manifest: dict[str, Any]
    destinations: dict[str, list[Path]]
    oracle_dir: Path
    dry_run: bool


def normalize(name: str) -> str:
    """Match key: lowercased with every non-alphanumeric removed.

    Lossy on purpose so `DS Tail Level`, `ds-tail-level` and `DS_Tail_Level` agree. It does not
    remove words, so a caption suffix survives normalization and simply fails to match.
    """
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def index_destinations(migrations_root: Path) -> tuple[dict[str, list[Path]], int]:
    """Map normalized folder name -> the existing folders that produce it.

    A list, not a single path, so an ambiguous key stays visible instead of being silently resolved.
    """
    index: dict[str, list[Path]] = {}
    if not migrations_root.is_dir():
        return index, 0
    folders = sorted(p for p in migrations_root.iterdir() if p.is_dir())
    for folder in folders:
        index.setdefault(normalize(folder.name), []).append(folder)
    return index, len(folders)


def load_manifest(oracle_dir: Path) -> dict[str, Any]:
    """Read the capture manifest, or raise a message that names the file we wanted.

    Read through :func:`tableau_oracle_manifest.read_manifest`, not ``json.loads``: an OLDER capture
    names uncertified bytes under the data leg's ``path``, and ``copy_view_files`` keys on exactly
    that -- so reading raw would place a body nothing established as CSV at ``<workbook>/data/*.csv``
    in the grouped folder, which is the shape #480 exists to remove.
    """
    path = oracle_dir / MANIFEST_NAME
    if not path.is_file():
        raise FileNotFoundError(f"no {MANIFEST_NAME} in {oracle_dir} - run capture_tableau_oracle.py first")
    return tableau_oracle_manifest.read_manifest(path)


def group_views(manifest: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Bucket the manifest's views by workbook name, preserving capture order within each bucket."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for view in manifest.get("views", []):
        buckets.setdefault(view.get("workbook_name") or "", []).append(view)
    return buckets


RENDER_LEGS: tuple[tuple[str, str], ...] = (("data", "data"), ("image", "images"), ("svg", "images"), ("pdf", "images"))

# Status stamped on a leg the SOURCE manifest called `ok` but whose artifact could not be copied.
# Deliberately not `ok` and not `failed`: the capture succeeded, the *grouping* did not, and a reader
# has to be able to tell those apart when deciding whether to re-capture (metered) or re-group (free).
NOT_COPIED_STATUS = "not_copied"


def copy_view_files(
    view: dict[str, Any], oracle_dir: Path, destination: Path, *, dry_run: bool
) -> tuple[list[str], dict[str, Any]]:
    """Copy one view's captured artifacts. Returns ``(relative paths written, the view AS GROUPED)``.

    A view whose capture failed has no `path` key, so nothing is copied and nothing is invented --
    the per-workbook manifest still records its failure status, which is the honest evidence grade.

    ⚠️ Every render leg the oracle can write MUST appear in ``RENDER_LEGS``. `--reference-best` now
    normally yields **SVG** on Cloud, and while this handled only `data` and `image` the grouped
    manifest asserted `svg.path`/`pdf.path` for files that were never copied -- a manifest pointing at
    absent evidence, which is worse than omitting it.

    ⚠️ **A copy that could not happen is returned as a DOWNGRADED leg, not merely warned about.**
    Skipping a missing artifact while handing the caller the source manifest's own `status: ok` and
    `path` re-creates that same shape one level up: the grouped folder asserts evidence nothing ever
    put there. The returned view is a copy -- the capture manifest is never mutated -- whose affected
    legs carry ``NOT_COPIED_STATUS``, no ``path``, and the reason.
    """
    written: list[str] = []
    grouped = dict(view)
    for kind, sub in RENDER_LEGS:
        entry = view.get(kind) or {}
        relative = entry.get("path")
        if entry.get("status") != "ok" or not relative:
            continue
        source = oracle_dir / relative
        if not source.is_file():
            LOG.warning("  MISSING on disk, not copied: %s", relative)
            downgraded = {k: v for k, v in entry.items() if k != "path"}
            downgraded["status"] = NOT_COPIED_STATUS
            downgraded["not_copied_reason"] = (
                f"the capture manifest names {relative}, which is absent from {oracle_dir}"
            )
            grouped[kind] = downgraded
            continue
        target = destination / sub / Path(relative).name
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        written.append(f"{sub}/{Path(relative).name}")
    return written, grouped


def subset_manifest(manifest: dict[str, Any], workbook: str, views: list[dict[str, Any]]) -> dict[str, Any]:
    """A per-workbook manifest carrying the SAME evidence grade as the capture-wide one.

    ⚠️ ``views`` must be the views **as grouped** -- ``copy_view_files``' second return value, not the
    capture manifest's own list. The counts below are what a consumer reads instead of listing the
    folder, so computing them from the capture's statuses claims evidence for artifacts this run may
    have failed to copy. Passing the raw views is the defect, not a shortcut.

    Counts are recomputed over this workbook's views rather than copied, so a folder that holds
    three good captures and one credential-blocked view says exactly that. The capture-wide GRADE
    fields are carried across verbatim: a consumer that reads only this file must still be able to
    see which render tier was obtained and whether a required reference went missing (#403), rather
    than inferring it from which files happen to exist.
    """

    def status_of(view: dict[str, Any], kind: str, default: str | None = None) -> str | None:
        return (view.get(kind) or ({"status": default} if default else {})).get("status")

    render_kinds = [kind for kind, _ in RENDER_LEGS if kind != "data"]

    def render_statuses(view: dict[str, Any], default: str | None = None) -> list[str | None]:
        return [status_of(view, kind, default) for kind in render_kinds]

    ok = [v for v in views if status_of(v, "data") == "ok"]
    blocked = [v for v in views if "source_credential" in {status_of(v, "data"), *render_statuses(v)}]
    failed = [
        v
        for v in views
        if any(
            status not in {"ok", "source_credential"} for status in (status_of(v, "data"), *render_statuses(v, "ok"))
        )
    ]
    not_copied = sum(1 for v in views for kind, _ in RENDER_LEGS if status_of(v, kind) == NOT_COPIED_STATUS)
    subset = {
        "schema": "tableau-oracle-workbook/1",
        "grouped_from": manifest.get("schema"),
        "captured_at": manifest.get("captured_at"),
        "server": manifest.get("server"),
        "site": manifest.get("site"),
        "rest_api_version": manifest.get("rest_api_version"),
        "workbook_name": workbook,
        "workbook_luid": next((v.get("workbook_luid") for v in views if v.get("workbook_luid")), None),
        "view_count": len(views),
        "data_ok": len(ok),
        # ⚠️ IMPORTED, not re-implemented (#471). This was a second copy of "row_count == 0", and a
        # second copy of a rule is how a per-workbook subset comes to disagree with the capture it
        # was sliced from. The count is unchanged; what is new is that it is now the SAME predicate
        # the capture-wide manifest counts with -- and the views are NAMED here too, for the reason
        # `data_empty_views` exists at all: a count cannot tell a reviewer which page to open.
        "data_empty": len([v for v in ok if tableau_oracle_manifest.empty_classification(v)]),
        "data_empty_views": tableau_oracle_manifest.data_empty_views(ok),
        # The third state, carried at the same grade as the other two. A per-workbook reader is the
        # one who acts on this, and a subset that reported `data_ok: 4` with nothing beside it said
        # "four good captures" about views whose rows were never measured.
        "data_unassessable": len([v for v in ok if tableau_oracle_manifest.unassessable_reason(v)]),
        "data_unassessable_views": tableau_oracle_manifest.data_unassessable_views(ok),
        "credential_blocked": len(blocked),
        "failed": len(failed),
        # Legs the CAPTURE obtained but this grouping could not place. Separate from `failed` so a
        # reader knows to re-run the (free) grouping rather than the (metered) capture.
        "not_copied": not_copied,
        "views": views,
    }
    for kind in render_kinds:
        subset[f"{'image' if kind == 'image' else kind}_ok"] = sum(1 for v in views if status_of(v, kind) == "ok")
    # Carried, not recomputed: these describe the CAPTURE RUN, not this workbook's slice of it.
    for field in ("render_capability", "requested_renders", "reference_required", "reference_missing"):
        if field in manifest:
            subset[field] = manifest[field]
    return subset


def build_parser() -> argparse.ArgumentParser:
    """CLI surface: which capture to read, which folder tree to group it into."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--oracle", required=True, type=Path, help="the capture directory holding oracle-manifest.json")
    parser.add_argument(
        "--migrations",
        type=Path,
        default=Path("migrations/workbooks"),
        help="root holding the per-workbook <slug>/ folders (default: migrations/workbooks)",
    )
    parser.add_argument("--dry-run", action="store_true", help="report what would be copied, write nothing")
    return parser


def _group_one(
    workbook: str,
    views: list[dict[str, Any]],
    ctx: _Context,
) -> tuple[str, dict[str, Any]]:
    """Place one workbook's views. Returns its outcome bucket and the record to report."""
    matches = ctx.destinations.get(normalize(workbook), [])
    if len(matches) > 1:
        LOG.warning("AMBIGUOUS  %-45s -> %s", workbook[:45], ", ".join(p.name for p in matches))
        return "ambiguous", {
            "workbook": workbook,
            "folders": [str(m) for m in matches],
            "views": len(views),
        }
    if not matches:
        LOG.warning("NO FOLDER  %-45s (normalized: %s)", workbook[:45], normalize(workbook))
        return "unmatched", {"workbook": workbook, "normalized": normalize(workbook), "views": len(views)}

    destination = matches[0] / "reference"
    files: list[str] = []
    grouped_views: list[dict[str, Any]] = []
    for view in views:
        written, grouped = copy_view_files(view, ctx.oracle_dir, destination, dry_run=ctx.dry_run)
        files.extend(written)
        grouped_views.append(grouped)
    subset = subset_manifest(ctx.manifest, workbook, grouped_views)
    if not ctx.dry_run:
        destination.mkdir(parents=True, exist_ok=True)
        (destination / MANIFEST_NAME).write_text(json.dumps(subset, indent=2) + "\n", encoding="utf-8")
    record = {
        "workbook": workbook,
        "folder": str(matches[0]),
        "views": len(views),
        "files": len(files),
        "not_copied": subset["not_copied"],
    }
    if subset["not_copied"]:
        # NOT "grouped". The folder exists and holds some evidence, but the capture manifest named
        # artifacts that are not on disk, so this workbook's reference set is incomplete and the
        # command must not report success for it.
        LOG.warning(
            "INCOMPLETE %-45s -> %s (%d view(s), %d file(s), %d artifact(s) missing from the capture)",
            workbook[:45],
            matches[0].name,
            len(views),
            len(files),
            subset["not_copied"],
        )
        return "incomplete", record
    LOG.info("ok         %-45s -> %s (%d view(s), %d file(s))", workbook[:45], matches[0].name, len(views), len(files))
    return "grouped", record


def run(oracle_dir: Path, migrations_root: Path, *, dry_run: bool) -> int:
    """Group the capture. Returns 0 when every workbook landed, 1 when some could not.

    "Could not" covers three things, and the third used to be invisible: no destination folder, an
    ambiguous destination, and a destination that was reached but did **not** receive every artifact
    the capture manifest named. All three mean the same to a caller gating on the exit code -- the
    per-workbook copies are partial and the flat capture remains the authoritative one.
    """
    manifest = load_manifest(oracle_dir)
    destinations, folder_count = index_destinations(migrations_root)
    buckets = group_views(manifest)
    LOG.info(
        "%d workbook(s) in the capture, %d candidate folder(s) under %s%s",
        len(buckets),
        folder_count,
        migrations_root,
        " [DRY RUN]" if dry_run else "",
    )

    outcomes: dict[str, list[dict[str, Any]]] = {"grouped": [], "incomplete": [], "unmatched": [], "ambiguous": []}
    ctx = _Context(manifest=manifest, destinations=destinations, oracle_dir=oracle_dir, dry_run=dry_run)
    for workbook, views in sorted(buckets.items()):
        bucket, record = _group_one(workbook, views, ctx)
        outcomes[bucket].append(record)

    report = {
        "schema": "tableau-oracle-grouping/1",
        "oracle_dir": str(oracle_dir),
        "migrations_root": str(migrations_root),
        "dry_run": dry_run,
        "workbooks_grouped": len(outcomes["grouped"]),
        "workbooks_incomplete": len(outcomes["incomplete"]),
        "workbooks_unmatched": len(outcomes["unmatched"]),
        "workbooks_ambiguous": len(outcomes["ambiguous"]),
        **outcomes,
    }
    if not dry_run:
        (oracle_dir / UNMATCHED_REPORT).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    LOG.info(
        "\n%d grouped, %d incomplete, %d without a folder, %d ambiguous%s",
        len(outcomes["grouped"]),
        len(outcomes["incomplete"]),
        len(outcomes["unmatched"]),
        len(outcomes["ambiguous"]),
        "" if dry_run else f" -> {oracle_dir / UNMATCHED_REPORT}",
    )
    if outcomes["incomplete"]:
        LOG.warning(
            "%d workbook(s) are missing artifacts their capture manifest names. The grouped manifests "
            "mark those legs '%s' rather than claiming them; re-run the grouping if the capture is "
            "intact, and only re-capture (metered) if it is not.",
            len(outcomes["incomplete"]),
            NOT_COPIED_STATUS,
        )
    if outcomes["unmatched"] or outcomes["ambiguous"] or outcomes["incomplete"]:
        LOG.warning(
            "the capture in %s remains complete and authoritative - only the per-workbook copies are partial",
            oracle_dir,
        )
        return 1
    return 0


def main() -> int:
    """Entry point: parse arguments, group the capture, map failures onto an exit code."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args()
    try:
        return run(args.oracle, args.migrations, dry_run=args.dry_run)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        LOG.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
