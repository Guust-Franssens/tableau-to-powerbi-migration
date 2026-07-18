"""
TMDL generator for the 'Stage to Screen: Broadway Musicals Turned into Movies'
Fabric semantic model (IronViz infographic migration).

Hand-authors the .SemanticModel/definition TMDL (no live Desktop/MCP available),
following the conventions proven by the sibling Tale-of-100 / Superstore models and
the pbi-semantic-builder gotchas (single-line DAX, bare `database`, explicit M
cultures, name-vs-sourceColumn discipline, measure/column namespace safety,
bracketed field-param sourceColumn [Value1/2/3], no 'Table'[Col]=[Measure] filters).

Design (see migration-spec.json + recovered .twb formulas):
  * 4 IMPORT tables from the materialized extract CSVs:
       '1 Films' (22-row film dimension hub) + 3 facts
       '4 Song Stats' (6811), '3 Accolades' (397, a 3A+3B UNION), '2 Chronology' (106)
  * 3 single-direction many-to-one relationships fact[title] -> '1 Films'[Title]
    (Tableau had NO active blends; each ws uses one source; highlight actions ->
    native cross-filter via these relationships). Song-stats Film relies on Power BI
    case-insensitive matching for 3 case-only title variants (verified 0 TRUE-unmatched).
  * Physical columns authored from the *CSV headers* (source of truth); name==sourceColumn
    (no renames -> zero rename-grep risk).
  * 9 conditional dimension-scoped FIXED LODs -> measures via CALCULATE/ALLEXCEPT + Type flag.
  * Tableau groups -> SWITCH calc columns; the 0.1 histogram bin -> FLOOR calc column.
  * Parameter 'Breakdown by' (Award/Category) -> dimension-flavored Field Parameter
    swapping '3 Accolades'[Award] <-> [Category (group)] (mirrors Superstore Scatter Plot Detail).
  * Dropped: .Apple/.Orange highlight helpers (x4 sources), trellis Column/Row (visual layout),
    Award or Category + A or C = Cat (superseded by the Field Parameter).
  * Capability gap: Column/Row Triangle polygon-vertex marks (no native PBI equivalent).
"""
import os, uuid, csv

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))            # ...\fabric
MODEL = os.path.join(BASE, "StageToScreen.SemanticModel")
DEFN = os.path.join(MODEL, "definition")
TABLES = os.path.join(DEFN, "tables")
DATA_FOLDER = os.path.abspath(os.path.join(BASE, "..", "data")) + "\\"           # ...\data\
CSV_DIR = os.path.abspath(os.path.join(BASE, "..", "data"))
NS = uuid.UUID("b52a7e00-2c00-4d00-9e00-000000000000")

def lt(*parts):
    return str(uuid.uuid5(NS, "|".join(parts)))

def esc_m(s):      # escape a string literal inside an M expression
    return s.replace('"', '""')

def q(name):       # escape a TMDL single-quoted identifier (apostrophe -> doubled)
    return name.replace("'", "''")

# ---- M type per TMDL dataType ----
MTYPE = {"string": "type text", "int64": "Int64.Type", "double": "type number",
         "dateTime": "type datetime", "boolean": "type logical"}

# ============================================================================
#  Physical column dtype / summarizeBy / hidden overrides (default: string / none / False).
#  Authored from the real CSV headers; dtypes verified across all rows.
# ============================================================================
DTYPE = {
    "ds.1_films.csv": {
        "World Premiere Date": "dateTime", "Row, Ticket": "int64", "Column, Ticket": "int64",
        "Row, Hex": "int64", "Column, Hex": "double", "IMDB Rating": "double",
        "Number Of Votes": "int64", "Runtime (Minutes)": "int64",
    },
    "ds.2_chronology.csv": {"NYC Broadway Index": "int64", "Date": "dateTime"},
    "ds.3_accolades.csv": {"Year": "int64"},
    "ds.4_song_stats.csv": {
        "Album Popularity ( %)": "int64", "Track Number": "int64",
        "Original Broadway Track Number": "int64", "Track Duration (ms)": "int64",
        "Time Signature": "int64", "Loudness (db) (-60 - 0)": "double",
        "Tempo (bpm)": "double", "Pivot Field Values": "double",
    },
}
# summarizeBy: only truly additive base numerics get sum; rates/ordinals/coords stay none
# (Tableau accessed popularity/tempo/loudness via explicit MIN/AVG, not SUM).
SUMM = {
    "ds.1_films.csv": {"Number Of Votes": "sum", "Runtime (Minutes)": "sum"},
    "ds.4_song_stats.csv": {"Track Duration (ms)": "sum"},
}
HIDDEN = {
    "ds.3_accolades.csv": {"Sheet", "Table Name"},   # union-provenance cols
}

def read_header(csv_name):
    with open(os.path.join(CSV_DIR, csv_name), encoding="utf-8-sig", newline="") as f:
        return next(csv.reader(f))

def phys_cols(csv_name):
    dt = DTYPE.get(csv_name, {}); sm = SUMM.get(csv_name, {}); hd = HIDDEN.get(csv_name, set())
    cols = []
    for h in read_header(csv_name):
        cols.append((h, h, dt.get(h, "string"), h in hd, sm.get(h, "none")))
    return cols

# ============================================================================
#  Group -> SWITCH helpers (exact member lists recovered from the .twb <categorical-bin>)
# ============================================================================
def in_list(col_ref, vals):
    return f'{col_ref} IN {{{", ".join(chr(34)+esc_m(v)+chr(34) for v in vals)}}}'

CAT = "'3 Accolades'[Category]"
CATEGORY_GROUP_DAX = (
    "SWITCH(TRUE(), "
    + in_list(CAT, ["Best Compilation Soundtrack Album for a Motion Picture or Television",
                    "Best Compilation Soundtrack Album for a Motion Picture, Television or Other Visual Media",
                    "Best Compilation Soundtrack Album for Motion Picture, Television or Other Visual Media",
                    "Grammy Award for Best Musical Show Album"]) + ', "Best Film Album", '
    + in_list(CAT, ["Best Musical", "Best Musical or Comedy - Motion Picture",
                    "Best Revival", "Best Revival of a Musical"]) + ', "Best Musical", '
    + in_list(CAT, ["Best Cast Show Album", "Best Musical Show Album", "Best Musical Theater Album"])
    + ', "Best Musical Album", '
    + in_list(CAT, ["Best Motion Picture - Musical or Comedy", "Best Picture",
                    "Best Picture - Comedy or Musical", "Best Picture - Musical/Comedy"])
    + ', "Best Picture", "Other")'
)

FILM = "'4 Song Stats'[Film]"
FILM_HL_DAX = (
    "SWITCH(TRUE(), "
    + in_list(FILM, ["Chicago", "Mamma Mia!", "Mamma Mia! Here We Go Again", "tick, tick... BOOM!"])
    + ', "Chicago, Mamma Mia!, Mamma Mia! Here We Go Again and 1 more", '
    + f'{FILM} = "Dear Evan Hansen", "Dear Evan Hansen", "Other")'
)

CHICAGO_NOTE = ('IF(\'2 Chronology\'[Broadway Show] = "Chicago" && \'2 Chronology\'[NYC Broadway Index] = 2, '
                '"Note that 31 Oct 2024 was not the show end date." & UNICHAR(10) & '
                '"It was merely the latest performance active date " & UNICHAR(10) & '
                '"that I knew when creating the infographic.", BLANK())')

# ============================================================================
#  Table specs.  cols come from CSV headers; calc_cols + measures below.
#  calc col: (name, dax, dataType, hidden, summarizeBy, formatString|None, desc)
#  measure : (name, dax, formatString|None, displayFolder|None, desc)
# ============================================================================
TABLE_SPECS = []

# ---- 1 Films : the film-dimension hub -------------------------------------
TABLE_SPECS.append(dict(
    name="1 Films", csv="ds.1_films.csv", cols=phys_cols("ds.1_films.csv"),
    desc="Film master dimension (22 rows, key [Title]). Source: Tableau extract '1 Films'. Hub of the model - the 3 fact tables relate many-to-one to [Title]. Physical trellis coords (Row/Column, Ticket|Hex) are materialized layout data kept as-is.",
    calc_cols=[
        ("Sondheim's Work",
         'CONTAINSSTRINGEXACT(\'1 Films\'[Original Broadway Lyricist(s)] & " " & \'1 Films\'[Original Broadway Musician(s)], "Sondheim")',
         "boolean", False, "none", None,
         "Tableau [Sondheim's Work] = CONTAINS([Lyricist(s)]+' '+[Musician(s)],'Sondheim'). Case-sensitive CONTAINS -> CONTAINSSTRINGEXACT. DAX & coerces blank->\"\" (Tableau + would null-propagate); NULL->FALSE is equivalent for this boolean flag."),
        ("Genres Revised",
         "SUBSTITUTE('1 Films'[Genres (full list)], \",\", \", \")",
         "string", False, "none", None,
         "Tableau [Genres Revised] = REPLACE([Genres (full list)],',',', '). REPLACE(all) -> SUBSTITUTE(all)."),
    ],
    measures=[
        ("Film Count", "COUNTROWS('1 Films')", "#,##0", None,
         "Row count of the film dimension (the infographic's '22 musicals' KPI)."),
    ],
))

# ---- 4 Song Stats : song-level fact ---------------------------------------
SS = "'4 Song Stats'"
TABLE_SPECS.append(dict(
    name="4 Song Stats", csv="ds.4_song_stats.csv", cols=phys_cols("ds.4_song_stats.csv"),
    desc="Song/track-level fact (6811 rows), keys [Film]/[Original], Type in {Theater,Movie}. Already carries the melted 'Pivot Field Names'/'Pivot Field Values' audio-feature columns (no Power Query unpivot needed - the reshape is materialized in the extract).",
    calc_cols=[
        ("Type (Cont)", "IF('4 Song Stats'[Type] = \"Theater\", 1, 2)", "double", False, "none", None,
         "Tableau [Type (Cont)] = FLOAT(IIF(Type='Theater',1,2)). Continuous 1.0/2.0 encoding of Type."),
        ("Song Stat Value (bin)",
         "ROUND(FLOOR('4 Song Stats'[Pivot Field Values] + 0.0000001, 0.1), 1)",
         "double", False, "none", "0.0",
         "Tableau bin on [Pivot Field Values], size 0.1, peg 0 -> FLOOR to nearest 0.1. +1e-7 epsilon guards FP boundary (values are <=3 decimals); ROUND(,1) cleans FP noise."),
        ("Film (Album Pop Highlight)", FILM_HL_DAX, "string", False, "none", None,
         "Tableau group [Film (Album Pop Highlight)] on [Film] (default 'Other'). Faithful member->bucket SWITCH."),
    ],
    measures=[
        ("Track Cnt", f"DISTINCTCOUNT({SS}[Track ID])", "#,##0", None,
         "Tableau [Track Cnt] = COUNTD([Track ID])."),
        ("Track Cnt, Theater only",
         f'CALCULATE(DISTINCTCOUNT({SS}[Track ID]), {SS}[Type] = "Theater")', "#,##0", None,
         "Tableau IIF(MIN([Type]='Theater'),[Track Cnt],NULL) - the MIN()-boolean all-rows-Theater guard is simplified to a direct Type='Theater' filter (equivalent when the viz splits by Type)."),
        ("Track Cnt, Movie only",
         f'CALCULATE(DISTINCTCOUNT({SS}[Track ID]), {SS}[Type] = "Movie")', "#,##0", None,
         "Tableau IIF(MIN([Type]='Movie'),[Track Cnt],NULL) -> direct Type='Movie' filter (same simplification as Theater only)."),
        ("Album Popularity, Theater",
         f'CALCULATE(MIN({SS}[Album Popularity ( %)]), ALLEXCEPT({SS}, {SS}[Film]), {SS}[Type] = "Theater")',
         "0", None,
         "Conditional FIXED LOD {FIXED [Film]: MIN(IIF([Type]='Theater',[Album Popularity ( %)],NULL))} -> CALCULATE(MIN,...,ALLEXCEPT([Film]),Type='Theater'). GROUND-TRUTHED."),
        ("Album Popularity, Movie",
         f'CALCULATE(MIN({SS}[Album Popularity ( %)]), ALLEXCEPT({SS}, {SS}[Film]), {SS}[Type] = "Movie")',
         "0", None,
         "Conditional FIXED LOD {FIXED [Film]: MIN(IIF([Type]='Movie',...))}. GROUND-TRUTHED."),
        ("Popularity Diff", "[Album Popularity, Movie] - [Album Popularity, Theater]", "0", None,
         "Tableau [Popularity Diff] = [Album Popularity, Movie] - [Album Popularity, Theater]."),
        ("Track Cnt, Movie Album Total",
         f'CALCULATE(DISTINCTCOUNT({SS}[Track ID]), ALLEXCEPT({SS}, {SS}[Original]), {SS}[Type] = "Movie")',
         "#,##0", None,
         "Conditional FIXED LOD {FIXED [Original]: COUNTD(IIF([Type]='Movie',[Track ID],NULL))}. GROUND-TRUTHED."),
        ("Track Cnt, Theater Album Total",
         f'CALCULATE(DISTINCTCOUNT({SS}[Track ID]), ALLEXCEPT({SS}, {SS}[Original]), {SS}[Type] = "Theater")',
         "#,##0", None,
         "Conditional FIXED LOD {FIXED [Original]: COUNTD(IIF([Type]='Theater',...))}. GROUND-TRUTHED."),
        ("Track Cnt, Movie Album + Stat",
         f'CALCULATE(DISTINCTCOUNT({SS}[Track ID]), ALLEXCEPT({SS}, {SS}[Original], {SS}[Pivot Field Names], {SS}[Song Stat Value (bin)]), {SS}[Type] = "Movie")',
         "#,##0", None,
         "Conditional FIXED LOD {FIXED [Original],[Pivot Field Names],[Song Stat Value (bin)]: COUNTD(IIF([Type]='Movie',...))}."),
        ("Track Cnt, Theater Album + Stat",
         f'CALCULATE(DISTINCTCOUNT({SS}[Track ID]), ALLEXCEPT({SS}, {SS}[Original], {SS}[Pivot Field Names], {SS}[Song Stat Value (bin)]), {SS}[Type] = "Theater")',
         "#,##0", None,
         "Conditional FIXED LOD {FIXED [Original],[Pivot Field Names],[Song Stat Value (bin)]: COUNTD(IIF([Type]='Theater',...))}."),
        ("Track Cnt%, Movie Album + Stat",
         "DIVIDE([Track Cnt, Movie Album + Stat], [Track Cnt, Movie Album Total])", "0.0%", None,
         "Tableau [Track Cnt, Movie Album + Stat] / [Track Cnt, Movie Album Total]."),
        ("Track Cnt%, Theater Album + Stat",
         "DIVIDE([Track Cnt, Theater Album + Stat], [Track Cnt, Theater Album Total])", "0.0%", None,
         "Tableau [Track Cnt, Theater Album + Stat] / [Track Cnt, Theater Album Total]."),
    ],
))

# ---- 3 Accolades : award-nomination fact (3A+3B union) --------------------
AC = "'3 Accolades'"
TABLE_SPECS.append(dict(
    name="3 Accolades", csv="ds.3_accolades.csv", cols=phys_cols("ds.3_accolades.csv"),
    desc="Award/nomination fact (397 rows) - a materialized UNION of Tableau's 3A Broadway + 3B Film accolade tables (see [Sheet]/[Table Name] provenance cols). Keys [Film]/[Original].",
    calc_cols=[
        ("Category (group)", CATEGORY_GROUP_DAX, "string", False, "none", None,
         "Tableau group [Category (group)] on [Category] (default 'Other'). Faithful member->bucket SWITCH. Used as the 'Category' arm of the 'Breakdown by' Field Parameter."),
        ("Record Key",
         f"{AC}[Award] & {AC}[Category] & {AC}[Nominee]", "string", True, "none", None,
         "Hidden helper reproducing Tableau's [Award]+[Category]+[Nominee] concatenation key for the COUNTD-of-concatenation measures. Never blank (Award/Category always populated)."),
    ],
    measures=[
        ("Record Count",
         f"CALCULATE(DISTINCTCOUNT({AC}[Record Key]), NOT ISBLANK({AC}[Nominee]))", "#,##0", None,
         "Tableau [Record Count] = COUNTD([Award]+[Category]+[Nominee]). The Nominee-not-blank filter reproduces Tableau + null-propagation (4 blank-Nominee rows excluded), avoiding a phantom BLANK distinct."),
        ("Nomination Count", "[Record Count]", "#,##0", None,
         "Tableau [Nomination Count] = {FIXED [Original],[Award or Category]: [Record Count]}. The FIXED grain is realized by the report's Original x (Breakdown by) axis, so the measure == [Record Count]."),
        ("Win Count",
         f'CALCULATE(DISTINCTCOUNT({AC}[Record Key]), {AC}[Result] = "Won", NOT ISBLANK({AC}[Nominee]))',
         "#,##0", None,
         "Tableau [Win Count] = {FIXED [Original],[Award or Category]: COUNTD(IIF([Result]='Won',[Award]+[Category]+[Nominee],NULL))}. GROUND-TRUTHED."),
    ],
))

# ---- 2 Chronology : event-level fact --------------------------------------
CH = "'2 Chronology'"
TABLE_SPECS.append(dict(
    name="2 Chronology", csv="ds.2_chronology.csv", cols=phys_cols("ds.2_chronology.csv"),
    desc="Event/timeline fact (106 rows) - Broadway start/end + movie premiere dates per show. Key [Broadway Show] relates many-to-one to '1 Films'[Title]. Type in {Theater,Movie}.",
    calc_cols=[
        ("Original or Revival",
         f'SWITCH(TRUE(), {CH}[NYC Broadway Index] = 1, "Original", {CH}[NYC Broadway Index] >= 2, "Revival", BLANK())',
         "string", False, "none", None,
         "Tableau IF [NYC Broadway Index]=1 'Original' ELSEIF >=2 'Revival' ELSE NULL."),
        ("Movie Name or Original",
         f"COALESCE({CH}[Movie Name], {CH}[Broadway Show])", "string", False, "none", None,
         "Tableau [Movie Name or Original] = IFNULL([Movie Name],[Broadway Show])."),
        ("Chicago Note", CHICAGO_NOTE, "string", False, "none", None,
         "Tableau [Chicago Note] = IIF([Broadway Show]='Chicago' AND [NYC Broadway Index]=2, <3-line note>, NULL). Line breaks preserved via UNICHAR(10)."),
    ],
    measures=[
        ("Latest Movie Premiere",
         f'CALCULATE(MAX({CH}[Date]), ALLEXCEPT({CH}, {CH}[Broadway Show]), {CH}[Type] = "Movie")',
         "General Date", None,
         "Conditional FIXED LOD {FIXED [Broadway Show]: MAX(IIF([Type]='Movie',[Date],NULL))} -> CALCULATE(MAX,...,ALLEXCEPT([Broadway Show]),Type='Movie'). GROUND-TRUTHED."),
    ],
))

# ============================================================================
#  Field Parameter: 'Breakdown by' (Award <-> Category group) - dimension-flavored
#  Mirrors Superstore 'Scatter Plot Detail' (bracketed sourceColumn [Value1/2/3]).
# ============================================================================
FIELD_PARAM = dict(
    name="Breakdown by", default_label="Award",
    desc="Field Parameter backing the Tableau 'Breakdown by' parameter (internal '[Parameter 1]', members Award/Category, default 'Award'). Dimension-flavored: it swaps which '3 Accolades' column drives the accolades breakdown - [Award] vs [Category (group)] - replacing the Tableau [Award or Category] CASE calc and the [A or C = Cat] echo helper. Report authors bind this table's 'Breakdown by' column directly to the visual's field well. sourceColumn values are the DAX table-constructor defaults [Value1/2/3] (bracketed), NOT the friendly names. NOTE: no extendedProperty ParameterMetadata is emitted (mirrors the Superstore reference); if native field-swap does not engage in Desktop, the report phase should add extendedProperty ParameterMetadata = {\"version\":3,\"kind\":2} to the 'Breakdown by' column.",
    rows=[("Award", "NAMEOF('3 Accolades'[Award])", 0),
          ("Category", "NAMEOF('3 Accolades'[Category (group)])", 1)],
)

# ============================================================================
#  Relationships: fact -> '1 Films'[Title]  (many-to-one, single cross-filter = TMDL defaults)
# ============================================================================
RELATIONSHIPS = [
    dict(name="rel_SongStats_Film_Films_Title",   from_t="4 Song Stats", from_c="Film",
         to_t="1 Films", to_c="Title",
         note="Song-stats Film -> film hub. Relies on Power BI case-insensitive matching for 3 case-only title variants (In the Heights / Matilda the Musical / The Phantom of the Opera); verified 0 TRUE-unmatched."),
    dict(name="rel_Accolades_Film_Films_Title",   from_t="3 Accolades", from_c="Film",
         to_t="1 Films", to_c="Title", note="Accolades Film -> film hub (all 19 match exactly)."),
    dict(name="rel_Chronology_Show_Films_Title",  from_t="2 Chronology", from_c="Broadway Show",
         to_t="1 Films", to_c="Title", note="Chronology Broadway Show -> film hub (all 21 match exactly)."),
]

# ============================================================================
#  Emitters
# ============================================================================
def emit_column(tname, disp, src, dtype, hidden, summ, calc_dax=None, fmt=None, desc=None):
    L = []
    if desc:
        L.append(f"\t/// {desc}")
    if calc_dax is not None:
        L.append(f"\tcolumn '{q(disp)}' = {calc_dax}")
    else:
        L.append(f"\tcolumn '{q(disp)}'")
    L.append(f"\t\tdataType: {dtype}")
    if fmt:
        L.append(f"\t\tformatString: {fmt}")
    elif dtype == "dateTime":
        L.append("\t\tformatString: General Date")
    L.append(f"\t\tlineageTag: {lt(tname, 'col', disp)}")
    L.append(f"\t\tsummarizeBy: {summ}")
    if calc_dax is None:
        L.append(f"\t\tsourceColumn: {src}")
    if hidden:
        L.append("\t\tisHidden")
    L.append("")
    L.append("\t\tannotation SummarizationSetBy = Automatic")
    return "\n".join(L)

def emit_measure(tname, name, dax, fmt, folder, desc):
    L = []
    if desc:
        L.append(f"\t/// {desc}")
    L.append(f"\tmeasure '{q(name)}' = {dax}")
    if fmt:
        L.append(f"\t\tformatString: {fmt}")
    L.append(f"\t\tlineageTag: {lt(tname, 'measure', name)}")
    if folder:
        L.append(f"\t\tdisplayFolder: {folder}")
    return "\n".join(L)

def emit_m_partition(tname, csv_name, cols):
    types = ", ".join("{\"%s\", %s}" % (src, MTYPE[dtype]) for (_d, src, dtype, _h, _s) in cols)
    src = (
        "\t\tsource =\n"
        "\t\t\t\tlet\n"
        f"\t\t\t\t    Source = Csv.Document(File.Contents(DataFolder & \"{csv_name}\"), [Delimiter=\",\", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),\n"
        "\t\t\t\t    #\"Promoted Headers\" = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),\n"
        f"\t\t\t\t    #\"Changed Type\" = Table.TransformColumnTypes(#\"Promoted Headers\", {{{types}}}, \"en-US\")\n"
        "\t\t\t\tin\n"
        "\t\t\t\t    #\"Changed Type\""
    )
    return f"\tpartition '{q(tname)}' = m\n\t\tmode: import\n{src}"

def emit_table(spec):
    t = spec["name"]
    out = [f"/// {spec['desc']}", f"table '{q(t)}'", f"\tlineageTag: {lt(t, 'table')}", ""]
    for m in spec["measures"]:
        out.append(emit_measure(t, *m)); out.append("")
    for (disp, src, dtype, hidden, summ) in spec["cols"]:
        out.append(emit_column(t, disp, src, dtype, hidden, summ)); out.append("")
    for (nm, dax, dtype, hidden, summ, fmt, desc) in spec["calc_cols"]:
        out.append(emit_column(t, nm, None, dtype, hidden, summ, calc_dax=dax, fmt=fmt, desc=desc)); out.append("")
    out.append(emit_m_partition(t, spec["csv"], spec["cols"]))
    out.append("")
    out.append("\tannotation PBI_ResultType = Table")
    out.append("")
    return "\n".join(out)

def emit_field_param(p):
    t = p["name"]
    names = {0: t, 1: t + " Fields", 2: t + " Order"}
    out = [f"/// {p['desc']}", f"table '{q(t)}'", f"\tlineageTag: {lt(t, 'table')}", ""]
    # primary (visible) column -> [Value1], sorted by the Order column
    out.append(f"\tcolumn '{q(names[0])}'")
    out.append(f"\t\tlineageTag: {lt(t, 'col', names[0])}")
    out.append(f"\t\tsummarizeBy: none")
    out.append(f"\t\tsourceColumn: [Value1]")
    out.append(f"\t\tsortByColumn: '{q(names[2])}'")
    out.append("")
    out.append(f"\t\tannotation SummarizationSetBy = Automatic")
    out.append("")
    # hidden fields column -> [Value2]
    out.append(f"\tcolumn '{q(names[1])}'")
    out.append(f"\t\tisHidden")
    out.append(f"\t\tlineageTag: {lt(t, 'col', names[1])}")
    out.append(f"\t\tsummarizeBy: none")
    out.append(f"\t\tsourceColumn: [Value2]")
    out.append("")
    out.append(f"\t\tannotation SummarizationSetBy = Automatic")
    out.append("")
    # hidden order column -> [Value3]
    out.append(f"\tcolumn '{q(names[2])}'")
    out.append(f"\t\tisHidden")
    out.append(f"\t\tformatString: 0")
    out.append(f"\t\tlineageTag: {lt(t, 'col', names[2])}")
    out.append(f"\t\tsummarizeBy: none")
    out.append(f"\t\tsourceColumn: [Value3]")
    out.append("")
    out.append(f"\t\tannotation SummarizationSetBy = Automatic")
    out.append("")
    rows = ", ".join(f'("{esc_m(lbl)}", {expr}, {order})' for (lbl, expr, order) in p["rows"])
    out.append(f"\tpartition '{q(t)}' = calculated\n\t\tmode: import\n\t\tsource = {{{rows}}}")
    out.append("")
    out.append("\tannotation PBI_ResultType = Table")
    out.append("")
    return "\n".join(out)

def emit_relationships(rels):
    blocks = []
    for r in rels:
        blocks.append(
            f"relationship {r['name']}\n"
            f"\tfromColumn: '{q(r['from_t'])}'.'{q(r['from_c'])}'\n"
            f"\ttoColumn: '{q(r['to_t'])}'.'{q(r['to_c'])}'\n"
        )
    return "\n".join(blocks)

# ============================================================================
#  Write files
# ============================================================================
os.makedirs(TABLES, exist_ok=True)

with open(os.path.join(MODEL, ".platform"), "w", encoding="utf-8") as f:
    f.write(
        '{\n'
        '  "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",\n'
        '  "metadata": {\n'
        '    "type": "SemanticModel",\n'
        '    "displayName": "StageToScreen"\n'
        '  },\n'
        '  "config": {\n'
        '    "version": "2.0",\n'
        f'    "logicalId": "{lt("platform", "logicalId")}"\n'
        '  }\n'
        '}\n'
    )

with open(os.path.join(MODEL, "definition.pbism"), "w", encoding="utf-8") as f:
    f.write(
        '{\n'
        '  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",\n'
        '  "version": "4.2",\n'
        '  "settings": {\n'
        '    "qnaEnabled": false\n'
        '  }\n'
        '}\n'
    )

with open(os.path.join(DEFN, "database.tmdl"), "w", encoding="utf-8") as f:
    f.write("database\n\tcompatibilityLevel: 1606\n")

with open(os.path.join(DEFN, "expressions.tmdl"), "w", encoding="utf-8") as f:
    f.write(f'expression DataFolder = "{DATA_FOLDER}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]\n')
    f.write(f"\tlineageTag: {lt('expr', 'DataFolder')}\n\n\tannotation PBI_ResultType = Text\n")

all_tables = [s["name"] for s in TABLE_SPECS] + [FIELD_PARAM["name"]]
qorder = '["' + '","'.join(all_tables + ["DataFolder"]) + '"]'
with open(os.path.join(DEFN, "model.tmdl"), "w", encoding="utf-8") as f:
    f.write("model Model\n\tculture: en-US\n\tdefaultPowerBIDataSourceVersion: powerBI_V3\n")
    f.write("\tsourceQueryCulture: en-US\n\tdataAccessOptions\n\t\tlegacyRedirects\n\t\treturnErrorValuesAsNull\n\n")
    f.write(f"annotation PBI_QueryOrder = {qorder}\n\n")
    f.write("annotation __PBI_TimeIntelligenceEnabled = 0\n\n")
    for t in all_tables:
        f.write(f"ref table '{q(t)}'\n")
    f.write("\n")

with open(os.path.join(DEFN, "relationships.tmdl"), "w", encoding="utf-8") as f:
    f.write(emit_relationships(RELATIONSHIPS))

for spec in TABLE_SPECS:
    with open(os.path.join(TABLES, spec["name"] + ".tmdl"), "w", encoding="utf-8") as f:
        f.write(emit_table(spec))
with open(os.path.join(TABLES, FIELD_PARAM["name"] + ".tmdl"), "w", encoding="utf-8") as f:
    f.write(emit_field_param(FIELD_PARAM))

# ---- console summary ----
nmeas = sum(len(s["measures"]) for s in TABLE_SPECS)
ncalc = sum(len(s["calc_cols"]) for s in TABLE_SPECS)
nphys = sum(len(s["cols"]) for s in TABLE_SPECS)
print("Wrote model to:", MODEL)
print("Data folder   :", DATA_FOLDER)
print(f"Tables        : {len(all_tables)} ({', '.join(all_tables)})")
print(f"Physical cols : {nphys}   Calc cols: {ncalc}   Measures: {nmeas}   Relationships: {len(RELATIONSHIPS)}")
print("Files:")
for root, _dirs, files in os.walk(DEFN):
    for fn in sorted(files):
        print("   ", os.path.relpath(os.path.join(root, fn), MODEL))
