"""
purpose: group flat `capture_tableau_oracle.py` captures into per-workbook reference folders
usage:   python scripts/group_oracle_by_workbook.py --oracle _oracle [--oracle _oracle-retry ...]
                                                    [--migrations migrations/workbooks] [--dry-run]

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

`--oracle` IS REPEATABLE, and it has to be (issue #423)
-------------------------------------------------------
A metered, timing-out capture is re-run in BATCHES, and the same view can succeed in a later batch
having failed in an earlier one. Field evidence: *Daily Monitoring* failed its data leg twice, then
on a third batch produced both a data leg and 905,098 bytes of PNG -- and the workbook's
`reference/` folder only ever cross-referenced the first two batches, so a successful capture sat
unused on disk. Grouping one directory at a time cannot fix that: the last invocation overwrites the
per-workbook manifest, so it does not merely miss the good artifact, it can REPLACE a good one with
a failure from a partial re-run.

So every batch is read and merged per view and PER LEG, newest-successful-wins:

* a leg is a candidate only if its status is `ok` AND the artifact it names is on disk -- a manifest
  entry alone is a claim, and this script already refuses to promote claims it cannot back;
* if no batch has a successful leg, the NEWEST batch's record for that leg is kept, so the failure
  (or `not_attempted`, or `source_credential`) stays visible. A view with no establishable render
  must not quietly vanish from the merged manifest -- that collapse is the whole of #423;
* every promoted leg records `source_batch`, and the merged manifest lists `batches`, so "which
  capture did this image come from" is answerable from the artifact rather than from memory.

WARNING: "Newest" is the view record's `captured_at`, falling back to the batch manifest's. When a batch
carries NEITHER, the order is undetermined and this script says so (`merge_order_basis:
"argument order"`, plus a warning) rather than pretending the argv order is a timestamp.
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

LOG = logging.getLogger("group-oracle")

MANIFEST_NAME = "oracle-manifest.json"
UNMATCHED_REPORT = "oracle-grouping-report.json"


@dataclass(frozen=True)
class _Context:
    """The per-run state `_group_one` reads; everything here is constant across workbooks."""

    manifest: dict[str, Any]
    destinations: dict[str, list[Path]]
    roots: dict[str, Path]
    dry_run: bool


@dataclass(frozen=True)
class _Batch:
    """One `_oracle/<dir>` capture, plus the two things the merge orders by.

    ``label`` is the directory NAME -- what a report shows and what a leg's ``source_batch`` records.
    ``order`` is the position on the command line, and is the LAST-RESORT tiebreak only: a batch that
    carries no ``captured_at`` anywhere cannot be dated, and argv order is an operator's habit rather
    than evidence, so relying on it is reported (see :func:`merge_batches`).
    """

    directory: Path
    manifest: dict[str, Any]
    label: str
    order: int

    @property
    def captured_at(self) -> str:
        """The batch-level capture time, or ``""`` when this manifest does not carry one."""
        return self.manifest.get("captured_at") or ""


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
    """Read the capture manifest, or raise a message that names the file we wanted."""
    path = oracle_dir / MANIFEST_NAME
    if not path.is_file():
        raise FileNotFoundError(f"no {MANIFEST_NAME} in {oracle_dir} - run capture_tableau_oracle.py first")
    return json.loads(path.read_text(encoding="utf-8"))


def load_batches(oracle_dirs: list[Path]) -> list[_Batch]:
    """Read every named capture directory, preserving the order they were given in.

    A missing manifest is fatal for the WHOLE run rather than skipped: silently grouping two of three
    batches would produce a merged folder that looks complete and is not, which is the exact failure
    class this script exists to make visible.
    """
    return [
        _Batch(directory, load_manifest(directory), directory.name, index)
        for index, directory in enumerate(oracle_dirs)
    ]


def _leg_is_promotable(entry: dict[str, Any], root: Path) -> bool:
    """A leg may only win the merge if it is ``ok`` AND the artifact it names is on disk.

    ⚠️ Both halves. A manifest entry is a CLAIM; a later batch whose manifest says ``ok`` for a file
    somebody has since deleted must not displace an earlier batch that still has the bytes. Without
    the on-disk half, merging could make a reference set worse than either input.
    """
    relative = entry.get("path")
    return bool(entry.get("status") == "ok" and relative and (root / relative).is_file())


def _merge_one_view(candidates: list[tuple[_Batch, dict[str, Any]]], roots: dict[str, Path]) -> dict[str, Any]:
    """Merge one view across batches, newest-successful-wins PER LEG.

    ``candidates`` is already ordered newest-first. The view's identity fields come from the newest
    batch that saw it at all; each leg is then resolved independently, because the field case that
    started this is precisely a view whose data and image succeeded in DIFFERENT batches.
    """
    newest_batch, newest_view = candidates[0]
    merged = dict(newest_view)
    merged["source_batch"] = newest_batch.label
    for kind, _sub in RENDER_LEGS:
        winner = next(
            (
                (batch, view[kind])
                for batch, view in candidates
                if _leg_is_promotable(view.get(kind) or {}, roots[batch.label])
            ),
            None,
        )
        if winner is None:
            # No batch established this leg. Keep the NEWEST record so the failure -- or the absence
            # of any record at all -- stays exactly as visible as it was, and mark where it came from.
            if kind in merged:
                merged[kind] = {**merged[kind], "source_batch": newest_batch.label}
            continue
        batch, entry = winner
        merged[kind] = {**entry, "source_batch": batch.label}
    return merged


def merge_batches(batches: list[_Batch]) -> tuple[dict[str, Any], dict[str, Path], str]:
    """Fold every batch into ONE manifest, newest-successful-wins per view and per leg.

    Returns ``(merged manifest, label -> directory, the basis the ordering used)``.

    The newest batch supplies the provenance fields (`server`, `site`, `rest_api_version`, the
    `#403` capability block), because those describe the run that produced the winning artifacts more
    often than any older one does. `batches` records every input in newest-first order, so a reader
    can see what was merged rather than infer it from one `source_batch` at a time.
    """
    roots = {batch.label: batch.directory for batch in batches}
    dated = [
        batch
        for batch in batches
        if batch.captured_at or any(v.get("captured_at") for v in batch.manifest.get("views", []))
    ]
    basis = "captured_at" if len(dated) == len(batches) else "argument order"

    def freshness(batch: _Batch, view: dict[str, Any]) -> tuple[str, int]:
        return (view.get("captured_at") or batch.captured_at or "", batch.order)

    by_view: dict[str, list[tuple[_Batch, dict[str, Any]]]] = {}
    for batch in batches:
        for view in batch.manifest.get("views", []):
            by_view.setdefault(view.get("view_luid") or "", []).append((batch, view))

    views = []
    for candidates in by_view.values():
        candidates.sort(key=lambda pair: freshness(pair[0], pair[1]), reverse=True)
        views.append(_merge_one_view(candidates, roots))

    newest = max(batches, key=lambda b: (b.captured_at, b.order))
    merged = {key: value for key, value in newest.manifest.items() if key != "views"}
    merged["views"] = views
    merged["batches"] = [b.label for b in sorted(batches, key=lambda b: (b.captured_at, b.order), reverse=True)]
    merged["merge_order_basis"] = basis
    return merged, roots, basis


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
    view: dict[str, Any], roots: dict[str, Path], destination: Path, *, dry_run: bool
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

    ⚠️ ``roots`` is a MAP, not one directory, because after #423 two legs of the same view can come
    from two different batches. Each leg is resolved against the batch that actually produced it
    (``source_batch``); resolving everything against a single root is what stranded a good image.
    """
    written: list[str] = []
    grouped = dict(view)
    for kind, sub in RENDER_LEGS:
        entry = view.get(kind) or {}
        relative = entry.get("path")
        if entry.get("status") != "ok" or not relative:
            continue
        oracle_dir = roots[entry.get("source_batch", next(iter(roots)))]
        source = oracle_dir / relative
        if not source.is_file():
            LOG.warning("  MISSING on disk, not copied: %s (%s)", relative, oracle_dir.name)
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
        "data_empty": len([v for v in ok if (v.get("data") or {}).get("row_count") == 0]),
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
    # `batches` / `merge_order_basis` travel with them (#423) so a consumer reading ONLY this file can
    # see which captures were folded together and on what evidence "newest" was decided -- otherwise
    # the per-leg `source_batch` names a directory the reader has no list of.
    for field in (
        "render_capability",
        "requested_renders",
        "reference_required",
        "reference_missing",
        "batches",
        "merge_order_basis",
    ):
        if field in manifest:
            subset[field] = manifest[field]
    return subset


def build_parser() -> argparse.ArgumentParser:
    """CLI surface: which capture to read, which folder tree to group it into."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--oracle",
        required=True,
        action="append",
        type=Path,
        metavar="DIR",
        help=(
            "a capture directory holding oracle-manifest.json. WARNING: REPEATABLE, and normally should be: "
            "a metered capture is re-run in batches, and the same view can succeed in a later one "
            "having failed earlier. Every batch given is merged newest-successful-wins per view and "
            "per LEG, and each promoted artifact records the batch it came from. Grouping one "
            "directory at a time strands a later good image -- or overwrites a good manifest with a "
            "partial re-run's failure"
        ),
    )
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
        written, grouped = copy_view_files(view, ctx.roots, destination, dry_run=ctx.dry_run)
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


def _group_all(buckets: dict[str, list[dict[str, Any]]], ctx: _Context) -> dict[str, list[dict[str, Any]]]:
    """Place every workbook, bucketed by outcome. Split out of ``run`` to keep it readable."""
    outcomes: dict[str, list[dict[str, Any]]] = {"grouped": [], "incomplete": [], "unmatched": [], "ambiguous": []}
    for workbook, views in sorted(buckets.items()):
        bucket, record = _group_one(workbook, views, ctx)
        outcomes[bucket].append(record)
    return outcomes


def _write_grouping_report(
    batches: list[_Batch],
    migrations_root: Path,
    basis: str,
    outcomes: dict[str, list[dict[str, Any]]],
    *,
    dry_run: bool,
) -> Path:
    """Write the run report beside the LAST capture given, and return that directory.

    ``oracle_dirs`` and ``merge_order_basis`` are new (#423): with several batches folded together,
    "which captures produced this" and "on what evidence was newest decided" are the two questions a
    reader of a merged reference folder actually has. ``oracle_dir`` is kept for callers that read it.
    """
    report_dir = batches[-1].directory
    report = {
        "schema": "tableau-oracle-grouping/1",
        "oracle_dir": str(report_dir),
        "oracle_dirs": [str(b.directory) for b in batches],
        "merge_order_basis": basis,
        "migrations_root": str(migrations_root),
        "dry_run": dry_run,
        "workbooks_grouped": len(outcomes["grouped"]),
        "workbooks_incomplete": len(outcomes["incomplete"]),
        "workbooks_unmatched": len(outcomes["unmatched"]),
        "workbooks_ambiguous": len(outcomes["ambiguous"]),
        **outcomes,
    }
    if not dry_run:
        (report_dir / UNMATCHED_REPORT).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report_dir


def run(oracle: Path | list[Path], migrations_root: Path, *, dry_run: bool) -> int:
    """Group one or more captures. Returns 0 when every workbook landed, 1 when some could not.

    "Could not" covers three things, and the third used to be invisible: no destination folder, an
    ambiguous destination, and a destination that was reached but did **not** receive every artifact
    the capture manifest named. All three mean the same to a caller gating on the exit code -- the
    per-workbook copies are partial and the flat capture remains the authoritative one.

    ``oracle`` accepts a single ``Path`` as well as a list, deliberately: the single-capture call is
    the common one and is what every existing caller writes. A list is folded newest-successful-wins
    by :func:`merge_batches` before anything is copied (#423), which is what stops an operator's
    third, finally-successful retry batch from being stranded on disk -- or, worse, a partial re-run
    from OVERWRITING a good per-workbook manifest with a failure.
    """
    batches = load_batches([oracle] if isinstance(oracle, Path) else list(oracle))
    manifest, roots, basis = merge_batches(batches)
    destinations, folder_count = index_destinations(migrations_root)
    buckets = group_views(manifest)
    LOG.info(
        "%d workbook(s) across %d capture(s), %d candidate folder(s) under %s%s",
        len(buckets),
        len(batches),
        folder_count,
        migrations_root,
        " [DRY RUN]" if dry_run else "",
    )
    if len(batches) > 1 and basis != "captured_at":
        # Not a detail. Merging is "newest wins", so an undated batch means the WINNER is decided by
        # the order somebody happened to type -- which is a habit, not evidence. Say so rather than
        # letting a merged manifest imply a provenance it does not have.
        LOG.warning(
            "at least one capture carries no captured_at, so 'newest' fell back to ARGUMENT ORDER "
            "(last --oracle wins). The merged manifests record merge_order_basis='%s'; pass the "
            "batches oldest-first, or re-capture with a manifest that carries a timestamp.",
            basis,
        )

    ctx = _Context(manifest=manifest, destinations=destinations, roots=roots, dry_run=dry_run)
    outcomes = _group_all(buckets, ctx)
    report_dir = _write_grouping_report(batches, migrations_root, basis, outcomes, dry_run=dry_run)

    LOG.info(
        "\n%d grouped, %d incomplete, %d without a folder, %d ambiguous%s",
        len(outcomes["grouped"]),
        len(outcomes["incomplete"]),
        len(outcomes["unmatched"]),
        len(outcomes["ambiguous"]),
        "" if dry_run else f" -> {report_dir / UNMATCHED_REPORT}",
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
            "the capture(s) in %s remain complete and authoritative - only the per-workbook copies are partial",
            ", ".join(str(b.directory) for b in batches),
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
