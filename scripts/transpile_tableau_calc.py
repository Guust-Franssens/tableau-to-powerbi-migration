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
  2. Imports `translation_router.check_candidate_dax` from the INSTALLED plugin by an expanded `~`
     path, and raises ImportError at import time when the plugin is absent. Deliberate - reusing
     his gate rather than reimplementing it is the point - but it needs to fail with a readable
     message instead of a traceback.

The pylint suppressions below cover the module-level driver code that shadows the helper functions'
locals. They should be deleted by the refactor, not carried forward.
"""

# These suppressions all trace to the debt above: (1) module-level driver code shadows helper
# locals and skips docstrings; (3) the runtime sys.path insert makes his plugin import
# unresolvable to a static checker, and necessarily non-top-level.
# pylint: disable=redefined-outer-name,too-many-return-statements,invalid-name
# pylint: disable=wrong-import-position,import-error,missing-function-docstring

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("REPORT")
parser.add_argument("TWB")
parser.add_argument("OUT")
parser.add_argument("--table")
args = parser.parse_args()

SKILL = Path(os.path.expanduser("~")) / (
    r".copilot\installed-plugins\tableau-collection\tableau-fabric-skills\skills\tableau-migration"
)
sys.path.insert(0, str(SKILL / "scripts"))
from translation_router import check_candidate_dax  # noqa: E402

REPORT, TWB, OUT = args.REPORT, args.TWB, args.OUT
TABLE = args.table or "airline_alliance_performance_2022_2025_1.csv"
T = f"'{TABLE}'"

root = ET.fromstring(Path(TWB).read_text(encoding="utf-8"))

# ---- 0a/0b: build the caption <-> internal-name and parameter maps -----------------
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
            calcs[nm] = {
                "caption": cap,
                "formula": calc.get("formula"),
                "role": c.get("role"),
            }
        else:
            basecols.add(nm)

# real model columns (source of truth for binding)
MODEL_COLS = set()
tmdl = next((Path(REPORT).parent / "pbip").rglob("tables"))
if args.table is None:
    # bind to whichever fact table the DETERMINISTIC translations already used
    votes = Counter()
    for _f in tmdl.glob("*.tmdl"):
        for m in re.finditer(r"'([^']+)'\[", _f.read_text(encoding="utf-8")):
            votes[m.group(1)] += 1
    if votes:
        TABLE = votes.most_common(1)[0][0]
        T = f"'{TABLE}'"
        print(f"[auto] bound to most-referenced table: {TABLE} ({votes.most_common(3)})")
tf = tmdl / f"{TABLE}.tmdl"
if tf.exists():
    MODEL_COLS = {
        m.group(1).strip("'") for m in re.finditer(r"^\tcolumn ('[^']+'|\S+)", tf.read_text(encoding="utf-8"), re.M)
    }

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


# ---- drive over the handoff -------------------------------------------------------
rep = json.loads(Path(REPORT).read_text(encoding="utf-8"))
reqs = []
for wb in rep.get("workbooks", []):
    reqs += (wb.get("model_translation_handoff") or {}).get("requests", [])
for ds in rep.get("datasources", []):
    reqs += (ds.get("translation_handoff") or {}).get("requests", [])

approved, skipped, gate_fail = {}, [], []
KNOWN_MEASURES, KNOWN_COLS = set(), set()
for _f in tmdl.glob("*.tmdl"):
    _t = _f.read_text(encoding="utf-8")
    KNOWN_MEASURES |= {m.group(1).strip("'") for m in re.finditer(r"^\tmeasure ('[^']+'|\S+)", _t, re.M)}
    KNOWN_COLS |= {m.group(1).strip("'") for m in re.finditer(r"^\tcolumn ('[^']+'|\S+)", _t, re.M)}
REQ_NAMES = {r["name"] for r in reqs}

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


for r in reqs:
    dax, note = transpile(r.get("formula") or "")
    if not dax:
        skipped.append((r["name"], note))
        continue
    ur = unknown_refs(dax)
    if ur:
        skipped.append((r["name"], f"reference not in model: {ur}"))
        continue
    sd = structural_defects(dax)
    if sd:
        skipped.append((r["name"], f"structural defect: {sd}"))
        continue
    g = check_candidate_dax(dax, request=r)
    if not g.get("ok"):
        gate_fail.append((r["name"], g.get("issues"), dax[:160]))
        continue
    if r.get("role") != "measure":
        # A Tier-1 transpiled CALCULATED COLUMN is uniquely dangerous: if its expression errors,
        # EVERY query against the whole model fails ("does not hold any data"), not just that
        # column -- the entire report goes blank. A failed MEASURE fails only where it is used.
        # Parameter-driven ones are also semantically wrong (frozen at refresh, slicer-inert).
        skipped.append((r["name"], "role=dimension -- calc columns are not landed by Tier 1"))
        continue
    approved[r["name"]] = dax

Path(OUT).write_text(json.dumps(approved, indent=1, ensure_ascii=False), encoding="utf-8")
print(f"requests={len(reqs)}  approved={len(approved)}  gate_fail={len(gate_fail)}  skipped={len(skipped)}")
print("\nSKIP REASONS:", Counter(n for _, n in skipped).most_common(12))
for nm, iss, d in gate_fail[:8]:
    print("GATEFAIL", nm, iss, "|", d)
Path("skipped.json").write_text(json.dumps(skipped, indent=1), encoding="utf-8")
