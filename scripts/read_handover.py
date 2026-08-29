"""
purpose: print the engine's handover work queue at a size that fits in an agent's context, with
         each category's guidance de-duplicated and the report-side findings (including visuals
         whose bindings were dropped entirely) surfaced instead of buried.
usage:   python scripts/read_handover.py <bundle-or-handover.json>            # queue summary
         python scripts/read_handover.py <target> --category <category>       # full repair detail
         python scripts/read_handover.py <target> --name '<calc name>'        # one calc, in full
         python scripts/read_handover.py <target> --viz [--severity blocking] # report-side queue
         python scripts/read_handover.py <target> --fidelity                  # per-visual evidence
         python scripts/read_handover.py <target> --gate-evidence             # exit 0/1/3 on coverage
         python scripts/read_handover.py <target> [--oracle <report.json>]    # layout drift by axis
         python scripts/read_handover.py <target> [--workbook <name>] [--json <file>]

Why this exists
---------------
A per-workbook handover slice is large - 347 KB for a 60-stub workbook - and most of that bulk is
redundant. It carries everything needed to finish a stubbed calculation in
``model_translation_handoff.requests[]``: the original Tableau ``formula``, the ``fields`` it
references, the ``target_table``, and per-category ``category_guidance``.

The problem is not that the data is unreachable. It is that reading it costs more than it should:

* **The file is too large to open in one go.** A file-read tool refuses it outright (measured:
  *"File too large to read at once (346.9 KB)"*), so every consumer has to recover - by parsing it
  programmatically, or by hunting byte ranges. That recovery works, but it is a round trip every
  agent pays on every workbook, and hunting ranges requires knowing offsets nobody has.
* **A meaningful slice of the size is duplication.** ``category_guidance`` is emitted per REQUEST,
  not per category. Verified across all 38 handovers in ``_bundle-208``: there is exactly ONE
  distinct guidance string per category estate-wide. In the worked example, 60 requests carry
  48,824 bytes of guidance where the 6 categories actually present need 4,049 - so **44,775 bytes,
  12.6% of the 347 KB file, is pure repetition**. Printing it once per category is lossless by
  measurement. (An earlier version of this docstring said "~53 KB" and "most of the bulk"; both
  were overstated. It is a worthwhile saving, not the dominant cost.)
* **The genuinely alarming findings are not the ones a reader lands on.** ``pbip_ref_drops`` marks
  visuals whose every field binding was dropped - they render blank on a report that validates
  clean. There are **15** in the worked example and **26 across 9 of the 38 workbooks**, sitting
  beside a 170-item worklist that does not rank them. No persona or skill surfaced them before this
  tool; whether a human ever looked is not something this file can know, and the earlier "nobody had
  looked at them" is narrowed accordingly. ``measure_filters_needs_review`` is the same class of
  buried report-side signal, but worse for sign-off: the visual renders and the values are wrong.
  This reader turns each dropped aggregate/calculated measure filter into a blocking work item so
  a severity-scoped triage sees the invisible numeric-fidelity risk beside visible report defects.
* **A model-side deferral is swallowed the same way.** ``partitions_needs_review`` is the engine's
  own record of a table whose M partition is a deploy-valid but EMPTY scaffold - e.g. "custom SQL
  native query for this connector isn't verified; complete it manually" - because the upstream
  query couldn't be auto-emitted (issue #326). Nothing surfaced it before this tool: a real
  migration resolved the gap by materializing ~2.3M rows from a packaged extract instead of
  completing the live translation the engine had already named as the needed manual step. This
  reader groups the reasons (a FAMILY, not one sentence) and ranks them ahead of the calc queue,
  because an unresolved partition means the table has ZERO rows, not just an unevaluated cell.

⚠️ **What this tool is NOT.** An earlier version of this docstring claimed that reading the slice
directly failed *silently* - that a truncated read returned ``needs_review[]`` (the same calcs with
5 fields and no formula) while the consumer believed it had the whole queue. **That was wrong and
was retracted.** Measured two ways: the read tool **refuses loudly** rather than truncating, and in a
controlled A/B an agent given only the old "read the handover file" instruction hit that error,
recovered by parsing the JSON, and returned the complete formula. There is no silent-decoy failure
mode. This tool is an ergonomics and triage improvement; it is not a correctness fix, and it should
not be described as one.

``needs_review[]`` is still worth knowing about - it is a strict field-subset of ``requests[]``
(``category``, ``fallback_reason``, ``has_suggestion``, ``name``, ``role``) and is sufficient to
*report* a stub but not to *repair* one - so this tool always works from ``requests[]``.

How ``--max-bytes`` is enforced
-------------------------------
``--max-bytes`` is a STRICT cap on everything a run prints, with exactly one documented exception.
Every view budgets each of its own sections - the category guidance, the emptied-visual block, the
request bodies, the worklist items, the fidelity groups, the cascadable names, the ``--list`` rows
and the truncation banner itself - and whatever will not fit is NAMED, or at minimum COUNTED, in the
output rather than dropped. ``_capped()`` sits over the assembled text as a last-resort net; it
should never fire, so when it does it prints ``HARD CAP`` loudly and the tests treat that string as
a budgeting defect rather than as a pass.

Caps below ``MIN_MAX_BYTES`` are REJECTED (exit 2). At ``--max-bytes 100`` this tool used to emit
738 bytes of truncation banner while reporting that it had honoured the cap; a cap too small to hold
the banner that explains it cannot be honoured, and failing loudly is the only honest answer.

The one exception is ``--name`` resolving to a SINGLE calculation: that calculation is printed in
full, uncapped. It is the escape hatch the truncation banner points at, so capping it would turn the
cap everywhere else into a data-loss bug (measured: all 356 exact-name lookups on a real estate fit,
the largest at 2,152 bytes). An AMBIGUOUS ``--name`` is NOT an exception - it prints the candidate
names, budgeted, and no bodies, because ``--name a`` matching 47 calculations is bulk output wearing
the hatch's clothes (measured: 66,075 bytes on one real file).

What it will NOT tell you
-------------------------
Whether a repair is correct. It surfaces the engine's own material - the source formula and the
category's guidance - and nothing here validates the DAX you write from it. It also cannot see the
model or report on disk: ``check_blank_placeholders.py`` is the gate that catches a stub that
survived into shipped TMDL, and ``check_field_bindings.py`` the one for PBIR references that
resolve to nothing.

Coverage vs findings: ``evidence`` (#371) and ``by_axis`` (#372)
---------------------------------------------------------------
Two engine signals were emitted for months with no consumer on this side. They answer *coverage*
questions, which every other field here structurally cannot:

**``viz_fidelity[].evidence``** (engine >= 2.335.0) is present in the handover and is read here.
``status: rebuilt`` records only that the emitter completed without raising - the engine author is
explicit that it "is a record that our code ran cleanly - it is not a statement that the emitted
``visual.json`` renders". ``evidence`` separates those two claims: ``emitted`` (nothing inspected
the artifact), ``emitted+linted`` (the shipped bytes were linted and no finding names it), and
``lint_failed`` (a finding does). A row with no ``evidence`` key at all is reported as ``unknown``,
never folded into ``emitted``. ``--gate-evidence`` turns that into three exit codes, because two
would force "never examined" to share a code with either a pass or a blocker. ⚠️ ``emitted+linted``
is **still not a render check** - it means the bytes passed a structural lint and nothing more.

**``summary.placement.by_axis``** (engine >= 2.332.0) is NOT in the handover, and this is the
measured finding rather than an assumption. It is produced by ``fidelity_oracle.py``, a **separate
opt-in tool**, into **its own** report (``kind: "tableau-fabric-structural-fidelity"``);
``migrate_estate.py`` never computes placement at all. Verified against engine 2.339.0: ``by_axis``
occurs in **zero** JSON files across the whole local corpus, including a fresh 2.339.0 bundle's
``handover/`` and its estate ``report.json`` (whose ``summary.placement`` is ``null``). So this
reader finds an oracle report beside the bundle - by ``kind``, never by a filename convention
nobody writes to - or takes one from ``--oracle``, and otherwise reports **NOT MEASURED**. Absence
of the block is not absence of drift, and today NOT MEASURED is the honest answer for every bundle
the deterministic pipeline produces on its own.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, NamedTuple

# One cohesive projection CLI; adding every engine handover signal here keeps drills consistent.
# pylint: disable=too-many-lines

DEFAULT_MAX_BYTES = 20_000

# The smallest cap this tool will accept. The truncation banner is content too: its head, its
# closing rule and its "... and N more" line cost ~540 bytes before it names a single item, and a
# view still needs a header on top of that. Below this floor the cap cannot be honoured, and
# pretending otherwise is what made `--max-bytes 100` emit 738 bytes and call it capped.
MIN_MAX_BYTES = 1_500

# Room set aside for the truncation banner before any body content is emitted, whatever the cap.
BANNER_RESERVE_FLOOR = 800

# Mode-specific recovery hints for the truncation banner. `--severity` only affects `--viz`
# (measured: `--fidelity --severity blocking` is byte-identical to `--fidelity`), so a shared
# default advertised a command that does nothing in two of the three views that print the banner.
RECOVERY_BY_NAME = "!!   --name '<name>'   to print any one of them IN FULL, uncapped"
RECOVERY_BY_SEVERITY = "!!   --severity <blocking|high|medium|low>   to work one band at a time"
RECOVERY_BY_JSON = "!!   --json <file>   to write every row to a machine-readable file instead"

# `viz_fidelity[]` rows that recorded no reason. They are still rows, and `--fidelity` claims to
# print the list in full, so they get their own group rather than surviving only as a tier count.
CLEAN_FIDELITY_GROUP = "(no fidelity reason recorded)"

# Order categories by how much judgement they need, so the summary reads as a suggested work order
# rather than an arbitrary dict ordering.
CATEGORY_ORDER = [
    "model_object_parameter",
    "missing_addressing_intent",
    "missing_outer_aggregation",
    "dax_language_gap",
    "type_or_shape_mismatch",
    "unresolved_reference",
    "unsupported_other",
]

SEVERITY_ORDER = ["blocking", "high", "medium", "low"]

MEASURE_FILTER_CATEGORY = "measure_filter_needs_review"
MEASURE_FILTER_MISSING = "not_recorded"
MEASURE_FILTER_NONE = "none"
MEASURE_FILTER_PRESENT = "present"
MEASURE_FILTER_INVALID = "invalid"
MEASURE_FILTER_DEFAULT_NOTE = (
    "worksheet filter on an aggregate/calculated measure was left to review (no faithful slicer "
    "mapping); it changes the values shown -- re-apply it as a visual-level filter in Power BI"
)
MEASURE_FILTER_RISK = "INVISIBLE NUMERIC FIDELITY RISK: visual renders, values are wrong until filter is re-applied."

# `partitions_needs_review[]` (workbook-level, confirmed against a real slice --
# `_bundle-208/handover/Admin_Insights_Starter.json`: `[{"kind": "m_partition", "reason": "...",
# "table": "..."}]`) is the engine's own record of a `_scaffold_review(...)` deferral: a table whose
# M partition is a deploy-valid but EMPTY typed-table scaffold because the upstream query could not
# be auto-emitted -- e.g. "custom SQL native query for this connector isn't verified; complete it
# manually" (connection_to_m.py). It is a FAMILY of reasons, not one sentence -- a neighbouring
# scaffold site uses a different reason for a catalog/database drill that isn't resolvable from the
# .tds -- so this groups by the reason text rather than matching a fixed string.
PARTITION_REVIEW_MISSING = "not_recorded"
PARTITION_REVIEW_NONE = "none"
PARTITION_REVIEW_PRESENT = "present"
PARTITION_REVIEW_INVALID = "invalid"

PBIP_WARNING_PREFIX = "manual attention required: "
PBIP_WARNING_CATEGORY = "pbip_warning"
PBIP_WARNING_MISSING = "not_recorded"
PBIP_WARNING_NONE = "none"
PBIP_WARNING_PRESENT = "present"
PBIP_WARNING_INVALID = "invalid"
PBIP_WARNING_REMEDIATION = {
    "ambiguous_field": "Verify each ambiguous field binding against the Tableau relation name; rebind wrong columns.",
    "no_relationship": "Add the intended model relationship, or document why the orphan table must stay disconnected.",
    "tableau_blend": "Model the Tableau blend explicitly, usually with relationships or COMBINEVALUES composite keys.",
    "max_path": (
        "Move/rebuild under a shorter output root or enable Windows long paths before local Desktop validation."
    ),
    "dangling_refs": "Run/fix field-binding repair; these visuals bind names the model does not contain.",
    "ref_drop": "Restore or intentionally replace the dropped visual binding before report sign-off.",
    "storage_decision": "Make the required storage decision and rebuild; skipped workbook PBIP is not a clean output.",
    "flatfile_not_landed": "Materialize the flat-file data to an absolute path, then rebuild and refresh.",
    "viz_lint": "Fix the PBIR validity violations emitted by the engine before visual sign-off.",
    "measure_rebind": "Verify the rebound measure reference still points at the intended translated measure.",
    "no_report": "Re-run or repair the viz stage; there is no openable report definition to bind.",
    "other": "Inspect the engine warning and route to the owner named by the affected artifact.",
}
PBIP_WARNING_SEVERITY = {
    "ambiguous_field": "blocking",
    "no_relationship": "blocking",
    "tableau_blend": "blocking",
    "no_report": "blocking",
    "dangling_refs": "high",
    "flatfile_not_landed": "high",
    "max_path": "high",
    "ref_drop": "high",
    "storage_decision": "high",
    "viz_lint": "high",
    "measure_rebind": "medium",
    "other": "medium",
}
PBIP_WARNING_PATTERNS = [
    ("is ambiguous within datasource", "ambiguous_field"),
    ("landed with no relationship", "no_relationship"),
    ("tableau blends", "tableau_blend"),
    (".pbip output path", "max_path"),
    ("visual field reference(s) name a model object", "dangling_refs"),
    ("dangling refs", "dangling_refs"),
    ("storage decision", "storage_decision"),
    ("pbir validity violation", "viz_lint"),
    ("no pbir report definition", "no_report"),
]


# `viz_fidelity[].evidence` (engine >= 2.335.0). Verified present in a real 2.339.0 slice:
# `{"evidence": "emitted+linted", "status": "warned", "tier": "degraded", ...}`. It answers a
# question `status` structurally cannot: `status: rebuilt` records that the EMITTER ran cleanly,
# never that anything inspected the bytes it wrote. The engine is explicit about the fail-closed
# rule -- when the lint did not run, every row stays `emitted`, because claiming `emitted+linted`
# on the strength of "nothing looked and found nothing" is the defect, not the fix.
EVIDENCE_EMITTED = "emitted"
EVIDENCE_LINTED = "emitted+linted"
EVIDENCE_LINT_FAILED = "lint_failed"
# Not an engine value: our name for a row that carries no `evidence` key at all (a pre-2.335.0
# bundle). It is deliberately NOT folded into `emitted` - "the emitter ran and nothing looked" and
# "we cannot tell what happened" are different claims, and only one of them is the engine's.
EVIDENCE_UNKNOWN = "unknown"
# Also not an engine value: a `viz_fidelity[]` entry that is not an object at all. It stays in the
# DENOMINATOR - dropping it made a 2-row list report "1 of 1 complete" and exit 0.
EVIDENCE_UNREADABLE = "unreadable_row"

# Loudest first: anything that is not `emitted+linted` outranks it, because the whole point is that
# an unexamined visual must never sort or read like a verified one.
EVIDENCE_ORDER = [EVIDENCE_LINT_FAILED, EVIDENCE_UNREADABLE, EVIDENCE_EMITTED, EVIDENCE_UNKNOWN, EVIDENCE_LINTED]

EVIDENCE_LABEL = {
    EVIDENCE_LINT_FAILED: "LINT FAILED - a PBIR lint finding names this visual",
    EVIDENCE_UNREADABLE: "UNREADABLE - the row is not an object; it cannot be assessed at all",
    EVIDENCE_EMITTED: "NEVER EXAMINED - emitter ran clean; nothing inspected the shipped bytes",
    EVIDENCE_UNKNOWN: "NOT RECORDED - no evidence key (bundle predates engine 2.335.0)",
    EVIDENCE_LINTED: "linted - shipped bytes were linted and no finding names it",
}

# The only value that supports a structural pass. `emitted+linted` is STILL not a render check.
EVIDENCE_VERIFIED = EVIDENCE_LINTED

FIDELITY_EVIDENCE_NONE = "none"
FIDELITY_EVIDENCE_MISSING = "not_recorded"
FIDELITY_EVIDENCE_PRESENT = "present"
FIDELITY_EVIDENCE_INVALID = "invalid"

# `summary.placement.by_axis` (engine >= 2.332.0). ⚠️ MEASURED, NOT INFERRED: this block is emitted
# by `fidelity_oracle.py`, a SEPARATE opt-in tool, into ITS report - `migrate_estate.py` never
# computes placement, so no handover slice and no estate `report.json` carries it. Verified against
# engine 2.339.0: zero occurrences of `by_axis` in any JSON under `_runs/`. So the honest consumer
# reads it from a fidelity-oracle report found beside the bundle (or named with `--oracle`), and
# reports NOT MEASURED - never zero drift - when there is none.
ORACLE_KIND = "tableau-fabric-structural-fidelity"
ORACLE_SNIFF_BYTES = 4096

LAYOUT_DRIFT_NOT_MEASURED = "not_measured"
LAYOUT_DRIFT_AXIS_BLIND = "axis_blind"
LAYOUT_DRIFT_EXACT = "pixel_exact"
LAYOUT_DRIFT_EDGE_ONLY = "edge_or_size_drift"
LAYOUT_DRIFT_PRESENT = "present"
LAYOUT_DRIFT_INVALID = "invalid"

# The states in which a per-axis number was genuinely produced. `EDGE_ONLY` belongs here: the axes
# WERE measured; it is the far edges the axis rollup cannot see.
LAYOUT_DRIFT_MEASURED = (LAYOUT_DRIFT_EXACT, LAYOUT_DRIFT_EDGE_ONLY, LAYOUT_DRIFT_PRESENT)

# Byte ceiling for the compressed drift line. Bytes, not characters: `--max-bytes` is a byte cap,
# and a 24-CHARACTER clip of a CJK filename measured 101 bytes against a bound believed to be ~70.
# The value is the previous EFFECTIVE width, now enforced in the right unit rather than assumed -
# raising it is not free, because `render_default`'s terse pass is what keeps the floor cap
# honourable and this line is part of its fixed tail.
TERSE_DRIFT_LINE_MAX_BYTES = 72

# The engine's own vocabulary for the signed direction counts, kept verbatim so a reader can move
# between our line and the oracle's markdown without re-learning which sign means which way.
AXIS_DIRECTIONS = {"x": ("right", "left"), "y": ("down", "up")}

# Exit codes. Matches the sibling gates in this folder (`check_connection_fidelity.py`,
# `check_pbir_layout.py`, `check_empty_model.py`): 0 ok, 1 the defect this gate names, 2 usage,
# 3 "could not verify". 3 exists because #366 shipped a SKIPPED result that read as a pass, which
# is the same conflation #371 is about - a visual nothing examined is not a visual that passed.
EXIT_OK = 0
EXIT_EVIDENCE_BLOCKED = 1
EXIT_USAGE = 2
EXIT_NOT_VERIFIED = 3


class HandoverError(RuntimeError):
    """A target that cannot be resolved to at least one workbook payload."""


class OracleSource(NamedTuple):
    """A fidelity-oracle report and where it was found, kept together so the numbers are always
    citable. An empty instance means "no oracle report", which is a first-class answer here."""

    payload: dict | None = None
    path: Path | None = None


# --------------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HandoverError(f"{path} is not valid JSON: {exc}") from exc


def _workbooks_from_payload(payload: Any, source: Path) -> list[tuple[str, dict, Path]]:
    """Accept either a handover slice (``{estate, workbook}``) or an estate ``report.json``."""
    if not isinstance(payload, dict):
        raise HandoverError(f"{source}: expected a JSON object at the top level")

    if isinstance(payload.get("workbook"), dict):
        wb = payload["workbook"]
        return [(wb.get("name") or source.stem, wb, source)]

    if isinstance(payload.get("workbooks"), list):
        out = []
        for i, wb in enumerate(payload["workbooks"]):
            if isinstance(wb, dict):
                out.append((wb.get("name") or f"workbook[{i}]", wb, source))
        return out

    raise HandoverError(f"{source}: no 'workbook' or 'workbooks' key - is this a handover slice or a report.json?")


def load_workbooks(target: Path) -> list[tuple[str, dict, Path]]:
    """Resolve a file or bundle directory to ``(name, workbook_payload, source_path)`` triples."""
    if not target.exists():
        raise HandoverError(f"{target} does not exist")

    if target.is_file():
        return _workbooks_from_payload(_read_json(target), target)

    handover_dir = target / "handover" if (target / "handover").is_dir() else target
    slices = sorted(p for p in handover_dir.glob("*.json"))
    if slices:
        found: list[tuple[str, dict, Path]] = []
        for path in slices:
            try:
                found.extend(_workbooks_from_payload(_read_json(path), path))
            except HandoverError:
                continue  # a stray JSON file in the folder is not an error
        if found:
            return found

    report = target / "report.json"
    if report.is_file():
        return _workbooks_from_payload(_read_json(report), report)

    raise HandoverError(
        f"{target}: found no handover/*.json slices and no report.json. "
        "Point at a bundle directory, a handover slice, or an estate report.json."
    )


def _sniff_oracle(path: Path) -> bool:
    """Cheap prefix test before paying to parse a file that may be a 347 KB handover slice.

    `kind` is the FIRST key the oracle writes (`_assemble_report` returns it first, and `json.dumps`
    preserves insertion order), so the marker is at the very start of the file. Reading 4 KB to
    reject a slice is the difference between scanning a bundle for free and re-parsing every
    workbook in it.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(ORACLE_SNIFF_BYTES)
    except OSError:
        return False
    return ORACLE_KIND.encode("utf-8") in head


def _oracle_candidates(directory: Path) -> list[tuple[Path, dict]]:
    """Every fidelity-oracle report directly inside ``directory``, identified by CONTENT.

    By `kind`, never by filename: `fidelity_oracle.py --out` takes an arbitrary path, so there is
    no filename convention to rely on and inventing one would only find reports we ourselves wrote.
    """
    if not directory.is_dir():
        return []
    out: list[tuple[Path, dict]] = []
    for path in sorted(directory.glob("*.json")):
        if not _sniff_oracle(path):
            continue
        try:
            payload = _read_json(path)
        except HandoverError:
            continue
        if isinstance(payload, dict) and payload.get("kind") == ORACLE_KIND:
            out.append((path, payload))
    return out


def find_oracle_report(
    target: Path, source: Path, wb_name: str, explicit: Path | None = None, workbook_count: int = 1
) -> OracleSource:
    """Locate the fidelity-oracle report carrying ``summary.placement.by_axis`` for one workbook.

    Returns an :class:`OracleSource`, empty when there is none - which is the answer for every
    bundle the deterministic pipeline produces today, because the oracle is a separate opt-in tool.
    An explicit ``--oracle`` wins; otherwise the bundle root, a ``fidelity/`` subfolder and the
    directory the slice itself came from are scanned, in that order.

    ⚠️ ``workbook_count`` is load-bearing, not decoration. A report whose filename names the
    workbook always wins. An arbitrarily-named SINGLE candidate is accepted **only when the target
    holds one workbook**: in a multi-workbook bundle where the oracle was run for A alone, the old
    unconditional singleton fallback handed A's numbers to B - and a pixel-exact A made an entirely
    unmeasured B read as verified. Ambiguity is refused rather than guessed, because attributing
    another workbook's measurements to this one is worse than reporting nothing.

    The real fix belongs upstream: the oracle report should carry the workbook identity it measured,
    so this is an identity check rather than a filename inference. Filed as the successor to #182.
    """
    directories: list[Path] = []
    if explicit is not None:
        if explicit.is_file():
            payload = _read_json(explicit)
            if not (isinstance(payload, dict) and payload.get("kind") == ORACLE_KIND):
                raise HandoverError(f"{explicit}: not a fidelity-oracle report (expected kind {ORACLE_KIND!r})")
            return OracleSource(payload, explicit)
        if not explicit.is_dir():
            raise HandoverError(f"{explicit} does not exist")
        directories.append(explicit)
    else:
        root = target if target.is_dir() else target.parent
        directories += [root, root / "fidelity", source.parent]

    seen: set[Path] = set()
    found: list[tuple[Path, dict]] = []
    for directory in directories:
        resolved = directory.resolve() if directory.exists() else directory
        if resolved in seen:
            continue
        seen.add(resolved)
        found += _oracle_candidates(directory)

    if not found:
        return OracleSource()
    named = [(p, d) for p, d in found if p.stem.lower() == wb_name.lower()]
    if not named:
        named = [(p, d) for p, d in found if wb_name.lower() in p.stem.lower()]
    if len(named) == 1:
        return OracleSource(named[0][1], named[0][0])
    if not named and len(found) == 1 and workbook_count == 1:
        return OracleSource(found[0][1], found[0][0])
    return OracleSource()


def select_workbook(found: list[tuple[str, dict, Path]], wanted: str | None) -> tuple[str, dict, Path]:
    """Pick one workbook, failing loudly rather than guessing when the choice is ambiguous."""
    if wanted:
        matches = [t for t in found if t[0].lower() == wanted.lower()]
        if not matches:
            matches = [t for t in found if wanted.lower() in t[0].lower()]
        if not matches:
            names = ", ".join(sorted(t[0] for t in found)[:20])
            raise HandoverError(f"no workbook matching {wanted!r}. Available: {names}")
        if len(matches) > 1:
            names = ", ".join(sorted(t[0] for t in matches))
            raise HandoverError(f"{wanted!r} is ambiguous - matches: {names}")
        return matches[0]

    if len(found) == 1:
        return found[0]

    names = ", ".join(sorted(t[0] for t in found)[:20])
    raise HandoverError(f"{len(found)} workbooks found - pass --workbook <name>. Available: {names}")


# --------------------------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------------------------


def handoff_of(wb: dict) -> dict:
    """``model_translation_handoff`` can legitimately be absent or null (a workbook with no calcs)."""
    h = wb.get("model_translation_handoff")
    return h if isinstance(h, dict) else {}


def requests_of(wb: dict) -> list[dict]:
    """The real work queue: every stubbed calc WITH its formula, fields, target table and guidance."""
    reqs = handoff_of(wb).get("requests")
    return [r for r in reqs if isinstance(r, dict)] if isinstance(reqs, list) else []


def guidance_by_category(reqs: list[dict]) -> dict[str, str]:
    """Collapse the per-request guidance to one block per category.

    Verified lossless on ``_bundle-208``: exactly one distinct string per category across all 38
    handovers. If that ever stops holding, the longest variant wins and the count is reported by
    ``--audit-guidance`` rather than being silently dropped.
    """
    best: dict[str, str] = {}
    for r in reqs:
        cat = r.get("category") or "uncategorised"
        text = (r.get("category_guidance") or "").strip()
        if text and len(text) > len(best.get(cat, "")):
            best[cat] = text
    return best


def category_counts(reqs: list[dict]) -> dict[str, int]:
    """How many requests sit in each category, which is what decides the work order."""
    counts: dict[str, int] = {}
    for r in reqs:
        cat = r.get("category") or "uncategorised"
        counts[cat] = counts.get(cat, 0) + 1
    return counts


def ordered_categories(counts: dict[str, int]) -> list[str]:
    """Known categories in judgement order first, then anything the engine adds later, sorted."""
    known = [c for c in CATEGORY_ORDER if c in counts]
    rest = sorted(c for c in counts if c not in CATEGORY_ORDER)
    return known + rest


# --------------------------------------------------------------------------------------------
# Rendering - model side
# --------------------------------------------------------------------------------------------


def _blen(text: str) -> int:
    """UTF-8 byte length. Budgeting on ``len()`` counts CHARACTERS, which under-counts every
    non-ASCII formula (a real one here carries U+25B2) and lets `--max-bytes` be quietly exceeded."""
    return len(text.encode("utf-8"))


def _clip(text: str, limit: int = 100) -> str:
    """Bound a header built from user-controlled text.

    Workbook names, category names and source paths all come from the payload, so a heading is only
    as short as the estate lets it be. A 400-character workbook name spent an entire small cap on
    the title alone and left every section below it budgeting against nothing. Clipping is safe here
    because a name in a HEADING is identity, not content - the full name is one `--list` away, and
    the request bodies below are never clipped.
    """
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _fit_lines(lines: list[str], budget: int, more: str) -> list[str]:
    """Take as many whole lines as ``budget`` allows, then say plainly how many were withheld.

    For the lists where a full truncation banner would cost more than the names it replaces - the
    cascadable stubs, the emptied-visual rows, the `--list` rows, the `--name` candidate list. The
    count is never dropped while there is room to print it, and the tail is reserved BEFORE the line
    it would replace, so appending it can never be what breaks the cap.

    ``more`` is a format string taking ``{n}``, the number of lines not shown.
    """
    kept: list[str] = []
    for i, line in enumerate(lines):
        tail = more.format(n=len(lines) - i)
        if _blen(line) + 1 + _blen(tail) + 1 > budget:
            return kept + ([tail] if _blen(tail) + 1 <= budget else [])
        kept.append(line)
        budget -= _blen(line) + 1
    return kept


def _capped(text: str, max_bytes: int) -> str:
    """Last-resort enforcement of ``--max-bytes`` over the fully assembled output.

    Every renderer budgets its own sections, so this should never fire. It exists because a cap that
    holds only while every renderer is correct is not a cap - and three separate paths (the banner
    at a tiny cap, oversized category guidance, the emptied-visual block) were each unbounded at
    some point while the tool reported it had capped. Firing is a DEFECT, not a feature, so it says
    ``HARD CAP`` where a reader and a test can both see it.
    """
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    rule = "!" * 78
    notice = f"\n{rule}\n!! HARD CAP: output exceeded --max-bytes={max_bytes} and was CUT HERE.\n{rule}"
    keep = max_bytes - _blen(notice)
    if keep <= 0:
        return notice.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")
    return raw[:keep].decode("utf-8", "ignore") + notice


def render_request(r: dict, index: int, total: int) -> str:
    """One request, in full. The formula is never abbreviated - it is the point of the tool."""
    lines = [
        f"[{index}/{total}] {r.get('name') or '(unnamed)'}",
        f"    role         : {r.get('role') or '?'}",
        f"    target_table : {r.get('target_table') or '?'}",
        f"    reason       : {r.get('fallback_reason') or '(none recorded)'}",
    ]
    if r.get("has_suggestion"):
        lines.append("    NOTE         : engine recorded a suggestion for this calc")

    fields = r.get("fields")
    if isinstance(fields, list) and fields:
        lines.append("    fields       :")
        for f in fields:
            if not isinstance(f, dict):
                lines.append(f"        - {f}")
                continue
            # Every recorded attribute is repair-relevant: `table`/`column` say WHERE to bind,
            # `type` constrains the DAX, and `references_formula` marks a dependency on another
            # stub - which decides ordering. Printing only caption+kind dropped all of it.
            head = f"        - {f.get('caption', '?')}  ({f.get('kind', '?')})"
            src = ".".join(str(f[k]) for k in ("table", "column") if f.get(k))
            if src:
                head += f"  [{src}]"
            if f.get("type"):
                head += f"  type={f['type']}"
            lines.append(head)
            if f.get("references_formula"):
                lines.append("          ^ references another calc's formula - repair that one first")

    formula = (r.get("formula") or "").rstrip()
    if formula:
        lines.append("    source formula:")
        lines.extend("        " + ln for ln in formula.splitlines())
    else:
        lines.append("    source formula: (none recorded)")
    return "\n".join(lines)


def _guidance_block(reqs: list[dict], category: str, max_bytes: int, budget: int) -> list[str]:
    """The category's guidance, printed once - or a loud one-liner when it alone blows the cap.

    Emitting it before budgeting was the second unbounded path: an 8,000-byte cap with 9,000 bytes
    of guidance emitted 9,523 bytes and still printed a truncation banner claiming the cap held.
    """
    text = guidance_by_category(reqs).get(category, "(no guidance recorded for this category)")
    block = ["--- GUIDANCE (applies to every request below; printed once) ---", text, ""]
    cost = sum(_blen(x) + 1 for x in block)
    if cost <= budget:
        return block
    return [
        f"--- GUIDANCE WITHHELD ({cost} bytes, more than --max-bytes={max_bytes} can hold) ---",
        f"    get it with: --max-bytes {max_bytes + cost}",
        "",
    ]


def render_category(wb_name: str, reqs: list[dict], category: str, max_bytes: int) -> str:
    """Guidance once, then every request in that category, with a loud stop if it will not fit."""
    selected = [r for r in reqs if (r.get("category") or "uncategorised") == category]
    if not selected:
        present = ", ".join(f"{c} ({n})" for c, n in sorted(category_counts(reqs).items()))
        return f"No requests in category {category!r} for {wb_name}.\nPresent: {present or '(none)'}"

    out = [f"=== {_clip(wb_name)} - {_clip(category)} - {len(selected)} request(s) ===", ""]
    banner_reserve = max(BANNER_RESERVE_FLOOR, max_bytes // 4)
    budget = max_bytes - sum(_blen(x) + 1 for x in out) - banner_reserve

    guidance = _guidance_block(reqs, category, max_bytes, budget - _blen("--- REQUESTS ---") - 1)
    out += guidance + ["--- REQUESTS ---"]
    budget -= sum(_blen(x) + 1 for x in guidance) + _blen("--- REQUESTS ---") + 1

    shown = 0
    for i, r in enumerate(selected, 1):
        block = render_request(r, i, len(selected))
        # No first-item bypass. `if shown and ...` emitted request 1 unconditionally, so a single
        # large request could blow any cap - including a 100-byte one - while still reporting
        # that it had honoured it.
        if _blen(block) + 1 > budget:
            break
        out.append(block)
        out.append("")
        budget -= _blen(block) + 2
        shown += 1

    if shown < len(selected):
        remaining = [r.get("name") or "(unnamed)" for r in selected[shown:]]
        out += _truncation_banner(max_bytes, shown, remaining, banner_reserve, recovery=RECOVERY_BY_NAME)
    return "\n".join(out)


def render_named(wb_name: str, reqs: list[dict], name: str, max_bytes: int) -> str:
    """One calculation in full, with its category guidance - the escape hatch from truncation.

    ``--max-bytes`` is deliberately IGNORED once a name resolves to a single calculation: this is
    the command the truncation banner points at, so capping it would make the cap elsewhere a
    data-loss bug. An ambiguous SUBSTRING match is a different thing wearing the same clothes -
    ``--name a`` returned 47 calculations and 66,075 bytes on one real file - so it lists candidate
    names, budgeted, and prints no bodies at all.
    """
    exact = [r for r in reqs if (r.get("name") or "").lower() == name.lower()]
    matches = exact or [r for r in reqs if name.lower() in (r.get("name") or "").lower()]
    if not matches:
        return f"No request named {name!r} in {wb_name}. Run without --name to list the queue."

    if not exact and len(matches) > 1:
        head = [
            f"=== {_clip(wb_name)} - {name!r} is AMBIGUOUS: {len(matches)} calculation(s) match ===",
            "",
            "--name prints ONE calculation in full and is the only uncapped view, so it will not",
            "dump every match. Re-run with one of these names (an exact name always wins):",
            "",
        ]
        budget = max_bytes - sum(_blen(x) + 1 for x in head)
        rows = [f"    {r.get('name') or '(unnamed)'}" for r in matches]
        more = "    ... and {n} more match(es) not named here; narrow --name or raise --max-bytes"
        return "\n".join(head + _fit_lines(rows, budget, more))

    out = []
    for r in matches:
        cat = r.get("category") or "uncategorised"
        out += [
            f"=== {wb_name} - {r.get('name')} ({cat}) ===",
            "",
            "--- GUIDANCE ---",
            guidance_by_category(reqs).get(cat, "(none recorded)"),
            "",
            render_request(r, 1, 1),
            "",
        ]
    return "\n".join(out)


def _category_table(reqs: list[dict], counts: dict[str, int]) -> list[str]:
    """The per-category count table, with an honest size estimate for each `--category` call."""
    rows = [f"    {'category':<28}{'n':>4}   detail size   next step"]
    for cat in ordered_categories(counts):
        detail = sum(_blen(render_request(r, 1, 1)) for r in reqs if r.get("category") == cat)
        rows.append(f"    {cat:<28}{counts[cat]:>4}   ~{detail // 1024 + 1:>4} KB      --category {cat}")
    return rows


def _cascadable_lines(handoff: dict, budget: int) -> list[str]:
    """Stubs that depend on other stubs. Repairing the outer one first still yields BLANK()."""
    triage = handoff.get("triage") if isinstance(handoff.get("triage"), dict) else {}
    cascadable = triage.get("cascadable")
    if not (isinstance(cascadable, list) and cascadable):
        return []
    head = [
        f"    CASCADABLE ({len(cascadable)}): these stubs depend on other stubs - fix in dependency order,",
        "    innermost first, or the outer one will still evaluate to BLANK():",
    ]
    # The heading is content too. Emitting it unconditionally into a budget already spent by a long
    # workbook name is how a "budgeted" section still overshoots; the count survives either way.
    if sum(_blen(x) + 1 for x in head) + 1 > budget:
        short = f"    CASCADABLE ({len(cascadable)}): not shown here - raise --max-bytes or use --json"
        return [short, ""] if _blen(short) + 2 <= budget else []
    rows = [f"      - {n}" for n in cascadable]
    more = "      ... and {n} more not named here; raise --max-bytes or use --json"
    # `- 1` for the trailing blank line this returns, which the caller costs like any other line.
    return head + _fit_lines(rows, budget - sum(_blen(x) + 1 for x in head) - 1, more) + [""]


def partitions_needs_review_status(wb: dict) -> tuple[str, list[dict]]:
    """Return whether the engine recorded any M-partition scaffold deferral, without treating an
    absent key as zero. Mirrors `measure_filter_status`/`pbip_warning_status` on purpose: this repo
    has shipped a false "0 deferrals" three times (#276, #299, #309) from exactly this conflation."""
    if "partitions_needs_review" not in wb:
        return PARTITION_REVIEW_MISSING, []
    rows = wb.get("partitions_needs_review")
    if not isinstance(rows, list):
        return PARTITION_REVIEW_INVALID, []
    rows = [r for r in rows if isinstance(r, dict) and (r.get("reason") or "").strip()]
    if not rows:
        return PARTITION_REVIEW_NONE, []
    return PARTITION_REVIEW_PRESENT, rows


def _partition_review_groups(rows: list[dict]) -> dict[str, list[str]]:
    """Group by the REASON text -- a family of deferrals, not one sentence -- tables listed under it.

    The engine repeats the same reason once per affected table (e.g. one Snowflake datasource with
    6 custom-SQL tables emits the identical sentence 6 times); printing it once per DISTINCT reason,
    with every table it covers, is the same de-duplication `guidance_by_category` already does for
    `category_guidance`.
    """
    grouped: dict[str, list[str]] = {}
    for row in rows:
        reason = (row.get("reason") or "").strip()
        grouped.setdefault(reason, []).append(str(row.get("table") or "?"))
    return grouped


def _partition_review_summary_line(wb: dict) -> str | None:
    """One-liner for the default view. `None` only for an explicit, present, empty list -- MISSING
    and INVALID both print a loud line so absence never reads as a clean zero."""
    status, rows = partitions_needs_review_status(wb)
    if status == PARTITION_REVIEW_PRESENT:
        groups = _partition_review_groups(rows)
        return (
            f"        !! {len(rows)} table partition(s) across {len(groups)} distinct reason(s) NEED MANUAL "
            "COMPLETION -- the engine emitted a deploy-valid but EMPTY scaffold; see the reason(s) below"
        )
    if status == PARTITION_REVIEW_MISSING:
        return "        partition scaffolds: NOT RECORDED (key missing; this is not a zero-deferral signal)"
    if status == PARTITION_REVIEW_INVALID:
        return "        partition scaffolds: INVALID SHAPE (key present but not a list; inspect raw handover)"
    return None


def _partition_review_group_block(reason: str, tables: list[str]) -> str:
    """One reason, printed ONCE, with every affected table listed under it."""
    return "\n".join([f"    REASON: {reason}", f"        tables: {', '.join(sorted(tables))}"])


def _partition_review_block(wb: dict, budget: int) -> list[str]:
    """The full, budgeted breakdown backing `_partition_review_summary_line` -- ranked ahead of the
    calc-stub queue in `_model_section` because unlike a stub measure (which still evaluates, just
    to BLANK), this scaffold's table has ZERO rows and carries a named manual completion step. A
    real migration resolved this gap by materializing ~2.3M rows from a packaged extract instead of
    completing the live translation the engine had already named (issue #326) -- exactly the outcome
    surfacing this reason exists to prevent.
    """
    status, rows = partitions_needs_review_status(wb)
    if status != PARTITION_REVIEW_PRESENT:
        return []
    groups = _partition_review_groups(rows)
    head = f"    --- {len(rows)} TABLE PARTITION(S) NEED MANUAL COMPLETION ({len(groups)} reason(s)) ---"
    blocks = [_partition_review_group_block(reason, groups[reason]) for reason in sorted(groups)]
    more = "    ... and {n} more reason group(s) not named here; raise --max-bytes or use --json"
    return [head] + _fit_lines(blocks, budget - _blen(head) - 1, more) + [""]


def _model_section(wb_name: str, wb: dict, reqs: list[dict], target: Path, budget: int) -> list[str]:
    """Model-side summary: partition scaffolds first, then coverage, the per-category queue, and
    any cascade ordering constraint."""
    handoff = handoff_of(wb)
    summary = handoff.get("summary") if isinstance(handoff.get("summary"), dict) else {}

    out = [f"=== HANDOVER QUEUE - {_clip(wb_name)} ===", f"source: {_clip(str(target))}", ""]

    # Ranked FIRST, ahead of the calc-stub queue: an unresolved M partition means the table has NO
    # rows at all, which is a more severe defect than a stub calc (which still evaluates to BLANK).
    partition_line = _partition_review_summary_line(wb)
    if partition_line:
        out.append(partition_line)
    partition_block = _partition_review_block(wb, budget - sum(_blen(x) + 1 for x in out))
    out += partition_block
    if partition_line and not partition_block:
        out.append("")

    if not reqs:
        return out + ["MODEL: no residual calculations in the handover queue.", ""]

    cov = summary.get("coverage_pct")
    cov_txt = f", coverage {cov}%" if cov is not None else ""
    out += [
        f"MODEL: {summary.get('total', '?')} calcs - {summary.get('translated', '?')} translated, "
        f"{summary.get('stub', len(reqs))} stubbed{cov_txt}",
        f"       {len(reqs)} request(s) in the queue, by category:",
        "",
    ]
    out += _category_table(reqs, category_counts(reqs))
    out.append("")
    out += _cascadable_lines(handoff, budget - sum(_blen(x) + 1 for x in out))
    return out


# --------------------------------------------------------------------------------------------
# Rendering - report side
# --------------------------------------------------------------------------------------------


def worklist_of(wb: dict) -> dict:
    """The report-side counterpart of `requests_of` - absent on workbooks with no emitted report."""
    rw = wb.get("remediation_worklist")
    return rw if isinstance(rw, dict) else {}


def _emptied_visuals(wb: dict) -> list[dict]:
    drops = wb.get("pbip_ref_drops")
    if not isinstance(drops, list):
        return []
    return [d for d in drops if isinstance(d, dict) and d.get("emptied")]


def measure_filter_status(wb: dict) -> tuple[str, dict]:
    """Return whether the engine recorded the measure-filter audit, without treating missing as zero."""
    if "measure_filters_needs_review" not in wb:
        return MEASURE_FILTER_MISSING, {}
    payload = wb.get("measure_filters_needs_review")
    if not isinstance(payload, dict):
        return MEASURE_FILTER_INVALID, {}
    count = payload.get("count")
    worksheets = payload.get("worksheets")
    if not count and not worksheets:
        return MEASURE_FILTER_NONE, payload
    return MEASURE_FILTER_PRESENT, payload


def _measure_filter_note(payload: dict) -> str:
    note = (payload.get("note") or "").strip()
    return note or MEASURE_FILTER_DEFAULT_NOTE


def _measure_filter_work_items(wb: dict) -> list[dict]:
    """Synthesize dropped measure filters as report worklist items, because they change numbers."""
    status, payload = measure_filter_status(wb)
    if status != MEASURE_FILTER_PRESENT:
        return []

    note = _measure_filter_note(payload)
    worksheets = payload.get("worksheets")
    rows = [r for r in worksheets if isinstance(r, dict)] if isinstance(worksheets, list) else []
    count = payload.get("count")
    if not rows and count:
        rows = [{"worksheet": f"{count} worksheet filter(s)", "reason": note}]

    out = []
    for row in rows:
        reason = (row.get("reason") or note).strip()
        out.append(
            {
                "category": MEASURE_FILTER_CATEGORY,
                "severity": "blocking",
                "reason": f"{MEASURE_FILTER_RISK} {reason}",
                "remediation": f"{note} ({MEASURE_FILTER_RISK})",
                "worksheet": row.get("worksheet") or "?",
            }
        )
    return out


def pbip_warning_status(wb: dict) -> tuple[str, list[str]]:
    """Return whether PBIP warnings were recorded, without treating absence as clean."""
    if "pbip_warnings" not in wb:
        return PBIP_WARNING_MISSING, []
    warnings = wb.get("pbip_warnings")
    if not isinstance(warnings, list):
        return PBIP_WARNING_INVALID, []
    rows = [str(w) for w in warnings if str(w).strip()]
    if not rows:
        return PBIP_WARNING_NONE, []
    return PBIP_WARNING_PRESENT, rows


def _pbip_warning_family(warning: str) -> str:
    """Classify the engine's free-text PBIP warning into a stable report queue family."""
    lower = warning.casefold()
    for token, family in PBIP_WARNING_PATTERNS:
        if token in lower:
            return family
    if "max_path" in lower:
        return "max_path"
    if "dropped" in lower and "reference(s)" in lower:
        return "ref_drop"
    if "flat-file" in lower and "not landed" in lower:
        return "flatfile_not_landed"
    if "rebound" in lower and "measure reference" in lower:
        return "measure_rebind"
    return "other"


def _pbip_warning_subject(family: str, warning: str, index: int) -> str:
    """Short identity for the drill-down line; the full warning remains in `reason`."""
    if family == "no_relationship" and "table '" in warning:
        return "table " + warning.split("table '", 1)[1].split("'", 1)[0]
    if family == "ambiguous_field" and "field '" in warning:
        return "field " + warning.split("field '", 1)[1].split("'", 1)[0]
    if family == "tableau_blend" and "Tableau BLENDS " in warning:
        return warning.split(" on ", 1)[0].replace(PBIP_WARNING_PREFIX, "")
    if family == "max_path":
        return "workbook .pbip path"
    if family == "ref_drop" and "visual '" in warning:
        return "visual " + warning.split("visual '", 1)[1].split("'", 1)[0]
    return f"PBIP warning {index}"


def _pbip_warning_family_counts(warnings: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for warning in warnings:
        family = _pbip_warning_family(warning)
        counts[family] = counts.get(family, 0) + 1
    return counts


def _pbip_warning_work_items(wb: dict) -> list[dict]:
    """Synthesize PBIP warnings as report worklist items, grouped by defect family."""
    status, warnings = pbip_warning_status(wb)
    if status != PBIP_WARNING_PRESENT:
        return []

    out = []
    for index, warning in enumerate(warnings, start=1):
        family = _pbip_warning_family(warning)
        clean = warning[len(PBIP_WARNING_PREFIX) :] if warning.startswith(PBIP_WARNING_PREFIX) else warning
        out.append(
            {
                "category": f"{PBIP_WARNING_CATEGORY}_{family}",
                "severity": PBIP_WARNING_SEVERITY.get(family, "medium"),
                "reason": clean,
                "remediation": PBIP_WARNING_REMEDIATION.get(family, PBIP_WARNING_REMEDIATION["other"]),
                "worksheet": _pbip_warning_subject(family, warning, index),
            }
        )
    return out


def report_items_of(wb: dict) -> list[dict]:
    """Report-side work queue, including invisible numeric-fidelity engine warnings."""
    raw = worklist_of(wb).get("items") or []
    items = [i for i in raw if isinstance(i, dict)] if isinstance(raw, list) else []
    return items + _measure_filter_work_items(wb) + _pbip_warning_work_items(wb)


def _severity_counts(items: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        sev = str(item.get("severity") or "?")
        counts[sev] = counts.get(sev, 0) + 1
    return counts


def _measure_filter_summary_line(wb: dict) -> str | None:
    status, payload = measure_filter_status(wb)
    if status == MEASURE_FILTER_PRESENT:
        count = payload.get("count") or len(_measure_filter_work_items(wb))
        return (
            f"        !! {count} dropped aggregate/calculated measure filter(s) - "
            "INVISIBLE numeric-fidelity risk; values change while visuals still render"
        )
    if status == MEASURE_FILTER_MISSING:
        return "        measure filters: NOT RECORDED (key missing; this is not a zero-dropped signal)"
    if status == MEASURE_FILTER_INVALID:
        return "        measure filters: INVALID SHAPE (key present but not an object; inspect raw handover)"
    return None


def _pbip_warning_summary_line(wb: dict) -> str | None:
    status, warnings = pbip_warning_status(wb)
    if status == PBIP_WARNING_PRESENT:
        counts = _pbip_warning_family_counts(warnings)
        families = " | ".join(f"{k} {counts[k]}" for k in sorted(counts))
        return f"        !! {len(warnings)} PBIP warning(s): {families}"
    if status == PBIP_WARNING_MISSING:
        return "        pbip warnings: NOT RECORDED (key missing; this is not a zero-warning signal)"
    if status == PBIP_WARNING_INVALID:
        return "        pbip warnings: INVALID SHAPE (key present but not a list; inspect raw handover)"
    return "        pbip warnings: none recorded (key present and empty)"


def _fidelity_tier_line(fidelity: list) -> str | None:
    """The existing tier roll-up, extracted so `_report_section` stays under pylint's local cap."""
    tiers: dict[str, int] = {}
    for v in fidelity:
        if isinstance(v, dict):
            tiers[v.get("tier") or "?"] = tiers.get(v.get("tier") or "?", 0) + 1
    if not tiers:
        return None
    return "        fidelity: " + " | ".join(f"{k} {n}" for k, n in sorted(tiers.items()))


def _severity_line(items: list[dict]) -> str | None:
    """The existing severity roll-up, extracted for the same reason as `_fidelity_tier_line`."""
    by_sev = _severity_counts(items)
    if not by_sev:
        return None
    parts = [f"{s} {by_sev[s]}" for s in SEVERITY_ORDER if s in by_sev]
    parts += [f"{s} {n}" for s, n in sorted(by_sev.items()) if s not in SEVERITY_ORDER]
    return "        severity: " + " | ".join(parts)


def _report_section(wb: dict, oracle: OracleSource | None = None, terse: bool = False) -> list[str]:
    oracle = oracle or OracleSource()
    items = report_items_of(wb)
    fidelity = wb.get("viz_fidelity") if isinstance(wb.get("viz_fidelity"), list) else []
    measure_line = _measure_filter_summary_line(wb)
    pbip_line = _pbip_warning_summary_line(wb)
    drift_lines = _layout_drift_lines(wb, oracle, terse)

    if not items and not fidelity and not measure_line and not pbip_line:
        return ["REPORT: no remediation worklist in this handover.", "", *drift_lines, ""]

    summary = worklist_of(wb).get("summary") if isinstance(worklist_of(wb).get("summary"), dict) else {}
    emptied = _emptied_visuals(wb)
    out = [
        f"REPORT: {len(items)} remediation item(s), {summary.get('visuals_flagged', '?')} "
        f"visual(s) flagged, {summary.get('visuals_clean', '?')} clean"
    ]

    # Evidence sits directly under the tier line, which is where a reader forms the belief "the
    # visuals were checked". Tiers are a verdict about what the emitter INTENDED; evidence is
    # whether anything then looked at what it wrote (#371).
    candidates = [
        _severity_line(items),
        _fidelity_tier_line(fidelity) if fidelity else None,
        _evidence_summary_line(wb, terse),
        f"        !! {len(emptied)} visual(s) EMPTIED - every field binding was dropped" if emptied else None,
        measure_line,
        pbip_line,
    ]
    out += [line for line in candidates if line]
    # Unconditional: "not measured" is the finding here, so it must print exactly when there is no
    # data, which is precisely when a conditional line would stay silent (#372).
    out += drift_lines

    out += ["", "        next step: --viz    (full worklist)   --viz --severity blocking", ""]
    return out


def _emptied_block(emptied: list[dict], budget: int) -> list[str]:
    """Visuals whose every field binding was dropped - they render blank, so they lead the queue.

    Budgeted like everything else. It used to be an unconditional bypass, justified by a 1,014-byte
    measurement on one real workbook - but that is a property of that sample, not a bound: 100
    emptied visuals emitted 21,937 bytes against a 20,000 cap, 1,000 emitted 219,038, and neither
    printed a truncation banner. It keeps its priority - it is claimed from the budget BEFORE the
    worklist detail - without keeping its exemption, and the heading always carries the full count,
    so the number of blank visuals is never lost even when their names do not fit.
    """
    if not emptied:
        return []
    head = f"--- EMPTIED VISUALS ({len(emptied)}) - these render blank; every binding was dropped ---"
    rows = []
    for d in emptied:
        dropped = d.get("dropped")
        text = ", ".join(str(x) for x in dropped) if isinstance(dropped, list) else str(dropped)
        rows.append(f"    {d.get('visual', '?')}: dropped {text}")
    more = "    ... and {n} more emptied visual(s) not named here; raise --max-bytes or use --json"
    # `- 1 - 1`: the heading line AND the trailing blank this returns are both costed by the caller,
    # so neither may be spent on names. Forgetting the blank overshot a cap by exactly one byte.
    return [head] + _fit_lines(rows, budget - _blen(head) - 1 - 1, more) + [""]


def _worklist_group_head(category: str, group: list[dict]) -> str:
    """One category's heading plus each distinct remediation text, printed once above its items."""
    block = ["", f"## {category}  ({len(group)} item(s))"]
    remedies = sorted({(i.get("remediation") or "").strip() for i in group} - {""})
    block += [f"    FIX: {text}" for text in remedies]
    return "\n".join(block)


def _worklist_item_label(category: str, item: dict) -> str:
    """Short identity for an item that did NOT fit, so the banner can name it precisely."""
    where = item.get("worksheet") or item.get("visual") or "?"
    page = item.get("page_display") or item.get("page")
    return f"{category}: {where} [{page}]" if page else f"{category}: {where}"


def _worklist_item_block(item: dict) -> str:
    """One worklist item: severity, where it lives, and why it is on the queue."""
    where = item.get("worksheet") or item.get("visual") or "?"
    page = item.get("page_display") or item.get("page")
    lines = [f"    - {(item.get('severity') or '?'):<8} {f'{where} [{page}]' if page else where}"]
    reason = (item.get("reason") or "").strip()
    if reason:
        lines.append(f"      why: {reason}")
    return "\n".join(lines)


def _truncation_banner(
    max_bytes: int,
    shown: int,
    omitted: list[str],
    name_budget: int,
    *,
    recovery: str,
) -> list[str]:
    """Loud, itemised stop, that ITSELF fits.

    Naming every omitted item is the goal, but it cannot be unconditional: 170 labels is ~11 KB,
    which silently blew the very cap this banner reports on. So names are printed until
    ``name_budget`` is spent, and the remainder is reported as an explicit count with the command
    that prints them - never dropped without saying so.

    ⚠️ Shared by `--category`, `--viz` and `--fidelity` ON PURPOSE. `render_category` used to carry
    its own copy that appended every remaining name unbudgeted; it survived three rounds of fixing
    this exact bug next door, and the 308-view sweep could not see it because no real category has
    enough long-named requests to overflow. One implementation is the only way that stays fixed.

    ``recovery`` has NO default, deliberately: the shared default hard-coded a `--severity` hint,
    which only `--viz` honours - `--fidelity --severity blocking` is byte-identical to `--fidelity`,
    and `--category` printed the correct `--name` hint and the useless `--severity` one together.
    Requiring each caller to state its own is what makes an untested view impossible to get wrong.
    """
    head = [
        "",
        "!" * 78,
        f"!! OUTPUT TRUNCATED at --max-bytes={max_bytes}. "
        f"{shown} of {shown + len(omitted)} item(s) shown; {len(omitted)} NOT shown.",
        "!! You have NOT seen the whole queue. Get the rest with either:",
        f"!!   --max-bytes {max_bytes * 3}",
        recovery,
        "!! NOT shown:",
    ]

    tail_rule = "!" * 78
    all_names = [f"!!     {n}" for n in omitted]
    # EVERY part of the banner is content and must be reserved before naming anything. Three
    # separate overshoots came from forgetting one of them: the names (24,631), the head
    # (20,401), and then the footer + closing rule (20,120) - each against a 20,000 cap.
    spent = sum(_blen(x) + 1 for x in head) + _blen(tail_rule) + 1

    # Naming every omitted item is the whole point, so check whether they ALL fit first. A blind
    # footer reserve is self-defeating at small caps: it costs ~130 bytes to say "and N more"
    # even when the names it replaces were cheaper than the sentence itself.
    if spent + sum(_blen(x) + 1 for x in all_names) <= name_budget:
        return head + all_names + [tail_rule]

    def _more_line(n: int) -> str:
        return f"!!     ... and {n} more not named here (the list itself would exceed --max-bytes)"

    # Only now is the footer certain, so only now does it earn a reserve. len(omitted) has at
    # least as many digits as the count actually printed, so this can never under-reserve.
    footer_reserve = _blen(_more_line(len(omitted))) + 1
    named: list[str] = []
    for line in all_names:
        if spent + footer_reserve + _blen(line) + 1 > name_budget:
            break
        named.append(line)
        spent += _blen(line) + 1
    named.append(_more_line(len(omitted) - len(named)))
    return head + named + [tail_rule]


# --------------------------------------------------------------------------------------------
# Evidence (#371) and layout drift (#372) - two engine signals that had no consumer
# --------------------------------------------------------------------------------------------


def fidelity_evidence_status(wb: dict) -> tuple[str, dict[str, int]]:
    """COVERAGE, not findings: for each visual, did the structural check actually RUN?

    Returns ``(status, counts)`` where ``counts`` maps each ``evidence`` value to how many visuals
    carry it. Mirrors `measure_filter_status`/`partitions_needs_review_status`, for the same reason:
    an absent key is reported as ABSENT, never as a clean zero.

    An unrecognised value (a future engine adds one) is counted under its own name and is therefore
    NOT verified - only the literal `emitted+linted` is. Failing that way round is the point: a new
    value must not inherit a pass from a consumer that has never heard of it.

    ⚠️ EVERY ROW COUNTS, including one that is not an object. Filtering unreadable rows out before
    counting made the denominator describe the rows this function could parse rather than the
    visuals the engine emitted: ``[{"evidence": "emitted+linted"}, 42]`` reported *"1 of 1 visual(s)
    emitted+linted - structural coverage complete"* and exited 0, with two visuals in and one out.
    That is the same collapse this whole module exists to prevent, one level down - a row nothing
    could assess is not a row that passed - so an unreadable row gets its own bucket instead.
    """
    if "viz_fidelity" not in wb:
        return FIDELITY_EVIDENCE_NONE, {}
    raw_rows = wb.get("viz_fidelity")
    if not isinstance(raw_rows, list):
        return FIDELITY_EVIDENCE_INVALID, {}
    if not raw_rows:
        return FIDELITY_EVIDENCE_NONE, {}
    counts: dict[str, int] = {}
    for row in raw_rows:
        if not isinstance(row, dict):
            counts[EVIDENCE_UNREADABLE] = counts.get(EVIDENCE_UNREADABLE, 0) + 1
            continue
        raw = row.get("evidence")
        value = raw.strip() if isinstance(raw, str) and raw.strip() else EVIDENCE_UNKNOWN
        counts[value] = counts.get(value, 0) + 1
    if set(counts) == {EVIDENCE_UNKNOWN}:
        return FIDELITY_EVIDENCE_MISSING, counts
    return FIDELITY_EVIDENCE_PRESENT, counts


def _evidence_order(counts: dict[str, int]) -> list[str]:
    """Known values loudest-first, then any value this consumer does not recognise."""
    known = [v for v in EVIDENCE_ORDER if counts.get(v)]
    return known + sorted(v for v in counts if v not in EVIDENCE_ORDER)


def evidence_gate(wb: dict) -> tuple[int, str]:
    """Exit code + verdict for `--gate-evidence`. THREE outcomes, never two.

    * ``EXIT_EVIDENCE_BLOCKED`` - a lint finding names at least one visual.
    * ``EXIT_NOT_VERIFIED``     - no finding, but coverage is incomplete or unknown: a visual left
      at ``emitted``, a row with no ``evidence`` key, an unrecognised value, an unreadable shape, or
      no ``viz_fidelity`` at all. This is its OWN state; it is never folded into the pass.
    * ``EXIT_OK``               - every visual is ``emitted+linted``. Still not a render check.
    """
    status, counts = fidelity_evidence_status(wb)
    if status == FIDELITY_EVIDENCE_INVALID:
        return (
            EXIT_NOT_VERIFIED,
            "EVIDENCE: NOT VERIFIED - viz_fidelity is present but not a list; inspect raw handover",
        )
    if status == FIDELITY_EVIDENCE_NONE:
        return EXIT_NOT_VERIFIED, "EVIDENCE: NOT VERIFIED - no viz_fidelity rows; nothing recorded a per-visual check"
    if status == FIDELITY_EVIDENCE_MISSING:
        return EXIT_NOT_VERIFIED, (
            f"EVIDENCE: NOT VERIFIED - {sum(counts.values())} visual(s), none carries an `evidence` "
            "key (bundle predates engine 2.335.0); coverage is unknown, not clean"
        )
    total = sum(counts.values())
    verified = counts.get(EVIDENCE_VERIFIED, 0)
    breakdown = " | ".join(f"{v} {counts[v]}" for v in _evidence_order(counts))
    if counts.get(EVIDENCE_LINT_FAILED):
        return EXIT_EVIDENCE_BLOCKED, (
            f"EVIDENCE: BLOCKED - {counts[EVIDENCE_LINT_FAILED]} of {total} visual(s) are named by a "
            f"PBIR lint finding ({breakdown})"
        )
    if verified != total:
        return EXIT_NOT_VERIFIED, (
            f"EVIDENCE: NOT VERIFIED - only {verified} of {total} visual(s) were examined "
            f"({breakdown}); an unexamined visual is not a verified one"
        )
    return EXIT_OK, (
        f"EVIDENCE: {total} of {total} visual(s) emitted+linted - structural coverage complete "
        "(NOT a render check; the bytes were linted, not drawn)"
    )


def _evidence_absent_line(status: str, total: int, terse: bool) -> str | None:
    """The two states where there is no distribution to report: unreadable, or never recorded."""
    if status == FIDELITY_EVIDENCE_INVALID:
        if terse:
            return "        evidence: INVALID SHAPE"
        return "        evidence: INVALID SHAPE (viz_fidelity present but not a list; inspect raw handover)"
    if terse:
        return f"        ?? evidence: NOT RECORDED on {total} visual(s)"
    return (
        f"        ?? evidence: NOT RECORDED on any of {total} visual(s) "
        "(pre-2.335.0 engine) - coverage UNKNOWN, not clean"
    )


def _evidence_summary_line(wb: dict, terse: bool = False) -> str | None:
    """One line for the default view. Loud unless every visual was actually examined.

    ``terse`` drops the explanation, never the signal: a budget too tight for the prose is still
    wide enough for the counts, and silently omitting the line is the failure this line prevents.
    """
    status, counts = fidelity_evidence_status(wb)
    if status == FIDELITY_EVIDENCE_NONE:
        return None
    total = sum(counts.values())
    if status in (FIDELITY_EVIDENCE_INVALID, FIDELITY_EVIDENCE_MISSING):
        return _evidence_absent_line(status, total, terse)
    verified = counts.get(EVIDENCE_VERIFIED, 0)
    mark = "!!" if counts.get(EVIDENCE_LINT_FAILED) else ("??" if verified != total else "  ")
    if terse:
        return f"        {mark} evidence: {verified}/{total} examined"
    breakdown = " | ".join(f"{v} {counts[v]}" for v in _evidence_order(counts))
    return f"        {mark} evidence: {verified}/{total} examined - {breakdown}"


def _evidence_legend(counts: dict[str, int]) -> list[str]:
    """Spell out each value present, so `emitted` is never read as a synonym for "fine"."""
    return [
        f"    {v:<14} {counts[v]:>4}  {EVIDENCE_LABEL.get(v, 'UNRECOGNISED VALUE - not verified')}"
        for v in _evidence_order(counts)
    ]


def placement_block_of(wb: dict, oracle: dict | None) -> dict | None:
    """The oracle's ``summary.placement``, or an inlined copy should the engine ever emit one.

    The workbook keys are checked FIRST and are forward-compatible only: no engine version emits
    placement into a handover slice today (measured against 2.339.0). They cost two dict lookups and
    mean this consumer keeps working if `migrate_estate.py` ever absorbs the oracle rollup.
    """
    for key in ("viz_placement", "placement"):
        candidate = wb.get(key)
        if isinstance(candidate, dict):
            return candidate
    if isinstance(oracle, dict):
        summary = oracle.get("summary")
        if isinstance(summary, dict) and isinstance(summary.get("placement"), dict):
            return summary["placement"]
    return None


def _num(value: Any) -> float | None:
    """Numeric coercion that refuses bools and non-finite values.

    Refusing bools stops a stray ``true`` posing as a pixel count; refusing ``nan``/``inf`` stops a
    corrupt number surviving a comparison it should fail. ``nan != nan`` would have made an axis
    look drifted; ``None == None`` made a MISSING one look exact, which is worse.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


# The engine derives both axes from the same `delta_px` dicts, so `_placement_rollup` emits them
# together or not at all - its own test asserts exactly that. One axis alone is therefore a
# malformed rollup, and reading it as "the other is fine" is the failure `by_axis` was built to fix.
EXPECTED_AXES = ("x", "y")

# Every axis record must carry these, finite and non-negative, before any comparison is made.
AXIS_MAGNITUDE_KEYS = ("median_abs_px", "mean_abs_px", "worst_abs_px")


def _valid_axis(stats: Any) -> bool:
    """Whether an axis record can be compared at all. Anything unassessable is NOT exact.

    Guards the trap directly: ``_num(missing) == _num(missing)`` is ``None == None`` -> True, so an
    EMPTY axis record used to satisfy "exact == evaluated" and report ``pixel_exact``. Measured:
    ``{"x": {}, "y": {}}`` returned ``pixel_exact`` with ``measured: true``.
    """
    if not isinstance(stats, dict):
        return False
    evaluated, exact = _num(stats.get("evaluated")), _num(stats.get("exact"))
    if evaluated is None or exact is None or evaluated < 0 or not 0 <= exact <= evaluated:
        return False
    if _num(stats.get("mean_signed_px")) is None:
        return False
    return all((_num(stats.get(k)) or 0.0) >= 0 and _num(stats.get(k)) is not None for k in AXIS_MAGNITUDE_KEYS)


def _placement_pixel_exact(placement: dict) -> bool | None:
    """Whether the ENCLOSING rollup calls itself pixel-exact. ``None`` when it does not say.

    ``by_axis`` measures ``left``/``top`` only, while the rollup around it also weighs the right and
    bottom edges and size drift. Measured against engine 2.339.0: a visual whose origin is perfect
    but whose far edges are 100 px out yields ``verdict: "drifted"``, ``worst_max_edge_px: 100.0`` -
    and reading only ``by_axis`` reported ``pixel_exact`` with ``measured: true``. The engine's own
    verdict is authoritative here; ours is a per-axis refinement of it, never a replacement.
    """
    verdict = placement.get("verdict")
    if isinstance(verdict, str):
        return verdict == "pixel-exact"
    evaluated, exact = _num(placement.get("evaluated")), _num(placement.get("pixel_exact"))
    if evaluated is None or exact is None:
        return None
    return exact == evaluated


def _by_axis_status(placement: dict) -> str | None:
    """Whether ``by_axis`` is usable at all. Returns a terminal status, or ``None`` to continue.

    Split out of `layout_drift_status` so the shape rules read as one list, and so the caller stays
    under pylint's return-statement ceiling.
    """
    by_axis = placement.get("by_axis")
    if by_axis is None or (isinstance(by_axis, dict) and not by_axis):
        return LAYOUT_DRIFT_AXIS_BLIND
    if not isinstance(by_axis, dict):
        return LAYOUT_DRIFT_INVALID
    if any(axis not in by_axis for axis in EXPECTED_AXES):
        return LAYOUT_DRIFT_INVALID
    if not all(_valid_axis(stats) for stats in by_axis.values()):
        return LAYOUT_DRIFT_INVALID
    return None


def layout_drift_status(wb: dict, oracle: dict | None = None) -> tuple[str, dict]:
    """Per-axis layout drift, distinguishing "measured, no drift" from "never measured".

    Six states, because collapsing any two of them recreates the axis-blind failure this block was
    built to fix:

    * ``LAYOUT_DRIFT_NOT_MEASURED`` - no placement rollup anywhere. Absence of the block is NOT
      absence of drift, and today this is the answer for every deterministic-pipeline bundle.
    * ``LAYOUT_DRIFT_AXIS_BLIND``   - a placement rollup with no ``by_axis`` (a pre-2.332.0 oracle
      report, or one with no zone->visual deltas). Per-axis drift is UNKNOWN.
    * ``LAYOUT_DRIFT_INVALID``      - ``by_axis`` is not an object of two comparable axis records:
      a missing axis, a non-object record, a non-finite or out-of-range count, or an enclosing
      rollup that never states its own verdict. Unassessable, therefore not a pass.
    * ``LAYOUT_DRIFT_EXACT``        - origins exact on every axis AND the enclosing rollup agrees it
      is pixel-exact.
    * ``LAYOUT_DRIFT_EDGE_ONLY``    - origins exact on every axis but the enclosing rollup is not
      pixel-exact: the far edges or the SIZE drifted. ``by_axis`` structurally cannot see this, so
      it is reported rather than absorbed into either neighbour.
    * ``LAYOUT_DRIFT_PRESENT``      - measured, with origin drift. ``payload["axes"]`` carries both
      axes and ``payload["worst"]`` the ``(name, stats)`` pair to lead with.
    """
    placement = placement_block_of(wb, oracle)
    if placement is None:
        return LAYOUT_DRIFT_NOT_MEASURED, {}
    shape = _by_axis_status(placement)
    if shape is not None:
        return shape, {"placement": placement}
    axes = dict(placement["by_axis"])
    payload = {"placement": placement, "axes": axes, "worst": _worst_axis(axes)}
    if not all(_num(s.get("exact")) == _num(s.get("evaluated")) for s in axes.values()):
        return LAYOUT_DRIFT_PRESENT, payload
    enclosing = _placement_pixel_exact(placement)
    if enclosing is None:
        # Origins are exact but nothing corroborates the edges or the size. "Cannot assess" is its
        # own answer; claiming pixel-exact on it is the collapse this function guards against.
        return LAYOUT_DRIFT_INVALID, {"placement": placement}
    return (LAYOUT_DRIFT_EXACT if enclosing else LAYOUT_DRIFT_EDGE_ONLY), payload


def _worst_axis(axes: dict[str, dict]) -> tuple[str, dict] | None:
    """The axis to lead with: largest worst-case absolute error, mean breaking the tie."""
    scored = [(name, stats) for name, stats in axes.items() if _num(stats.get("worst_abs_px")) is not None]
    if not scored:
        return None
    return max(
        scored, key=lambda pair: (_num(pair[1].get("worst_abs_px")) or 0.0, _num(pair[1].get("mean_abs_px")) or 0.0)
    )


def _axis_phrase(name: str, stats: dict) -> str:
    """One axis, WITH ITS SIGN. `+108px down` and `-56px up` are different defects; the absolute
    value that collapses them is exactly what made `max_edge_px` unable to answer the question."""
    positive, negative = AXIS_DIRECTIONS.get(name.lower(), ("+", "-"))
    signed = _num(stats.get("mean_signed_px"))
    signed_txt = f"{signed:+g}px" if signed is not None else "?px"
    return (
        f"{name.upper()} {stats.get('exact', '?')}/{stats.get('evaluated', '?')} exact, "
        f"worst {stats.get('worst_abs_px', '?')}px, median {stats.get('median_abs_px', '?')}px, "
        f"signed {signed_txt} ({stats.get('positive', '?')} {positive} / {stats.get('negative', '?')} {negative})"
    )


def _terse_drift_line(status: str, payload: dict) -> str:
    """The compressed form, deliberately tiny. Never drops the signal - a budget too tight for the
    prose is still wide enough for the verdict, and omitting the line is exactly the failure the
    line exists to prevent. Bounded in bytes so the fallback cannot itself overrun the cap.
    """
    if status == LAYOUT_DRIFT_NOT_MEASURED:
        return "        ?? drift: NOT MEASURED (--oracle)"
    if status == LAYOUT_DRIFT_AXIS_BLIND:
        return "        ?? drift: PER-AXIS UNKNOWN"
    if status == LAYOUT_DRIFT_EXACT:
        return "        drift: MEASURED, pixel-exact"
    if status == LAYOUT_DRIFT_EDGE_ONLY:
        return "        !! drift: origins exact, EDGE/SIZE drift"
    worst = payload.get("worst")
    if not worst:
        return "        !! drift: MEASURED, worst axis unknown"
    signed = _num(worst[1].get("mean_signed_px"))
    signed_txt = f"{signed:+g}" if signed is not None else "?"
    return f"        !! drift: worst {worst[0].upper()} {worst[1].get('worst_abs_px', '?')}px {signed_txt}px"


def _edge_only_line(placement: dict, axes: dict[str, dict]) -> str:
    """Origins land pixel-perfect, yet the enclosing rollup does not call itself pixel-exact.

    Named rather than absorbed, because `by_axis` measures ``left``/``top`` only: it structurally
    cannot see a far-edge or SIZE mismatch, which is precisely the customer-reported shape in #372
    (visuals compressed to ~34-48% of intended height with their origins in the right place).
    """
    detail = " | ".join(f"{n.upper()} {s.get('exact', '?')}/{s.get('evaluated', '?')}" for n, s in sorted(axes.items()))
    return (
        f"        !! layout drift: origins pixel-exact ({detail}) but the rollup reports "
        f"{placement.get('verdict', '?')!s} - EDGE or SIZE drift that by_axis cannot see; "
        f"worst edge {placement.get('worst_max_edge_px', '?')}px"
    )


def _unmeasured_drift_line(status: str, payload: dict) -> str | None:
    """The full-form lines for the states that carry no per-axis numbers. ``None`` to continue."""
    if status == LAYOUT_DRIFT_INVALID:
        return (
            "        ?? layout drift: NOT ASSESSABLE - by_axis is not two comparable axis records, "
            "or the rollup states no verdict; treated as unknown, never as no drift"
        )
    if status == LAYOUT_DRIFT_NOT_MEASURED:
        return (
            "        ?? layout drift: NOT MEASURED - no oracle placement rollup here "
            "(run fidelity_oracle.py or pass --oracle); absence is not zero drift"
        )
    if status == LAYOUT_DRIFT_AXIS_BLIND:
        placement = payload.get("placement") or {}
        return (
            "        ?? layout drift: PER-AXIS UNKNOWN - placement measured but carries no by_axis "
            f"(pre-2.332.0); axis-blind worst edge {placement.get('worst_max_edge_px', '?')}px"
        )
    return None


def _layout_drift_summary_line(wb: dict, oracle: dict | None = None, terse: bool = False) -> str:
    """One line per workbook - the triage #372 asks for, ahead of anyone opening the report.

    BOTH axes always appear together in the full form. The engine's own rollup test pins that rule
    ("a consumer comparing axes must never get one of them; that reads as 'the other is fine'"), and
    it applies just as hard one layer up.
    """
    status, payload = layout_drift_status(wb, oracle)
    if terse and status != LAYOUT_DRIFT_INVALID:
        return _terse_drift_line(status, payload)
    unmeasured = _unmeasured_drift_line(status, payload)
    if unmeasured is not None:
        return unmeasured
    axes = payload["axes"]
    worst = payload["worst"]
    if status == LAYOUT_DRIFT_EDGE_ONLY:
        return _edge_only_line(payload["placement"], axes)
    if status == LAYOUT_DRIFT_EXACT:
        detail = " | ".join(
            f"{n.upper()} {s.get('exact', '?')}/{s.get('evaluated', '?')}" for n, s in sorted(axes.items())
        )
        return f"        layout drift: MEASURED, pixel-exact on every axis ({detail})"
    lead = _axis_phrase(*worst) if worst else "worst axis unknown"
    rest = " | ".join(
        f"{n.upper()} {s.get('exact', '?')}/{s.get('evaluated', '?')} exact, worst {s.get('worst_abs_px', '?')}px"
        for n, s in sorted(axes.items())
        if not worst or n != worst[0]
    )
    tail = f" | {rest}" if rest else ""
    return f"        !! layout drift: MEASURED, worst {lead}{tail}"


def _clip_tail(text: str, limit: int = 88) -> str:
    """Clip from the LEFT by BYTES, keeping the tail.

    Two things, both load-bearing. Clipping from the RIGHT loses the filename, which is the only
    part identifying which report the numbers came from. And the limit is a BYTE limit because
    ``--max-bytes`` is a byte cap: a 24-CHARACTER clip of a CJK filename measured 101 bytes against
    a bound the code believed was ~70, and pushed the whole render past the cap. ``errors="ignore"``
    drops a code point the cut landed inside, so the result is never mojibake.
    """
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return "..." + raw[-(limit - 3) :].decode("utf-8", errors="ignore")


def _clip_head(text: str, limit: int) -> str:
    """Clip from the RIGHT by BYTES, keeping the head. The mirror of `_clip_tail`, for text whose
    MEANING is at the front (a verdict) rather than at the end (a path)."""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[: limit - 3].decode("utf-8", errors="ignore") + "..."


def _layout_drift_lines(wb: dict, oracle: OracleSource, terse: bool = False) -> list[str]:
    """The drift verdict plus, when a number was measured, WHERE it came from.

    Provenance is emitted here rather than printed alongside the render because ``--max-bytes`` is a
    strict cap on everything a run prints: a line printed outside the budgeted body made the cap a
    suggestion (measured - 1632 bytes emitted under a 1500-byte cap, with no truncation banner).

    Under ``terse`` the citation collapses onto the verdict line and is byte-clipped to fit, and the
    whole line is then clamped to ``TERSE_DRIFT_LINE_MAX_BYTES``. The clamp is NOT redundant: the
    verdict alone can exceed the ceiling on extreme input (a long axis name, a huge pixel value), so
    a bound enforced only on the citation is a bound that holds for well-behaved data and no other.
    """
    verdict = _layout_drift_summary_line(wb, oracle.payload, terse)
    if not terse:
        if oracle.path is None:
            return [verdict]
        return [verdict, f"           measured from: {_clip_tail(str(oracle.path))}"]
    line = verdict
    if oracle.path is not None:
        # Below ~4 bytes a citation is "...", which identifies nothing; the VERDICT is the signal
        # and the citation is its provenance, so this is the one place dropping it is right.
        room = TERSE_DRIFT_LINE_MAX_BYTES - _blen(verdict) - len(" []")
        if room >= 4:
            line = f"{verdict} [{_clip_tail(oracle.path.name, room)}]"
    return [_clip_head(line, TERSE_DRIFT_LINE_MAX_BYTES)]


def _fidelity_counts(wb: dict) -> list[str]:
    """Tier counts plus the evidence distribution - cheap enough to always print, so no view can
    hide either that visual fidelity exists or that some of it was never examined."""
    rows = [v for v in (wb.get("viz_fidelity") or []) if isinstance(v, dict)]
    if not rows:
        return []
    tiers: dict[str, int] = {}
    for v in rows:
        tiers[str(v.get("tier") or "?")] = tiers.get(str(v.get("tier") or "?"), 0) + 1
    flagged = sum(1 for v in rows if (v.get("reason") or "").strip())
    out = [
        f"--- VISUAL FIDELITY ({len(rows)} visual(s)): " + " | ".join(f"{k} {n}" for k, n in sorted(tiers.items())),
    ]
    if flagged:
        out.append(f"    {flagged} visual(s) recorded a fidelity reason - see --fidelity for each")
    status, counts = fidelity_evidence_status(wb)
    if status in (FIDELITY_EVIDENCE_PRESENT, FIDELITY_EVIDENCE_MISSING):
        verified = counts.get(EVIDENCE_VERIFIED, 0)
        out.append(f"--- EVIDENCE (engine >= 2.335.0): {verified}/{sum(counts.values())} visual(s) examined")
        out += _evidence_legend(counts)
    return out + [""]


def _fidelity_groups(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    """Every `viz_fidelity[]` row, grouped - findings first, then the rows that recorded no reason.

    The clean rows are a group rather than an omission because `--fidelity` claims to print the
    list in full and did not: one real file has 99 rows of which 30 carry no reason, and those 30
    appeared only inside an aggregate tier count. Last in the order, so they are also the first
    thing a tight budget drops.
    """
    by_reason: dict[str, list[dict]] = {}
    for v in rows:
        by_reason.setdefault((v.get("reason") or "").strip(), []).append(v)
    groups = [(reason, by_reason[reason]) for reason in sorted(k for k in by_reason if k)]
    if by_reason.get(""):
        groups.append((CLEAN_FIDELITY_GROUP, by_reason[""]))
    return groups


def _fidelity_row(v: dict) -> str:
    """Per-visual line. `evidence` sits beside `status` deliberately: `status: rebuilt` records that
    the EMITTER ran, and the two are read together or the first is over-read (#371)."""
    raw = v.get("evidence")
    evidence = raw.strip() if isinstance(raw, str) and raw.strip() else EVIDENCE_UNKNOWN
    mark = "  " if evidence == EVIDENCE_VERIFIED else "!!"
    return (
        f"    {mark} {str(v.get('status') or '?'):<22} {evidence:<14} {v.get('worksheet') or '?'}"
        f" ({v.get('visual_type') or '?'}, tier {v.get('tier') or '?'})"
    )


def render_fidelity(wb_name: str, wb: dict, max_bytes: int) -> str:
    """`viz_fidelity[]` in full, grouped by reason, with the reason-less rows in their own group.

    Its own view because the detail is ~15 KB on the worked example - large enough to blow the
    whole `--viz` budget on its own. It is not optional information, though: measured on
    `_bundle-208`, 17 `rebuilt_with_deferrals` reasons appear ONLY here and in no remediation
    worklist item, so a consumer working from `--viz` alone never learns of them.
    """
    rows = [v for v in (wb.get("viz_fidelity") or []) if isinstance(v, dict)]
    if not rows:
        return f"=== {_clip(wb_name)} - VISUAL FIDELITY ===\n\nNo viz_fidelity rows recorded."

    out = [f"=== {_clip(wb_name)} - VISUAL FIDELITY ({len(rows)} visual(s)) ==="]
    out += _fidelity_counts(wb)
    banner_reserve = max(BANNER_RESERVE_FLOOR, max_bytes // 4)
    budget = max_bytes - sum(_blen(x) + 1 for x in out) - banner_reserve
    omitted: list[str] = []
    shown = 0

    for reason, group in _fidelity_groups(rows):
        head = f"\n## {reason}  ({len(group)} visual(s))"
        if omitted or _blen(head) + 1 > budget:
            omitted += [f"{reason}: {v.get('worksheet') or '?'}" for v in group]
            continue
        out.append(head)
        budget -= _blen(head) + 1
        for v in group:
            line = _fidelity_row(v)
            # `if omitted or ...`: once anything is omitted the view stops resuming, so the banner's
            # "N of M shown" describes a prefix rather than an arbitrary scatter.
            if omitted or _blen(line) + 1 > budget:
                omitted.append(f"{reason}: {v.get('worksheet') or '?'}")
                continue
            out.append(line)
            budget -= _blen(line) + 1
            shown += 1

    if omitted:
        out += _truncation_banner(max_bytes, shown, omitted, banner_reserve, recovery=RECOVERY_BY_JSON)
    return "\n".join(out)


def _budgeted_worklist(grouped: dict[str, list[dict]], budget: int) -> tuple[list[str], list[str], int]:
    """Fill ``budget`` with worklist detail, returning (lines, omitted labels, shown count).

    Split out of `render_viz` so the budgeting is testable on its own and the caller stays under
    pylint's local-variable ceiling. Once anything has been omitted the loop keeps collecting
    labels rather than resuming: a queue that skips item 40 and then prints item 41 reads as if
    40 does not exist, which is the failure mode the banner exists to prevent. The GROUP condition
    said that and the ITEM condition did not - an oversized first item was omitted and the small
    second item printed anyway - so both now start with ``if omitted``.
    """
    lines: list[str] = []
    omitted: list[str] = []
    shown = 0
    for cat in sorted(grouped, key=lambda c: -len(grouped[c])):
        group = grouped[cat]
        head = _worklist_group_head(cat, group)
        if omitted or _blen(head) + 1 > budget:
            omitted += [_worklist_item_label(cat, i) for i in group]
            continue
        lines.append(head)
        budget -= _blen(head) + 1
        for item in group:
            line = _worklist_item_block(item)
            if omitted or _blen(line) + 1 > budget:
                omitted.append(_worklist_item_label(cat, item))
                continue
            lines.append(line)
            budget -= _blen(line) + 1
            shown += 1
    return lines, omitted, shown


def _viz_sections(
    wb: dict,
    items: list[dict],
    grouped: dict[str, list[dict]],
    filters: tuple[str | None, str | None],
    budget: int,
) -> tuple[list[str], int]:
    """Emptied visuals + fidelity counts + the item-count heading, all charged to ``budget``.

    Returns the lines and what is LEFT of the budget, so the caller can hand the remainder to the
    worklist. Split out of `render_viz` only to keep that function under pylint's local-variable
    cap; the ordering inside is load-bearing and commented where it is.
    """
    severity, category = filters
    counts = _fidelity_counts(wb) if category is None else []
    measure_line = _measure_filter_summary_line(wb) if category in (None, MEASURE_FILTER_CATEGORY) else None
    pbip_line = (
        _pbip_warning_summary_line(wb) if category is None or category.startswith(PBIP_WARNING_CATEGORY) else None
    )
    extra_lines = [line for line in (measure_line, pbip_line) if line]
    budget -= sum(_blen(x) + 1 for x in counts + extra_lines)

    scope_parts = [f"severity {severity!r}"] if severity else []
    if category:
        scope_parts.append(f"category {category!r}")
    section = (
        f"--- {len(items)} ITEM(S) IN {len(grouped)} CATEGORY(IES) ---"
        if items
        else f"No remediation worklist items{' at ' + ', '.join(scope_parts) if scope_parts else ''}."
    )
    # Costed BEFORE the emptied block, because it is emitted unconditionally afterwards. Letting
    # the emptied names spend the budget down to 14 bytes and then appending a 39-byte heading is
    # how a section that IS budgeted still overshoots by exactly one heading.
    budget -= _blen(section) + 1

    emptied = _emptied_block(_emptied_visuals(wb), budget) if category is None else []
    budget -= sum(_blen(x) + 1 for x in emptied)
    return emptied + counts + extra_lines + [section], budget


def render_viz(wb_name: str, wb: dict, severity: str | None, category: str | None, max_bytes: int) -> str:
    """Report-side queue: emptied visuals first, then worklist items grouped by category.

    ``max_bytes`` governs EVERY section, the emptied-visuals block included. That block keeps its
    priority - it is the highest-severity content in the file, so it is claimed from the budget
    before any worklist detail - but priority is not exemption: see `_emptied_block`.
    """
    items = report_items_of(wb)
    if severity:
        items = [i for i in items if (i.get("severity") or "").lower() == severity.lower()]
    if category:
        items = [i for i in items if (i.get("category") or "uncategorised") == category]

    out = [f"=== {_clip(wb_name)} - REPORT REMEDIATION QUEUE ===", ""]
    banner_reserve = max(BANNER_RESERVE_FLOOR, max_bytes // 4)
    budget = max_bytes - sum(_blen(x) + 1 for x in out) - banner_reserve

    grouped: dict[str, list[dict]] = {}
    for item in items:
        grouped.setdefault(item.get("category") or "uncategorised", []).append(item)

    head, budget = _viz_sections(wb, items, grouped, (severity, category), budget)
    out += head

    if not items:
        return "\n".join(out)

    # Budget PER ITEM, not per category. Emitting whole category chunks meant one big category
    # blew the cap outright, and the banner could only name the category it stopped on - so the
    # individual items you had not seen were never listed.
    body, omitted, shown = _budgeted_worklist(grouped, budget)
    out += body

    if omitted:
        out += _truncation_banner(
            max_bytes,
            shown,
            omitted,
            banner_reserve,
            recovery=RECOVERY_BY_JSON if category else RECOVERY_BY_SEVERITY,
        )
    return "\n".join(out)


# --------------------------------------------------------------------------------------------
# Default view
# --------------------------------------------------------------------------------------------


NEEDS_REVIEW_NOTE = """\
NOTE: `model_translation_handoff.needs_review[]` in the raw JSON is NOT this queue.
It lists the same calculations by name but carries only 5 fields - no formula, no fields,
no target_table, no guidance. It is enough to REPORT a stub and structurally insufficient to
REPAIR one, so this tool always works from `requests[]`."""


def render_default(wb_name: str, wb: dict, target: Path, max_bytes: int, oracle: OracleSource | None = None) -> str:
    """The landing view: both queues at a glance, plus which list the detail came from.

    Budgeted too. The cascadable list is the one unbounded thing here - it names every stub that
    depends on another stub - so the fixed tail (the report section and the `needs_review` note) is
    costed first and the cascade list gets whatever is left.

    TWO PASSES, because the tail grew. `_model_section` emits an unbudgeted head (the title, the
    source line, the coverage line and the category table), so a tail that alone approaches the cap
    can push the total over it - and at `MIN_MAX_BYTES` the added evidence and drift lines did
    exactly that, firing the `HARD CAP` net this module treats as a budgeting DEFECT rather than a
    pass. When the full assembly does not fit, the tail is rebuilt terse; the two new signals are
    compressed, never dropped.
    """

    def _assemble(terse: bool) -> str:
        report = _report_section(wb, oracle, terse)
        tail_cost = sum(_blen(x) + 1 for x in report) + _blen(NEEDS_REVIEW_NOTE) + 1
        out = _model_section(wb_name, wb, requests_of(wb), target, max_bytes - tail_cost)
        return "\n".join(out + report + [NEEDS_REVIEW_NOTE])

    full = _assemble(False)
    return full if _blen(full) <= max_bytes else _assemble(True)


def _list_row(name: str, wb: dict) -> tuple[str, int, int, int, int, int, bool, int, bool, int, bool]:
    """Urgency tuple with calc, report, invisible-warning, partition-scaffold, and missing-audit counts."""
    items = report_items_of(wb)
    blocking = sum(1 for i in items if (i.get("severity") or "").lower() == "blocking")
    status, payload = measure_filter_status(wb)
    warning_status, warnings = pbip_warning_status(wb)
    partition_status, partition_rows = partitions_needs_review_status(wb)
    measure_filters = int(payload.get("count") or 0) if status == MEASURE_FILTER_PRESENT else 0
    return (
        name,
        len(requests_of(wb)),
        len(items),
        blocking,
        len(_emptied_visuals(wb)),
        measure_filters,
        status == MEASURE_FILTER_MISSING,
        len(warnings) if warning_status == PBIP_WARNING_PRESENT else 0,
        warning_status == PBIP_WARNING_MISSING,
        len(partition_rows) if partition_status == PARTITION_REVIEW_PRESENT else 0,
        partition_status == PARTITION_REVIEW_MISSING,
    )


def _list_line(row: tuple[str, int, int, int, int, int, bool, int, bool, int, bool]) -> str:
    """Format one `--list` row. The name is clipped so a long one cannot push the line off-cap."""
    line = f"    {_clip(row[0], 52):<52} {row[1]:>4} calc request(s)  {row[2]:>4} report item(s)  {row[3]:>3} blocking"
    if row[4]:
        line += f"  !! {row[4]} EMPTIED"
    if row[5]:
        line += f"  !! {row[5]} MEASURE-FILTERS"
    elif row[6]:
        line += "  ?? measure-filter key missing"
    if row[7]:
        line += f"  !! {row[7]} PBIP-WARNINGS"
    elif row[8]:
        line += "  ?? pbip-warning key missing"
    if row[9]:
        line += f"  !! {row[9]} PARTITION-SCAFFOLDS"
    elif row[10]:
        line += "  ?? partition-review key missing"
    return line


def _pbip_warning_estate_lines(found: list[tuple[str, dict, Path]]) -> list[str]:
    """Estate totals for `--list`, including missing vs present-empty audit states."""
    states = {PBIP_WARNING_PRESENT: 0, PBIP_WARNING_NONE: 0, PBIP_WARNING_MISSING: 0, PBIP_WARNING_INVALID: 0}
    families: dict[str, int] = {}
    workbooks_with_warnings = 0
    for _, wb, _ in found:
        status, warnings = pbip_warning_status(wb)
        states[status] = states.get(status, 0) + 1
        if warnings:
            workbooks_with_warnings += 1
        for family, count in _pbip_warning_family_counts(warnings).items():
            families[family] = families.get(family, 0) + count
    total = sum(families.values())
    out = [
        f"PBIP warnings: {total} warning(s) across {workbooks_with_warnings} workbook(s); "
        f"present-empty {states[PBIP_WARNING_NONE]}, missing {states[PBIP_WARNING_MISSING]}, "
        f"invalid {states[PBIP_WARNING_INVALID]}",
    ]
    if families:
        out.append("PBIP warning families: " + " | ".join(f"{k} {families[k]}" for k in sorted(families)))
    return out + [""]


def _partition_review_estate_lines(found: list[tuple[str, dict, Path]]) -> list[str]:
    """Estate totals for `--list`: unresolved M-partition scaffolds, missing vs present-empty."""
    states = {
        PARTITION_REVIEW_PRESENT: 0,
        PARTITION_REVIEW_NONE: 0,
        PARTITION_REVIEW_MISSING: 0,
        PARTITION_REVIEW_INVALID: 0,
    }
    reasons: dict[str, int] = {}
    workbooks_with_partitions = 0
    for _, wb, _ in found:
        status, rows = partitions_needs_review_status(wb)
        states[status] = states.get(status, 0) + 1
        if rows:
            workbooks_with_partitions += 1
        for reason, tables in _partition_review_groups(rows).items():
            reasons[reason] = reasons.get(reason, 0) + len(tables)
    total = sum(reasons.values())
    out = [
        f"Partition scaffolds: {total} table(s) need manual completion across "
        f"{workbooks_with_partitions} workbook(s); present-empty {states[PARTITION_REVIEW_NONE]}, "
        f"missing {states[PARTITION_REVIEW_MISSING]}, invalid {states[PARTITION_REVIEW_INVALID]}",
    ]
    if reasons:
        out.append("Partition scaffold reasons: " + " | ".join(f"{r[:60]!r} {reasons[r]}" for r in sorted(reasons)))
    return out + [""]


def _evidence_estate_tally(found: list[tuple[str, dict, Path]]) -> tuple[dict[str, int], list[str], list[str]]:
    """Roll the per-workbook evidence up, keeping the unassessable workbooks VISIBLE.

    Returns ``(counts, gap_labels, unassessable_names)``. A workbook with no ``viz_fidelity`` or an
    unreadable one contributes nothing to ``counts``, so it must be NAMED instead: skipping it
    silently is how a mixed estate claimed full coverage while a workbook in the very same list was
    never assessed at all.
    """
    counts: dict[str, int] = {}
    gaps: list[tuple[str, int, int]] = []
    unassessable: list[str] = []
    for name, wb, _ in found:
        status, wb_counts = fidelity_evidence_status(wb)
        if status in (FIDELITY_EVIDENCE_NONE, FIDELITY_EVIDENCE_INVALID):
            unassessable.append(name)
            continue
        for value, n in wb_counts.items():
            counts[value] = counts.get(value, 0) + n
        total = sum(wb_counts.values())
        verified = wb_counts.get(EVIDENCE_VERIFIED, 0)
        if verified != total:
            gaps.append((name, verified, total))
    labels = [
        f"    !! {_clip(n, 52):<52} {v}/{t} examined"
        for n, v, t in sorted(gaps, key=lambda g: (g[1] - g[2], g[0].lower()))
    ]
    return counts, labels, sorted(unassessable)


def _evidence_estate_lines(found: list[tuple[str, dict, Path]], budget: int) -> list[str]:
    """Estate totals for `--list`: how much of the estate's visual surface was actually examined.

    Named per workbook, not just totalled: "38 of 40 examined" estate-wide hides that all 2 gaps sit
    in one report. Workbooks with nothing examined are listed first for the same reason.

    BUDGETED, like every other section in this module. It was not, and one unconditional line per
    workbook is unbounded in the estate's width: at `MIN_MAX_BYTES` a 30-workbook estate fired
    `HARD CAP` and cut 18 of the 30 signals with no omission line and exit 0. A dropped signal under
    budget pressure is a silent false-clean - the exact failure this section exists to prevent - so
    the omission line is reserved BEFORE any workbook is named, and the estate totals always
    survive.
    """
    counts, labels, unassessable = _evidence_estate_tally(found)
    head: list[str] = []
    if counts:
        total = sum(counts.values())
        head.append(
            f"Visual evidence: {counts.get(EVIDENCE_VERIFIED, 0)}/{total} visual(s) examined "
            f"({' | '.join(f'{v} {counts[v]}' for v in _evidence_order(counts))})"
        )
        # Named ONLY when the estate is MIXED. When nothing was assessable the headline below
        # already says so and naming every workbook is noise; when SOME were, silence about the
        # rest is what let a "1/1 examined" total sit above two workbooks nobody assessed.
        if unassessable:
            head.append(
                f"    ?? {len(unassessable)} workbook(s) NOT ASSESSABLE (no viz_fidelity, or an "
                "unreadable one) - they are NOT in the total above"
            )
            labels = [f"    ?? {_clip(n, 52):<52} not assessable" for n in unassessable] + labels
    else:
        head.append(
            f"Visual evidence: NOT RECORDED anywhere in this estate - none of {len(found)} "
            "workbook(s) carries viz_fidelity rows; coverage is unknown, not zero"
        )
    if not labels:
        return head + [""]
    more = "    ... and {n} more workbook(s) not named here; raise --max-bytes or use --json"
    remaining = budget - sum(_blen(x) + 1 for x in head) - 1
    return head + _fit_lines(labels, remaining, more) + [""]


def render_list(found: list[tuple[str, dict, Path]], max_bytes: int) -> str:
    """Every workbook in a bundle, ranked by urgency, with the size of both its queues.

    Sorted by urgency rather than name, and carrying the emptied count, because alphabetical
    order actively buried the signal this view exists to surface: measured on `_bundle-208`,
    `Meridian_Hostile_Identifiers` has an emptied visual but zero calc requests and zero
    worklist items, so a name-sorted `N calc / N report` line rendered it as `0 / 0` - the
    least urgent-looking row in the estate.
    """
    rows = [_list_row(name, wb) for name, wb, _ in found]
    out = [
        f"{len(found)} workbook(s), most urgent first (unresolved M-partition scaffolds > PBIP "
        "warnings > invisible measure filters > emptied visuals > blocking items > queue size):",
        "",
    ]
    out += _pbip_warning_estate_lines(found)
    out += _partition_review_estate_lines(found)
    # Half the remaining budget, so a wide estate cannot spend the whole cap naming unexamined
    # workbooks and leave the ranked queue below with nothing. Both sections then omit-and-count.
    spent = sum(_blen(x) + 1 for x in out)
    out += _evidence_estate_lines(found, max(0, (max_bytes - spent) // 2))
    # Partition scaffolds rank FIRST: unlike a stub calc or a dropped visual binding, an unresolved
    # M partition means the table has ZERO rows -- and it carries a named manual completion step
    # the engine already wrote down (issue #326).
    ranked = sorted(rows, key=lambda r: (-r[9], -r[7], -r[5], -r[4], -r[3], -(r[1] + r[2]), r[0].lower()))
    budget = max_bytes - sum(_blen(x) + 1 for x in out)
    more = "    ... and {n} more workbook(s) not listed here; raise --max-bytes"
    return "\n".join(out + _fit_lines([_list_line(r) for r in ranked], budget, more))


def _layout_drift_json(wb: dict, oracle: OracleSource) -> dict:
    """Machine-readable drift. ``status`` is always present, so a consumer that never looks at the
    numbers still cannot mistake "not measured" for "measured, no drift"."""
    status, payload = layout_drift_status(wb, oracle.payload)
    worst = payload.get("worst")
    return {
        "status": status,
        "measured": status in LAYOUT_DRIFT_MEASURED,
        "origins_pixel_exact": status == LAYOUT_DRIFT_EXACT,
        "edge_or_size_drift": status == LAYOUT_DRIFT_EDGE_ONLY,
        "enclosing_verdict": (payload.get("placement") or {}).get("verdict"),
        "by_axis": payload.get("axes"),
        "worst_axis": worst[0] if worst else None,
        "worst_axis_stats": worst[1] if worst else None,
        "placement": payload.get("placement"),
        "measured_from": str(oracle.path) if oracle.path is not None else None,
    }


def build_json(wb_name: str, wb: dict, category: str | None, oracle: OracleSource | None = None) -> dict:
    """Machine-readable form: guidance hoisted out of the requests, so it appears once."""
    oracle = oracle or OracleSource()
    reqs = requests_of(wb)
    if category:
        reqs = [r for r in reqs if (r.get("category") or "uncategorised") == category]
    slim = [{k: v for k, v in r.items() if k != "category_guidance"} for r in reqs]
    evidence_status, evidence_counts = fidelity_evidence_status(wb)
    gate_code, gate_verdict = evidence_gate(wb)
    return {
        "workbook": wb_name,
        "guidance": guidance_by_category(requests_of(wb)),
        "counts": category_counts(requests_of(wb)),
        "requests": slim,
        "report_items": report_items_of(wb),
        "emptied_visuals": _emptied_visuals(wb),
        "measure_filters_needs_review": wb.get("measure_filters_needs_review")
        if "measure_filters_needs_review" in wb
        else MEASURE_FILTER_MISSING,
        "measure_filter_items": _measure_filter_work_items(wb),
        "pbip_warnings": wb.get("pbip_warnings") if "pbip_warnings" in wb else PBIP_WARNING_MISSING,
        "pbip_warning_items": _pbip_warning_work_items(wb),
        "partitions_needs_review": wb.get("partitions_needs_review")
        if "partitions_needs_review" in wb
        else PARTITION_REVIEW_MISSING,
        "partitions_needs_review_groups": _partition_review_groups(partitions_needs_review_status(wb)[1]),
        "viz_fidelity": [v for v in (wb.get("viz_fidelity") or []) if isinstance(v, dict)],
        "viz_fidelity_evidence": {
            "status": evidence_status,
            "counts": evidence_counts,
            "total": sum(evidence_counts.values()),
            # Named explicitly rather than left for the consumer to derive: deriving it means
            # deciding which values count as verified, and that decision is what #371 is about.
            "verified": evidence_counts.get(EVIDENCE_VERIFIED, 0),
            "never_examined": evidence_counts.get(EVIDENCE_EMITTED, 0),
            "lint_failed": evidence_counts.get(EVIDENCE_LINT_FAILED, 0),
            "unknown": evidence_counts.get(EVIDENCE_UNKNOWN, 0),
            "gate_exit_code": gate_code,
            "gate_verdict": gate_verdict,
        },
        "layout_drift": _layout_drift_json(wb, oracle),
    }


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI surface. Every detail view is reachable from a command printed by the default view."""
    p = argparse.ArgumentParser(
        description="Print the engine handover work queue at a readable size.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("target", type=Path, help="bundle dir, handover/<workbook>.json, or report.json")
    p.add_argument("--workbook", help="select one workbook when the target holds several")
    p.add_argument(
        "--category", help="print full repair detail for one model category; with --viz, filter report items"
    )
    p.add_argument("--name", help="print one calculation in full, by name")
    p.add_argument("--viz", action="store_true", help="print the report-side remediation queue")
    p.add_argument(
        "--fidelity",
        action="store_true",
        help="print viz_fidelity[] in full, grouped by reason (its own view: ~15 KB, and it "
        "carries deferral reasons that appear in no remediation item)",
    )
    p.add_argument("--severity", help="with --viz: blocking | high | medium | low (no effect on other views)")
    p.add_argument(
        "--oracle",
        type=Path,
        metavar="PATH",
        help="fidelity-oracle report (or a directory holding one) carrying summary.placement.by_axis. "
        "The layout-drift measurement lives in fidelity_oracle.py's report, NOT in the handover, so "
        "without one this reports NOT MEASURED. Auto-detected by kind in the bundle root, "
        "<bundle>/fidelity/ and beside the slice",
    )
    p.add_argument(
        "--gate-evidence",
        action="store_true",
        help="exit by viz_fidelity[].evidence instead of always 0: "
        f"{EXIT_OK} every visual emitted+linted, {EXIT_EVIDENCE_BLOCKED} a lint finding names one, "
        f"{EXIT_NOT_VERIFIED} coverage incomplete or unknown (an unexamined visual is NOT a pass)",
    )
    p.add_argument("--list", action="store_true", help="list workbooks in the target and exit")
    p.add_argument("--json", type=Path, metavar="FILE", help="also write a machine-readable form")
    p.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help=f"strict cap on printed output; never silent, rejected below {MIN_MAX_BYTES} "
        f"(default {DEFAULT_MAX_BYTES}). Ignored ONLY for a --name resolving to one calculation",
    )
    return p.parse_args(argv)


def _force_utf8_stdout() -> None:
    """Tableau formulas carry glyphs like U+25B2 that a cp1252 console cannot encode.

    Without this the tool dies with a UnicodeEncodeError partway through a formula, handing the
    caller a partial queue and a traceback. Reproduced under a real subprocess with
    ``PYTHONIOENCODING=cp1252``: exit 1 without this call, exit 0 with it. ``errors="replace"``
    keeps an unmappable glyph on a stream that cannot be reconfigured from costing the whole run.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - unusual redirected stream
                pass


def _render(
    args: argparse.Namespace, selected: tuple[str, dict, Path], budget: int, oracle: OracleSource | None = None
) -> str:
    """Pick the view. Every one of these is capped; `--name` is handled by the caller.

    ``selected`` is `select_workbook`'s own ``(name, payload, source)`` triple, kept whole rather
    than splatted into three parameters.
    """
    wb_name, wb, source = selected
    if args.viz:
        return render_viz(wb_name, wb, args.severity, args.category, budget)
    if args.category:
        return render_category(wb_name, requests_of(wb), args.category, budget)
    if args.fidelity:
        return render_fidelity(wb_name, wb, budget)
    return render_default(wb_name, wb, source, budget, oracle)


def main(argv: list[str] | None = None) -> int:
    """Exit 0 on a rendered queue, 2 on an unresolvable target or a cap too small to honour.

    With ``--gate-evidence`` the exit code instead reports coverage: 0 / 1 / 3 per `evidence_gate`.
    Opt-in on purpose - every existing caller of this reader keeps getting 0 on a rendered queue,
    and nothing here starts failing a pipeline because a reader learned to have an opinion.
    """
    _force_utf8_stdout()
    args = parse_args(argv)
    if args.max_bytes < MIN_MAX_BYTES:
        print(
            f"read_handover: --max-bytes {args.max_bytes} is below the minimum {MIN_MAX_BYTES}. "
            "The truncation banner that explains a cut costs ~540 bytes before it names anything, "
            "so a smaller cap cannot be honoured - and emitting more than you asked for while "
            "reporting that the cap held is worse than this error.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    # `print` appends a newline and that newline is output too, so a renderer gets one byte less
    # than the caller asked for. `--json` prints a confirmation, which is output as well.
    json_notice = f"\nwrote {args.json}" if args.json else ""
    budget = args.max_bytes - 1 - (_blen(json_notice) + 1 if json_notice else 0)

    try:
        found = load_workbooks(args.target)
        if args.list:
            print(_capped(render_list(found, budget), budget))
            return EXIT_OK
        selected = select_workbook(found, args.workbook)
        wb_name, wb, source = selected
        oracle = find_oracle_report(args.target, source, wb_name, args.oracle, len(found))
    except HandoverError as exc:
        print(f"read_handover: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if args.name:
        print(render_named(wb_name, requests_of(wb), args.name, budget))
    else:
        print(_capped(_render(args, selected, budget, oracle), budget))

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        payload = build_json(wb_name, wb, args.category, oracle)
        args.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(json_notice)

    if args.gate_evidence:
        code, verdict = evidence_gate(wb)
        print(verdict, file=sys.stderr)
        return code
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
