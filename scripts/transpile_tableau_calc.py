"""
purpose: deterministic Tableau->DAX transpiler for the recurring idiom families in a
         Tier-0 handoff; gates every candidate and writes approved_dax.json
usage:   python scripts/transpile_tableau_calc.py <report.json> <twb> <out.json> [--table T]

STATUS: RESEARCH ARTIFACT, pending refactor. Preserved because it is the evidence behind the
central finding of the 2026-08-04 end-to-end run: it authored **126 of 188** stubbed calcs
deterministically, in one pass, and every candidate it landed that was later checked against an
independent oracle matched. That refutes the deterministic tier's own framing of the calc tail as
work "there is no script" for.

Known debt, all deliberate and none yet fixed - do not treat this as a repo tool:
  1. `--table` defaults to an airline-specific table name.
  2. Imports `translation_router.check_candidate_dax` from the INSTALLED plugin (resolved by
     `engine_source`, the single canonical engine - issue #107), and raises ImportError at import
     time when the plugin is absent. Deliberate - reusing his gate rather than reimplementing it is
     the point - but it needs to fail with a readable message instead of a traceback.

The pylint suppressions cover its compact research-code style and plugin import. They should be
deleted by the refactor, not carried forward.
"""

# These suppressions all trace to the research artifact's compact helper functions, main driver, and
# plugin import.
# pylint: disable=redefined-outer-name,too-many-return-statements,invalid-name,too-many-locals
# pylint: disable=too-many-branches,too-many-statements,global-variable-undefined
# pylint: disable=wrong-import-position,import-error,import-outside-toplevel,missing-function-docstring

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine_source import engine_skill_dir  # noqa: E402

# ---- reference rewriting ----------------------------------------------------------
REF = re.compile(r"\[Parameters\]\.\[([^\]]+)\]|\[([^\]]+)\]")


def raw_lit(f):
    """Tableau string literals are single-quoted; in DAX single quotes denote a TABLE.
    MUST run on the RAW formula, before rewrite_refs emits 'Table'[Col] qualifiers."""
    f = " ".join(f.split())
    return re.sub(r"'([^']*)'", lambda m: '"' + m.group(1).replace('"', '""') + '"', f)


def rewrite_refs(f, inline_keystone=True):
    """Replace every Tableau [ref] with the right DAX identifier. Returns (dax, unresolved[])."""
    bad = []

    def sub(m):
        if m.group(1) is not None:  # [Parameters].[X]
            cap = params.get(m.group(1))
            if not cap:
                bad.append(m.group(0))
                return m.group(0)
            return f"[{cap} Value]"
        nm = m.group(2)
        if nm in calcs:
            cap = calcs[nm]["caption"]
            # keystone-style parameter-driven scalar dimensions must be INLINED,
            # never referenced as a (refresh-frozen) calculated column
            if inline_keystone and calcs[nm]["role"] == "dimension" and "[Parameters]" in calcs[nm]["formula"]:
                inner, b = rewrite_refs(raw_lit(calcs[nm]["formula"]), inline_keystone=False)
                bad.extend(b)
                return "(" + funcs(inner) + ")"
            return f"[{cap}]"
        if nm in MODEL_COLS:
            return f"{T}[{nm}]"
        if nm in basecols and not MODEL_COLS:
            return f"{T}[{nm}]"
        bad.append(m.group(0))
        return m.group(0)

    return REF.sub(sub, f), bad


# ---- function / operator rewriting ------------------------------------------------
def funcs(s):
    s = re.sub(r'DATETRUNC\(\s*"month"\s*,', "__DTM__(", s, flags=re.I)
    s = re.sub(r'DATETRUNC\(\s*"year"\s*,', "__DTY__(", s, flags=re.I)
    s = re.sub(r'DATEADD\(\s*"month"\s*,\s*(-?\d+)\s*,', r"EDATE(\1@", s, flags=re.I)
    s = re.sub(r"\bMAKEDATE\(", "DATE(", s, flags=re.I)
    s = re.sub(r"\bZN\(", "COALESCE0(", s, flags=re.I)
    # balanced-paren rewrite for the placeholder forms
    s = _rewrite_call(s, "__DTM__", lambda a: f"EOMONTH({a}, -1) + 1")
    s = _rewrite_call(s, "__DTY__", lambda a: f"DATE(YEAR({a}), 1, 1)")
    s = _rewrite_call(s, "COALESCE0", lambda a: f"COALESCE({a}, 0)")
    s = re.sub(r"EDATE\((-?\d+)@", r"EDATE_SWAP\1(", s)
    for m in set(re.findall(r"EDATE_SWAP(-?\d+)\(", s)):
        s = _rewrite_call(s, f"EDATE_SWAP{m}", lambda a, m=m: f"EDATE({a}, {m})")
    s = re.sub(r"\bAND\b", "&&", s)
    s = re.sub(r"\bOR\b", "||", s)
    s = re.sub(r"\bNOT\b", "NOT", s)
    return s


def _rewrite_call(s, name, build):
    while True:
        i = s.find(name + "(")
        if i < 0:
            return s
        j = i + len(name)
        depth, k = 0, j
        while k < len(s):
            if s[k] == "(":
                depth += 1
            elif s[k] == ")":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        s = s[:i] + build(s[j + 1 : k]) + s[k + 1 :]


IFRE = re.compile(r"\bIF\b(?P<c>.*?)\bTHEN\b(?P<t>.*?)(?:\bELSE\b(?P<e>.*?))?\bEND\b", re.S | re.I)
# innermost IF ... END (no nested IF inside) -- rewritten repeatedly, inside-out
IF_FULL = re.compile(r"\bIF\b(?P<body>(?:(?!\bIF\b).)*?)\bEND\b", re.S | re.I)
BRANCH = re.compile(r"\bELSEIF\b|\bELSE\b|\bTHEN\b", re.I)


def _one_if(body):
    """`c THEN t [ELSEIF c2 THEN t2]* [ELSE e]` -> nested DAX IF()."""
    toks, last = [], 0
    for m in BRANCH.finditer(body):
        toks.append((body[last : m.start()].strip(), m.group(0).upper()))
        last = m.end()
    toks.append((body[last:].strip(), None))
    pairs, els, i = [], None, 0
    while i < len(toks) - 1:
        seg, kw = toks[i]
        if kw != "THEN":
            i += 1
            continue
        val, nxt = toks[i + 1]
        pairs.append((seg, val))
        if nxt == "ELSE":
            els = toks[i + 2][0] if i + 2 < len(toks) else None
            break
        i += 1
    if not pairs:
        return None
    out = els
    for cond, val in reversed(pairs):
        out = f"IF({cond}, {val}" + (f", {out})" if out is not None else ")")
    return out


def if_to_dax(s):
    """Convert Tableau IF/ELSEIF/ELSE/END (including nesting) to nested DAX IF()."""
    guard = 0
    while guard < 60:
        guard += 1
        m = IF_FULL.search(s)
        if not m:
            return s
        rep = _one_if(m.group("body"))
        if rep is None:
            return s
        s = s[: m.start()] + rep + s[m.end() :]
    return s


AGG = re.compile(r"^\s*(SUM|AVG|COUNTD|COUNT|MIN|MAX)\s*\(\s*(.+)\)\s*$", re.S | re.I)
AGGX = {
    "SUM": "SUMX",
    "AVG": "AVERAGEX",
    "COUNT": "COUNTAX",
    "MIN": "MINX",
    "MAX": "MAXX",
}


def transpile(formula):
    """Return (dax, note) or (None, why-not)."""
    f = raw_lit(formula)
    for bad in (
        "WINDOW_",
        "INDEX(",
        "RANK(",
        "FIRST(",
        "LAST(",
        "LOOKUP(",
        "MAKEPOINT",
        "MAKELINE",
        "TOTAL(",
        "RUNNING_",
        "SIZE(",
        "SCRIPT_",
        "REGEXP",
        "{FIXED",
        "{INCLUDE",
        "{EXCLUDE",
    ):
        if bad in f.upper():
            return None, f"no faithful DAX form (contains {bad})"
    m = AGG.match(f)
    if m:
        agg, inner = m.group(1).upper(), m.group(2)
        body, bad = rewrite_refs(inner)
        if bad:
            return None, f"unresolved refs {sorted(set(bad))}"
        body = if_to_dax(funcs(body))
        if agg == "COUNTD":
            raw = re.match(r"\s*IF\b(.*?)\bTHEN\b(.*?)\bEND\s*$", inner, re.S | re.I)
            if raw:
                cond, badc = rewrite_refs(raw.group(1))
                val, badv = rewrite_refs(raw.group(2))
                if badc or badv:
                    return None, "unresolved refs in COUNTD"
                cond, val = if_to_dax(funcs(cond)), if_to_dax(funcs(val)).strip()
                return (
                    f"CALCULATE(DISTINCTCOUNT({val}), FILTER({T}, {cond.strip()}))",
                    "COUNTD(IF..) -> CALCULATE(DISTINCTCOUNT, FILTER)",
                )
            return None, "COUNTD shape not recognised"
        return f"{AGGX[agg]}({T}, {body})", f"{agg}(IF..) -> {AGGX[agg]} iterator"
    # non-aggregate: measure-level arithmetic over other calcs
    body, bad = rewrite_refs(f)
    if bad:
        return None, f"unresolved refs {sorted(set(bad))}"
    body = if_to_dax(funcs(body))
    body = re.sub(r"\(\s*(\[[^\]]+\])\s*/\s*(\[[^\]]+\])\s*\)", r"DIVIDE(\1, \2)", body)
    return body, "scalar/measure arithmetic"


DAX_FUNCS = {
    "IF",
    "AND",
    "OR",
    "NOT",
    "SUMX",
    "AVERAGEX",
    "COUNTAX",
    "COUNTX",
    "MINX",
    "MAXX",
    "CALCULATE",
    "FILTER",
    "DISTINCTCOUNT",
    "SUM",
    "AVERAGE",
    "COUNT",
    "COUNTA",
    "MIN",
    "MAX",
    "DIVIDE",
    "COALESCE",
    "BLANK",
    "DATE",
    "YEAR",
    "MONTH",
    "DAY",
    "EOMONTH",
    "EDATE",
    "ALL",
    "ALLEXCEPT",
    "ALLSELECTED",
    "REMOVEFILTERS",
    "VALUES",
    "SELECTEDVALUE",
    "RELATED",
    "ABS",
    "ROUND",
    "INT",
    "EXACT",
    "LEFT",
    "RIGHT",
    "LEN",
    "TRIM",
    "UPPER",
    "LOWER",
    "FORMAT",
    "CONCATENATE",
    "SUBSTITUTE",
    "SEARCH",
    "FIND",
    "ISBLANK",
    "SWITCH",
    "TRUE",
    "FALSE",
    "COUNTROWS",
    "SUMMARIZE",
    "RANKX",
    "OFFSET",
    "DATEDIFF",
    "TODAY",
    "NOW",
    "WEEKDAY",
    "WEEKNUM",
    "QUARTER",
    "VALUE",
    "DATEVALUE",
    "MAXA",
    "MINA",
    "SQRT",
    "POWER",
    "LOG",
    "EXP",
    "SIGN",
    "CEILING",
    "FLOOR",
    "MOD",
    "TRUNC",
    "CONCATENATEX",
    "IFERROR",
}
CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_.]*)\s*\(")


def _calls(dax):
    """Function names actually invoked -- with [bracketed refs] and "literals" stripped first,
    or a measure named [CM Distance (A)] is misread as a call to DISTANCE()."""
    s = re.sub(r"\[[^\]]*\]", "#", dax)
    s = re.sub(r'"[^"]*"', "#", s)
    return {c.upper() for c in CALL.findall(s)}


def unknown_refs(dax):
    """Bare [Ref] tokens (measure refs) that exist nowhere in the model or the landing batch.
    check_candidate_dax does NOT catch these; they fail only at query time."""
    bare = set(re.findall(r"(?<!\])\[([^\]]+)\]", dax))
    return sorted(b for b in bare if b not in KNOWN_MEASURES and b not in KNOWN_COLS and b not in REQ_NAMES)


def structural_defects(dax):
    """Defects check_candidate_dax passes but the ENGINE rejects at refresh/query time.
    Each entry here cost a full land+refresh cycle to discover.

    The function ALLOWLIST is load-bearing. A denylist of Tableau functions is unbounded
    (STR, DATENAME, ZN, IIF, DATEPARSE, ...) and every miss lands a calculated COLUMN that
    errors -- which poisons EVERY query against the whole model, not just that column."""
    bad = []
    if re.search(r'"[A-Za-z0-9_. ]+"\s*\[', dax):
        bad.append("table qualifier emitted as \"Table\"[Col] (must be 'Table'[Col])")
    for kw in ("THEN", "ELSEIF", "ELSE", "END"):
        if re.search(rf"\b{kw}\b", dax):
            bad.append(f"un-transpiled Tableau keyword {kw}")
    if re.search(r"\bIIF\b|\bZN\b|\bDATETRUNC\b|\bDATEADD\b|\bMAKEDATE\b", dax, re.I):
        bad.append("un-transpiled Tableau function")
    unknown = sorted(_calls(dax) - DAX_FUNCS)
    if unknown:
        bad.append(f"not a DAX function: {unknown}")
    return bad


def main():
    """Transpile the handoff requests named by the command line."""
    global REPORT, TWB, OUT, TABLE, T, params, calcs, basecols, MODEL_COLS, tmdl, check_candidate_dax
    global KNOWN_MEASURES, KNOWN_COLS, REQ_NAMES

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("REPORT")
    parser.add_argument("TWB")
    parser.add_argument("OUT")
    parser.add_argument("--table")
    args = parser.parse_args()

    skill = engine_skill_dir()
    sys.path.insert(0, str(skill / "scripts"))
    from translation_router import check_candidate_dax as candidate_gate

    check_candidate_dax = candidate_gate
    REPORT, TWB, OUT = args.REPORT, args.TWB, args.OUT
    TABLE = args.table or "airline_alliance_performance_2022_2025_1.csv"
    T = f"'{TABLE}'"

    root = ET.fromstring(Path(TWB).read_text(encoding="utf-8"))
    params, calcs, basecols = {}, {}, set()
    for ds in root.iter("datasource"):
        is_param = ds.get("name") == "Parameters"
        for c in ds.iter("column"):
            nm = (c.get("name") or "").strip("[]")
            cap = c.get("caption") or nm
            if is_param:
                params[nm] = cap
                continue
            calc = c.find("calculation")
            if calc is not None and calc.get("formula"):
                calcs[nm] = {"caption": cap, "formula": calc.get("formula"), "role": c.get("role")}
            else:
                basecols.add(nm)

    MODEL_COLS = set()
    tmdl = next((Path(REPORT).parent / "pbip").rglob("tables"))
    if args.table is None:
        votes = Counter()
        for file in tmdl.glob("*.tmdl"):
            for match in re.finditer(r"'([^']+)'\[", file.read_text(encoding="utf-8")):
                votes[match.group(1)] += 1
        if votes:
            TABLE = votes.most_common(1)[0][0]
            T = f"'{TABLE}'"
            print(f"[auto] bound to most-referenced table: {TABLE} ({votes.most_common(3)})")
    table_file = tmdl / f"{TABLE}.tmdl"
    if table_file.exists():
        MODEL_COLS = {
            match.group(1).strip("'")
            for match in re.finditer(r"^\tcolumn ('[^']+'|\S+)", table_file.read_text(encoding="utf-8"), re.M)
        }

    rep = json.loads(Path(REPORT).read_text(encoding="utf-8"))
    reqs = []
    for wb in rep.get("workbooks", []):
        reqs += (wb.get("model_translation_handoff") or {}).get("requests", [])
    for ds in rep.get("datasources", []):
        reqs += (ds.get("translation_handoff") or {}).get("requests", [])

    approved, skipped, gate_fail = {}, [], []
    KNOWN_MEASURES, KNOWN_COLS = set(), set()
    for file in tmdl.glob("*.tmdl"):
        text = file.read_text(encoding="utf-8")
        KNOWN_MEASURES |= {match.group(1).strip("'") for match in re.finditer(r"^\tmeasure ('[^']+'|\S+)", text, re.M)}
        KNOWN_COLS |= {match.group(1).strip("'") for match in re.finditer(r"^\tcolumn ('[^']+'|\S+)", text, re.M)}
    REQ_NAMES = {request["name"] for request in reqs}

    for request in reqs:
        dax, note = transpile(request.get("formula") or "")
        if not dax:
            skipped.append((request["name"], note))
            continue
        unresolved = unknown_refs(dax)
        if unresolved:
            skipped.append((request["name"], f"reference not in model: {unresolved}"))
            continue
        defects = structural_defects(dax)
        if defects:
            skipped.append((request["name"], f"structural defect: {defects}"))
            continue
        gate = check_candidate_dax(dax, request=request)
        if not gate.get("ok"):
            gate_fail.append((request["name"], gate.get("issues"), dax[:160]))
            continue
        if request.get("role") != "measure":
            skipped.append((request["name"], "role=dimension -- calc columns are not landed by Tier 1"))
            continue
        approved[request["name"]] = dax

    Path(OUT).write_text(json.dumps(approved, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"requests={len(reqs)}  approved={len(approved)}  gate_fail={len(gate_fail)}  skipped={len(skipped)}")
    print("\nSKIP REASONS:", Counter(n for _, n in skipped).most_common(12))
    for name, issues, dax in gate_fail[:8]:
        print("GATEFAIL", name, issues, "|", dax)
    Path("skipped.json").write_text(json.dumps(skipped, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
