"""
purpose: harvest the engine-gap evidence that already sits in a bundle - compare the engine's
         pristine `reports/`+`semantic_models/` baseline against the shipped `pbip/` working copy,
         ATTRIBUTE every difference to whoever wrote it, classify its shape, and report frequencies
         with denominators.
usage:   python scripts/harvest_engine_gaps.py <bundle> [...] [--json <file>] [--markdown <file>]
                                               [--quiet] [--warn-only] [--top N]

Full methodology, every measurement behind it, and the estate-scale results:
`docs/engine-gap-harvest.md`. The essentials, because they change how the output must be read:

**A difference between `reports/` and `pbip/` is NOT evidence the engine was wrong.** Measured on
`_runs/estate-2.339.0-20260829/` (2026-08-30): 37 of 44 report pairs differ, 500 files - and **100%
of those bytes were written by the engine itself**. All 2481 recorded artifacts still hash-match
(0 mismatched, 0 missing); no `_build/` directory and no edit declaration exists anywhere. Nobody
edited that bundle. The raw delta answers *"what does the engine change between its own reference and
its own bound emission?"* - by design. Only the ATTRIBUTED subset answers issue #274's question, and
this module reports the two separately and never merges them.

Axis 1 - PROVENANCE. **This module does NOT arbitrate provenance itself.** It delegates to
`check_migration_progress.adjudicate_generated_drift()` - the structured core of the `--tamper` gate
this repo already ships - and only interprets the answer. That is a deliberate mechanism change, not
a preference: a blind review of the first version (PR #399) found FOUR independent defects in this
module's own hand-rolled attribution, and measured the existing gate returning `DRIFT` on every one
of the same bundles this module called `complete`:

    probe (measured 2026-08-30)          harvest v1        tamper_check()
    reports/ replaced with pbip/         complete, 0 diffs DRIFT (undeclared change to reports/)
    working file added after the engine  complete          DRIFT (undeclared added)
    working file deleted after it        complete          DRIFT (undeclared missing)
    stale/unrelated declaration          complete, declared DRIFT (classified UNDECLARED)

Reusing the gate dissolved all four. What is left here is the interpretation the gate does not make -
which SIDE of the bundle a drifting path sits on, and what that means for the harvest's own verdict:

    baseline recorded + working recorded, neither drifted -> engine_internal   NOT a tier fix
    working path drifted (changed / added / missing)      -> tier_edit         THE EVIDENCE
    ANY drift under reports/ or semantic_models/          -> baseline_tampered refuse; exit 1
    no usable baseline, or a path it does not cover       -> unattributed      never laundered

Two policy divergences from the tamper gate, both stricter, both deliberate:
* a `slice_only_backfill` baseline is treated as **unavailable** here, not merely caveated. The gate
  asks "did anything change since the baseline was recorded"; this module asks "who wrote this byte
  relative to the ENGINE boundary", and a backfill has no engine boundary behind it to answer with.
* drift under `reports/`+`semantic_models/` is `untrustworthy` **even when declared**. Those trees
  are "NEVER edited, by anyone" (AGENTS.md); a declaration makes an edit visible, not legitimate.

Axis 2 - SHAPE lives in `harvest_gap_shapes.py`: a structural JSON-pointer diff plus a TMDL line
diff. Buckets were chosen by measuring the corpus first; `UNCLASSIFIED` is retained and reported.

This module does NOT use git, and that is a correctness fix. Measured: of 44 pairs, the mandated
`git diff --no-index --stat` produced NO stat line for 3 (worst path 261/285/287 vs 259 for the 41 it
could read), while agreeing with this module on 41 of 41 that it could. A Python content comparison
reads all three, so UNASSESSABLE falls from 3 to 0 - and the blind spot is still reported, because it
is evidence about the mandated command.

⚠️ **Unassessable, absent and malformed input must never collapse into a clean-looking result.** That
single class produced four of the seven review findings, so it is now handled structurally rather
than case by case: an unreadable path is withdrawn from both sides *with every descendant* and forces
a non-zero exit; a compared path the baseline does not cover forces `incomplete`; an
undecodable/unreadable manifest or declaration ledger returns unavailable attribution and exit 3,
never the exit 1 that means "a tampered baseline was positively detected".

Standalone rather than a `run_estate.py` phase, deliberately: a phase inside the run can only observe
a bundle the tier has not touched yet - the degenerate case above, where `tier_edit` is 0 by
construction. Wiring `check_unit.py` is a follow-up, not done here.

What this does NOT tell you: **effort** (counts are not hours), **why** (that lives in the handover
and `limitations_encountered`), whether the **engine is wrong** (`engine_internal` is by construction
not defect evidence), and anything about a NATURALLY OCCURRING tier edit - no bundle on the machine
this was built on contained one. The `tier_edit` / `baseline_tampered` paths are proven on real
engine artifacts with an INJECTED change (a copied unit reported exactly 1 tier edit, then
`untrustworthy`/exit 1 when its baseline was touched); the field case is still unconfirmed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

from check_migration_progress import adjudicate_generated_drift, load_generated_artifact_baseline
from harvest_gap_shapes import SHAPE_REVERTED, added_removed_shape, bound_model_tables, shapes_for_change
from migration_bundle import ENGINE_RECEIPT, sha256_file

REPORT_VERSION = 1

EXIT_OK = 0
EXIT_UNTRUSTWORTHY = 1
EXIT_USAGE = 2
EXIT_INCOMPLETE = 3

STATUS_COMPLETE = "complete"
STATUS_INCOMPLETE = "incomplete"
STATUS_UNTRUSTWORTHY = "untrustworthy"

PROV_ENGINE = "engine_internal"
PROV_TIER = "tier_edit"
PROV_TAMPERED = "baseline_tampered"
PROV_UNATTRIBUTED = "unattributed"
PROVENANCES = (PROV_ENGINE, PROV_TIER, PROV_TAMPERED, PROV_UNATTRIBUTED)

PAIR_IDENTICAL = "identical"
PAIR_DIFFERS = "differs"
PAIR_NO_BASELINE = "unpaired_no_baseline"
PAIR_NO_WORKING = "unpaired_no_working"
PAIR_UNASSESSABLE = "unassessable"

LAYER_REPORT = "report"
LAYER_MODEL = "model"

# Longest full path git read successfully on the estate corpus; 261/285/287 all failed with no stat
# line. Kept local rather than imported from `check_path_ceiling` because that module's ceilings are
# a Power BI Desktop measurement and this one is a git measurement - two different instruments that
# happen to agree, and collapsing them would hide the day one of them moves.
GIT_READABLE_PATH_MAX = 259

DEFAULT_TOP = 12

# The two bundle trees the engine emits as a pristine reference, and the one the tier is allowed to
# edit. Drift under a BASELINE root is refused outright; drift under the WORKING root is the evidence
# this module exists to collect. Trailing slash on purpose - prefix matching, never `in`.
BASELINE_ROOTS = ("reports/", "semantic_models/")
WORKING_ROOT = "pbip/"

DRIFT_CHANGED = "changed"
DRIFT_MISSING = "missing"
DRIFT_ADDED = "added"


class Pair(NamedTuple):
    """One baseline/working pair for one layer.

    `artifact` is the folder name without its suffix and is what the two sides are matched on;
    `unit` is the owning `pbip/<unit>/` directory. They are NOT interchangeable: measured on the
    estate corpus, every one of the 51 units holds exactly one `.SemanticModel` and only 7 of them
    are named after their unit - `pbip/HR Dashboard/` holds `HumanResources.SemanticModel`. Pairing
    the model layer by unit name reported 21 units as having no engine baseline when 20 of them do.
    """

    artifact: str
    unit: str
    layer: str
    baseline: Path | None
    working: Path | None


class TreeDelta(NamedTuple):
    """The raw content comparison of two trees, with unreadable entries kept apart."""

    added: list[str]
    removed: list[str]
    changed: list[str]
    unassessable: list[dict[str, str]]
    baseline_files: int
    working_files: int
    longest_path: int
    scoped: bool = True


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe(text: str) -> str:
    """A console/JSON-safe rendering of a path that may carry undecodable bytes."""
    return text.encode("utf-8", "backslashreplace").decode("ascii", "replace")


def hash_tree(root: Path) -> tuple[dict[str, str], list[dict[str, str]], int]:
    """Hash every file under `root`, returning (by-relative-path, unreadable, longest full path).

    Unreadable entries are returned SEPARATELY and are never given a digest, because a file that
    cannot be read is not a file that is the same - the single defect shape this repo keeps
    re-introducing. Each carries `relative` when one could be computed, so the caller can withdraw
    that path from BOTH sides of a comparison rather than letting it masquerade as an addition.
    """
    digests: dict[str, str] = {}
    unreadable: list[dict[str, str]] = []
    longest = 0
    root_str = str(root)

    def on_error(exc: OSError) -> None:
        failed = str(getattr(exc, "filename", "") or root_str)
        record = {"path": _safe(failed), "reason": f"{type(exc).__name__}: {exc.strerror or exc}"}
        try:
            record["relative"] = Path(failed).relative_to(root).as_posix()
        except ValueError:
            pass
        unreadable.append(record)

    for dirpath, dirnames, filenames in os.walk(root_str, onerror=on_error):
        for name in list(dirnames) + list(filenames):
            longest = max(longest, len(os.path.join(dirpath, name)))
        for name in filenames:
            full = Path(dirpath) / name
            relative = None
            try:
                relative = full.relative_to(root).as_posix()
                digests[relative] = sha256_file(full)
            except (OSError, ValueError) as exc:
                record = {"path": _safe(str(full)), "reason": f"{type(exc).__name__}: {exc}"}
                if relative is not None:
                    record["relative"] = relative
                unreadable.append(record)
    return digests, unreadable, longest


def _withdraw(keys: set[str], blocked: frozenset[str]) -> set[str]:
    """Drop every key that IS a blocked path or lives BENEATH one.

    ⚠️ Exact-equality withdrawal is not enough, and that gap fabricated evidence. `os.walk` reports
    only the DIRECTORY it could not enter, so an unreadable `pages/blocked/` withdrew exactly one
    key - `pages/blocked` - while every descendant visible on the *other* side (`pages/blocked/
    visual.json`) stayed in the comparison and was counted as an addition. Measured (blind review of
    PR #399): an injected `PermissionError` on one baseline directory produced an unassessable record
    for the directory AND a fabricated `delta.added` entry beneath it.
    """
    return {key for key in keys if key not in blocked and not any(key.startswith(f"{p}/") for p in blocked)}


def compare_trees(baseline: Path, working: Path) -> TreeDelta:
    """Content-compare two trees without git, so a long path is assessed rather than skipped."""
    a, a_bad, a_longest = hash_tree(baseline)
    b, b_bad, b_longest = hash_tree(working)
    unassessable = a_bad + b_bad
    # A path that could not be read on EITHER side is withdrawn from BOTH key sets, with everything
    # beneath it, so it can never masquerade as an addition or a removal. Matching is done on the
    # POSIX relative path, not the rendered absolute one: `Path(root) / "a/b"` stringifies to
    # `root\a/b` on Windows and would never match the `root\a\b` that `os.walk` produced.
    blocked = frozenset(record["relative"] for record in unassessable if "relative" in record)
    # A failure whose relative path could not be computed at all cannot be scoped, so nothing about
    # this pair can be trusted: the caller suppresses its difference records entirely rather than
    # reporting a subset that looks complete.
    scoped = not any("relative" not in record for record in unassessable)
    a_keys = _withdraw(set(a), blocked)
    b_keys = _withdraw(set(b), blocked)
    return TreeDelta(
        added=sorted(b_keys - a_keys),
        removed=sorted(a_keys - b_keys),
        changed=sorted(k for k in a_keys & b_keys if a[k] != b[k]),
        unassessable=unassessable,
        baseline_files=len(a),
        working_files=len(b),
        longest_path=max(a_longest, b_longest),
        scoped=scoped,
    )


class Evidence:
    """Who wrote a byte, INTERPRETED from `check_migration_progress`'s drift adjudication.

    This class deliberately owns no hashing and no declaration rules of its own. It holds the
    engine-time inventory (`recorded`), the adjudicated post-engine drift keyed by bundle-relative
    path (`drift`), and answers two questions the gate does not: which side of the bundle a path sits
    on, and whether the evidence is usable at all.

    `usable` has to be inspectable by the caller so an unattributable run is reported as such instead
    of silently reporting everything as `unattributed` with no explanation.
    """

    def __init__(
        self,
        bundle: Path,
        recorded: dict[str, str] | None,
        drift: dict[str, dict[str, Any]] | None,
        notes: list[str],
        unavailable_reason: str | None = None,
    ) -> None:
        self.bundle = bundle
        self.recorded = recorded or {}
        self.drift = drift or {}
        self.notes = notes
        self.unavailable_reason = unavailable_reason
        self.usable = unavailable_reason is None

    def state(self, relative: str) -> str:
        """`match`, `drift_<kind>`, `unrecorded` or `unavailable` for one bundle-relative path."""
        if not self.usable:
            return "unavailable"
        entry = self.drift.get(relative)
        if entry is not None:
            return f"drift_{entry['kind']}"
        return "match" if relative in self.recorded else "unrecorded"

    def declared_by(self, relative: str) -> str | None:
        """The declaring script, ONLY where the tamper gate accepted the declaration as proof.

        Populated from `adjudicate_generated_drift`, which requires the declaration to name this run
        id, this baseline hash, this operation and this exact resulting hash. The first version
        credited any declaration that merely mentioned the target, so a file edited AGAIN after being
        declared was attributed to an unrelated old script (blind review of PR #399); the gate
        classified that same edit as UNDECLARED.
        """
        entry = self.drift.get(relative)
        return entry["declared_by"] if entry else None

    def verdict(self, baseline_rel: str | None, working_rel: str | None) -> str:
        """Provenance of one difference. `None` means that side has no file."""
        if not self.usable:
            return PROV_UNATTRIBUTED
        base = self.state(baseline_rel) if baseline_rel else None
        work = self.state(working_rel) if working_rel else None
        if base is not None and base.startswith("drift_"):
            return PROV_TAMPERED
        if work is not None and work.startswith("drift_"):
            return PROV_TIER
        states = {s for s in (base, work) if s is not None}
        if states == {"match"}:
            return PROV_ENGINE
        return PROV_UNATTRIBUTED

    def side_drift(self, roots: tuple[str, ...]) -> list[dict[str, Any]]:
        """Adjudicated drift under the given bundle roots, sorted by path."""
        return [
            dict(entry, target=target)
            for target, entry in sorted(self.drift.items())
            if any(target.startswith(root) for root in roots)
        ]

    def working_drift_under(self, root: str) -> dict[str, dict[str, Any]]:
        """Post-engine drift inside ONE working artifact, keyed by artifact-relative path."""
        prefix = f"{root}/"
        return {target[len(prefix) :]: entry for target, entry in self.drift.items() if target.startswith(prefix)}


def _unavailable(bundle: Path, reason: str, note: str) -> Evidence:
    return Evidence(bundle, None, None, [note], unavailable_reason=reason)


def _load_evidence(bundle: Path) -> Evidence:
    """Load the engine-time inventory and adjudicate drift, or say why it cannot be done.

    Every failure here is UNAVAILABLE attribution (`incomplete`, exit 3) - never exit 1, which this
    module reserves for a positively detected tampered baseline. Measured (blind review of PR #399):
    an `input_manifest.json` carrying invalid UTF-8 raised `UnicodeDecodeError` out of `main()`,
    exiting 1 with no structured output at all, so an automated consumer could not tell a corrupt
    manifest from a rewritten engine baseline.
    """
    try:
        generated = load_generated_artifact_baseline(bundle)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return _unavailable(
            bundle,
            f"{type(exc).__name__}: {exc}",
            f"input_manifest.json could not be read or decoded ({type(exc).__name__}) - attribution is "
            "unavailable and every difference is reported as unattributed. This is NOT tamper evidence: "
            "an unreadable manifest and a rewritten baseline are different situations and exit differently.",
        )
    if generated is None:
        return _unavailable(
            bundle,
            "no_usable_baseline",
            "no usable generated_artifacts baseline in input_manifest.json - every difference is "
            "reported as unattributed. The delta below is real; the claim about WHO caused it is "
            "withheld, not guessed (issue #230).",
        )
    if generated.get("coverage") == "slice_only_backfill":
        return _unavailable(
            bundle,
            "slice_only_backfill",
            "baseline was backfilled by `run_estate.py --slice-only`, not recorded at the engine's own "
            "run boundary. `--tamper` can still use it to prove nothing changed SINCE the backfill, but "
            "THIS module asks who wrote a byte relative to the ENGINE boundary - and a backfill has no "
            "engine boundary behind it. Attribution is therefore unavailable, not merely caveated.",
        )
    if not generated["files"]:
        return _unavailable(
            bundle,
            "empty_inventory",
            "generated_artifacts.files is EMPTY: the manifest claims the engine produced no artifacts "
            "at all, so it cannot cover a single compared path. Reported as unavailable rather than "
            "read as 'everything was added after the engine ran'.",
        )
    try:
        adjudicated = adjudicate_generated_drift(bundle, generated)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return _unavailable(
            bundle,
            f"{type(exc).__name__}: {exc}",
            f"the engine-time inventory could not be re-hashed ({type(exc).__name__}) - attribution is "
            "unavailable. The delta below is real; authorship is withheld.",
        )
    drift = {str(item["target"]): item for item in adjudicated}
    notes = []
    if drift:
        notes.append(
            f"{len(drift)} generated artifact(s) moved after the engine ran, adjudicated by "
            "`check_migration_progress.adjudicate_generated_drift` - the same machinery `--tamper` uses."
        )
    return Evidence(bundle, dict(generated["files"]), drift, notes)


def _unit_names(directory: Path, suffix: str) -> dict[str, Path]:
    if not directory.is_dir():
        return {}
    return {p.name[: -len(suffix)]: p for p in directory.iterdir() if p.is_dir() and p.name.endswith(suffix)}


def _working_artifacts(bundle: Path, suffix: str) -> list[tuple[str, str, Path]]:
    """Every shipping artifact of one kind under `pbip/`, as (artifact, unit, path).

    Returned as a LIST, not a dict: a shared published datasource is copied into every consuming
    unit, so one artifact name legitimately has several working copies (measured: `Meridian Calc
    Gauntlet (Live Snowflake).SemanticModel` exists in 3 units). Each copy is compared separately,
    because each is an independent emission that can diverge on its own.
    """
    pbip = bundle / "pbip"
    if not pbip.is_dir():
        return []
    found = []
    for unit_dir in sorted(p for p in pbip.iterdir() if p.is_dir()):
        for artifact_dir in sorted(p for p in unit_dir.iterdir() if p.is_dir() and p.name.endswith(suffix)):
            found.append((artifact_dir.name[: -len(suffix)], unit_dir.name, artifact_dir))
    return found


def discover_pairs(bundle: Path) -> list[Pair]:
    """Every baseline/working pair in the bundle, for both layers.

    A side that is missing is still returned, as None: an artifact with no engine baseline is a
    FINDING (issue #179), not an artifact to drop from the denominator.
    """
    pairs: list[Pair] = []
    for layer, baseline_dir, suffix in (
        (LAYER_REPORT, "reports", ".Report"),
        (LAYER_MODEL, "semantic_models", ".SemanticModel"),
    ):
        baselines = _unit_names(bundle / baseline_dir, suffix)
        matched: set[str] = set()
        for artifact, unit, working in _working_artifacts(bundle, suffix):
            matched.add(artifact)
            pairs.append(
                Pair(artifact=artifact, unit=unit, layer=layer, baseline=baselines.get(artifact), working=working)
            )
        for artifact in sorted(set(baselines) - matched):
            pairs.append(Pair(artifact=artifact, unit="", layer=layer, baseline=baselines[artifact], working=None))
    return pairs


def _base_record(pair: Pair, relative: str, kind: str, shapes: list[str], differences: int) -> dict[str, Any]:
    return {
        "artifact": pair.artifact,
        "unit": pair.unit,
        "layer": pair.layer,
        "path": relative,
        "kind": kind,
        "shapes": shapes,
        "differences": differences,
    }


def _post_engine(evidence: Evidence, working_rel: str | None) -> str | None:
    """What happened to a working path AFTER the engine ran: modified / created / deleted / None."""
    if working_rel is None or not evidence.usable:
        return None
    entry = evidence.drift.get(working_rel)
    if entry is None:
        return None
    return {DRIFT_CHANGED: "modified", DRIFT_ADDED: "created", DRIFT_MISSING: "deleted"}[entry["kind"]]


def _changed_records(
    pair: Pair,
    delta: TreeDelta,
    roots: tuple[str, str],
    evidence: Evidence,
) -> list[dict[str, Any]]:
    """One record per file present on both sides with different content."""
    tables = bound_model_tables(pair.working) if pair.layer == LAYER_REPORT else None
    base_root, work_root = roots
    records = []
    for relative in delta.changed:
        shapes, count = shapes_for_change(pair.baseline / relative, pair.working / relative, tables)
        base_rel, work_rel = f"{base_root}/{relative}", f"{work_root}/{relative}"
        records.append(
            _base_record(pair, relative, "changed", shapes, count)
            | {
                "baseline_path": base_rel,
                "working_path": work_rel,
                "provenance": evidence.verdict(base_rel, work_rel),
                "post_engine": _post_engine(evidence, work_rel),
                "declared_by": evidence.declared_by(work_rel),
            }
        )
    return records


def _added_removed_records(
    pair: Pair,
    delta: TreeDelta,
    roots: tuple[str, str],
    evidence: Evidence,
) -> list[dict[str, Any]]:
    """One record per file present on only one side."""
    base_root, work_root = roots
    records = []
    for relative in delta.added:
        side = f"{work_root}/{relative}"
        records.append(
            _base_record(pair, relative, "added", [added_removed_shape(relative, True)], 1)
            | {
                "baseline_path": None,
                "working_path": side,
                "provenance": evidence.verdict(None, side),
                "post_engine": _post_engine(evidence, side),
                "declared_by": evidence.declared_by(side),
            }
        )
    for relative in delta.removed:
        base_side, work_side = f"{base_root}/{relative}", f"{work_root}/{relative}"
        # ⚠️ A removed file has TWO readings and the first version only ever checked one. A path the
        # engine never emitted into the working tree has no working side at all - that is the engine's
        # own reference-only emission. A path the engine DID emit and that is now gone is a post-engine
        # DELETION, and reading it from the baseline side alone reported it as `engine_internal`.
        deleted_after_engine = evidence.state(work_side).startswith("drift_")
        records.append(
            _base_record(pair, relative, "removed", [added_removed_shape(relative, False)], 1)
            | {
                "baseline_path": base_side,
                "working_path": work_side if deleted_after_engine else None,
                "provenance": evidence.verdict(base_side, work_side if deleted_after_engine else None),
                "post_engine": _post_engine(evidence, work_side) if deleted_after_engine else None,
                "declared_by": evidence.declared_by(work_side) if deleted_after_engine else None,
            }
        )
    return records


def _post_engine_records(
    pair: Pair,
    delta: TreeDelta,
    work_root: str,
    evidence: Evidence,
) -> list[dict[str, Any]]:
    """Tier edits the baseline-vs-working delta cannot see, from the engine-time inventory.

    ⚠️ The delta answers "how do the two trees differ NOW"; it cannot answer "what changed after the
    engine ran", and three ordinary PBIR fix shapes fall straight through the gap. Measured (blind
    review of PR #399), all three returned `complete` with zero tier edits while the tamper gate
    returned `DRIFT`:

    * a newly authored working file - present on one side only, so it was `unattributed`, not a tier
      addition (this path now resolves through the inventory, so it is attributed as `created`);
    * a deleted working file - the record only ever inspected the BASELINE path, so a deletion looked
      like the engine's own reference-only emission;
    * a deleted engine-emitted working-ONLY file - absent from both trees, so it vanished from the
      comparison entirely. Only the inventory still remembers it existed.

    So the record set is the UNION of the delta's paths and the working tree's post-engine drift.
    """
    seen = set(delta.changed) | set(delta.added) | set(delta.removed)
    records = []
    for relative, entry in sorted(evidence.working_drift_under(work_root).items()):
        if relative in seen:
            continue
        kind = entry["kind"]
        if kind == DRIFT_MISSING:
            shapes, record_kind = [added_removed_shape(relative, False)], "removed_after_engine"
        elif kind == DRIFT_ADDED:
            shapes, record_kind = [added_removed_shape(relative, True)], "added_after_engine"
        else:
            # Changed since the engine, yet identical to the reference baseline: the working copy was
            # reverted onto the engine's own reference emission. Still a tier edit.
            shapes, record_kind = [SHAPE_REVERTED], "changed_after_engine"
        records.append(
            _base_record(pair, relative, record_kind, shapes, 1)
            | {
                "baseline_path": None,
                "working_path": f"{work_root}/{relative}",
                "provenance": evidence.verdict(None, f"{work_root}/{relative}"),
                "post_engine": _post_engine(evidence, f"{work_root}/{relative}"),
                "declared_by": entry["declared_by"],
            }
        )
    return records


def _difference_records(
    bundle: Path,
    pair: Pair,
    delta: TreeDelta,
    evidence: Evidence,
) -> list[dict[str, Any]]:
    """One record per differing file: its shape, its provenance, and who declared it (if anyone)."""
    if pair.baseline is None or pair.working is None:
        return []
    if not delta.scoped:
        # A traversal failure that could not even be located: no subset of this pair can be trusted,
        # so nothing is reported for it. The pair still counts as UNASSESSABLE, which is non-clean.
        return []
    roots = (pair.baseline.relative_to(bundle).as_posix(), pair.working.relative_to(bundle).as_posix())
    return (
        _changed_records(pair, delta, roots, evidence)
        + _added_removed_records(pair, delta, roots, evidence)
        + _post_engine_records(pair, delta, roots[1], evidence)
    )


def _pair_status(pair: Pair, delta: TreeDelta | None, records: list[dict[str, Any]]) -> str:
    if pair.baseline is None:
        return PAIR_NO_BASELINE
    if pair.working is None or delta is None:
        return PAIR_NO_WORKING
    if delta.unassessable:
        return PAIR_UNASSESSABLE
    if delta.added or delta.removed or delta.changed or records:
        return PAIR_DIFFERS
    return PAIR_IDENTICAL


def _reference_resolves(pair: Pair) -> bool | None:
    """Whether the BASELINE report's dataset reference resolves. None when there is nothing to ask."""
    if pair.layer != LAYER_REPORT or pair.baseline is None:
        return None
    return bound_model_tables(pair.baseline) is not None


def _engine_metadata(bundle: Path) -> dict[str, Any]:
    receipt = bundle / ENGINE_RECEIPT
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"available": False}
    engine = payload.get("engine") if isinstance(payload, dict) else None
    if not isinstance(engine, dict):
        return {"available": False}
    return {
        "available": True,
        "version": engine.get("version"),
        "root": engine.get("root"),
        "canonical": engine.get("canonical"),
    }


def _layer_summary(entries: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = Counter(entry["status"] for entry in entries)
    assessed = [e for e in entries if e["status"] in {PAIR_IDENTICAL, PAIR_DIFFERS}]
    resolved = [e["baseline_reference_resolves"] for e in entries if e["baseline_reference_resolves"] is not None]
    return {
        "artifacts": len(entries),
        "pairs_assessed": len(assessed),
        "identical": statuses[PAIR_IDENTICAL],
        "differs": statuses[PAIR_DIFFERS],
        "unpaired_no_baseline": statuses[PAIR_NO_BASELINE],
        "unpaired_no_working": statuses[PAIR_NO_WORKING],
        "unassessable": statuses[PAIR_UNASSESSABLE],
        "files_changed": sum(e["files"]["changed"] for e in entries),
        "files_added": sum(e["files"]["added"] for e in entries),
        "files_removed": sum(e["files"]["removed"] for e in entries),
        "files_post_engine_only": sum(e["files"]["post_engine_only"] for e in entries),
        "baseline_reference_resolves": sum(1 for r in resolved if r),
        "baseline_reference_checked": len(resolved),
    }


def _shape_rows(records: list[dict[str, Any]], denominator: int) -> list[dict[str, Any]]:
    files: Counter[str] = Counter()
    artifacts: dict[str, set[str]] = {}
    for record in records:
        for shape in record["shapes"]:
            files[shape] += 1
            artifacts.setdefault(shape, set()).add(f"{record['unit']}/{record['artifact']}")
    return [
        {
            "shape": shape,
            "files": count,
            "artifacts": len(artifacts[shape]),
            "share_of_differing_files": round(count / denominator, 4) if denominator else None,
        }
        for shape, count in files.most_common()
    ]


def _pair_entry(pair: Pair, delta: TreeDelta | None, status: str, pair_records: list[dict[str, Any]]) -> dict[str, Any]:
    """The per-pair row of the report."""
    return {
        "artifact": pair.artifact,
        "unit": pair.unit,
        "layer": pair.layer,
        "status": status,
        "files": {
            "changed": len(delta.changed) if delta else 0,
            "added": len(delta.added) if delta else 0,
            "removed": len(delta.removed) if delta else 0,
            # Post-engine changes the baseline-vs-working delta cannot see at all; without their own
            # counter a deleted working-only file would still be invisible in every summary.
            "post_engine_only": sum(1 for r in pair_records if r["kind"].endswith("_after_engine")),
            "baseline": delta.baseline_files if delta else 0,
            "working": delta.working_files if delta else 0,
        },
        "longest_path": delta.longest_path if delta else None,
        "unassessable": len(delta.unassessable) if delta else 0,
        "provenance": dict(Counter(r["provenance"] for r in pair_records)),
        "shapes": sorted({s for r in pair_records for s in r["shapes"]}),
        "baseline_reference_resolves": _reference_resolves(pair),
    }


def _coverage(records: list[dict[str, Any]]) -> dict[str, Any]:
    """How much of the COMPARED corpus the engine baseline can actually speak for.

    ⚠️ The first version checked only that an attribution object existed - not that its file map
    covered anything. Measured (blind review of PR #399): a manifest whose `generated_artifacts.files`
    was `{}` returned `complete` and `usable: true` while every one of its differences was
    `unattributed`. Existence is not coverage, and coverage is not a footnote: it is the difference
    between "the engine wrote all of this" and "we have no idea who wrote any of this".
    """
    attributed = sum(1 for r in records if r["provenance"] != PROV_UNATTRIBUTED)
    return {
        "paths_compared": len(records),
        "paths_attributed": attributed,
        "paths_unattributed": len(records) - attributed,
        "complete": attributed == len(records),
    }


def _overall_status(
    entries: list[dict[str, Any]],
    tampered: list[dict[str, Any]],
    unassessable: list[dict[str, str]],
    evidence: Evidence,
    coverage: dict[str, Any],
) -> str:
    """`untrustworthy` beats `incomplete` beats `complete`.

    A tampered baseline outranks everything: the delta is not merely partial, it is wrong. An
    incomplete run is one whose numbers are real but do not cover the estate - unpaired artifacts,
    unreadable paths, an unattributable bundle, or **any single difference this module could not
    attribute**. Neither ever reports as clean.
    """
    if tampered:
        return STATUS_UNTRUSTWORTHY
    partial = any(e["status"] in {PAIR_NO_BASELINE, PAIR_NO_WORKING, PAIR_UNASSESSABLE} for e in entries)
    if unassessable or not evidence.usable or not coverage["complete"] or partial or not entries:
        return STATUS_INCOMPLETE
    return STATUS_COMPLETE


def harvest(bundle: Path) -> dict[str, Any]:
    """Compare, attribute and classify one bundle. Returns the machine-readable report."""
    evidence = _load_evidence(bundle)

    # ⚠️ The engine baseline is validated INDEPENDENTLY of the delta, and BEFORE it is read. The
    # first version hash-checked only the baseline files still present in the final delta, so
    # replacing `reports/WB.Report` wholesale with a copy of `pbip/WB/WB.Report` erased its own
    # evidence: the two trees then agreed, and the harvest reported `complete` with zero differing
    # files and zero tampering, while `tamper_check()` on the same bundle returned DRIFT.
    baseline_drift = evidence.side_drift(BASELINE_ROOTS)

    entries: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    unassessable: list[dict[str, str]] = []
    git_blind: list[dict[str, Any]] = []

    for pair in discover_pairs(bundle):
        delta = compare_trees(pair.baseline, pair.working) if pair.baseline and pair.working else None
        if delta is not None:
            unassessable.extend(delta.unassessable)
            if delta.longest_path > GIT_READABLE_PATH_MAX:
                git_blind.append(
                    {
                        "artifact": pair.artifact,
                        "unit": pair.unit,
                        "layer": pair.layer,
                        "longest_path": delta.longest_path,
                    }
                )
        pair_records = _difference_records(bundle, pair, delta, evidence) if delta is not None else []
        records.extend(pair_records)
        entries.append(_pair_entry(pair, delta, _pair_status(pair, delta, pair_records), pair_records))

    provenance = Counter(record["provenance"] for record in records)
    tampered = [r for r in records if r["provenance"] == PROV_TAMPERED]
    coverage = _coverage(records)
    status = _overall_status(entries, tampered or baseline_drift, unassessable, evidence, coverage)

    return {
        "version": REPORT_VERSION,
        "generated_at": _utcnow(),
        "bundle": str(bundle),
        "status": status,
        "engine": _engine_metadata(bundle),
        "attribution": {
            "usable": evidence.usable,
            "unavailable_reason": evidence.unavailable_reason,
            "files_recorded": len(evidence.recorded),
            "coverage": coverage,
            "notes": evidence.notes,
        },
        "layers": {
            layer: _layer_summary([e for e in entries if e["layer"] == layer]) for layer in (LAYER_REPORT, LAYER_MODEL)
        },
        "provenance": {name: provenance.get(name, 0) for name in PROVENANCES} | {"differing_files": len(records)},
        "shapes": _shape_rows(records, len(records)),
        "tier_edits": [r for r in records if r["provenance"] == PROV_TIER],
        "baseline_tampered": tampered,
        "baseline_drift": baseline_drift,
        "pairs": entries,
        "unassessable": unassessable,
        "git_blind_spot": {
            "count": len(git_blind),
            "path_max": GIT_READABLE_PATH_MAX,
            "pairs": git_blind,
            "note": (
                "these pairs exceed the longest path git read on the measured corpus; the command"
                " AGENTS.md mandates returns exit 1 with NO stat line for them. They ARE assessed"
                " here - this module compares content in Python, which reads them."
            ),
        },
    }


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
    if report["baseline_drift"]:
        lines.append(
            f"  BASELINE DRIFT        : {len(report['baseline_drift'])} engine-baseline path(s) under"
            f" {'/'.join(r.rstrip('/') for r in BASELINE_ROOTS)} moved after the engine ran."
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
    if report["unassessable"]:
        lines.append(f"  UNASSESSABLE (not passed): {len(report['unassessable'])} path(s) could not be read")
        for record in report["unassessable"][:top]:
            lines.append(f"      {record['reason']}  {record['path']}")
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
    lines += [f"      {name:<18}: {_pct(provenance[name], total)}" for name in PROVENANCES]
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
    for name in PROVENANCES:
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
    if report["unassessable"]:
        lines.append(
            f"- **{len(report['unassessable'])} path(s) could not be read** and are excluded from every count above."
        )
    return "\n".join(lines) + "\n"


def _emit(text: str, stream) -> None:
    """Print one line, degrading only characters this stream cannot encode."""
    try:
        print(text, file=stream)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None)
        if isinstance(encoding, str):
            try:
                print(text.encode(encoding, "backslashreplace").decode(encoding, "replace"), file=stream)
                return
            except LookupError:  # pragma: no cover - an encoding name Python does not know
                pass
        print(text.encode("ascii", "backslashreplace").decode("ascii"), file=stream)


def _exit_code(reports: list[dict[str, Any]]) -> int:
    if any(r["status"] == STATUS_UNTRUSTWORTHY for r in reports):
        return EXIT_UNTRUSTWORTHY
    if any(r["status"] == STATUS_INCOMPLETE for r in reports):
        return EXIT_INCOMPLETE
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Harvest the engine-gap evidence in a migration bundle's baseline/working delta.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("bundles", nargs="+", type=Path, help="migration bundle director(ies)")
    parser.add_argument("--json", type=Path, help="also write the machine-readable report here")
    parser.add_argument("--markdown", type=Path, help="also write an upstream-fileable markdown summary here")
    parser.add_argument("--quiet", action="store_true", help="print only the verdict line")
    parser.add_argument("--warn-only", action="store_true", help="report findings but always exit 0")
    parser.add_argument(
        "--top", type=int, default=DEFAULT_TOP, help=f"rows to name per section (default {DEFAULT_TOP})"
    )
    args = parser.parse_args(argv)

    missing = [str(b) for b in args.bundles if not b.is_dir()]
    if missing:
        _emit(f"ERROR: not a directory: {', '.join(missing)}", sys.stderr)
        return EXIT_USAGE
    if args.top < 1:
        _emit("ERROR: --top must be >= 1", sys.stderr)
        return EXIT_USAGE

    reports = [harvest(bundle.resolve()) for bundle in args.bundles]

    # The machine-readable artifacts are written BEFORE anything is printed: console rendering can
    # fail on a path this terminal cannot encode, and an ordering that printed first would destroy
    # the very output an automated consumer asked for. `--json` is a contract.
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        payload = reports[0] if len(reports) == 1 else {"version": REPORT_VERSION, "bundles": reports}
        args.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text("\n".join(render_markdown(r, args.top) for r in reports), encoding="utf-8")

    for report in reports:
        _emit(f"{report['status']}: {report['bundle']}" if args.quiet else render(report, args.top), sys.stdout)

    return EXIT_OK if args.warn_only else _exit_code(reports)


if __name__ == "__main__":
    raise SystemExit(main())
