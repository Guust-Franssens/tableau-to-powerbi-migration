"""
purpose: Convert ONLY the prescriptive "State KPI Difference" filledMap to an
         azureMap choropleth, reusing the exact objects (mapControls / bubbleLayer /
         referenceLayer) from the Desktop-authored reference azureMap on Page 1.
         The measure already restricts to the selected region, so the redundant
         Region Filter is dropped. The 5 small regional small-multiples are left
         as filledMap (azureMap does not suit 384px small-multiples well).
usage:   python migrations/superstore-sales-performance/_work/convert_choropleth_only.py
"""

from __future__ import annotations

import json
from pathlib import Path

REPORT = Path(__file__).resolve().parents[1] / "fabric" / "SuperstoreSalesPerformance.Report"
PAGES = REPORT / "definition" / "pages"

# Desktop-authored reference azureMap (ground truth) and the target choropleth slot.
REFERENCE = PAGES / "723f6f17350b547bd330" / "visuals" / "1d5cd178f69085a49ae5" / "visual.json"
TARGET = PAGES / "prescriptive" / "visuals" / "9d3297e633e4cdaa9e20" / "visual.json"


def main() -> None:
    reference = json.loads(REFERENCE.read_text(encoding="utf-8"))
    target = json.loads(TARGET.read_text(encoding="utf-8"))

    ref_visual = reference["visual"]
    objects = ref_visual["objects"]

    # Drop the buggy polygonStrokeColor entry (its Desktop-authored mid color is the
    # placeholder literal 'midColor'); keep only the polygonFillColor gradient.
    for entry in objects.get("referenceLayer", []):
        entry.get("properties", {}).pop("polygonStrokeColor", None)

    tgt_visual = target["visual"]
    tgt_visual["visualType"] = "azureMap"
    tgt_visual["query"] = ref_visual["query"]  # Category = State
    tgt_visual["objects"] = objects
    # Keep the target's visualContainerObjects (title/background) and position.
    target.pop("filterConfig", None)  # measure already restricts to the selected region

    TARGET.write_text(json.dumps(target, indent=2), encoding="utf-8")
    print(f"Converted choropleth {TARGET.parent.name} -> azureMap (filterConfig removed)")


if __name__ == "__main__":
    main()
