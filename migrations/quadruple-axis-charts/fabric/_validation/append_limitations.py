"""Append semantic_build limitations to migration-spec.json (idempotent)."""
import json, os, sys
sys.stdout.reconfigure(encoding="utf-8")

SPEC = os.path.join(os.path.dirname(__file__), "..", "..", "migration-spec.json")

NEW = [
 dict(item="semantic_model.materialization", severity="medium", stage="semantic_build",
      issue="The single data source (Orders / Sample Superstore) is a Tableau EXTRACT (.hyper) with no live upstream. Built as a Power BI IMPORT table from the already-materialized extract CSV (data/ds.orders_sample_superstore.csv, 9994 rows) via the DataFolder M parameter. Repoint the partition if a real upstream exists."),

 dict(item="semantic_model.no_date_table", severity="medium", stage="semantic_build",
      issue="NO marked Date table was added, by design. The workbook has no time-intelligence / prior-period / YoY logic; every date use is a simple axis bucket (Ship Date->month, Order Date->quarter). A marked Date table would (a) impose cross-filtering the source never had and (b) risk the DATESBETWEEN axis-wiping trap on window measures. Date bucketing is done with plain calc columns on Orders (Ship Month, Order Quarter) - which is exactly what the window table-calcs address over."),

 dict(item="semantic_model.relationships", severity="low", stage="semantic_build",
      issue="1 fact table (Orders) + 4 disconnected parameter tables; ZERO relationships. Faithful to Tableau (single source, 0 joins/blends). Parameter tables are intentionally disconnected slicers read via SELECTEDVALUE value-measures."),

 dict(item="multiaxis.capability_boundary", severity="high", stage="semantic_build",
      issue="CORE OF THIS WORKBOOK + KEY REPORT-LAYER HANDOFF. It is a catalog of 'quadruple/multi-axis' tricks: Tableau fakes 3rd..6th axes by stacking measures on Rows/Columns (dual-axis) PLUS constant 'anchor' placeholders (MAX(1.0)/MAX(0.0)/0) on extra axes, then layering unicode SHAPE MARKS driven by table calcs. Power BI combo charts support AT MOST a DUAL axis. The DATA behind every mark is modeled here as measures; the literal N-axis stacking is a REPORT-LAYER concern and a hard capability bound. Report builder guidance: recreate each figure as a combo chart (line + column, 2 axes max) or small-multiple / overlaid transparent visuals; the constant anchors are provided as [Axis Anchor One]=1 and [Axis Anchor Zero]=0 for secondary-axis scaling if needed."),

 dict(item="parser.pane_flattening", severity="high", stage="semantic_build",
      issue="PARSER GAP (known): dual-axis / multi-pane worksheets are flattened to the first pane/mark card. E.g. rows=[SUM(Sales),SUM(Sales)] in Area+Dot+Line+Circle and Circle+Dot+Line+Circle is a collapsed DUAL-axis; columns=[SUM(Sales),SUM(Sales)] in the Scatter sheets is a dual x-axis. The real per-axis measures and mark cards were recovered by reading the .twb XML directly. Full per-pane mark encodings (which glyph on which axis) remain a report-layer reconstruction task."),

 dict(item="parser.calc_formula_null", severity="high", stage="semantic_build",
      issue="PARSER GAP: every calculated field's `formula` is null in migration-spec.json. All 71 formulas (plus 11 helper/anchor calcs) were recovered from the .twbx -> .twb XML <calculation formula='...'> and saved to data/_calc_formulas.json, which was the actual source of truth for translation."),

 dict(item="tablecalc.addressing_grounded", severity="medium", stage="semantic_build",
      issue="The 16 table calcs' addressing (partition/axis) was RECOVERED from the surviving worksheet `encodings` blocks (not guessed): Map Rows/Columns INDEX() over State (States ws); Dot/Circle Sales windows over Ship Month (Area/Circle+Dot+Line, Region=South); Sales East/West + 5th/6th-axis windows over Order Quarter (Bar-in-Bar ws, Region in {East,West}); Bar/Triangles/Dot/Hash Profit(-Ratio) windows over Sub-Category (SubCat Bar / L-Bar). One residual inference: Tableau's LAST()/sort DIRECTION for non-date axes (Sub-Category) is assumed alphabetical (the default table addressing); re-verify if a report sorts a pane by measure."),

 dict(item="tablecalc.window_stdev_source_quirk", severity="medium", stage="semantic_build",
      issue="FAITHFUL-QUIRK PRESERVED + FLAG TO CUSTOMER. In Bar Profit Window, Triangles/Dot/Hash Profit Ratio Window the 'Control' band's UPPER bound uses WINDOW_STDEV(SUM([Sales])) while the measured value is Profit / Profit Ratio (the lower bound correctly uses the measure's own stdev). This is a copy-paste bug in the source workbook. Translated faithfully (VAR SPlus over [Total Sales], VAR SMinus over the measure) rather than silently 'fixed'; recommend the customer confirm intent."),

 dict(item="tablecalc.groundtruth", severity="low", stage="semantic_build",
      issue="NUMERIC GROUND-TRUTH (data/ ..., _validation/groundtruth_tablecalcs.py) re-computed key table calcs two independent ways (Tableau semantics vs a literal DAX-mechanics replica) and matched to <1e-6: (1) Map Tile Grid over 49 states alphabetical - Alabama=(col0,row0), Georgia=(col9,row0), Idaho=(col0,row1), Wyoming=(col8,row4); (2) Sales East/West Max = $98,023.26 (peak quarter 2024-Q4), identical with/without the Region in {East,West} filter; (3) Late % = 29.06% (2,904 late of 9,994); (4 bonus) Order Volume rank over Sub-Category - Binders=1 (n=1523), Paper=2, Furnishings=3."),

 dict(item="fld.on_time_ship_pct.missing_else_quirk", severity="low", stage="semantic_build",
      issue="[On Time Ship %] = AVG(IF <on-time SLA> THEN 1 END) has NO ELSE branch, so late rows are null and Tableau AVG ignores them -> the value trends to 1 on any all-on-time context. Translated faithfully as AVERAGEX('Orders', IF(<cond>,1)) (DAX AVERAGEX also ignores blanks). Flag as a likely source bug (intended ELSE 0 gives a true on-time rate); the row-level [On Time Ship?] column supports a correct rate if desired."),

 dict(item="report.measure_names_values_pivots", severity="medium", stage="semantic_build",
      issue="5 worksheets use the Tableau Measure Names/Measure Values virtual pivot (Bar-in-Bar Measure Values: Sales West/East; L-Bar: Sales + a calc; Ship Mode Test Sheet: Profit + 2 LOD sums; SubCat Bar; On Time Ship Pies). No native Power BI equivalent - the [:Measure Names] pseudo-field is excluded from the model. Report phase: bind each resolved measure directly (one field per axis series) or use a Field Parameter."),

 dict(item="report.reference_line_marks", severity="low", stage="semantic_build",
      issue="Several worksheets fabricate axes/marks with REFERENCE LINES labeled by unicode glyphs (Bar-in-Bar: max/average lines labeled with '-' and dot marks; SubCat Bar: 'Goal: <Value>' average line from [Parameter 3]). These are report-layer analytics (Power BI reference lines / error bars / constant lines) - modeled here only via their backing values (e.g. [Profit Ratio Goal Value], [Sales East/West Max], [bar-in-bar negative offset])."),

 dict(item="report.shape_mark_string_measures", severity="low", stage="semantic_build",
      issue="~30 translated measures return UNICODE SHAPE/SYMBOL STRINGS (dots the marks: circle, filled-circle, triangle, bar, star, quartile arcs, hatching, indicator grids, +/- symbols, check/asterisk). These are visual encodings meant for Tableau's shape/text marks. In Power BI they are best realized as conditional formatting / custom icons / measure-driven markers at the report layer; they are provided as measures so the report can bind them, but the exact mark placement is a report-layer task."),

 dict(item="lod.exclude_pie_centering", severity="low", stage="semantic_build",
      issue="[Not Profitable Circle - Exclude Ship] and [Profitable - Exclude Ship] use LOD {EXCLUDE [On Time Ship?]: ...}, a pie label-centering trick (compute ignoring the on-time split). Translated with CALCULATE(..., REMOVEFILTERS('Orders'[On Time Ship?])). Cosmetic; verify if surfaced on a real visual."),

 dict(item="params.disconnected_slicers", severity="low", stage="semantic_build",
      issue="4 Tableau parameters -> 4 disconnected single-column slicer tables with SELECTEDVALUE value-measures: 'Select Highlight Function' ([Parameter 2], list Max/Min/Max & Min/Last/Control/All/None, default Max) drives all window highlight marks; 'Profit Ratio Goal' ([Parameter 3], REAL RANGE default 0.12 - approximated as a discrete list 0..0.30); 'Select Hashing' ([Parameter 4], list of hatch patterns, default '/// '); 'Variable Metric' ([Parameter 5], list None/-Order/-Customer/-Product Count, default '- Order Count'). (No 'Parameter 1' exists in the source.)"),

 dict(item="fld.field_renames", severity="low", stage="semantic_build",
      issue="Two Tableau fields had BRACKET-LADEN auto-generated captions illegal as measure names and were renamed: 'WINDOW_MAX(MAX([Sales - East],[Sales - West]))' -> [Sales East/West Window Max] (a thin alias of [Sales East/West Max], its identical twin); '[5th/6th Axis Metric - East]/WINDOW_MAX([5th/6th Axis Metric - East])' -> [Axis Metric East Normalized]. Also '5th/6th Axis Metric - MAX East/West' -> [Axis Metric MAX East/West]. All DAX references use the new names."),

 dict(item="fld.scrambled_internal_names", severity="low", stage="semantic_build",
      issue="Heavy Ctrl-drag duplication left internal names systematically mismatched to captions (e.g. internal '[5th Axis Metric - East (copy)_3743054306899050499]' has caption 'Variable Metric - West'; 'Sales - 2017 (copy)...' = 'Sales - East'). Every translation resolved fields via the parser CAPTION, never the internal name, so operand identity is correct despite the scramble."),

 dict(item="anchors.worksheet_calcs_not_in_spec", severity="low", stage="semantic_build",
      issue="5 worksheet-referenced calcs are NOT in the spec fields[] (parser dropped them): they are the constant fake-axis anchors MAX(1.0) (Highlight Table), MAX(0.0) (L-Bar), 0.0 x2 (On Time Ship Pies), 0 (SubCat Bar). Modeled as [Axis Anchor One]=1 and [Axis Anchor Zero]=0 (collapsed) and documented as report-layer secondary-axis scaffolding (see multiaxis.capability_boundary)."),

 dict(item="cols.type_choices", severity="low", stage="semantic_build",
      issue="Postal Code kept as TEXT to preserve leading-zero ZIPs (no calc uses it numerically). F22 (=Ship Date + ~2556 days, an Excel export artifact referenced by nothing) retained HIDDEN for lineage. Row ID hidden. Import types set with an explicit 'en-US' culture in every Table.TransformColumnTypes (guards against custom-locale refresh failures)."),

 dict(item="dispositions.calc_field_fates", severity="low", stage="semantic_build",
      issue="All 71 spec calculated fields dispositioned (coverage asserted in generate_tmdl.py): 2 became CALC COLUMNS (row-level: Days to Ship, On Time Ship?); 69 became MEASURES (aggregations / references to measures / table calcs / parameters). Row-level date buckets Ship Month & Order Quarter added as helper calc columns for window addressing. Aggregating fields -> measures; pure-cosmetic string/shape fields also modeled as measures but flagged report-layer (see report.shape_mark_string_measures)."),

 dict(item="semantic_model.validation_mode", severity="medium", stage="semantic_build",
      issue="No live Power BI Desktop / MCP / EVALUATE was available. Validation = (1) STRUCTURAL: TmdlSerializer.DeserializeDatabaseFromFolder -> OK (5 tables, 30 columns incl. 4 calculated, 79 measures, 0 relationships, compat 1606) PLUS integrity asserts: model-wide measure-name uniqueness, no measure==column name per table, every DAX [bracket] token resolves, AND description coverage 100% (tables 5/5, columns 30/30, measures 79/79) via the tmdl_validate --require-descriptions gate; (2) NUMERIC: Python ground-truth of 4 table calcs two independent ways, all PASS (see tablecalc.groundtruth). Measures were NOT executed in a live engine; a Desktop refresh + spot-check is recommended before publishing."),

 dict(item="ai.copilot_readiness", severity="low", stage="semantic_build",
      issue="AI/COPILOT READINESS PASS (post-build). Every table (5), column (30 incl. 4 calc) and measure (79) carries a business-meaning `description` - TMDL /// maps to TOM .Description, which DAX Copilot reads (first ~200 chars). Every categorical/dimension column lists its enum domain: Ship Mode (First Class/Same Day/Second Class/Standard Class), Segment (Consumer/Corporate/Home Office), Region (Central/East/South/West), Category (Furniture/Office Supplies/Technology), Sub-Category (17 values), Country (United States); high-cardinality columns (State 49, City 531, Customer 793, Product 1862, IDs) describe cardinality + role instead of enumerating. The 4 disconnected-parameter slicer columns list their option domains. Business-queryable measures lead with plain meaning, faithful Tableau-formula provenance retained after; visual-encoding/glyph helper measures keep their purpose descriptions. Natural-language aliases folded into prose (Sales a.k.a. revenue, Profit a.k.a. margin, State a.k.a. province). Authored in generate_tmdl.py (single source of truth) and persisted to fabric/QuadrupleAxisCharts.SemanticModel/definition/ - NOT via an MCP ExportToTmdlFolder round-trip (no live MCP/Desktop here; the round-trip would cosmetically reformat and desync the generator). Coverage enforced by tmdl_validate --require-descriptions (100%)."),

 dict(item="ai.copilot_skipped", severity="low", stage="semantic_build",
      issue="Deliberately NOT set in this pass (no stable offline Git/file contract, or Fabric-service-side): (1) AI data schema / linguistic modeling (LSDL synonyms schema) - no stable committable TMDL contract; NL aliases folded into description prose instead. (2) Semantic-model 'AI instructions' text - no committable file contract. (3) Verified answers - not Git-supported. (4) 'Approved for Copilot' / semantic-model indexing / Copilot enablement - Fabric service settings applied AFTER publish, not in model files. Apply these in the Power BI / Fabric service post-deployment."),
]

with open(SPEC, encoding="utf-8") as f:
    d = json.load(f)

d.setdefault("limitations_encountered", [])
before = [x for x in d["limitations_encountered"] if x.get("stage") != "semantic_build"]
d["limitations_encountered"] = before + NEW

with open(SPEC, "w", encoding="utf-8") as f:
    json.dump(d, f, indent=2, ensure_ascii=False)
    f.write("\n")

print(f"parse-stage kept: {len(before)}; semantic_build appended: {len(NEW)}; total: {len(d['limitations_encountered'])}")
