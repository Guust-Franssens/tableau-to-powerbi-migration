"""
Generate DISPOSITIONS.md: a complete audit mapping EVERY Tableau calculated field
(61 across the 5 sources) to its fate in the semantic model, with a reason.
Asserts 100% coverage (fails loudly if the spec has a calc field not in the registry).
"""

import json, os
from pathlib import Path

SPEC = str(Path(__file__).resolve().parents[2] / "migration-spec.json")
OUT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "DISPOSITIONS.md"))

DS_SHORT = {
    "ds.per_capita_electricity_fossil_nuclear_renewables": "fnr",
    "ds.tree": "tree",
    "ds.pivoted_per_capita_electricity_generation_by_source": "pivoted",
    "ds.by_source": "bysource",
    "ds.elec_generation_per_capita_regions": "elecgen",
}

# (ds_short, caption) -> (fate, target table/location, reason)
CC, ME, DR, GAP = "calc column", "measure", "dropped", "capability gap"
DISP = {
    # ---- Per Capita FNR ----
    ("fnr", "FF%"): (CC, "Per Capita FNR", "Row-level ratio FF/(FF+Nuc+Ren). Ground truth World 2022=0.61432."),
    ("fnr", "Nuclear%"): (CC, "Per Capita FNR", "Row-level ratio. World 2022=0.09152."),
    ("fnr", "Renewables %"): (CC, "Per Capita FNR", "Row-level ratio. World 2022=0.29416."),
    # ---- Tree geometry (kept as calc columns) ----
    ("tree", "X Normalized"): (CC, "Tree", "[x]/{MAX([x])} -> DIVIDE(X, MAXX(ALL(Tree),X))."),
    ("tree", "Y Normalized"): (CC, "Tree", "[y]/{MAX([y])}."),
    ("tree", "X revised (for nodes)"): (CC, "Tree", "Node-only scaling by Usage/{MAX(Usage)}."),
    ("tree", "Y revised (for nodes)"): (CC, "Tree", "Node-only scaling on Y."),
    ("tree", "X fixed for last path"): (
        CC,
        "Tree",
        "DIMENSION-FIXED LOD {FIXED [id]: MAX(IF type='link' THEN path)} -> CALCULATE(MAX(Path), ALLEXCEPT(Tree,Id), Type='link'). Ground truth mlp verified.",
    ),
    ("tree", "Y fixed for last path"): (CC, "Tree", "FIXED[id] LOD on Y (internal 'X doubled for last path (copy)')."),
    ("tree", "Calculation1"): (
        CC,
        "Tree",
        "FIXED[id] LOD {FIXED [id]: AVG(Usage)} -> CALCULATE(AVERAGE(Usage), ALLEXCEPT(Tree,Id)).",
    ),
    ("tree", "Consumption axis"): (CC, "Tree", "= [child] passthrough."),
    ("tree", "Child (copy)"): (CC, "Tree", "= [child] duplicate."),
    ("tree", "Source label"): (CC, "Tree", "IF type='link' THEN [parent]."),
    ("tree", "Filter"): (CC, "Tree", "Boolean [type]='link' AND ISNULL([parent]) (top-of-tree)."),
    ("tree", "X Axis -  Bar chart"): (ME, "Tree", "MIN(0.0) -> constant 0 axis anchor measure."),
    ("tree", "X Axis - Rank"): (ME, "Tree", "MIN(0.0) -> 0 axis anchor."),
    ("tree", "X Axis - Text"): (ME, "Tree", "MIN(0.0) -> 0 axis anchor."),
    # ---- Tree consumption calcs relocated to Elec Generation ----
    ("tree", "CY"): (ME, "Elec Generation", "{MAX([Year])} partition-less LOD -> CALCULATE(MAX(Year), ALL()). =2023."),
    ("tree", "CY Consumption"): (
        ME,
        "Elec Generation",
        "DIMENSION-FIXED LOD {FIXED [child]: sum(IF Year=CY THEN percap)}; CY hoisted to VAR. Ground truth Norway@2023=28056.230.",
    ),
    ("tree", "Calculation2"): (
        ME,
        "Elec Generation",
        "Boolean [CY Consumption] > {FIXED [Region]: AVG(percap)}; measure hoisted to VAR (no compact filter).",
    ),
    ("tree", "Max year"): (
        ME,
        "Elec Generation",
        "WINDOW table calc WINDOW_MAX(MAX([Year]))=MAX([Year]) -> CALCULATE(MAX(Year), ALLEXCEPT(Entity)) vs MAX(Year) in context. Norway@2023=28056.230, @2022=BLANK.",
    ),
    ("tree", "Elec gen at 2022"): (CC, "Elec Generation", "IF [Year]=2022 THEN percap. Relocated row-level column."),
    ("tree", "Max year (copy)"): (
        CC,
        "Elec Generation",
        "IF [Year]=[CY] THEN percap -> calc column 'Consumption at CY'.",
    ),
    ("tree", "Region and country count"): (
        CC,
        "Elec Generation",
        "FIXED[Region] COUNTD(Entity) -> ALLEXCEPT(Region) label string.",
    ),
    # ---- Tree view toggles (DZV) -> measures on 'Show chart as' ----
    ("tree", "DZV: show tree map"): (ME, "Show chart as", "[Parameter 1]='Tree' -> SELECTEDVALUE toggle measure."),
    ("tree", "DZV: table with region"): (ME, "Show chart as", "[Parameter 1]='Table'."),
    ("tree", "DZV: table without region"): (ME, "Show chart as", "[Parameter 1]='Table no region'."),
    # ---- Tree DROPPED (trellis / window / redundant aliases) ----
    ("tree", "X-trellis"): (
        DR,
        "-",
        "Small-multiple grid X = (index()-1)%int(SQRT(size())). Native PBI small-multiples feature.",
    ),
    ("tree", "Y-trllis"): (DR, "-", "Small-multiple grid Y. Native PBI small-multiples."),
    ("tree", "Consumption Axis Test"): (DR, "-", "WINDOW_MIN(...) table calc for mark positioning; visual-only."),
    ("tree", "Y Axis Position"): (DR, "-", "Depends on Consumption Axis Test + Normalised consumption; visual layout."),
    ("tree", "Normalised consumption"): (DR, "-", "Window min/max normalization; use visual axis normalization."),
    ("tree", "Year Axis Consumption"): (DR, "-", "= [Year] redundant alias; report binds Elec Generation[Year]."),
    ("tree", "Year Axis CY"): (DR, "-", "= [CY] redundant alias; use measure [CY]."),
    # ---- Tree spatial (Sankey) -> capability gap ----
    ("tree", "Nodes"): (GAP, "-", "MAKEPOINT node geometry; no native visual. Underlying X/Y Normalized retained."),
    ("tree", "Links"): (GAP, "-", "MAKEPOINT link geometry; Sankey gap."),
    ("tree", "Border"): (GAP, "-", "MAKEPOINT border geometry; Sankey gap."),
    ("tree", "Nodes with usage"): (GAP, "-", "MAKEPOINT node geometry scaled by usage; Sankey gap."),
    ("tree", "Links with usage"): (GAP, "-", "MAKEPOINT link geometry scaled by usage; Sankey gap."),
    # ---- Pivoted ----
    ("pivoted", "FF Electricity"): (
        CC,
        "Pivoted",
        "DIMENSION-FIXED LOD {FIXED [Year],[Code]: SUM(FF)} -> CALCULATE(SUM, FILTER(ALLEXCEPT(Year,Code), Fuel='Fossil Fuel')). USA 2022=7559.257.",
    ),
    ("pivoted", "Nuclear Electricity"): (CC, "Pivoted", "FIXED[Year,Code] Nuclear. FRA 2022=4560.504."),
    ("pivoted", "Renewable Electricity"): (CC, "Pivoted", "FIXED[Year,Code] Renewables."),
    ("pivoted", "FF Electricity%"): (CC, "Pivoted", "FF/(FF+Nuc+Ren) over the FIXED LODs. USA 2022=0.59652."),
    ("pivoted", "Nuclear Electricity%"): (CC, "Pivoted", "Nuc/(...). FRA 2022=0.62795."),
    ("pivoted", "Renewable Electricity%"): (CC, "Pivoted", "Ren/(...). USA 2022=0.22351."),
    ("pivoted", "Year for label"): (CC, "Pivoted", "IF [Year]=2020 THEN [Year]."),
    ("pivoted", "Region and country count"): (
        CC,
        "Pivoted",
        "FIXED[Region] COUNTD(Country) -> ALLEXCEPT(Region) label.",
    ),
    ("pivoted", "DZV: World"): (ME, "View data for", "[Parameter 2]='World' -> SELECTEDVALUE toggle."),
    ("pivoted", "DZV: Region"): (ME, "View data for", "[Parameter 2]='Region'."),
    ("pivoted", "DZV: Country"): (ME, "View data for", "[Parameter 2]='Country'."),
    ("pivoted", "X-trellis"): (DR, "-", "Small-multiple grid X; native PBI small-multiples."),
    ("pivoted", "Y-trllis"): (DR, "-", "Small-multiple grid Y; native PBI small-multiples."),
    ("pivoted", "Rank"): (DR, "-", "index() rank; use visual Top-N / rank."),
    # ---- By Source (vestigial) ----
    ("bysource", "% of Fossil Fuel Electricity"): (CC, "By Source", "Row-level FF ratio (vestigial source)."),
    ("bysource", "% of Nuclear Electricity"): (CC, "By Source", "Row-level Nuclear ratio (vestigial)."),
    ("bysource", "% of Renewable Electricity"): (CC, "By Source", "Row-level Renewable ratio (vestigial)."),
    ("bysource", "Year for label"): (CC, "By Source", "IF [Year]=2020 THEN [Year] (vestigial)."),
    ("bysource", "X-trellis"): (DR, "-", "Small-multiple grid X (vestigial)."),
    ("bysource", "Y-trllis"): (DR, "-", "Small-multiple grid Y (vestigial)."),
    # ---- Elec Generation ----
    ("elecgen", "Global avg in 2022"): (
        ME,
        "Elec Generation",
        "Literal 3616.7 (=FNR World 2022 total). Constant reference-line measure.",
    ),
    ("elecgen", "Ratio"): (ME, "Elec Generation", "AVG(percap)/3616.7 benchmark ratio."),
}

d = json.load(open(SPEC, encoding="utf-8"))
rows_by_ds = {}
missing = []
for ds in d["data_sources"]:
    short = DS_SHORT[ds["id"]]
    for f in ds["fields"]:
        if f.get("kind") != "calculated":
            continue
        cap = f["caption"]
        key = (short, cap)
        if key not in DISP:
            missing.append(key)
            continue
        fate, target, reason = DISP[key]
        rows_by_ds.setdefault(ds["id"], []).append((cap, fate, target, reason))

if missing:
    raise SystemExit("UNMAPPED calc fields (coverage gap): " + repr(missing))

# counts
from collections import Counter

allrows = [r for rs in rows_by_ds.values() for r in rs]
fate_counts = Counter(r[1] for r in allrows)

lines = []
lines.append("# Calculated-field dispositions - ElectricityPerCapita\n")
lines.append(
    f"Every one of the **{len(allrows)}** Tableau calculated fields across the 5 data sources is dispositioned below.\n"
)
lines.append(
    f"**Totals:** {fate_counts[CC]} calc columns, {fate_counts[ME]} measures, {fate_counts[DR]} dropped, {fate_counts[GAP]} capability gaps.\n"
)
lines.append("- **calc column** / **measure** - translated into the model (table shown).")
lines.append(
    "- **dropped** - visual-layout table calc (trellis/index/window-normalization) or redundant alias; belongs to the report, not the model."
)
lines.append(
    "- **capability gap** - spatial MAKEPOINT geometry for the Sankey; no native Power BI equivalent (see limitations_encountered).\n"
)

DS_TITLE = {
    "ds.per_capita_electricity_fossil_nuclear_renewables": "1. per-capita-electricity-fossil-nuclear-renewables -> table **Per Capita FNR**",
    "ds.tree": "2. tree -> table **Tree** (+ consumption calcs relocated to **Elec Generation**)",
    "ds.pivoted_per_capita_electricity_generation_by_source": "3. Pivoted -> table **Pivoted**",
    "ds.by_source": "5. By source -> table **By Source** (VESTIGIAL - no worksheet binds it)",
    "ds.elec_generation_per_capita_regions": "4. Elec generation per capita + regions -> table **Elec Generation**",
}
order = [
    "ds.per_capita_electricity_fossil_nuclear_renewables",
    "ds.tree",
    "ds.pivoted_per_capita_electricity_generation_by_source",
    "ds.elec_generation_per_capita_regions",
    "ds.by_source",
]
for ds_id in order:
    lines.append(f"\n## {DS_TITLE[ds_id]}\n")
    lines.append("| Tableau field | Fate | Target | Reason |")
    lines.append("|---|---|---|---|")
    for cap, fate, target, reason in rows_by_ds[ds_id]:
        lines.append(f"| {cap} | {fate} | {target} | {reason} |")

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
print("Wrote", OUT)
print(
    f"Coverage: {len(allrows)} calc fields | calc-col={fate_counts[CC]} measure={fate_counts[ME]} dropped={fate_counts[DR]} gap={fate_counts[GAP]}"
)
