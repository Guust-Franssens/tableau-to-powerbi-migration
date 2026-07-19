import json
from pathlib import Path

P = str(Path(__file__).resolve().parents[2] / "migration-spec.json")
d = json.load(open(P, encoding="utf-8"))
lim = d["limitations_encountered"]
before = len(lim)

NEW = [
    {
        "item": "viz.sankey (ds.tree spatial fields: Nodes, Links, Border, Nodes with usage, Links with usage)",
        "issue": "The dashboard's tree/Sankey diagram is drawn from 5 spatial MAKEPOINT calculated fields on the 'tree' source; a Sankey/flow diagram has NO native Power BI visual and MAKEPOINT/MAKELINE have no DAX/Power Query equivalent. The 5 spatial fields are NOT modelled. Instead the underlying normalized layout coordinates are retained as ordinary columns on the 'Tree' table (X Normalized, Y Normalized, X/Y revised (for nodes), X/Y fixed for last path, Consumption axis, Source label, Filter) so the report can drive a custom/AppSource Sankey visual or fall back to a decomposition tree / treemap. HIGH capability gap for pbi-report-builder.",
        "severity": "high",
        "stage": "semantic_build",
    },
    {
        "item": "extract.tree.hyper (multi-table extract not fully exported)",
        "issue": "The 'tree' source's tree.hyper packages TWO tables - tree.csv (3,320 geometry rows, exported to ds.tree.csv) AND 'Elec generation' (6,061 consumption rows) - but the extractor exported only the geometry table. The consumption columns the tree calcs reference ([Per capita electricity - kWh], [Year], [Region], [Entity]) are ABSENT from ds.tree.csv. They were sourced from ds.elec_generation_per_capita_regions.csv (identical data) and the tree consumption calcs (CY, CY Consumption, Max year, Calculation2, Elec gen at 2022, 'Max year (copy)') were relocated onto the 'Elec Generation' table (child=Entity). Parser/extraction gap: multi-table .hyper sources should export every referenced table, not just the first.",
        "severity": "medium",
        "stage": "semantic_build",
    },
    {
        "item": "data blending premise (data_sources[])",
        "issue": "The '5 data sources = data blending' premise is inaccurate for this workbook: every one of the 17 worksheets is single-source and there are NO dashboard actions, so no active Tableau blend exists. The sources are alternative shapes of the same OWID electricity dataset. Mapped to 5 independent fact tables. The only genuine cross-table relationship is the tree noodle tree.csv[child] = 'Elec generation'[Entity] (authoritative from the .twb XML), modelled many-to-many. A conformed 'Year' dimension (1:many, single cross-filter to the 4 year-bearing facts) was added as the faithful translation of a 'blend on Year'.",
        "severity": "medium",
        "stage": "semantic_build",
    },
    {
        "item": "geo/entity conformed dimension (deliberately not materialised)",
        "issue": "The Country/Region 'blend keys' were intentionally NOT built into a shared geo dimension. Entity domains differ across sources (Per Capita FNR has 223 entities incl. 'World'; Elec Generation has 209 countries + 7 regions, no 'World'; Pivoted is country-grain) so a conformed geo dimension would introduce referential-integrity mismatches, and the single-source worksheets never require cross-source geo filtering. Documented as a design decision, not a defect; a report needing cross-source geo slicing would require a curated Entity/Region bridge.",
        "severity": "low",
        "stage": "semantic_build",
    },
    {
        "item": "rel.Tree_ElecGeneration (m:m direction)",
        "issue": "Tree[Child] -> Elec Generation[Entity] is modelled many-to-many with single cross-filter (Tree filters Elec Generation), chosen so the tree-source worksheets ('Country trend', 'Advanced table', 'Advanced table no region') filter consumption by child node. Deliberate assumption: switch to bothDirections if the report needs an Elec-Generation-side slicer (e.g. Region/Income group) to also filter which tree nodes display. child is non-unique in tree.csv (link+node+border rows) and Entity is non-unique per year, hence m:m rather than a 1:many star.",
        "severity": "low",
        "stage": "semantic_build",
    },
    {
        "item": "ds.tree / ds.pivoted / ds.by_source trellis + window-layout table calcs (dropped)",
        "issue": "Tableau small-multiple/layout and window table calcs were DROPPED as they compute visual grid coordinates, not model values: X-trellis = (index()-1) % int(SQRT(size())) and Y-trllis = int((index()-1)/int(sqrt(size()))) [small multiples are a native PBI visual feature]; Rank = index(); Consumption Axis Test = WINDOW_MIN(...); Y Axis Position; Normalised consumption = window min/max normalization; and the redundant aliases Year Axis Consumption (= [Year]) and Year Axis CY (= [CY]). The report should use the visual's built-in small-multiples, rank/Top-N and axis-normalization features instead.",
        "severity": "low",
        "stage": "semantic_build",
    },
    {
        "item": "ds.by_source ('By Source' table, vestigial)",
        "issue": "The 'By source' data source (caption 'Country & Regions', 5,496 rows) is bound by ZERO worksheets (verified across all 17). It is modelled as the 'By Source' table only to satisfy the explicit 5-table requirement; it is vestigial and can be dropped with no loss of report fidelity. Its % ratio calc columns were translated for completeness.",
        "severity": "low",
        "stage": "semantic_build",
    },
    {
        "item": "ds.elec_generation 'Global avg in 2022' (constant provenance)",
        "issue": "Tableau [Global avg in 2022] is the literal constant 3616.7, which equals the FNR 'World' entity's 2022 TOTAL per-capita (3616.692) - NOT the mean of the 209 country per-capita values in this source (that mean is 4074.4). Preserved as the literal 3616.7 to match the workbook's reference line; noted here so a future maintainer does not 'correct' it to a computed average.",
        "severity": "info",
        "stage": "semantic_build",
    },
]

lim.extend(NEW)
with open(P, "w", encoding="utf-8") as f:
    json.dump(d, f, indent=2, ensure_ascii=False)

print(f"appended {len(NEW)} entries ({before} -> {len(lim)})")

# re-validate against schema
import jsonschema

schema = json.load(
    open(
        Path(__file__).resolve().parents[4] / "docs" / "migration-spec.schema.json",
        encoding="utf-8",
    )
)
jsonschema.validate(d, schema)
print("SCHEMA VALIDATION: OK")
