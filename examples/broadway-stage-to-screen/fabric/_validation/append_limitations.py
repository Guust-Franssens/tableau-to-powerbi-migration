"""Append semantic_build limitations to migration-spec.json (idempotent).

Drops any prior stage=='semantic_build' entries and re-appends the current set, so it can
be re-run after model edits without duplicating. Keys mirror the parse-stage entries
(item / issue / severity / stage).
"""
import json, os

SPEC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "migration-spec.json"))

NEW = [
    dict(item="parser.calculated_field_formula_gap", severity="high", stage="semantic_build",
         issue="PARSER GAP: every calculated field in migration-spec.json has formula=null (39/39), although is_lod / is_table_calc / referenced_fields / reshape_hint were captured. All Tableau formulas had to be recovered from the source .twb XML (<calculation class='tableau' formula='...'>, plus <categorical-bin>/<bin> for the groups and the 0.1 histogram bin). This is not workbook-specific - it reproduces across every migration-spec in the repo."),
    dict(item="semantic_model.materialization", severity="medium", stage="semantic_build",
         issue="All 4 data sources are Tableau .hyper extracts with no live upstream. Built as Power BI IMPORT tables from the materialized extract CSVs (data/ds.*.csv) via the DataFolder M parameter (absolute path). If the customer has real upstream systems behind the extracts, repoint the M partitions."),
    dict(item="semantic_model.blend_to_relationships", severity="medium", stage="semantic_build",
         issue="Tableau had ZERO active data blends and each worksheet uses a single source (only '3 Accolades' also references the Parameters pseudo-source). The 4-source 'blend' is conceptual, keyed on Film/Original title. Modeled as 3 single-direction many-to-one relationships fact->'1 Films'[Title]: '4 Song Stats'[Film], '3 Accolades'[Film], '2 Chronology'[Broadway Show]. Single cross-filter (TMDL default) reproduces the 11 Tableau dashboard highlight actions as native cross-highlight without imposing bidirectional filtering the source never had."),
    dict(item="semantic_model.relationship_case_insensitive_match", severity="low", stage="semantic_build",
         issue="'4 Song Stats'[Film] has 3 case-only variants vs '1 Films'[Title] (In the Heights / Matilda the Musical / The Phantom of the Opera). Verified 0 TRUE (case-insensitive) unmatched keys across all 3 relationships, so Power BI's documented case-insensitive relationship matching joins them correctly ('1 Films'[Title] has no case-collisions). Keys kept RAW (name==sourceColumn, no renames) to eliminate rename-grep risk. Accolades (19/19) and Chronology (21/21) match exactly."),
    dict(item="ds.3_accolades.union_to_flat_table", severity="low", stage="semantic_build",
         issue="'3 Accolades' is a Tableau UNION of 3A Broadway + 3B Film accolade tables, already materialized to one 397-row CSV (the [Sheet]/[Table Name] provenance columns are retained but hidden). Modeled as one flat import table - the union is not rebuilt in Power Query."),
    dict(item="fld.trellis_column_row.dropped_visual_layout", severity="medium", stage="semantic_build",
         issue="Custom-geometry trellis table calcs [Column]=(index()-1)%int(sqrt(size())) and [Row]=int((index()-1)/int(sqrt(size()))) (present in BOTH '1 Films' and '4 Song Stats') were DROPPED as visual-layout concerns. They densify a grid of marks via INDEX()/SIZE() over the pane; the film grid is better realized by the materialized 'Row/Column, Ticket' & 'Row/Column, Hex' coordinate columns (kept as real data) or by a native Power BI small-multiples / grid layout in the report phase."),
    dict(item="fld.column_row_triangle.capability_gap", severity="high", stage="semantic_build",
         issue="CAPABILITY GAP: [Column Triangle]=CASE INDEX() WHEN 1 THEN -SQRT(3)/2 WHEN 2 THEN 0 WHEN 3 THEN SQRT(3)/2 END and [Row Triangle]=FLOAT(CASE INDEX() WHEN 1 THEN -1 WHEN 2 THEN 1 WHEN 3 THEN -1 END) build custom polygon/triangle MARKS (3 vertices per triangle over a densified INDEX() axis). Power BI has no native polygon-mark equivalent; these are NOT translatable to a model column/measure and were omitted. The report phase must approximate the triangular marks with a custom/AppSource visual, an image, or a shape, or accept reduced fidelity."),
    dict(item="fld.apple_orange.highlight_helpers_dropped", severity="low", stage="semantic_build",
         issue="[.Apple]=\"Apple\" and [.Orange]=\"Orange\" constant fields exist in ALL 4 sources (8 fields total) purely as Tableau dashboard-action highlight helpers. Dropped - superseded by native Power BI cross-highlight via the relationships."),
    dict(item="fld.award_or_category.superseded_by_field_parameter", severity="low", stage="semantic_build",
         issue="[Award or Category]=IIF([Parameters].[Parameter 1]='Award',[Award],[Category (group)]) and its echo helper [A or C = Cat]=([Parameters].[Parameter 1]='Category') are the parameter-swap idiom. Both DROPPED as standalone fields and replaced by a dimension-flavored Field Parameter 'Breakdown by' that swaps '3 Accolades'[Award] <-> [Category (group)] natively."),
    dict(item="semantic_model.field_parameter_no_metadata_marker", severity="medium", stage="semantic_build",
         issue="The 'Breakdown by' Field Parameter mirrors the Superstore 'Scatter Plot Detail' reference EXACTLY: 3-column calc table, bracketed sourceColumn [Value1]/[Value2]/[Value3], sortByColumn on the Order column, NAMEOF() rows. Like the reference, NO 'extendedProperty ParameterMetadata = {\"version\":3,\"kind\":2}' annotation is emitted. If native field-swap does not engage when the report binds this column, the report phase should add that extendedProperty to the 'Breakdown by' column (per the pbi-semantic-builder gotcha for dimension-flavored FPs)."),
    dict(item="fld.track_cnt_type_only.min_boolean_guard_simplified", severity="low", stage="semantic_build",
         issue="[Track Cnt, Theater only]=IIF(MIN([Type]='Theater'),[Track Cnt],NULL) (and the Movie twin) use Tableau's MIN()-over-booleans all-rows-are-Theater guard. Simplified to CALCULATE(DISTINCTCOUNT([Track ID]), [Type]=flag) - equivalent whenever the viz splits by Type (the intended use). If a report ever aggregates across mixed Type without splitting, Tableau would return NULL where the DAX returns the flag-filtered count."),
    dict(item="fld.record_count.null_nominee_and_concat_key", severity="low", stage="semantic_build",
         issue="[Record Count]=COUNTD([Award]+[Category]+[Nominee]). Modeled with a hidden [Record Key]=[Award]&[Category]&[Nominee] calc column + CALCULATE(DISTINCTCOUNT([Record Key]), NOT ISBLANK([Nominee])). The Nominee-not-blank filter reproduces Tableau's + null-propagation (4 of 397 rows have blank Nominee and are excluded) and prevents a phantom BLANK distinct. [Nomination Count]={FIXED [Original],[Award or Category]:[Record Count]} is modeled as =[Record Count] (the FIXED grain is realized by the report's Original x Breakdown-by axis). [Win Count] adds [Result]='Won'. All ground-truthed."),
    dict(item="fld.song_stat_value_bin.floor_epsilon", severity="low", stage="semantic_build",
         issue="Tableau bin on [Pivot Field Values] (size 0.1, peg 0) -> calc column ROUND(FLOOR([Pivot Field Values]+0.0000001, 0.1), 1). The +1e-7 epsilon guards a FP boundary (source values are <=3 decimals) and ROUND(,1) cleans FP noise; verified matching the Tableau bin across 0.0..1.0 boundary probes. Used as an ALLEXCEPT grain column by the 'Album + Stat' measures."),
    dict(item="fld.groups_to_switch", severity="low", stage="semantic_build",
         issue="Two Tableau groups became SWITCH calc columns with the exact member lists from the .twb <categorical-bin> (default 'Other'): [Category (group)] on '3 Accolades'[Category] (buckets Best Film Album / Best Musical / Best Musical Album / Best Picture; 331/397 rows fall to 'Other' - faithful) and [Film (Album Pop Highlight)] on '4 Song Stats'[Film] (Chicago+Mamma Mia! x2+tick,tick...BOOM! bucket, and Dear Evan Hansen)."),
    dict(item="ds.value_aliases_kept_raw", severity="medium", stage="semantic_build",
         issue="Tableau value-aliases are DISPLAY relabelings kept RAW in the model (report phase should apply them, or add a remap): '3 Accolades'[Award] (Academy Awards->Oscar, Golden Globe Awards->Golden Globe, Golden Raspberry->Golden Raspberry, Grammy->Grammy, Tony->Tony); '2 Chronology'[NYC Broadway Index] (1->(Original)..6->(5th Revival)); '1 Films'[Watched?] (the extract exported the boolean as 't'/blank text, and Tableau aliases true->Yes / null->No - so remap 't'->Yes, blank->No); the 'Sweeney Todd: The Demon Barber of Fleet Street'->'Sweeney Todd' alias (NOT applied to key columns - all relationships use the full title, which matches '1 Films'[Title]); and the 'Song Stat Dimension' relabel of '4 Song Stats'[Pivot Field Names] (Acousticness (0-1)->Acousticness, ..., Valence (0-1)->'Musical Positiveness')."),
    dict(item="semantic_model.summarize_by_choices", severity="low", stage="semantic_build",
         issue="Base numeric columns use summarizeBy=none EXCEPT truly additive ones (Number Of Votes, Runtime (Minutes), Track Duration (ms) = sum). Rates/ordinals/coordinates (Album Popularity ( %), IMDB Rating, Tempo (bpm), Loudness (db), Pivot Field Values, Track Number, Time Signature, trellis Row/Column) are none - Tableau accessed them via explicit MIN/AVG, not SUM, and auto-summing a percentage/rating is misleading. This is a deliberate deviation from the BPA 'keep base numerics as sum' default; the explicit LOD measures provide the real aggregations."),
    dict(item="fld.sondheims_work.null_vs_false", severity="low", stage="semantic_build",
         issue="[Sondheim's Work]=CONTAINS([Lyricist(s)]+' '+[Musician(s)],'Sondheim') -> boolean calc column CONTAINSSTRINGEXACT([Original Broadway Lyricist(s)] & \" \" & [Original Broadway Musician(s)], \"Sondheim\") (case-sensitive, matching Tableau CONTAINS). DAX & coerces blank->\"\" so the 1 film with a blank lyricist/musician yields FALSE where Tableau's + null-propagation yields NULL - equivalent for a boolean 'is Sondheim' flag."),
    dict(item="fld.latest_movie_premiere.dimension_as_measure", severity="low", stage="semantic_build",
         issue="[Latest Movie Premiere] is a Tableau role=dimension date field but its formula is an aggregating FIXED LOD ({FIXED [Broadway Show]: MAX(...)}), so it is modeled as a DAX MEASURE returning a scalar date per show (formatString General Date). If the report needs it as a slicer/axis dimension rather than a value, materialize it as a calc column on '2 Chronology' instead."),
    dict(item="semantic_model.columns_from_csv_headers", severity="low", stage="semantic_build",
         issue="Physical columns are authored from the extract CSV headers (source of truth), not the spec fields[] list. '4 Song Stats' keeps all 48 physical columns including the sparse 'Track Artists - 1..20' and the already-melted 'Pivot Field Names'/'Pivot Field Values' pair (so NO Power Query unpivot is needed - the audio-feature reshape is materialized in the extract). Note [Type] values differ by source: Theater/Movie in Song Stats & Chronology, but Theater/Film in Accolades (irrelevant to the modeled measures, which don't filter Accolades[Type])."),
    dict(item="semantic_model.compatibility_level", severity="low", stage="semantic_build",
         issue="compatibilityLevel=1606. All translated DAX uses classic CALCULATE/ALLEXCEPT/MIN/MAX/DISTINCTCOUNT/SWITCH/FLOOR (works at 1500+); no window functions (OFFSET/INDEX/WINDOW) are used, so no higher level is required and everything validates offline."),
    dict(item="semantic_model.validation_mode", severity="medium", stage="semantic_build",
         issue="No live Power BI Desktop / MCP / EVALUATE was available, so validation is: (1) STRUCTURAL - Microsoft.AnalysisServices.Tabular.TmdlSerializer.DeserializeDatabaseFromFolder -> OK (5 tables, 100 columns of which 10 calculated + 3 field-parameter, 17 measures, 3 relationships, compat 1606) plus model-integrity assertions (model-wide measure-name uniqueness; no measure==column in a table; every DAX [bracket] ref resolves) -> all OK; (2) NUMERIC - Python ground-truth re-implementing each conditional-FIXED-LOD two independent ways (Tableau FIXED semantics vs a literal CALCULATE/ALLEXCEPT replica) -> all PASS on Chicago/Phantom/Cats/Mamma Mia!/etc probes (see _validation/groundtruth_lods.py). A Desktop refresh + EVALUATE spot-check is recommended before publishing."),
]

with open(SPEC, encoding="utf-8") as f:
    d = json.load(f)

d.setdefault("limitations_encountered", [])
kept = [x for x in d["limitations_encountered"] if x.get("stage") != "semantic_build"]
before = len(kept)
d["limitations_encountered"] = kept + NEW

with open(SPEC, "w", encoding="utf-8") as f:
    json.dump(d, f, indent=2, ensure_ascii=False)
    f.write("\n")

print(f"parse-stage kept: {before}; semantic_build appended: {len(NEW)}; total: {len(d['limitations_encountered'])}")
