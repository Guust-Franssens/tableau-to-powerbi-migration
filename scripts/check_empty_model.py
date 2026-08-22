"""
purpose: fail loudly when a migration produces a semantic model that OPENS BUT LOADS NO ROWS -
         an Import partition whose flat-file source was never landed, or landed with no data.
usage:   python scripts/check_empty_model.py <bundle-or-model-dir> [...]
                                             [--json <file>] [--quiet] [--warn-only]

Why this exists
---------------
Every other failure in an estate run is LOUD. The engine names what is missing, `run_estate.py`
turns `definition_of_done: failed` into a non-zero exit, and the bundle is refused downstream.

This one is not. Measured on a 38-workbook estate (2026-08-12, engine 2.126.0), `global_superstores_db`
came back `definition_of_done: warn`, `report_bound: true`, `pbip_status: built` - a complete,
openable, deployable PBIP whose single Import partition reads:

    Source = Excel.Workbook(File.Contents("/Users/<someone>/.../Global Superstore.xlsx"), null, true)

That absolute path belongs to the machine the Tableau workbook was authored on. On the migration host
it does not exist, so the model opens, reports success, and loads **zero rows**. Nothing downstream
distinguishes "migrated" from "migrated and empty"; the failure surfaces in front of the customer.
The engine does say so in a `pbip_warnings` string - *"is flat-file but its data was not landed to an
absolute path -- the model opens but loads no rows"* - but a warning string among hundreds is not a
gate, and on the same estate that same warning was attached to a workbook (`RESTAPISample`) that
produced no model at all while the workbook that DID produce an empty one was only `warn`.

Hence the rule this module implements: **attribute from the artifact you inspect, never from a field
you copy forward.** Every finding names the `.SemanticModel` folder, the table, the partition and the
literal path that was read out of the TMDL on disk. No engine warning is trusted, parsed or echoed.

Why here and not in `probe_bundle.py` / `check_datamodel.py`
------------------------------------------------------------
* `probe_bundle.py` answers "can a LIVE source be reached", and needs Power BI Desktop, a refresh and
  usually a credential. It cannot run on a laptop with no tenant, which is exactly when this class of
  defect ships.
* `check_datamodel.py` answers "is the M/TMDL well-formed". The M here is perfectly well-formed. That
  is the whole point: this is the repo's *"structural validation is necessary, not sufficient"* gap
  made concrete - a clean parse proves shape, not that a single row will land.

So this check is deliberately OFFLINE: no Fabric, no Desktop, no credential. It reads TMDL and stats
files. That is also its limit - see "What it will NOT tell you" below.

What counts as EMPTY (the only three shapes that block)
-------------------------------------------------------
An Import-mode partition whose data comes from a **local file**, where that file cannot yield rows on
this host. The path is resolved first, because the engine routinely writes it as
`#"SourceFolder" & "\\public_Extract.csv"` rather than as a literal - measured on that estate, 22 of
34 file-backed partitions were parameterised, so a check that only understood literals would have
left two thirds of its own subject matter unjudged:

1. `missing_file`  - the resolved path does not exist.
2. `foreign_path`  - the resolved path is written for a different operating system than this host
                     (a POSIX `/Users/...` path on Windows, a `C:\\...` path on Linux). This is the
                     shape a Tableau workbook authored on a Mac produces, and it is called out
                     separately because `Path.exists()` alone can silently REMAP it - on Windows,
                     `/Users/<name>/x` is probed against the current drive, so a same-named local folder
                     would answer "present" for a file the model can never read.
3. `empty_file`    - the file exists but carries no data rows: zero bytes, or a delimited text file
                     with nothing after its header.

What is NEVER flagged (the false-positive posture, stated on purpose)
---------------------------------------------------------------------
| skipped as      | why it legitimately has no local rows                                          |
|-----------------|--------------------------------------------------------------------------------|
| `live`          | `mode: directQuery` / `dual` - rows come from the warehouse at query time       |
| `remote_import` | Import over a database connector (Snowflake, Sql, PostgreSQL, Databricks...)    |
| `calculated`    | `= calculated` partitions are DAX over other tables                            |
| `dynamic_path`  | the path expression is not resolvable offline (a computed or undeclared        |
|                 | parameter) - unknowable is never the same as empty                            |
| `stub`          | `#table(type table [], {})` - the engine ALREADY reports these loudly as       |
|                 | "N table(s) landed as a needs-review partition stub"; double-counting them     |
|                 | here would make the new gate fire on workbooks that are already blocked        |
| `inline`        | `#table(...)` carrying literal rows                                            |
| `unrecognized`  | no data source this module recognises - an honest "cannot tell"                |

A false alarm here blocks a customer estate, so the rule is: **anything this module cannot prove
empty from the artifact is not empty.** Every category is counted and printed on every run, pass or
fail, so the posture is auditable rather than assumed.

What it will NOT tell you
-------------------------
* Whether a reachable database actually returns rows - that needs `probe_bundle.py` and a credential.
* Whether the rows that DO land are correct. Row presence is not fidelity.
* Anything about a bundle produced on another machine: a Windows-authored bundle inspected from Linux
  is all `foreign_path`. Run this on the host that produced the bundle (as `run_estate.py` does).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from bundle_corpus import shipping_models

# `partition <name> = <kind>` - the head line. TMDL is indentation-scoped, so the body is every
# following line indented deeper than the head; `_partition_blocks` walks that rather than guessing
# a terminator, which is what makes it robust to new child keywords appearing upstream.
_PARTITION_HEAD = re.compile(r"^(?P<indent>[ \t]*)partition\s+(?P<name>.+?)\s*=\s*(?P<kind>[A-Za-z]\w*)\s*$")
_MODE = re.compile(r"^[ \t]*mode:\s*(?P<mode>\w+)\s*$", re.MULTILINE)

# A Power Query string literal: M has no backslash escape, only a doubled quote for a literal `"`.
# That is why paths appear verbatim (`"C:\data\x.csv"`) and why unescaping is a single replace.
_M_STRING = r'"(?P<path>(?:[^"]|"")*)"'
# Functions whose FIRST argument names a local file or folder.
_FILE_FUNCTIONS = ("File.Contents", "Folder.Files", "Folder.Contents")
# `expression SourceFolder = "C:\...\X.Data" meta [IsParameterQuery=true, ...]` in expressions.tmdl.
# The engine emits exactly this for a landed flat-file model, so a partition's path is routinely
# `#"SourceFolder" & "\public_Extract.csv"` rather than a literal. Measured on a 38-workbook estate:
# 22 of 34 file-backed partitions were parameterised this way. Not resolving them would leave two
# thirds of the file-backed surface unjudged - i.e. the gate would mostly not run.
_M_PARAM_DEF = re.compile(r"""(?m)^\s*expression\s+(?:'([^']+)'|#"([^"]+)"|([^\s=]+))\s*=\s*(?P<value>[^\r\n]*)""")
_M_PARAM_META = re.compile(r"\s+meta\s*\[.*$")
_M_PARAM_REF = re.compile(r'^(?:#"(?P<quoted>[^"]+)"|(?P<bare>[A-Za-z_]\w*))$')

# An empty-schema, empty-rows placeholder the engine lands when it cannot resolve a connector.
_EMPTY_TABLE_STUB = re.compile(r"#table\s*\(\s*type\s+table\s*\[\s*\]\s*,\s*\{\s*\}\s*\)")
_INLINE_TABLE = re.compile(r"#table\s*\(")

# Import-mode partitions whose rows come from a SERVER. Offline row presence is unknowable for these
# and claiming otherwise would be the false alarm this module exists to avoid.
_REMOTE_CONNECTOR = re.compile(
    r"\b(?:Sql|Snowflake|Databricks|Oracle|MySQL|PostgreSQL|AmazonRedshift|Odbc|GoogleBigQuery"
    r"|Teradata|AnalysisServices|PowerBI|Web|SharePoint|AzureStorage|DataLake|Lakehouse|Fabric)"
    r"\.[A-Za-z]+\s*\("
)

_POSIX_ABSOLUTE = re.compile(r"^/(?!/)")
_WINDOWS_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|//)")

# Delimited text whose data rows can be counted without a parser. Anything else (xlsx, parquet, json,
# hyper) is judged on size alone - a partial answer is better than a wrong one.
_DELIMITED_SUFFIXES = frozenset({".csv", ".tsv", ".txt", ".psv", ".tab"})

CATEGORY_FILE_OK = "file_ok"
# Blocking categories - the three shapes of "opens but loads no rows".
CATEGORY_MISSING = "missing_file"
CATEGORY_FOREIGN = "foreign_path"
CATEGORY_EMPTY = "empty_file"
BLOCKING_CATEGORIES = (CATEGORY_MISSING, CATEGORY_FOREIGN, CATEGORY_EMPTY)
JUDGED_CATEGORIES = BLOCKING_CATEGORIES + (CATEGORY_FILE_OK,)

EXIT_OK = 0
EXIT_EMPTY_MODEL = 5
EXIT_USAGE = 2

# The host this check is running on, read ONCE into a module global so a test can exercise the other
# host without a second CI runner. `os.name` is not read anywhere else in this module.
HOST_OS = os.name

REPORT_VERSION = 1
REPORT_NAME = "empty-model-check.json"

_REMEDIES = {
    CATEGORY_MISSING: (
        "land the data next to the bundle and repoint the partition "
        "(scripts/extract_hyper_data.py materializes a packaged .hyper extract to CSV), "
        "or convert the table to a live connection"
    ),
    CATEGORY_FOREIGN: (
        "the Tableau workbook carried an absolute path from the machine it was authored on; "
        "supply the file on this host and repoint the partition, or convert the table to a live connection"
    ),
    CATEGORY_EMPTY: (
        "the source file landed with no data rows - re-materialize it "
        "(scripts/extract_hyper_data.py reports a row_count per relation) before shipping the model"
    ),
}


def _unescape_m_string(raw: str) -> str:
    """Turn an M string literal's body into the path it denotes."""
    return raw.replace('""', '"')


def model_parameters(model_dir: Path) -> dict[str, str]:
    """Text-valued M parameters (`expression Name = "..."`) declared by a model.

    Only literal-valued parameters are collected. A parameter computed from anything else stays
    unresolved on purpose, so a partition that depends on it is reported as `dynamic_path` rather
    than judged against a guess.
    """
    params: dict[str, str] = {}
    for tmdl in sorted((model_dir / "definition").glob("*.tmdl")):
        for match in _M_PARAM_DEF.finditer(tmdl.read_text(encoding="utf-8-sig", errors="replace")):
            name = match.group(1) or match.group(2) or match.group(3)
            value = _M_PARAM_META.sub("", match.group("value")).strip()
            if name and len(value) >= 2 and value.startswith('"') and value.endswith('"'):
                params[name] = _unescape_m_string(value[1:-1])
    return params


def _first_call_arg(body: str, function: str) -> list[str]:
    """The first argument of every `<function>(...)` call, as raw M text.

    Scanned with a paren/quote depth counter rather than a regex because the argument is routinely a
    concatenation containing its own calls and commas.
    """
    args: list[str] = []
    for match in re.finditer(re.escape(function) + r"\s*\(", body):
        depth, in_string, start = 1, False, match.end()
        index = start
        while index < len(body) and depth:
            char = body[index]
            if in_string:
                in_string = not (char == '"' and body[index : index + 2] != '""')
                index += 1 + (1 if char == '"' and body[index : index + 2] == '""' else 0)
                continue
            if char == '"':
                in_string = True
            elif char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
                if not depth:
                    break
            elif char == "," and depth == 1:
                break
            index += 1
        args.append(body[start:index])
    return args


def _split_concat(expr: str) -> list[str]:
    """Split an M expression on top-level `&`, leaving strings and nested calls intact."""
    terms, depth, in_string, start = [], 0, False, 0
    index = 0
    while index < len(expr):
        char = expr[index]
        if in_string:
            if char == '"':
                if expr[index : index + 2] == '""':
                    index += 2
                    continue
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "&" and not depth:
            terms.append(expr[start:index])
            start = index + 1
        index += 1
    terms.append(expr[start:])
    return terms


def eval_m_path(expr: str, params: dict[str, str]) -> str | None:
    """Evaluate an M path expression to a literal, or None when it cannot be resolved offline.

    Handles exactly what the engine emits - string literals and text parameters joined by `&`. Any
    other shape (a function call, arithmetic, an undeclared parameter) returns None, which the caller
    reports as `dynamic_path`: unknowable is never the same as empty.
    """
    resolved = []
    for term in _split_concat(expr):
        term = term.strip()
        if len(term) >= 2 and term.startswith('"') and term.endswith('"') and '"' not in term[1:-1].replace('""', ""):
            resolved.append(_unescape_m_string(term[1:-1]))
            continue
        ref = _M_PARAM_REF.match(term)
        value = params.get(ref.group("quoted") or ref.group("bare")) if ref else None
        if value is None:
            return None
        resolved.append(value)
    return "".join(resolved)


def _partition_blocks(text: str) -> list[dict]:
    """Every `partition ... = <kind>` block in a TMDL file, with its indentation-scoped body."""
    lines = text.splitlines()
    blocks: list[dict] = []
    for index, line in enumerate(lines):
        head = _PARTITION_HEAD.match(line)
        if not head:
            continue
        depth = len(head.group("indent").expandtabs(4))
        body: list[str] = []
        for follower in lines[index + 1 :]:
            if follower.strip() and len(follower[: len(follower) - len(follower.lstrip())].expandtabs(4)) <= depth:
                break
            body.append(follower)
        blocks.append(
            {
                "partition": head.group("name").strip().strip("'"),
                "kind": head.group("kind"),
                "line": index + 1,
                "body": "\n".join(body),
            }
        )
    return blocks


def _path_flavour(raw: str) -> str:
    """Classify a literal path as `posix`, `windows` or `relative` WITHOUT touching the filesystem."""
    if _WINDOWS_ABSOLUTE.match(raw):
        return "windows"
    if _POSIX_ABSOLUTE.match(raw):
        return "posix"
    return "relative"


def _host_flavour() -> str:
    """The path flavour this host can actually read."""
    return "windows" if HOST_OS == "nt" else "posix"


def _is_foreign(flavour: str) -> bool:
    """Whether an absolute path is written for a different OS than the one running this check.

    Deliberately decided BEFORE any `exists()` call: on Windows, `Path('/Users/<name>/x').exists()` is
    probed against the current drive, so a mac path can be answered by an unrelated local folder and
    an unreadable model would pass silently.
    """
    if flavour == "relative":
        return False
    return flavour != _host_flavour()


def _has_data_rows(path: Path) -> bool:
    """Whether a landed source file carries at least one data row.

    Delimited text is read incrementally and abandoned after the second non-blank line, so a
    multi-gigabyte CSV costs one buffer. Every other format is judged on size alone - this module
    refuses to guess at a binary it cannot parse offline.
    """
    if path.stat().st_size == 0:
        return False
    if path.suffix.lower() not in _DELIMITED_SUFFIXES:
        return True
    seen = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.strip():
                seen += 1
                if seen > 1:
                    return True
    return False


def _folder_has_data(folder: Path) -> bool:
    """Whether a folder referenced by `Folder.Files` holds at least one non-empty file."""
    return any(child.is_file() and child.stat().st_size > 0 for child in folder.rglob("*"))


def _classify_file_source(raw: str, model_dir: Path, expect_folder: bool = False) -> tuple[str, str, str]:
    """Judge one resolved path. Returns (category, resolved path, detail)."""
    flavour = _path_flavour(raw)
    if _is_foreign(flavour):
        return CATEGORY_FOREIGN, raw, f"{flavour} absolute path cannot be read from this {_host_flavour()} host"

    candidate = Path(raw) if flavour != "relative" else (model_dir / raw)
    noun = "folder" if expect_folder else "file"
    if not (candidate.is_dir() if expect_folder else candidate.is_file()):
        return CATEGORY_MISSING, str(candidate), f"no {noun} at this path"
    if not (_folder_has_data(candidate) if expect_folder else _has_data_rows(candidate)):
        return CATEGORY_EMPTY, str(candidate), f"{noun} is present but has no data rows"
    return CATEGORY_FILE_OK, str(candidate), f"{noun} is present and carries data rows"


def _judge_file_sources(body: str, model_dir: Path, params: dict[str, str]) -> dict | None:
    """Judge every local-file reference in a partition body, or None when there is none.

    A partition can hold several. The first BLOCKING one decides, because a model with one unloadable
    table is already the failure this gate exists to stop; an unresolvable path never overrides a
    blocking one, and never becomes one.
    """
    unresolved = False
    healthy: dict | None = None
    for function in _FILE_FUNCTIONS:
        for arg in _first_call_arg(body, function):
            resolved = eval_m_path(arg, params)
            if resolved is None:
                unresolved = True
                continue
            category, path, detail = _classify_file_source(resolved, model_dir, function.startswith("Folder."))
            if category in BLOCKING_CATEGORIES:
                return {"category": category, "detail": detail, "path": resolved, "resolved_path": path}
            healthy = {"category": CATEGORY_FILE_OK, "detail": detail, "path": resolved, "resolved_path": path}
    if unresolved:
        return {"category": "dynamic_path", "detail": "path is not resolvable offline (non-literal M expression)"}
    return healthy


def _classify_shape(block: dict, body: str, mode: str) -> dict | None:
    """Verdicts decided by the partition's declared SHAPE, before any path is looked at.

    These three run first and that ordering is load-bearing: a `directQuery` partition whose M
    happens to name a file connector is still a live source, and judging it on the file would turn a
    correct migration into a false alarm.
    """
    if block["kind"].lower() != "m":
        return {"category": "calculated", "detail": f"`= {block['kind']}` partition, not a data load"}
    if mode.lower() in {"directquery", "dual"}:
        return {"category": "live", "detail": f"mode: {mode} - rows come from the source at query time"}
    if _EMPTY_TABLE_STUB.search(body):
        return {"category": "stub", "detail": "needs-review partition stub, already reported by the engine"}
    return None


def _classify_non_file(body: str) -> dict:
    """Everything left once no local file is involved. None of it is ever blocking."""
    if _REMOTE_CONNECTOR.search(body):
        return {"category": "remote_import", "detail": "Import over a remote connector - not knowable offline"}
    if _INLINE_TABLE.search(body):
        return {"category": "inline", "detail": "#table literal carrying its own rows"}
    return {"category": "unrecognized", "detail": "no recognizable data source - not judged"}


def classify_partition(block: dict, model_dir: Path, params: dict[str, str] | None = None) -> dict:
    """Decide whether one partition would load rows, and record WHY that verdict was reached."""
    body = block["body"]
    mode_match = _MODE.search(body)
    mode = mode_match.group("mode") if mode_match else ""
    verdict = dict(block)
    verdict.pop("body")
    verdict["mode"] = mode

    decided = (
        _classify_shape(block, body, mode)
        or _judge_file_sources(body, model_dir, params or {})
        or _classify_non_file(body)
    )
    return {**verdict, **decided}


def scan_model(model_dir: Path, root: Path) -> dict:
    """Classify every partition of one `.SemanticModel` folder."""
    params = model_parameters(model_dir)
    partitions = []
    for tmdl in sorted((model_dir / "definition" / "tables").glob("*.tmdl")):
        text = tmdl.read_text(encoding="utf-8-sig", errors="replace")
        table = tmdl.stem
        for block in _partition_blocks(text):
            verdict = classify_partition(block, model_dir, params)
            verdict["table"] = table
            verdict["tmdl"] = tmdl.relative_to(root).as_posix() if tmdl.is_relative_to(root) else str(tmdl)
            partitions.append(verdict)

    findings = [p for p in partitions if p["category"] in BLOCKING_CATEGORIES]
    return {
        "model": model_dir.name,
        "model_path": model_dir.relative_to(root).as_posix() if model_dir.is_relative_to(root) else str(model_dir),
        "owner": _owner(model_dir, root),
        "partitions_total": len(partitions),
        "categories": _counts(partitions),
        "findings": findings,
        "status": "EMPTY" if findings else "OK",
    }


def _owner(model_dir: Path, root: Path) -> str:
    """Which workbook this model belongs to, derived from where the artifact SITS.

    The engine emits `pbip/<workbook>/<Model>.SemanticModel` for a workbook-owned model and
    `semantic_models/<Model>.SemanticModel` for one that lands on its own. Reading the owner off the
    path is what keeps attribution honest: the same engine warning string was measured attached to
    the wrong workbook, so no reported field is copied forward.
    """
    if not model_dir.is_relative_to(root):
        return model_dir.parent.name
    parts = model_dir.relative_to(root).parts
    if len(parts) >= 2 and parts[0] == "pbip":
        return parts[1]
    if len(parts) >= 1 and parts[0] == "semantic_models":
        return "(standalone datasource model)"
    return parts[0] if len(parts) > 1 else "(bundle root)"


def _counts(partitions: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for part in partitions:
        counts[part["category"]] = counts.get(part["category"], 0) + 1
    return dict(sorted(counts.items()))


def find_models(root: Path) -> list[Path]:
    """Every model check_empty_model can honestly judge, including standalone datasource models."""
    return shipping_models(root, include_standalone=True)


def scan(root: Path) -> dict:
    """Scan a bundle (or a single model) and return the machine-readable report."""
    models = [scan_model(model, root) for model in find_models(root)]
    empty = [m for m in models if m["status"] == "EMPTY"]
    return {
        "version": REPORT_VERSION,
        "root": str(root),
        "host": "windows" if HOST_OS == "nt" else "posix",
        "models_scanned": len(models),
        "models_empty": len(empty),
        "status": "EMPTY_MODELS" if empty else "OK",
        "models": models,
    }


def _totals_line(prefix: str, totals: dict[str, int]) -> str:
    """One `k=v, k=v` line, or an explicit `(none)` so a zero is never mistaken for a missing check."""
    return prefix + (", ".join(f"{k}={v}" for k, v in totals.items()) or "(none)")


def category_totals(report: dict, categories: tuple[str, ...], invert: bool = False) -> dict[str, int]:
    """Estate-wide partition counts for (or excluding) a set of categories."""
    totals: dict[str, int] = {}
    for model in report["models"]:
        for category, count in model["categories"].items():
            if (category in categories) is not invert:
                totals[category] = totals.get(category, 0) + count
    return dict(sorted(totals.items()))


def render(report: dict) -> str:
    """Human-readable verdict: what is empty, which artifact says so, and what to do about it."""
    lines = [f"EMPTY-MODEL CHECK: {report['models_scanned']} model(s) under {report['root']}"]
    if not report["models_scanned"]:
        lines.append("  no .SemanticModel folders found - nothing to judge")
        return "\n".join(lines)

    # Both lines print on every run, pass or fail: a gate whose false-positive posture is invisible is
    # a gate nobody can audit. If `live` ever collapses to zero on an estate that plainly has
    # warehouses in it, that is the signal the classifier has drifted.
    lines.append(_totals_line("  judged (local file sources) : ", category_totals(report, JUDGED_CATEGORIES)))
    lines.append(
        _totals_line("  not judged (no local rows expected): ", category_totals(report, JUDGED_CATEGORIES, invert=True))
    )

    if report["status"] == "OK":
        lines.append("  OK - every file-backed Import partition resolves to a file with data rows.")
        return "\n".join(lines)

    lines.append(f"\nEMPTY_MODEL: {report['models_empty']} model(s) would open and load NO ROWS")
    for model in report["models"]:
        if model["status"] != "EMPTY":
            continue
        lines.append(f"  {model['owner']} -> {model['model']}  ({model['model_path']})")
        for finding in model["findings"]:
            lines.append(f"      table '{finding['table']}' partition '{finding['partition']}': {finding['detail']}")
            if finding.get("path"):
                lines.append(f"        path: {finding['path']}")
            lines.append(f"        fix : {_REMEDIES[finding['category']]}")
    lines.append(
        "\n  These models are STRUCTURALLY VALID - they parse, they validate, they open in Desktop and\n"
        "  they report success. They contain no data. Do not deploy them: a report that looks finished\n"
        "  and shows nothing fails in front of the customer instead of in front of us."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Fail when a migrated semantic model would open but load no rows (offline check).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("targets", nargs="+", type=Path, help="bundle dir(s) or .SemanticModel folder(s)")
    parser.add_argument("--json", type=Path, help="also write the machine-readable report here")
    parser.add_argument("--quiet", action="store_true", help="print only the verdict line")
    parser.add_argument(
        "--warn-only",
        action="store_true",
        help="report findings but always exit 0 (for an explicitly accepted snapshot)",
    )
    args = parser.parse_args(argv)

    missing = [str(t) for t in args.targets if not t.is_dir()]
    if missing:
        print(f"ERROR: not a directory: {', '.join(missing)}", file=sys.stderr)
        return EXIT_USAGE

    reports = [scan(target.resolve()) for target in args.targets]
    for report in reports:
        print(render(report) if not args.quiet else f"{report['status']}: {report['root']}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        payload = reports[0] if len(reports) == 1 else {"version": REPORT_VERSION, "roots": reports}
        args.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    if args.warn_only:
        return EXIT_OK
    return EXIT_EMPTY_MODEL if any(r["status"] == "EMPTY_MODELS" for r in reports) else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
