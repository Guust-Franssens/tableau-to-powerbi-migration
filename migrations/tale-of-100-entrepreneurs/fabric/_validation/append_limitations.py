"""Append semantic_build limitations to migration-spec.json (idempotent)."""
import json, os

SPEC = os.path.join(os.path.dirname(__file__), "..", "..", "migration-spec.json")

NEW = [
    dict(item="semantic_model.materialization", severity="medium", stage="semantic_build",
         issue="All 4 data sources are Tableau extracts (.hyper) with no live upstream connection. Built as Power BI IMPORT tables from the already-materialized extract CSVs in the workbook's data/ folder. If the customer has real upstream systems behind these extracts, repoint the M partitions (DataFolder parameter) accordingly."),
    dict(item="semantic_model.relationships", severity="low", stage="semantic_build",
         issue="Modeled as 4 disconnected fact tables (Top 100 IPOs, Securities, Latest Tall SP Data, Stocks (DJIA)) + 3 disconnected parameter tables, with ZERO relationships. Faithful to Tableau: the source had 0 joins and 0 data blends between the sources, and each worksheet uses a single source. A shared Date dimension across Securities/Latest Tall SP Data/Stocks is a possible future enhancement but was intentionally NOT added, to avoid imposing cross-filtering the source never had (which would also distort the per-Symbol table-calc grain)."),
    dict(item="semantic_model.tablecalc_partition_inferred", severity="high", stage="semantic_build",
         issue="The 8 Stocks (DJIA) table calculations are ORPHANED - no worksheet or dashboard binds them - so their Tableau addressing/partitioning is not recorded in the workbook. PARTITION BY Symbol, ORDER BY Date was INFERRED from the 'growth of $X invested' financial semantics and confirmed numerically in Python against the CSV (UTX, AA; see _validation/groundtruth_tablecalcs.py). If a report later binds these fields at a different grain, re-verify the DAX filter context (ALLEXCEPT('Stocks (DJIA)',[Symbol]))."),
    dict(item="fld...calculation3.total_scope", severity="medium", stage="semantic_build",
         issue="Calculation3 uses Tableau TOTAL(SUM([Revenue_Inf_Adjusted])), translated as CALCULATE(SUM(...), ALL('Top 100 IPOs')) = the grand total. TOTAL()'s exact scope depends on the pane layout of the worksheet it is placed on; since it is orphaned this is the whole-table grand total. Ground truth: grand total 31,790.76 < 100*100000 -> gate FALSE -> 'No' for every row at the default Dollar amount=100."),
    dict(item="fld...calculation1.last_gate_dropped", severity="medium", stage="semantic_build",
         issue="PARSER GAP: Top 100 IPOs [Calculation1] = IF LAST()=0 AND [Parameter 1]*100000 >= SUM([Revenue_Inf_Adjusted]) THEN 'keep' ELSE 'hide' contains a LAST()=0 TABLE CALCULATION that the parser did NOT flag as is_table_calc (only Calculation3 was flagged in this source). The LAST()=0 addressing (evaluate only on the partition's last row - a Tableau viz-level row-filter helper) has no first-class model equivalent and is superseded by native Power BI visual filtering; it was dropped and the threshold logic preserved. Recommend a report-level filter instead."),
    dict(item="fld...original_investment_amt.degenerate_countd", severity="low", stage="semantic_build",
         issue="[Original Investment Amt] = [Investment Amount] / CountD([Number of Records]). [Number of Records] is the constant 1, so DISTINCTCOUNT is always 1 and the division is a structural no-op (result = the Investment Amount parameter). Translated faithfully (a hidden 'Number of Records'=1 column preserves the DISTINCTCOUNT), but flag as a likely source-workbook modelling quirk."),
    dict(item="fld...calendar.dynamic_granularity", severity="medium", stage="semantic_build",
         issue="[Calendar] and [Bars] are driven by the [Date Detail] parameter (year/quarter/month/week/day) to dynamically truncate/scale dates. Implemented as measures via SWITCH([Date Detail Value], ...). A dynamic date-granularity AXIS is better realized as a Date Granularity Field Parameter in the report phase; the measures preserve the scalar behavior but a field parameter gives the native axis-swap UX."),
    dict(item="ds.latest_tall_sp_data.metric_alias_remaps", severity="low", stage="semantic_build",
         issue="Base [Metric] plus 3 Tableau duplicate fields (Financial metric, Financial metric 2, Metric3) each carry DIFFERENT value-aliases (display relabelings of the same underlying Metric values) - they are NOT pure duplicates. Materialized as: 'Financial metric' and 'Metric3' = SWITCH value-remap calculated columns (their alias dicts); 'Financial metric 2' = passthrough (no aliases); base 'Metric' kept RAW. All are orphaned (Latest Tall SP Data is not used by any worksheet)."),
    dict(item="ds.securities.segment_alias", severity="low", stage="semantic_build",
         issue="Securities [Segment] has a single Tableau value-alias ('Network / Infrastructure/ EAI' -> 'Infrastructure'). Kept RAW in the model (the field is orphaned; Securities is unused by any worksheet). Apply a SWITCH remap if this dimension is later surfaced in a report."),
    dict(item="semantic_model.parameter_tables", severity="low", stage="semantic_build",
         issue="Tableau parameters became 3 disconnected single-column slicer tables with SELECTEDVALUE value-measures: 'Investment Amount' (range, default 500), 'Dollar Amount' (=[Parameter 1], range, default 100), 'Date Detail' (list year/quarter/month/week/day, default month). The two RANGE parameters have no fixed Tableau domain, so a representative discrete choice list was seeded; each value measure defaults to Tableau's current value when nothing is selected. Cross-table measure refs (e.g. [Investment Amount Value]) resolve globally."),
    dict(item="semantic_model.spec_vs_csv_columns", severity="medium", stage="semantic_build",
         issue="The physical extract CSVs (not the spec's declared field list) are the source of truth for import columns. Stocks CSV carries 'Adj Close' and 'Company' columns that the spec's fields[] omitted (Adj Close is the basis for every stock table calc); several spec-declared but hidden columns in Securities/Top 100 IPOs were not materialized. Columns were authored from the CSV headers; display names use the Tableau caption where a matching field exists."),
    dict(item="semantic_model.measure_names_pseudofield", severity="low", stage="semantic_build",
         issue="The [:Measure Names] pseudo-field (Tableau Measure Names/Measure Values virtual pivot on ws.top_ipos_2, parse item 13) is not a real column and was EXCLUDED from the semantic model. The report phase should reproduce the multi-measure pivot with a Field Parameter or a matrix with multiple measures."),
    dict(item="semantic_model.compatibility_level", severity="low", stage="semantic_build",
         issue="compatibilityLevel = 1606. All translated DAX uses classic CALCULATE/ALLEXCEPT/FILTER/EARLIER (works at 1500+), so no higher level is required. The DAX window-function alternatives (OFFSET/INDEX/WINDOW) for the LOOKUP/FIRST/LAST/running patterns require compatibilityLevel 1702+ and a live Desktop to author/validate; they are documented in the report's NEW GOTCHAS section but intentionally not used here since they cannot be validated without a live engine."),
    dict(item="semantic_model.validation_mode", severity="medium", stage="semantic_build",
         issue="No live Power BI Desktop / MCP / EVALUATE was available. Validation = (1) STRUCTURAL: Microsoft.AnalysisServices.Tabular.TmdlSerializer.DeserializeDatabaseFromFolder -> OK (7 tables, 48 columns, 21 measures, 0 relationships, compat 1606); (2) NUMERIC: Python ground-truth re-implementing each of the 9 table calcs two independent ways (Tableau semantics vs a literal DAX-mechanics replica) - all PASS on UTX/AA probe rows. Measures have not been executed in a live engine; a Desktop refresh + spot-check is recommended before publishing."),
]

with open(SPEC, encoding="utf-8") as f:
    d = json.load(f)

# idempotent: drop any prior semantic_build entries, then append
d["limitations_encountered"] = [x for x in d["limitations_encountered"] if x.get("stage") != "semantic_build"]
before = len(d["limitations_encountered"])
d["limitations_encountered"].extend(NEW)

with open(SPEC, "w", encoding="utf-8") as f:
    json.dump(d, f, indent=2, ensure_ascii=False)
    f.write("\n")

print(f"parse-stage kept: {before}; semantic_build appended: {len(NEW)}; total: {len(d['limitations_encountered'])}")
