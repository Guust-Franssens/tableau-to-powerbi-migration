"""
purpose: Statically detect z-order occlusion in a PBIR report - any visual (typically a
         carried-over Tableau background image) whose rectangle fully contains another
         visual that sits at a LOWER z, making the covered visual invisible in Desktop.
         No Power BI Desktop required; reads visual.json only.
usage:   python detect_occlusion.py <path-to-*.Report> [--fix] [--json <out.json>]
         --fix rewrites position.z in place: occluders are sent to the back of their page.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# A visual must cover at least this fraction of the page to count as a "background" layer.
# Below it, overlap is ordinary design (a card on a shape), not a whole-page cover.
BACKGROUND_AREA_FRACTION = 0.5

# Visual types that never carry data; occluding one of these is cosmetic, not a data loss.
NON_DATA_TYPES = {"image", "textbox", "shape", "actionButton", "basicShape"}


def visual_type(doc: dict) -> str:
    """PBIR nests the type differently for grouped vs plain visuals."""
    vis = doc.get("visual") or {}
    return vis.get("visualType") or doc.get("visualType") or ("group" if "visualGroup" in doc else "?")


def load_visuals(report_dir: Path) -> dict[str, list[dict]]:
    """Return {page_name: [visual_record, ...]} for every visual.json under the report."""
    pages: dict[str, list[dict]] = {}
    for vj in sorted(report_dir.rglob("visuals/*/visual.json")):
        try:
            doc = json.loads(vj.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError) as ex:
            print(f"  ! unreadable {vj}: {ex}", file=sys.stderr)
            continue
        pos = doc.get("position") or {}
        if not {"x", "y", "width", "height"} <= set(pos):
            continue
        page = vj.parents[2].name
        pages.setdefault(page, []).append(
            {
                "path": vj,
                "name": doc.get("name") or vj.parent.name,
                "type": visual_type(doc),
                "x": float(pos["x"]),
                "y": float(pos["y"]),
                "w": float(pos["width"]),
                "h": float(pos["height"]),
                "z": float(pos.get("z", 0)),
                "doc": doc,
            }
        )
    return pages


def contains(outer: dict, inner: dict, pad: float = 1.0) -> bool:
    """True when outer's rect fully covers inner's rect (pad = tolerance in px)."""
    return (
        outer["x"] <= inner["x"] + pad
        and outer["y"] <= inner["y"] + pad
        and outer["x"] + outer["w"] >= inner["x"] + inner["w"] - pad
        and outer["y"] + outer["h"] >= inner["y"] + inner["h"] - pad
    )


def page_canvas(report_dir: Path, page: str) -> tuple[float, float]:
    """Read the page's declared canvas size, falling back to the 1280x720 default."""
    pj = report_dir / "definition" / "pages" / page / "page.json"
    if pj.exists():
        try:
            doc = json.loads(pj.read_text(encoding="utf-8-sig"))
            return float(doc.get("width", 1280)), float(doc.get("height", 720))
        except (json.JSONDecodeError, OSError, TypeError):
            pass
    return 1280.0, 720.0


def analyse(report_dir: Path) -> list[dict]:
    """Find every (occluder, victim) pair on every page."""
    findings = []
    for page, visuals in load_visuals(report_dir).items():
        cw, ch = page_canvas(report_dir, page)
        page_area = cw * ch
        for occluder in visuals:
            # Only whole-page, non-data layers can plausibly be a carried-over background.
            if occluder["type"] not in NON_DATA_TYPES:
                continue
            if page_area and (occluder["w"] * occluder["h"]) / page_area < BACKGROUND_AREA_FRACTION:
                continue
            victims = [v for v in visuals if v is not occluder and v["z"] < occluder["z"] and contains(occluder, v)]
            if not victims:
                continue
            data_victims = [v for v in victims if v["type"] not in NON_DATA_TYPES]
            findings.append(
                {
                    "page": page,
                    "occluder": occluder["name"],
                    "occluder_type": occluder["type"],
                    "occluder_z": occluder["z"],
                    "occluder_rect": [
                        occluder["x"],
                        occluder["y"],
                        occluder["w"],
                        occluder["h"],
                    ],
                    "canvas": [cw, ch],
                    "covered_total": len(victims),
                    "covered_data_visuals": len(data_victims),
                    "covered_names": sorted(v["name"] for v in data_victims),
                    "covered_types": sorted({v["type"] for v in data_victims}),
                }
            )
    return findings


def fix(report_dir: Path) -> int:
    """Send every detected occluder behind everything else on its page. Returns files changed."""
    changed = 0
    flagged = {(f["page"], f["occluder"]) for f in analyse(report_dir)}
    if not flagged:
        return 0
    for page, visuals in load_visuals(report_dir).items():
        page_flagged = [v for v in visuals if (page, v["name"]) in flagged]
        if not page_flagged:
            continue
        floor = min(v["z"] for v in visuals)
        # Stack occluders strictly below the lowest existing z, preserving their relative order.
        for offset, v in enumerate(sorted(page_flagged, key=lambda d: -d["z"]), start=1):
            v["doc"]["position"]["z"] = floor - offset
            v["path"].write_text(json.dumps(v["doc"], indent=2), encoding="utf-8")
            changed += 1
    return changed


def main() -> int:
    """Parse arguments, report occlusions, and optionally fix them in place."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("report", type=Path, help="path to the *.Report folder")
    ap.add_argument("--fix", action="store_true", help="send occluders to the back, in place")
    ap.add_argument("--json", type=Path, help="write findings as JSON")
    args = ap.parse_args()

    if not args.report.exists():
        print(f"ERROR: no such report folder: {args.report}", file=sys.stderr)
        return 2

    findings = analyse(args.report)
    if args.json:
        args.json.write_text(json.dumps(findings, indent=2), encoding="utf-8")

    if not findings:
        print(f"OCCLUSION: none detected in {args.report.name}")
        return 0

    total_data = sum(f["covered_data_visuals"] for f in findings)
    # Several occluders can stack over the same visuals, so also report the unique count.
    unique = {(f["page"], n) for f in findings for n in f["covered_names"]}
    print(
        f"OCCLUSION: {len(findings)} occluder(s) hiding {len(unique)} distinct data visual(s) "
        f"({total_data} occluder-visual pairs) in {args.report.name}"
    )
    for f in sorted(findings, key=lambda d: (d["page"], -d["covered_data_visuals"])):
        rect = ", ".join(f"{n:.0f}" for n in f["occluder_rect"])
        print(
            f"  {f['page']}: {f['occluder_type']} z={f['occluder_z']:.0f} ({rect}) "
            f"covers {f['covered_data_visuals']} data visual(s) {f['covered_types']}"
        )

    if args.fix:
        n = fix(args.report)
        print(f"FIXED: rewrote position.z in {n} visual.json file(s)")
        remaining = analyse(args.report)
        print(f"RE-CHECK: {len(remaining)} occluder(s) remain")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
