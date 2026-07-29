"""
purpose: Convert deprecated Bing `filledMap` visuals to `azureMap` data-bound
         reference-layer choropleths in the Superstore PBIR report, reusing the
         ground-truth encoding hand-authored in Power BI Desktop (Category = key
         column, objects.referenceLayer = [url entry, polygonFillColor entry]).
usage:   python migrations/superstore-sales-performance/_work/convert_filledmaps_to_azuremap.py
"""

from __future__ import annotations

import json
from pathlib import Path

REPORT_DIR = Path(__file__).resolve().parents[1] / "fabric" / "SuperstoreSalesPerformance.Report"
PAGES_DIR = REPORT_DIR / "definition" / "pages"

# Public US-states boundary GeoJSON; its `name` property holds full state names
# (e.g. "California"), which matches the model's [State] column for auto-binding.
GEOJSON_URL = "https://raw.githubusercontent.com/PublicaMundi/MappingAPI/master/data/geojson/us-states.json"

# Clean region-highlighter presentation: road basemap for geographic context,
# a FIXED continental-US view (autoZoom off so Alaska/Hawaii/PR in the boundary
# file don't shrink the lower-48), no chrome. Bubbles off; only the region's
# states paint (unmappedObjectVisibility=false).
MAP_CONTROLS = {
    "defaultStyle": {"expr": {"Literal": {"Value": "'road'"}}},
    "showStylePicker": {"expr": {"Literal": {"Value": "false"}}},
    "showLabels": {"expr": {"Literal": {"Value": "false"}}},
    "showNavigationControls": {"expr": {"Literal": {"Value": "false"}}},
    "showSelectionControl": {"expr": {"Literal": {"Value": "false"}}},
    "autoZoom": {"expr": {"Literal": {"Value": "false"}}},
    "zoom": {"expr": {"Literal": {"Value": "2.0D"}}},
    "centerLatitude": {"expr": {"Literal": {"Value": "39.27954090366731D"}}},
    "centerLongitude": {"expr": {"Literal": {"Value": "-97.43611791666353D"}}},
}

DEFAULT_GREY = {"solid": {"color": {"expr": {"Literal": {"Value": "'#BFBFBF'"}}}}}


def extract_fill(visual: dict) -> dict:
    """Return the polygonFillColor value (a `fill`-typed object) from a filledMap.

    Prefers a measure-driven `fill` (FillRule gradient) when present, else the
    static `defaultColor`, else a neutral grey.
    """
    data_point = visual.get("objects", {}).get("dataPoint", [])
    props = data_point[0]["properties"] if data_point else {}
    if "fill" in props:  # measure-driven gradient choropleth
        return props["fill"]
    if "defaultColor" in props:  # static single-colour regional map
        return props["defaultColor"]
    return DEFAULT_GREY


def build_reference_layer(fill: dict) -> list[dict]:
    """Two-entry referenceLayer: URL datasource + conditional polygon fill.

    `unmappedObjectVisibility=false` hides states that are filtered out of the
    visual's data (e.g. non-selected regions), so only the region's states paint.
    """
    return [
        {
            "properties": {
                "datasourceType": {"expr": {"Literal": {"Value": "'url'"}}},
                "referenceLayerUrl": {"expr": {"Literal": {"Value": f"'{GEOJSON_URL}'"}}},
                "unmappedObjectVisibility": {"expr": {"Literal": {"Value": "false"}}},
            }
        },
        {
            "properties": {"polygonFillColor": fill},
            "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}]},
        },
    ]


def convert(visual_path: Path) -> bool:
    """Transform one filledMap visual.json to azureMap in place. Returns True if changed."""
    doc = json.loads(visual_path.read_text(encoding="utf-8"))
    visual = doc.get("visual", {})
    if visual.get("visualType") != "filledMap":
        return False

    fill = extract_fill(visual)
    visual["visualType"] = "azureMap"
    # Rebuild the map's object layers; keep query, visualContainerObjects, filterConfig.
    visual["objects"] = {
        "mapControls": [{"properties": MAP_CONTROLS}],
        "bubbleLayer": [{"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}],
        "referenceLayer": build_reference_layer(fill),
    }
    visual_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return True


def main() -> None:
    converted = []
    for visual_path in sorted(PAGES_DIR.glob("*/visuals/*/visual.json")):
        if convert(visual_path):
            page = visual_path.parents[2].name
            converted.append(f"{page}/{visual_path.parent.name}")
    print(f"Converted {len(converted)} filledMap -> azureMap:")
    for item in converted:
        print(f"  {item}")


if __name__ == "__main__":
    main()
