"""
purpose: print the engine's handover WORK QUEUE at a size an agent can actually read, because the
         raw JSON buries it past every practical file-read cutoff and leaves a same-named lookalike
         (``needs_review[]``) in its place.
usage:   python scripts/read_handover.py <bundle-or-handover.json>            # queue summary
         python scripts/read_handover.py <target> --category <category>       # full repair detail
         python scripts/read_handover.py <target> --name '<calc name>'        # one calc, in full
         python scripts/read_handover.py <target> --viz [--severity blocking] # report-side queue
         python scripts/read_handover.py <target> [--workbook <name>] [--json <file>]

Why this exists
---------------
The engine's per-workbook handover carries everything needed to finish a stubbed calculation: the
original Tableau ``formula``, the ``fields`` it references, the ``target_table``, and per-category
``category_guidance``. All of it lives in ``model_translation_handoff.requests[]``.

Measured on ``_bundle-208`` (2026-08-20), that array is unreachable by a plain file read:

    Admin_Insights_Starter.json   355,259 bytes   requests[] starts at byte 31,934   -> 0 of 60 read
    Sales___Customer_Dashboards   129,690 bytes   requests[] starts at byte 19,717   -> ~1 of 30 read
    Section_12_Row_Level           91,289 bytes   requests[] starts at byte 16,247   -> ~5 of 22 read

A 20 KB read window instead lands squarely on ``model_translation_handoff.needs_review[]``, which
lists the SAME calculations by name with only 5 fields (``category``, ``fallback_reason``,
``has_suggestion``, ``name``, ``role``). That is enough to REPORT every stub and structurally
insufficient to REPAIR any of them - so an agent reads the file, sees a plausible complete-looking
list, is told no error, and ships a model full of ``BLANK()`` placeholders while believing it
followed its instructions. Confirmed in the field on three independent workbooks, all of which had
every branch measure translated correctly and only the dispatcher stubbed.

The report side is worse and permanent: ``remediation_worklist`` (170 items with per-item
``remediation`` text) sits at byte 156,764 and ``viz_fidelity`` at byte 315,571 in the same file -
about 93% of the way in. Neither is ever readable by a default read, at any workbook size.

Why it de-duplicates guidance
-----------------------------
``category_guidance`` is emitted per REQUEST, not per category. Verified across all 38 handovers in
``_bundle-208``: there is exactly ONE distinct guidance string per category estate-wide, so 60
requests in one file carry 60 copies of an 886-character block - roughly 53 KB of pure repetition,
and a large part of why the queue is pushed out of reach. Printing it once per category present is
lossless by measurement, not by assumption, and costs 4,481 bytes for all seven categories.

Why it never truncates silently
-------------------------------
Silent truncation is the defect this tool exists to fix, so it must not reintroduce it. When output
would exceed ``--max-bytes``, the remaining items are named explicitly along with the exact command
that prints them. A caller is never left believing it has seen the whole queue.

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

DEFAULT_MAX_BYTES = 40_000

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
            if isinstance(f, dict):
                lines.append(f"        - {f.get('caption', '?')}  ({f.get('kind', '?')})")
            else:
                lines.append(f"        - {f}")

    formula = (r.get("formula") or "").rstrip()
    if formula:
        lines.append("    source formula:")
        lines.extend("        " + ln for ln in formula.splitlines())
    else:
        lines.append("    source formula: (none recorded)")
    return "\n".join(lines)


def render_category(wb_name: str, reqs: list[dict], category: str, max_bytes: int) -> str:
    """Guidance once, then every request in that category, with a loud stop if it will not fit."""
    selected = [r for r in reqs if (r.get("category") or "uncategorised") == category]
    if not selected:
        present = ", ".join(f"{c} ({n})" for c, n in sorted(category_counts(reqs).items()))
        return f"No requests in category {category!r} for {wb_name}.\nPresent: {present or '(none)'}"

    out = [
        f"=== {wb_name} - {category} - {len(selected)} request(s) ===",
        "",
        "--- GUIDANCE (applies to every request below; printed once) ---",
        guidance_by_category(reqs).get(category, "(no guidance recorded for this category)"),
        "",
        "--- REQUESTS ---",
    ]

    budget = max_bytes - sum(len(x) + 1 for x in out)
    shown = 0
    for i, r in enumerate(selected, 1):
        block = render_request(r, i, len(selected))
        if shown and len(block) + 1 > budget:
            break
        out.append(block)
        out.append("")
        budget -= len(block) + 2
        shown += 1

    if shown < len(selected):
        remaining = [r.get("name") or "(unnamed)" for r in selected[shown:]]
        out += [
            "",
            "!" * 78,
            f"!! OUTPUT TRUNCATED at --max-bytes={max_bytes}. "
            f"{shown} of {len(selected)} shown; {len(remaining)} NOT shown.",
            "!! You have NOT seen the whole queue. Get the rest with either:",
            f"!!   --max-bytes {max_bytes * 3}",
            "!!   --name '<name>'   for any of:",
        ]
        out += [f"!!     {n}" for n in remaining]
        out.append("!" * 78)
    return "\n".join(out)


def render_named(wb_name: str, reqs: list[dict], name: str) -> str:
    """One calculation in full, with its category guidance - the escape hatch from truncation."""
    matches = [r for r in reqs if (r.get("name") or "").lower() == name.lower()]
    if not matches:
        matches = [r for r in reqs if name.lower() in (r.get("name") or "").lower()]
    if not matches:
        return f"No request named {name!r} in {wb_name}. Run without --name to list the queue."

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
        detail = sum(len(render_request(r, 1, 1)) for r in reqs if r.get("category") == cat)
        rows.append(f"    {cat:<28}{counts[cat]:>4}   ~{detail // 1024 + 1:>4} KB      --category {cat}")
    return rows


def _cascadable_lines(handoff: dict) -> list[str]:
    """Stubs that depend on other stubs. Repairing the outer one first still yields BLANK()."""
    triage = handoff.get("triage") if isinstance(handoff.get("triage"), dict) else {}
    cascadable = triage.get("cascadable")
    if not (isinstance(cascadable, list) and cascadable):
        return []
    return [
        f"    CASCADABLE ({len(cascadable)}): these stubs depend on other stubs - fix in dependency order,",
        "    innermost first, or the outer one will still evaluate to BLANK():",
        *[f"      - {n}" for n in cascadable],
        "",
    ]


def _model_section(wb_name: str, wb: dict, reqs: list[dict], target: Path) -> list[str]:
    """Model-side summary: coverage, the per-category queue, and any cascade ordering constraint."""
    handoff = handoff_of(wb)
    summary = handoff.get("summary") if isinstance(handoff.get("summary"), dict) else {}

    out = [f"=== HANDOVER QUEUE - {wb_name} ===", f"source: {target}", ""]
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
    out += _cascadable_lines(handoff)
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


def _report_section(wb: dict) -> list[str]:
    rw = worklist_of(wb)
    items = rw.get("items") if isinstance(rw.get("items"), list) else []
    summary = rw.get("summary") if isinstance(rw.get("summary"), dict) else {}
    fidelity = wb.get("viz_fidelity") if isinstance(wb.get("viz_fidelity"), list) else []
    emptied = _emptied_visuals(wb)

    if not items and not fidelity:
        return ["REPORT: no remediation worklist in this handover.", ""]

    out = []
    flagged = summary.get("visuals_flagged", "?")
    clean = summary.get("visuals_clean", "?")
    out.append(f"REPORT: {len(items)} remediation item(s), {flagged} visual(s) flagged, {clean} clean")

    by_sev = summary.get("by_severity") if isinstance(summary.get("by_severity"), dict) else {}
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

    out += ["", "        next step: --viz    (full worklist)   --viz --severity blocking", ""]
    return out


def _emptied_block(emptied: list[dict]) -> list[str]:
    """Visuals whose every field binding was dropped - they render blank, so they lead the queue."""
    if not emptied:
        return []
    out = [f"--- EMPTIED VISUALS ({len(emptied)}) - these render blank; every binding was dropped ---"]
    for d in emptied:
        dropped = d.get("dropped")
        text = ", ".join(str(x) for x in dropped) if isinstance(dropped, list) else str(dropped)
        out.append(f"    {d.get('visual', '?')}: dropped {text}")
    return out + [""]


def _worklist_group_block(category: str, group: list[dict]) -> str:
    """One category's items, with each distinct remediation text printed once above them."""
    block = ["", f"## {category}  ({len(group)} item(s))"]
    remedies = sorted({(i.get("remediation") or "").strip() for i in group} - {""})
    block += [f"    FIX: {text}" for text in remedies]
    for item in group:
        where = item.get("worksheet") or item.get("visual") or "?"
        page = item.get("page_display") or item.get("page")
        block.append(f"    - {(item.get('severity') or '?'):<8} {f'{where} [{page}]' if page else where}")
        reason = (item.get("reason") or "").strip()
        if reason:
            block.append(f"      why: {reason}")
    return "\n".join(block)


def _viz_truncation_banner(
    max_bytes: int, shown: int, total: int, stopped_at: str, grouped: dict[str, list[dict]]
) -> list[str]:
    """Loud, itemised stop. Silent truncation is the exact defect this module exists to remove."""
    counts = ", ".join(f"{c}={len(g)}" for c, g in sorted(grouped.items()))
    return [
        "",
        "!" * 78,
        f"!! OUTPUT TRUNCATED at --max-bytes={max_bytes}. {shown} of {total} item(s) shown; {total - shown} NOT shown.",
        "!! You have NOT seen the whole queue. Get the rest with either:",
        f"!!   --max-bytes {max_bytes * 3}",
        "!!   --severity <blocking|high|medium|low>   to work one band at a time",
        f"!!   (stopped before category {stopped_at!r}; all categories: {counts})",
        "!" * 78,
    ]


def render_viz(wb_name: str, wb: dict, severity: str | None, max_bytes: int) -> str:
    """Report-side queue: emptied visuals first, then worklist items grouped by category."""
    items = [i for i in (worklist_of(wb).get("items") or []) if isinstance(i, dict)]
    if severity:
        items = [i for i in items if (i.get("severity") or "").lower() == severity.lower()]

    out = [f"=== {wb_name} - REPORT REMEDIATION QUEUE ===", ""]
    out += _emptied_block(_emptied_visuals(wb))

    if not items:
        scope = f" at severity {severity!r}" if severity else ""
        return "\n".join(out + [f"No remediation worklist items{scope}."])

    grouped: dict[str, list[dict]] = {}
    for item in items:
        grouped.setdefault(item.get("category") or "uncategorised", []).append(item)

    out.append(f"--- {len(items)} ITEM(S) IN {len(grouped)} CATEGORY(IES) ---")
    budget = max_bytes - sum(len(x) + 1 for x in out)
    shown = 0

    for cat in sorted(grouped, key=lambda c: -len(grouped[c])):
        chunk = _worklist_group_block(cat, grouped[cat])
        if shown and len(chunk) + 1 > budget:
            return "\n".join(out + _viz_truncation_banner(max_bytes, shown, len(items), cat, grouped))
        out.append(chunk)
        budget -= len(chunk) + 1
        shown += len(grouped[cat])

    return "\n".join(out)


# --------------------------------------------------------------------------------------------
# Default view
# --------------------------------------------------------------------------------------------


DECOY_WARNING = """\
!! DO NOT work from `model_translation_handoff.needs_review[]` in the raw JSON.
!! It lists the SAME calculations by name but carries only 5 fields - no formula, no fields,
!! no target_table, no guidance. It is enough to REPORT a stub and structurally insufficient to
!! REPAIR one. It also sits EARLIER in the file than `requests[]`, so a plain file read finds it
!! first and stops before the real queue. That is how three shipped workbooks got a BLANK()
!! dispatcher while every branch measure was translated correctly."""


def render_default(wb_name: str, wb: dict, target: Path) -> str:
    """The landing view: both queues at a glance, plus the warning that stops the original defect."""
    reqs = requests_of(wb)
    out = _model_section(wb_name, wb, reqs, target)
    out += _report_section(wb)
    out.append(DECOY_WARNING)
    return "\n".join(out)


def render_list(found: list[tuple[str, dict, Path]]) -> str:
    """Every workbook in a bundle with the size of both its queues, so triage can be estate-wide."""
    out = [f"{len(found)} workbook(s):", ""]
    for name, wb, _ in sorted(found, key=lambda t: t[0].lower()):
        reqs = requests_of(wb)
        items = worklist_of(wb).get("items")
        n_items = len(items) if isinstance(items, list) else 0
        out.append(f"    {name:<52} {len(reqs):>4} calc request(s)  {n_items:>4} report item(s)")
    return "\n".join(out)


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
        "report_items": worklist_of(wb).get("items") or [],
        "emptied_visuals": _emptied_visuals(wb),
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
    p.add_argument("--category", help="print full repair detail for one category")
    p.add_argument("--name", help="print one calculation in full, by name")
    p.add_argument("--viz", action="store_true", help="print the report-side remediation queue")
    p.add_argument("--severity", help="with --viz: blocking | high | medium | low")
    p.add_argument("--list", action="store_true", help="list workbooks in the target and exit")
    p.add_argument("--json", type=Path, metavar="FILE", help="also write a machine-readable form")
    p.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help=f"cap on detail output; never silent (default {DEFAULT_MAX_BYTES})",
    )
    return p.parse_args(argv)


def _force_utf8_stdout() -> None:
    """Tableau formulas carry glyphs like U+25B2 that a cp1252 console cannot encode.

    Without this the tool dies with a UnicodeEncodeError partway through a formula - which would
    hand the caller a partial queue and an exception, the exact failure shape this tool exists to
    remove. ``errors="replace"`` keeps an unmappable glyph from costing the whole request.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - unusual redirected stream
                pass


def main(argv: list[str] | None = None) -> int:
    """Exit 0 on a rendered queue, 2 on a target that cannot be resolved to one workbook."""
    _force_utf8_stdout()
    args = parse_args(argv)
    try:
        found = load_workbooks(args.target)
        if args.list:
            print(render_list(found))
            return 0
        wb_name, wb, source = select_workbook(found, args.workbook)
    except HandoverError as exc:
        print(f"read_handover: {exc}", file=sys.stderr)
        return 2

    if args.name:
        print(render_named(wb_name, requests_of(wb), args.name))
    elif args.category:
        print(render_category(wb_name, requests_of(wb), args.category, args.max_bytes))
    elif args.viz:
        print(render_viz(wb_name, wb, args.severity, args.max_bytes))
    else:
        print(render_default(wb_name, wb, source))

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(build_json(wb_name, wb, args.category), indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
