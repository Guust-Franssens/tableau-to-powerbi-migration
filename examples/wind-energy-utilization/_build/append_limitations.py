"""Append semantic_build + validate dispositions to migration-spec.json's
limitations_encountered (idempotent: skips if a semantic_build entry already exists).
Documents the fate of every parameter, spatial field, table calc, LOD, param-equality
idiom, source bug, and the data materialization + validation outcomes."""
import json
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]

P = str(_REPO / "migrations" / "wind-energy-utilization" / "migration-spec.json")
spec = json.load(open(P, encoding="utf-8"))
lim = spec.setdefault("limitations_encountered", [])

if any(e.get("stage") == "semantic_build" for e in lim):
    print("semantic_build entries already present - skipping (idempotent)")
    raise SystemExit

SB = "semantic_build"
VA = "validate"


def e(item, issue, severity, stage=SB):
    return {"item": item, "issue": issue, "severity": severity, "stage": stage}


new = [
    # ---------- data materialization ----------
    e("data_source.turbine_master_data_nl_wind_energy_2024",
      "Extract-based source (no live connection). All 4 relationship-model tables materialized from the packaged .hyper to import-mode CSVs: Turbine (30), Daily Performance (10,980=30x366), CO2 Savings (30), NL Densification (126 Year/Num spiral scaffold). Modeled as a star: Turbine (dim) 1->* Daily Performance and 1->* CO2 Savings on Turbine Id; NL Densification disconnected.",
      "info"),
    e("parse.null_calc_formulas",
      "Parser emitted null `formula` for all 87 calculated fields + 8 parameters. Recovered from the .twb <calculation formula='...'> XML (95 formulas) and used as the translation source of truth. Internal names are heavily Ctrl-drag-scrambled (e.g. internal 'CM CO2 Saved (Tn) (copy)' -> caption 'CM Homes Powered'); dispositions keyed on parser-resolved captions, never internal names.",
      "low"),

    # ---------- 8 parameters ----------
    e("parameter.Parameter 1 (Month Parameter)",
      "-> disconnected slicer table 'Month Parameter' (Month Number/Month/Month Order) + measure [Month Parameter Value] = SELECTEDVALUE('Month Parameter'[Month Number], 6). Default 6=June baked in so a bare EVALUATE reproduces the default dashboard state. 2024='24 means annual/all-year. Gates every CM/PM measure (the P1 month filter).",
      "info"),
    e("parameter.Parameter 2 (Page Parameter)",
      "-> slicer table 'Page Parameter' + [Page Parameter Value] (default '1'). Backed Tableau's show/hide-sheet page navigation; in Power BI page navigation is native (buttons/bookmarks), so retained only as selection domain. The Page 1..6 boolean calcs are NOT materialized (bookmark/nav concern).",
      "info"),
    e("parameter.Parameter 3 (Radius Parameter)",
      "-> single-row seed table 'Radius Parameter' + [Radius Parameter Value] = SELECTEDVALUE(...,500). Spiral base-radius tuning knob held at the Tableau default; can be upgraded to a what-if slider for interactive spiral tuning.",
      "info"),
    e("parameter.Parameter 4 (Spiral Start Point)",
      "-> single-row seed table 'Spiral Start Point' + [Spiral Start Point Value] = SELECTEDVALUE(...,180). Spiral start-angle (degrees) knob at Tableau default.",
      "info"),
    e("parameter.Parameter 5 (Thickness)",
      "-> single-row seed table 'Thickness' + [Thickness Value] = SELECTEDVALUE(...,0.1). Spiral radius-increment knob at Tableau default.",
      "info"),
    e("parameter.Region Parameter",
      "-> slicer table 'Region Parameter' + [Region Parameter Value] (default 'Flevoland', 5 NL provinces). NOT referenced by any calculated field; exposed as a native region slicer domain.",
      "info"),
    e("parameter.Turbine Id Parameter",
      "-> slicer table 'Turbine Id Parameter' + [Turbine Id Parameter Value] (default 'NL-WT-2024-001', 30 ids). NOT referenced by any calculated field; exposed as a native turbine-id slicer domain.",
      "info"),
    e("parameter.Upd Turbine Name Parameter",
      "-> disconnected single-select slicer table 'Upd Turbine Name Parameter' + [Upd Turbine Name Parameter Value] (default 'GRWF Turbine 18', 30 turbines). Drives every 'T ...' turbine-specific measure and the spiral length (parameter-equality idiom, SELECTEDVALUE equivalent).",
      "info"),

    # ---------- 6 spatial fields ----------
    e("field.Spiral",
      "Spatial MAKEPOINT([X],[Y]) - a CARTESIAN spiral point, NOT geography. Disposition: render as a native scatter/line visual driven by measures [Spiral X]/[Spiral Y] over the NL Densification Num index (higher fidelity than a map). The MAKEPOINT geometry object itself is not materialized (no DAX equivalent); the underlying cartesian coords are exposed as measures instead.",
      "info"),
    e("field.Spiral 20s",
      "Spatial MAKEPOINT decorative marker at Num in (100,200,300,400,500). Disposition: report-layer annotation (reference points on the spiral scatter). Not materialized as a model field.",
      "low"),
    e("field.Spiral Axis Guides",
      "Spatial MAKEPOINT decorative up/down/left/right axis guide markers. Disposition: report-layer annotation. Not materialized.",
      "low"),
    e("field.Spiral Zero",
      "Spatial MAKEPOINT(0,0) centre marker. Disposition: provided as constant measures [Spiral Zero X]=0 / [Spiral Zero Y]=0 for a single origin reference point on the scatter.",
      "info"),
    e("field.Spiral First Point",
      "Spatial MAKEPOINT of the spiral's first point (Num=0). Disposition: report-layer annotation (first point derivable from [Spiral X]/[Spiral Y] at Num=0). Not materialized.",
      "low"),
    e("field.Spiral Last Point",
      "Spatial MAKEPOINT of the spiral's last point (Num=MIN([Max Path])). Disposition: report-layer annotation (last point = [Spiral X]/[Spiral Y] at Num=[Spiral Length]). Not materialized.",
      "low"),
    e("maps.no_makeline",
      "NO MAKELINE / great-circle geometry anywhere in the workbook - therefore NO arc/route capability gap. The only real geography (Map worksheet) is turbine point locations.",
      "info"),
    e("field.turbine_latitude_longitude (Map worksheet)",
      "Real turbine geography (latitude/longitude on turbine_master_data, plotted by the 'Map' worksheet). Disposition: kept as columns on the Turbine dim, tagged dataCategory Latitude/Longitude (+ Region=StateOrProvince) so the report binds them to a native azureMap point/bubble layer (30 turbines).",
      "info"),

    # ---------- table calcs (4) + LOD (1) ----------
    e("field.Index",
      "Table calc INDEX(). Disposition: positional index = report-visual sort/rank concern, not materialized as a model field.",
      "low"),
    e("field.Rank",
      "Table calc RANK([CM Total Output],'desc'). Disposition: measure [Rank] = RANKX(ALL('Turbine'[Upd Turbine Name]), [CM Total Output], , DESC, Dense) on Turbine.",
      "info"),
    e("field.Max Monthly Output / Min Monthly Output",
      "Table calcs WINDOW_MAX/WINDOW_MIN over the month axis. Disposition: measures [Max Monthly Output]/[Min Monthly Output] using MAXX/MINX over ALLSELECTED('Daily Performance'[Month]) returning the 'Max'/'Min' label (evaluate with Month on the axis).",
      "info"),
    e("field.Max Path",
      "LOD FIXED [upd_turbine_name]:SUM(FIXED [upd_turbine_name],[Num]: IF [Spiral Filter] THEN 1 END) - 1 (per-turbine spiral point count). Disposition: simplified to measure [Spiral Length] = INT([T CM Performance Ratio (Abs)]) - with integer Num, count of Num in [0,abs] minus 1 = INT(abs). Verified = 104 at default.",
      "low"),
    e("field.Spiral Check / Spiral Filter / Spiral Colour",
      "Num-index gating calcs referencing the scalar [T CM Performance Ratio (Abs)]. Disposition: measures on NL Densification. [Spiral Colour] adds an explicit NOT ISBLANK guard because DAX BLANK<=100 evaluates TRUE (would flip Tableau's 'B' to 'A').",
      "info"),

    # ---------- parameter-equality idiom (simplify) ----------
    e("field.Selected Turbine / Selected Turbine ID / Selected Month / Selected Month (copy) / Selected Month = 2024 / Selected Page / Page 1-6",
      "Parameter-equality idiom (IF [Parameters].[X] = [Dim] ...). Disposition: NOT materialized as calculated columns; replaced by native slicers/bookmarks on the parameter tables. Only [Selected Turbine ID] is kept as a convenience measure (returns the selected turbine's turbine_id).",
      "info"),
    e("field.Selected Month (Bars) / Month in View / vs. PM Label / in 2024 Label / Month Label",
      "Param-dependent label fields. Disposition: implemented as measures (they depend on the live Month slicer, so cannot be static calc columns). [Month Label] IS a calc column - its Tableau IF/ELSE branches were identical (dead conditional), simplified to unconditional LEFT(month,1).",
      "info"),
    e("field.Country / Today",
      "Constants. Disposition: measures [Country]=\"Netherlands\", [Today]=TODAY().",
      "info"),
    e("field.Header size / info",
      "Decorative constants (180 / 'info'). Disposition: not materialized (layout-only, no analytical value).",
      "low"),
    e("worksheet_local.radial_months_arc",
      "4 worksheet-local decorative calc ids (radial 'Months' arc, e.g. Calculation_2281636170049314942) exist only in worksheet shelves, not in the datasource field list. Disposition: report-builder decorative concern; not modeled.",
      "low"),

    # ---------- source bugs (translated faithfully + flagged) ----------
    e("field.T PM Capacity Factor",
      "SOURCE BUG: Tableau's [T PM Capacity Factor] omits the /100 that [T CM Capacity Factor] applies, so [T *MoM Capacity Factor] compares a fraction (~0.29) to a percent (~35.6) - a 100x scale mismatch (T Neg MoM CapFac ~ -99%). Translated exactly as authored and flagged; NOT silently 'fixed'.",
      "medium"),
    e("field.CM Total Capacity / T CM Total Capacity",
      "SOURCE ISSUE: Tableau SUMs capacity_mw over month-filtered DAILY rows, fan-out-inflating by the day count (e.g. June = 30x130.2 = 3,906 MW); [T CM Total Capacity] additionally has an AND/OR precedence bug (missing parens). Implemented as sensible STATIC nameplate (fleet SUM = 130.2 MW; per-turbine = selected capacity) and flagged, since a capacity KPI returning 3,906 MW is unusable.",
      "medium"),
    e("field.T CM Cars Offset",
      "SOURCE BUG: AND/OR precedence (missing parens) -> 'MONTH=P1 OR (P1=2024 AND name=param)'. For a specific month it is NOT turbine-filtered (returns the fleet mean); only turbine-specific when annual (P1=2024). Translated faithfully as IF(P1=2024, <selected turbine>, <fleet mean>) and flagged.",
      "medium"),
    e("field.T Pos/Neg/Neut MoM CO2 Saved",
      "CO2 is ANNUAL per turbine, so [T CM CO2] = [T PM CO2] always -> T MoM CO2 is definitionally 0 (Neut) / blank (Pos,Neg). Translated faithfully and flagged as a data-grain characteristic (no monthly CO2 breakdown exists).",
      "low"),
    e("field.CM CO2 Saved (Tn) / CM Homes Powered / CM Trees / T CM CO2 / T CM Homes",
      "CO2/homes/trees are ANNUAL per turbine; the Tableau MONTH([date]) gate over the daily-joined rows is month-INVARIANT (all 30 turbines have complete 366-day data, so every month weights turbines equally). Implemented as AVERAGE over CO2 Savings (fleet) / selected-turbine CALCULATE - proven to equal the Tableau value (fleet default = 5,634.38). The month gate is a structural no-op.",
      "info"),
    e("field.Total Fleet Capacity / No of Turbines / Active Turbines / Onshore Turbines",
      "Fleet-summary KPI tiles not present as datasource calc fields but shown on the dashboard header. Provisioned as measures on Turbine ([No of Turbines]=30, [Active Turbines]=28 Operational, [Onshore Turbines]=24, [CM Total Capacity]=130.2) so the report has backing fields.",
      "info"),
    e("naming.spiral_measure_renames",
      "Tableau [Radius]/[Angle]/[X]/[Y] renamed to [Spiral Radius]/[Spiral Angle]/[Spiral X]/[Spiral Y] to avoid confusion/ambiguity with the 'Radius Parameter'[Radius] column and single-letter names. All references updated.",
      "info"),

    # ---------- validation outcomes ----------
    e("validate.structural",
      "TmdlSerializer.DeserializeDatabaseFromFolder OK: 12 tables, 2 relationships. Integrity asserted programmatically: 75 measures all uniquely named model-wide; no measure name equals a column name in the same table; every DAX [bracket] reference resolves to a real measure/column.",
      "info", VA),
    e("validate.ground_truth",
      "16/16 numeric ground-truth checks vs the extracted CSVs PASSED, incl. the parameter-driven [CM Total Output] at its Month=6/June default = 28,240.79, [T CM Total Output] (GRWF Turbine 18, June) = 1,117.32, [T CM Performance Ratio (Abs)] = 104.68 -> [Spiral Length] = 104, [Total Actual Output (2024)] = 453,167.28, [Total Co2 Saved] = 169,031.37, [CM CO2 Saved (Tn)] = 5,634.38.",
      "info", VA),
]

lim.extend(new)
json.dump(spec, open(P, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
print("APPENDED", len(new), "entries. Total limitations now:", len(lim))
