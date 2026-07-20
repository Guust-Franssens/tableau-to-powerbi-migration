"""
purpose: Score parsed Tableau workbook specs (from the harvester's _harvest/specs/) by
         "idiom hardness" to pick the most challenging, idiom-diverse candidates for a
         full Tableau -> Power BI migration. Weights the idioms we have NOT yet stressed
         (LOD expressions, live/non-extract connections, heavy table calcs) highest, so
         the selection avoids easy extract-based single-dashboard smoke tests.
usage:   python scripts/classify_harvest_hardness.py [--specs _harvest/specs] [--top 20]
"""

import argparse
import json
from pathlib import Path

# Idiom weights - unexercised / high-learning idioms score highest.
WEIGHTS = {
    "lod": 8.0,  # {FIXED/INCLUDE/EXCLUDE} - documented #1 gap, barely exercised
    "tcalc": 5.0,  # WINDOW_*/RUNNING_*/RANK/LOOKUP/INDEX - only lightly hit (tale100)
    "live": 30.0,  # non-extract connection.mode - never exercised end to end
    "spatial": 4.0,  # MAKEPOINT/MAKELINE geometry
    "joins": 3.0,  # multi-table relationship graphs
    "dash": 2.5,  # multi-dashboard navigation
    "ds": 2.0,  # multiple data sources / blends
    "params": 1.5,  # parameter-driven interactivity
    "calc": 1.0,  # plain calculated fields
    "ws": 0.5,  # worksheet breadth
}


def score_spec(spec: dict) -> dict:
    """Return the hardness metrics + weighted score for one parsed migration-spec."""
    sources = spec.get("data_sources", [])
    fields = [f for ds in sources for f in ds.get("fields", [])]
    metrics = {
        "lod": sum(1 for f in fields if f.get("is_lod")),
        "tcalc": sum(1 for f in fields if f.get("is_table_calc")),
        "calc": sum(1 for f in fields if f.get("kind") == "calculated"),
        "spatial": sum(1 for f in fields if f.get("data_type") == "spatial"),
        "live": 1 if any((ds.get("connection") or {}).get("mode") == "live" for ds in sources) else 0,
        "joins": sum(len(ds.get("joins", [])) for ds in sources),
        "ws": len(spec.get("worksheets", [])),
        "dash": len(spec.get("dashboards", [])),
        "params": len(spec.get("parameters", [])),
        "ds": len(sources),
    }
    score = sum(weight * metrics[k] for k, weight in WEIGHTS.items())
    # Primary hard idiom drives diversity of the final pick.
    primary = "extract-simple"
    if metrics["live"]:
        primary = "LIVE-conn"
    elif metrics["lod"] >= 3:
        primary = "LOD-heavy"
    elif metrics["tcalc"] >= 3:
        primary = "tablecalc-heavy"
    elif metrics["spatial"] >= 1:
        primary = "spatial"
    elif metrics["dash"] >= 3:
        primary = "multi-dashboard"
    elif metrics["joins"] >= 2:
        primary = "multi-join/blend"
    return {"score": round(score, 1), "primary": primary, **metrics}


def main() -> None:
    """Rank harvested specs by idiom hardness and print the top-N table."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--specs", type=Path, default=Path("_harvest") / "specs")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    rows = []
    for f in sorted(args.specs.glob("*.json")):
        try:
            spec = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        rows.append({"repo": f.stem, "name": (spec.get("source") or {}).get("file_name", ""), **score_spec(spec)})

    rows.sort(key=lambda r: r["score"], reverse=True)
    header = f"{'repo':<40} {'score':>6} {'primary':<16} {'LOD':>3} {'TC':>3} {'calc':>4} {'live':>4}"
    header += f" {'sp':>3} {'jn':>3} {'ws':>3} {'dsh':>3} {'par':>3} {'ds':>3}"
    print(header)
    for r in rows[: args.top]:
        line = f"{r['repo'][:40]:<40} {r['score']:>6} {r['primary']:<16} {r['lod']:>3} {r['tcalc']:>3}"
        line += f" {r['calc']:>4} {r['live']:>4} {r['spatial']:>3} {r['joins']:>3}"
        line += f" {r['ws']:>3} {r['dash']:>3} {r['params']:>3} {r['ds']:>3}"
        print(line)
    print(f"\ntotal specs scored: {len(rows)}")


if __name__ == "__main__":
    main()
