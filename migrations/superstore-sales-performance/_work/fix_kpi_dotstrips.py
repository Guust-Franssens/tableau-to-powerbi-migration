"""
purpose: Rebuild the 3 Region-Comp KPI dot-strips (SALES / PROFIT RATIO / AVG DAYS
         TO SHIP) on the Descriptive page as valid scatter "dot plots": one dot per
         Region on a constant Y baseline, positioned + sized by the per-region value,
         coloured red->grey->blue by the signed performance-vs-prior-period diff.
         Fixes the invalid Y=[Region] binding that broke the visuals.
usage:   python migrations/superstore-sales-performance/_work/fix_kpi_dotstrips.py
"""

from __future__ import annotations

import json
from pathlib import Path

VISUALS = (
    Path(__file__).resolve().parents[1]
    / "fabric"
    / "SuperstoreSalesPerformance.Report"
    / "definition"
    / "pages"
    / "descriptive"
    / "visuals"
)

# visual id -> (per-region current-value measure, signed performance-diff measure)
STRIPS = {
    "2dbe9853d394a1b57953": ("CP Sales (All Regions)", "Sales Perf Diff (All Regions)"),
    "a80649c388c75d6e1b41": ("CP Profit Ratio (All Regions)", "Profit Ratio Perf Diff (All Regions)"),
    "9c76a20b369912a7aa9a": ("CP Days To Ship (All Regions)", "Days To Ship Perf Diff (All Regions)"),
}

ENTITY = "Sample Superstore"


def measure_projection(name: str, active: bool = False) -> dict:
    proj = {
        "field": {"Measure": {"Expression": {"SourceRef": {"Entity": ENTITY}}, "Property": name}},
        "queryRef": f"{ENTITY}.{name}",
        "nativeQueryRef": name,
    }
    if active:
        proj["active"] = True
    return proj


def diverging_fill(diff_measure: str) -> dict:
    """red (decreased) -> grey (0) -> blue (improved) gradient on the signed diff."""
    return {
        "solid": {
            "color": {
                "expr": {
                    "FillRule": {
                        "Input": {
                            "Measure": {"Expression": {"SourceRef": {"Entity": ENTITY}}, "Property": diff_measure}
                        },
                        "FillRule": {
                            "linearGradient3": {
                                "min": {"color": {"Literal": {"Value": "'#FC4237'"}}},
                                "mid": {
                                    "color": {"Literal": {"Value": "'#E6E6E6'"}},
                                    "value": {"Literal": {"Value": "0D"}},
                                },
                                "max": {"color": {"Literal": {"Value": "'#34657F'"}}},
                                "nullColoringStrategy": {"strategy": {"Literal": {"Value": "'asZero'"}}},
                            }
                        },
                    }
                }
            }
        }
    }


def fix(visual_id: str, value_measure: str, diff_measure: str) -> None:
    path = VISUALS / visual_id / "visual.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    qs = doc["visual"]["query"]["queryState"]

    # Y baseline (constant) so the scatter has a valid numeric Y; all dots on one line.
    qs["Y"] = {"projections": [measure_projection("Dot Baseline")]}
    # Size = per-region current value ("circle size represents current period value").
    qs["Size"] = {"projections": [measure_projection(value_measure)]}

    objects = doc["visual"]["objects"]
    objects["dataPoint"] = [
        {
            "properties": {"fill": diverging_fill(diff_measure)},
            "selector": {"data": [{"dataViewWildcard": {"matchingOption": 0}}]},
        }
    ]
    # Hide the meaningless (constant) Y axis + its title; keep the X value axis + region labels.
    objects["valueAxis"] = [
        {
            "properties": {
                "show": {"expr": {"Literal": {"Value": "false"}}},
                "showAxisTitle": {"expr": {"Literal": {"Value": "false"}}},
            }
        }
    ]

    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print(f"  {visual_id}: Y->Dot Baseline, Size->{value_measure}, colour->{diff_measure}")


def main() -> None:
    print("Rebuilt Region-Comp dot-strips:")
    for visual_id, (value_measure, diff_measure) in STRIPS.items():
        fix(visual_id, value_measure, diff_measure)


if __name__ == "__main__":
    main()
