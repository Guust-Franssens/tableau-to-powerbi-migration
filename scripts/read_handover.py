"""
purpose: print the engine's handover work queue at a size that fits in an agent's context, with
         each category's guidance de-duplicated and the report-side findings (including visuals
         whose bindings were dropped entirely) surfaced instead of buried.
usage:   python scripts/read_handover.py <bundle-or-handover.json>            # queue summary
         python scripts/read_handover.py <target> --category <category>       # full repair detail
         python scripts/read_handover.py <target> --name '<calc name>'        # one calc, in full
         python scripts/read_handover.py <target> --viz [--severity blocking] # report-side queue
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
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

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


class HandoverError(RuntimeError):
    """A target that cannot be resolved to at least one workbook payload."""


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


def _model_section(wb_name: str, wb: dict, reqs: list[dict], target: Path, budget: int) -> list[str]:
    """Model-side summary: coverage, the per-category queue, and any cascade ordering constraint."""
    handoff = handoff_of(wb)
    summary = handoff.get("summary") if isinstance(handoff.get("summary"), dict) else {}

    out = [f"=== HANDOVER QUEUE - {_clip(wb_name)} ===", f"source: {_clip(str(target))}", ""]
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


def report_items_of(wb: dict) -> list[dict]:
    """Report-side work queue, including invisible numeric-fidelity measure filter drops."""
    raw = worklist_of(wb).get("items") or []
    items = [i for i in raw if isinstance(i, dict)] if isinstance(raw, list) else []
    return items + _measure_filter_work_items(wb)


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


def _report_section(wb: dict) -> list[str]:
    items = report_items_of(wb)
    fidelity = wb.get("viz_fidelity") if isinstance(wb.get("viz_fidelity"), list) else []
    emptied = _emptied_visuals(wb)
    measure_line = _measure_filter_summary_line(wb)

    if not items and not fidelity and not measure_line:
        return ["REPORT: no remediation worklist in this handover.", ""]

    out = []
    summary = worklist_of(wb).get("summary") if isinstance(worklist_of(wb).get("summary"), dict) else {}
    flagged = summary.get("visuals_flagged", "?")
    clean = summary.get("visuals_clean", "?")
    out.append(f"REPORT: {len(items)} remediation item(s), {flagged} visual(s) flagged, {clean} clean")

    by_sev = _severity_counts(items)
    if by_sev:
        parts = [f"{s} {by_sev[s]}" for s in SEVERITY_ORDER if s in by_sev]
        parts += [f"{s} {n}" for s, n in sorted(by_sev.items()) if s not in SEVERITY_ORDER]
        out.append("        severity: " + " | ".join(parts))

    if fidelity:
        tiers: dict[str, int] = {}
        for v in fidelity:
            if isinstance(v, dict):
                tiers[v.get("tier") or "?"] = tiers.get(v.get("tier") or "?", 0) + 1
        out.append("        fidelity: " + " | ".join(f"{k} {v}" for k, v in sorted(tiers.items())))

    if emptied:
        out.append(f"        !! {len(emptied)} visual(s) EMPTIED - every field binding was dropped")
    if measure_line:
        out.append(measure_line)

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


def _fidelity_counts(wb: dict) -> list[str]:
    """Tier counts only - cheap enough to always print, so `--viz` never hides that this exists."""
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
    return (
        f"    {str(v.get('status') or '?'):<22} {v.get('worksheet') or '?'}"
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
    measure_lines = [measure_line] if measure_line else []
    budget -= sum(_blen(x) + 1 for x in counts + measure_lines)

    scope_parts = []
    if severity:
        scope_parts.append(f"severity {severity!r}")
    if category:
        scope_parts.append(f"category {category!r}")
    scope = " at " + ", ".join(scope_parts) if scope_parts else ""
    section = (
        f"--- {len(items)} ITEM(S) IN {len(grouped)} CATEGORY(IES) ---"
        if items
        else f"No remediation worklist items{scope}."
    )
    # Costed BEFORE the emptied block, because it is emitted unconditionally afterwards. Letting
    # the emptied names spend the budget down to 14 bytes and then appending a 39-byte heading is
    # how a section that IS budgeted still overshoots by exactly one heading.
    budget -= _blen(section) + 1

    emptied = _emptied_block(_emptied_visuals(wb), budget) if category is None else []
    budget -= sum(_blen(x) + 1 for x in emptied)
    return emptied + counts + measure_lines + [section], budget


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


def render_default(wb_name: str, wb: dict, target: Path, max_bytes: int) -> str:
    """The landing view: both queues at a glance, plus which list the detail came from.

    Budgeted too. The cascadable list is the one unbounded thing here - it names every stub that
    depends on another stub - so the fixed tail (the report section and the `needs_review` note) is
    costed first and the cascade list gets whatever is left.
    """
    reqs = requests_of(wb)
    report = _report_section(wb)
    tail_cost = sum(_blen(x) + 1 for x in report) + _blen(NEEDS_REVIEW_NOTE) + 1
    out = _model_section(wb_name, wb, reqs, target, max_bytes - tail_cost)
    return "\n".join(out + report + [NEEDS_REVIEW_NOTE])


def _list_row(name: str, wb: dict) -> tuple[str, int, int, int, int, int, bool]:
    """Urgency tuple: (name, calc requests, report items, blocking, emptied, measure filters, missing)."""
    items = report_items_of(wb)
    blocking = sum(1 for i in items if (i.get("severity") or "").lower() == "blocking")
    status, payload = measure_filter_status(wb)
    measure_filters = int(payload.get("count") or 0) if status == MEASURE_FILTER_PRESENT else 0
    return (
        name,
        len(requests_of(wb)),
        len(items),
        blocking,
        len(_emptied_visuals(wb)),
        measure_filters,
        status == MEASURE_FILTER_MISSING,
    )


def _list_line(row: tuple[str, int, int, int, int, int, bool]) -> str:
    """Format one `--list` row. The name is clipped so a long one cannot push the line off-cap."""
    name, n_reqs, n_items, blocking, emptied, measure_filters, measure_missing = row
    short = _clip(name, 52)
    line = f"    {short:<52} {n_reqs:>4} calc request(s)  {n_items:>4} report item(s)  {blocking:>3} blocking"
    if emptied:
        line += f"  !! {emptied} EMPTIED"
    if measure_filters:
        line += f"  !! {measure_filters} MEASURE-FILTERS"
    elif measure_missing:
        line += "  ?? measure-filter key missing"
    return line


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
        f"{len(found)} workbook(s), most urgent first "
        "(invisible measure filters > emptied visuals > blocking items > queue size):",
        "",
    ]
    ranked = sorted(rows, key=lambda r: (-r[5], -r[4], -r[3], -(r[1] + r[2]), r[0].lower()))
    budget = max_bytes - sum(_blen(x) + 1 for x in out)
    more = "    ... and {n} more workbook(s) not listed here; raise --max-bytes"
    return "\n".join(out + _fit_lines([_list_line(r) for r in ranked], budget, more))


def build_json(wb_name: str, wb: dict, category: str | None) -> dict:
    """Machine-readable form: guidance hoisted out of the requests, so it appears once."""
    reqs = requests_of(wb)
    if category:
        reqs = [r for r in reqs if (r.get("category") or "uncategorised") == category]
    slim = [{k: v for k, v in r.items() if k != "category_guidance"} for r in reqs]
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
        "viz_fidelity": [v for v in (wb.get("viz_fidelity") or []) if isinstance(v, dict)],
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


def _render(args: argparse.Namespace, wb_name: str, wb: dict, source: Path, budget: int) -> str:
    """Pick the view. Every one of these is capped; `--name` is handled by the caller."""
    if args.viz:
        return render_viz(wb_name, wb, args.severity, args.category, budget)
    if args.category:
        return render_category(wb_name, requests_of(wb), args.category, budget)
    if args.fidelity:
        return render_fidelity(wb_name, wb, budget)
    return render_default(wb_name, wb, source, budget)


def main(argv: list[str] | None = None) -> int:
    """Exit 0 on a rendered queue, 2 on an unresolvable target or a cap too small to honour."""
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
        return 2

    # `print` appends a newline and that newline is output too, so a renderer gets one byte less
    # than the caller asked for. `--json` prints a confirmation, which is output as well.
    json_notice = f"\nwrote {args.json}" if args.json else ""
    budget = args.max_bytes - 1 - (_blen(json_notice) + 1 if json_notice else 0)

    try:
        found = load_workbooks(args.target)
        if args.list:
            print(_capped(render_list(found, budget), budget))
            return 0
        wb_name, wb, source = select_workbook(found, args.workbook)
    except HandoverError as exc:
        print(f"read_handover: {exc}", file=sys.stderr)
        return 2

    if args.name:
        print(render_named(wb_name, requests_of(wb), args.name, budget))
    else:
        print(_capped(_render(args, wb_name, wb, source, budget), budget))

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(build_json(wb_name, wb, args.category), indent=2) + "\n", encoding="utf-8")
        print(json_notice)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
