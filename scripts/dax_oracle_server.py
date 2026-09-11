"""
purpose: fill the deterministic tier's `fabric_oracle(dax_query) -> result` socket with a real DAX
         executor, so `translation_reconcile` can finally compare a translated measure against the
         model it actually produced. Speaks his `persistent_oracle` protocol - newline-delimited
         JSON on stdio, one `{"dax": "..."}` request per line, one JSON response per line.
usage:   python scripts/dax_oracle_server.py --pid <pbidesktop-pid>          # serve (the normal mode)
         python scripts/dax_oracle_server.py --pid <pbidesktop-pid> --certify # certify against Desktop
         python scripts/dax_oracle_server.py --certify --offline              # certify with NO Desktop
         python scripts/dax_oracle_server.py --pid <pid> --query "EVALUATE ROW(\"v\", 1)"

WHY THIS EXISTS
---------------
`translation_reconcile` is the empirical half of the engine's second compiler: it builds a probe
query, compares two numbers, and labels a translation verified / mismatch / not-evaluated. It
deliberately executes nothing - the executor is injected. Upstream's own words on issue #96: *"no
real executor has ever been attached... please do prototype against it"*. So the empirical half was
written, tested, and **unreachable**. This is the missing half, from our side.

It is deliberately the SMALLEST thing that closes the loop. It does not schedule, cache, batch or
retry. Those are optimisations of a loop that has never once run end-to-end; making it run at all
comes first.

THE THREE OBLIGATIONS, AND WHY THE THIRD IS THE ONE THAT MATTERS
----------------------------------------------------------------
His contract states three, all checkable offline by `fabric_oracle.conforms`:

1. **Never raise** - an oracle that raises is downgraded to `not-evaluated`, costing the caller the
   reason. Return `{"error": ...}` instead.
2. **Be a pure read** - enforced here by a statement allow-list, not by good intentions. ADOMD can
   execute more than `EVALUATE`, and this process is pointed at a model somebody is mid-migration on.
3. **Report absence honestly** - *"a fabricated zero is indistinguishable from a real one and would
   produce a false `verified`, which is the single worst outcome in this system."*

Obligation 3 is why `_scalar_row` never coerces and why an empty result set is an ERROR rather than
`0`. It is the same failure this repo keeps meeting under different names: a green result that was
never actually measured.

THE MARSHALLING TRAP (measured, not theoretical)
------------------------------------------------
Values come back as .NET types, and two of them will break this protocol if passed through naively:

* `System.Decimal` - what Power BI's Currency/Fixed-Decimal columns return. pythonnet marshals it to
  `decimal.Decimal`, which `json.dumps` **cannot serialise**. Unhandled, that raises inside the
  response path (obligation 1) *and* corrupts the NDJSON stream, killing the session rather than the
  query. Currency columns are not an edge case in a finance migration; they are most of it.
* `System.DBNull` / a DAX `BLANK()` - a real, meaningful "no value". It must reach the caller as
  `None`, never as `0`, or obligation 3 is violated at the point it matters most.

So every value goes through `_json_safe`, and numerics stay NUMERIC - returning `"12.5"` as a string
would make `compare_scalars` do a string comparison and quietly mislabel a correct translation.
"""

from __future__ import annotations

# Wire types are deliberately exact; bool and float are not integral evidence.
# pylint: disable=unidiomatic-typecheck
import argparse
import hashlib
import json
import logging
import math
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_SCRIPTS = REPO_ROOT / ".github" / "skills" / "pbip-model-refresh" / "scripts"

log = logging.getLogger("dax_oracle")

# Obligation 2, enforced. `EVALUATE`/`DEFINE` are the query surface; `SELECT` reaches the $SYSTEM
# DMVs, which is how the model is introspected. Everything else - and notably anything that could
# alter a model somebody is mid-migration on - is refused before it reaches the connection.
READ_ONLY_PREFIXES = ("EVALUATE", "DEFINE", "SELECT")

# The deterministic tier's contract module comes from the ONE canonical engine (issue #107): the
# installed plugin, resolved by `engine_source`, never a second copy found by searching. Import is
# OPTIONAL - the server runs without it; only `--certify` needs it, because certification is his
# function, not our reimplementation.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from engine_source import (  # noqa: E402  # pylint: disable=wrong-import-position
    PLUGIN_ENGINE_ROOT,
    EngineNotFoundError,
    engine_scripts_dir,
)
from current_artifact_revision import parse_json_bytes  # noqa: E402  # pylint: disable=wrong-import-position


RESULT_VERSION = 1
VALUE_KINDS = frozenset({"blank", "string", "boolean", "int32", "int64", "decimal", "double", "date", "datetime"})
CLR_KINDS = {
    "System.String": "string",
    "System.Boolean": "boolean",
    "System.Int32": "int32",
    "System.Int64": "int64",
    "System.Decimal": "decimal",
    "System.Double": "double",
    "System.DateTime": "datetime",
}


class ResultError(ValueError):
    """A closed diagnostic; result cells and driver exception text are never diagnostics."""

    CODES = frozenset(
        {"RESULT_SCHEMA", "RESULT_TYPE", "RESULT_NONFINITE", "RESULT_TRUNCATED", "RESULT_COLUMNS", "QUERY_INVALID"}
    )

    def __init__(self, code: str) -> None:
        self.code = code if code in self.CODES else "RESULT_SCHEMA"
        super().__init__(self.code)


@dataclass(frozen=True)
class TypedValue:
    """Lossless cell. Decimal coefficients/exponents and IEEE doubles never pass through JSON numbers."""

    kind: str
    value: str | None


@dataclass(frozen=True)
class TypedResult:
    """A complete single result set, bound to the exact executed UTF-8 query bytes."""

    query_sha256: str
    columns: tuple[tuple[str, str], ...]
    rows: tuple[tuple[TypedValue, ...], ...]

    @property
    def row_count(self) -> int:
        """Returned rows, not a COUNTROWS total."""
        return len(self.rows)

    def to_bytes(self) -> bytes:
        """Canonical local payload; this contains customer data and is NOT shareable metadata."""
        payload = {
            "schema_version": RESULT_VERSION,
            "query_sha256": self.query_sha256,
            "columns": [{"name": name, "kind": kind} for name, kind in self.columns],
            "rows": [[{"kind": cell.kind, "value": cell.value} for cell in row] for row in self.rows],
        }
        _validate_result(payload)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def _decimal_text(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if type(value).__name__ == "Decimal" and hasattr(value, "ToString"):
        # CLR formatting must not depend on the operator's decimal separator.
        from System.Globalization import CultureInfo  # pylint: disable=import-outside-toplevel,import-error

        return str(value.ToString(CultureInfo.InvariantCulture))
    raise ResultError("RESULT_TYPE")


def typed_value(value: Any, kind: str | None = None) -> TypedValue:  # pylint: disable=too-many-branches
    """Encode supported Python/ADOMD cells without coercing BLANK, text, or fixed decimals."""
    if value is None or type(value).__name__ == "DBNull":
        return TypedValue("blank", None)
    inferred = {
        str: "string",
        bool: "boolean",
        int: "int64",
        float: "double",
        Decimal: "decimal",
        date: "date",
        datetime: "datetime",
    }.get(type(value))
    kind = kind or inferred
    if kind == "string" and isinstance(value, str):
        text = value
    elif kind == "boolean" and isinstance(value, bool):
        text = "true" if value else "false"
    elif kind in {"int32", "int64"} and type(value) is int:
        text = str(value)
    elif kind == "decimal":
        text = _decimal_text(value)
    elif kind == "double" and type(value) is float:
        text = value.hex()
    elif kind == "date" and type(value) is date:
        text = value.isoformat()
    elif kind == "datetime" and isinstance(value, datetime):
        text = value.isoformat()
    elif kind == "datetime" and type(value).__name__ == "DateTime" and hasattr(value, "ToString"):
        text = str(value.ToString("O"))
    else:
        raise ResultError("RESULT_TYPE")
    cell = TypedValue(kind, text)
    _validate_cell(cell)
    return cell


def _validate_cell(cell: TypedValue) -> None:  # pylint: disable=too-many-branches
    if cell.kind not in VALUE_KINDS or (cell.kind == "blank" and cell.value is not None):
        raise ResultError("RESULT_TYPE")
    if cell.kind == "blank":
        return
    text = cell.value
    if not isinstance(text, str):
        raise ResultError("RESULT_TYPE")
    try:
        text.encode("utf-8")
        if cell.kind in {"int32", "int64"}:
            bits = 32 if cell.kind == "int32" else 64
            if not re.fullmatch(r"-?(?:0|[1-9][0-9]*)", text) or not -(2 ** (bits - 1)) <= int(text) < 2 ** (bits - 1):
                raise ResultError("RESULT_TYPE")
        elif cell.kind == "decimal":
            if not Decimal(text).is_finite():
                raise ResultError("RESULT_NONFINITE")
            if not re.fullmatch(r"-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:E[+-]?[0-9]+)?", text):
                raise ResultError("RESULT_TYPE")
        elif cell.kind == "double":
            number = float.fromhex(text)
            if not math.isfinite(number):
                raise ResultError("RESULT_NONFINITE")
            if number.hex() != text:
                raise ResultError("RESULT_TYPE")
        elif cell.kind == "boolean" and text not in {"true", "false"}:
            raise ResultError("RESULT_TYPE")
        elif cell.kind == "date":
            if date.fromisoformat(text).isoformat() != text:
                raise ResultError("RESULT_TYPE")
        elif cell.kind == "datetime":
            # Validate without rewriting: CLR DateTime's seventh fractional digit must survive.
            if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,7})?(?:Z|[+-]\d\d:\d\d)?", text):
                raise ResultError("RESULT_TYPE")
            datetime.fromisoformat(text)
    except (ValueError, ArithmeticError, UnicodeError) as error:
        if isinstance(error, ResultError):
            raise
        raise ResultError("RESULT_TYPE") from None


def _validate_result(payload: dict) -> None:  # pylint: disable=too-many-branches
    if set(payload) != {"schema_version", "query_sha256", "columns", "rows"}:
        raise ResultError("RESULT_SCHEMA")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != RESULT_VERSION:
        raise ResultError("RESULT_SCHEMA")
    if not isinstance(payload["query_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", payload["query_sha256"]):
        raise ResultError("RESULT_SCHEMA")
    columns, rows = payload["columns"], payload["rows"]
    if not isinstance(columns, list) or not columns or not isinstance(rows, list):
        raise ResultError("RESULT_SCHEMA")
    names = []
    for column in columns:
        if not isinstance(column, dict) or set(column) != {"name", "kind"}:
            raise ResultError("RESULT_COLUMNS")
        name, kind = column["name"], column["kind"]
        if not isinstance(name, str) or not name or not isinstance(kind, str) or kind not in VALUE_KINDS - {"blank"}:
            raise ResultError("RESULT_COLUMNS")
        names.append(name.casefold())
    if len(set(names)) != len(names):
        raise ResultError("RESULT_COLUMNS")
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            raise ResultError("RESULT_SCHEMA")
        for column, cell in zip(columns, row):
            if not isinstance(cell, dict) or set(cell) != {"kind", "value"} or not isinstance(cell["kind"], str):
                raise ResultError("RESULT_SCHEMA")
            if cell["kind"] not in {column["kind"], "blank"}:
                raise ResultError("RESULT_TYPE")
            _validate_cell(TypedValue(**cell))


def read_typed_result(blob: bytes) -> TypedResult:
    """Strict held-byte reader. No duplicated columns, missing cells, or arbitrary success fields."""
    try:
        payload = parse_json_bytes(blob)
        _validate_result(payload)
    except (ValueError, RuntimeError, TypeError, KeyError) as error:
        if isinstance(error, ResultError):
            raise
        raise ResultError("RESULT_SCHEMA") from None
    return TypedResult(
        payload["query_sha256"],
        tuple((column["name"], column["kind"]) for column in payload["columns"]),
        tuple(tuple(TypedValue(**cell) for cell in row) for row in payload["rows"]),
    )


def execute_typed(connection, dax: str, *, max_rows: int = 100_000, timeout_seconds: int = 120) -> TypedResult:
    """Execute one bounded complete result set on the caller's already catalogue-bound connection.

    The completion adapter adds the credential-aware wall clock around this call. Legacy NDJSON
    callers continue using adomd_executor/_json_safe; that lossy compatibility representation is
    deliberately never used for evidence hashing.
    """
    if not isinstance(dax, str) or not is_read_only(dax) or type(max_rows) is not int or max_rows <= 0:
        raise ResultError("QUERY_INVALID")
    command = connection.CreateCommand()
    command.CommandText = dax
    command.CommandTimeout = timeout_seconds
    reader = command.ExecuteReader()
    try:
        columns = tuple(
            (str(reader.GetName(i)), CLR_KINDS.get(str(reader.GetFieldType(i).FullName), "unsupported"))
            for i in range(reader.FieldCount)
        )
        query_hash = hashlib.sha256(dax.encode("utf-8")).hexdigest()
        _validate_result(
            {
                "schema_version": RESULT_VERSION,
                "query_sha256": query_hash,
                "columns": [{"name": n, "kind": k} for n, k in columns],
                "rows": [],
            }
        )
        rows = []
        while reader.Read():
            if len(rows) >= max_rows:
                raise ResultError("RESULT_TRUNCATED")
            rows.append(tuple(typed_value(reader.GetValue(i), kind) for i, (_, kind) in enumerate(columns)))
        if reader.NextResult():
            raise ResultError("RESULT_TRUNCATED")
        return TypedResult(query_hash, columns, tuple(rows))
    finally:
        reader.Close()


def _json_safe(value: Any) -> Any:  # pylint: disable=too-many-return-statements  # a type dispatch
    """Coerce one .NET/CLR value into something `json.dumps` accepts, WITHOUT changing its meaning.

    Numerics stay numeric (a stringified number would silently become a string comparison upstream);
    null stays null (never 0 - see obligation 3); anything genuinely foreign degrades to `str`, which
    is lossy but honest and cannot crash the stream.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # before int: bool IS an int in Python
        return value
    if isinstance(value, (int, float, str)):
        return value
    name = type(value).__name__
    if name == "DBNull":  # System.DBNull -> a real, meaningful absence
        return None
    if name == "Decimal":  # decimal.Decimal (Currency/Fixed-Decimal) - json.dumps cannot take it
        return float(value)
    try:  # System.Decimal that pythonnet left as a CLR object, DateTime, Guid, ...
        return float(value) if hasattr(value, "__float__") else str(value)
    except (TypeError, ValueError, ArithmeticError):
        return str(value)


def is_read_only(dax: str) -> bool:
    """True when the statement is one of the read-only forms we allow."""
    stripped = dax.strip().lstrip("\ufeff")
    while stripped.startswith("//"):  # tolerate leading line comments
        _, _, stripped = stripped.partition("\n")
        stripped = stripped.strip()
    return stripped.upper().startswith(READ_ONLY_PREFIXES)


def make_oracle(execute: Callable[[str], list[dict]]) -> Callable[[str], dict]:
    """Wrap a raw `execute(dax) -> rows` in the contract's guarantees.

    `execute` is injected so the contract obligations can be certified with NO Power BI Desktop and
    no tenant - which is the whole point of his `conforms()`. The ADOMD executor is one
    implementation of this callable; a stub is another.
    """

    def oracle(dax_query: str) -> dict:
        if not isinstance(dax_query, str) or not dax_query.strip():
            return {"error": "empty DAX query"}
        if not is_read_only(dax_query):
            return {"error": f"refused: not a read-only statement (allowed: {', '.join(READ_ONLY_PREFIXES)})"}
        try:
            rows = execute(dax_query)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            # Obligation 1, and the reason the catch has to be this broad: an ADOMD failure arrives
            # as an arbitrary .NET exception type that we cannot enumerate. Narrowing this would let
            # one escape, and an oracle that raises is downgraded to `not-evaluated` WITHOUT the
            # reason - so the caller loses the only diagnostic it had.
            return {"error": f"{type(exc).__name__}: {exc}"}
        if not rows:
            # Obligation 3. An empty result set is NOT zero. Returning 0 here would let a measure
            # that evaluates to nothing be labelled `verified` against a real 0 - the exact false
            # green his contract singles out as the worst outcome in the system.
            return {"error": "query returned no rows"}
        return {"rows": rows}

    return oracle


def adomd_executor(port: int) -> Callable[[str], list[dict]]:
    """A real `execute(dax) -> rows` bound to one Power BI Desktop instance's local AS engine.

    The connection is opened ONCE and held: opening a PBIP and refreshing it costs minutes, which is
    exactly why his contract offers `persistent_oracle`. Reuses `probe_desktop_query.discover_port`'s
    pid-scoped lookup rather than re-deriving it - that function refuses to widen to "any msmdsrv on
    the machine", which in a parallel batch is the difference between querying your model and
    querying a sibling's.
    """
    sys.path.insert(0, str(SKILL_SCRIPTS))
    # pythonnet must host CoreCLR BEFORE `import clr`, which is why loading ADOMD is deferred into a
    # function in the skill and why importing it at module scope here would be wrong.
    # pylint: disable-next=import-outside-toplevel
    import probe_desktop_query as pdq  # noqa: PLC0415

    # pylint: disable-next=protected-access,no-member  # the skill's ADOMD loader; resolved at runtime
    connection = pdq._load_adomd()(f"Data Source=localhost:{port}")  # noqa: SLF001
    connection.Open()

    def execute(dax: str) -> list[dict]:
        command = connection.CreateCommand()
        command.CommandText = dax
        reader = command.ExecuteReader()
        try:
            columns = [reader.GetName(i) for i in range(reader.FieldCount)]
            rows = []
            while reader.Read():
                rows.append({c: _json_safe(reader.GetValue(i)) for i, c in enumerate(columns)})
            return rows
        finally:
            reader.Close()

    execute.close = connection.Close  # type: ignore[attr-defined]
    return execute


def serve(oracle: Callable[[str], dict], stdin=None, stdout=None) -> int:
    """The `persistent_oracle` protocol: one JSON request per line in, one response per line out.

    A malformed line answers with an error and keeps the session alive. Killing the process on bad
    input would turn one unreadable query into a lost model refresh, and the refresh is the
    expensive part.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            dax = request["dax"] if isinstance(request, dict) else None
        except (ValueError, KeyError, TypeError):
            dax = None
        response = oracle(dax) if isinstance(dax, str) else {"error": f"expected {{'dax': ...}}, got {line[:120]}"}
        stdout.write(json.dumps(response) + "\n")
        stdout.flush()
    return 0


def _load_contract():
    """Import the deterministic tier's `fabric_oracle` module, or None if it is not installed."""
    try:
        candidate = engine_scripts_dir()
    except EngineNotFoundError:
        return None
    if (candidate / "fabric_oracle.py").is_file():
        sys.path.insert(0, str(candidate))
        # Deferred and unresolvable to a static checker on purpose: the deterministic tier is an
        # OPTIONAL peer, found at runtime on one of two paths. A top-level import would make this
        # whole script unimportable on a machine that only ever runs the offline modes.
        # pylint: disable-next=import-outside-toplevel,import-error
        import fabric_oracle  # noqa: PLC0415

        return fabric_oracle
    return None


def certify(oracle: Callable[[str], dict]) -> int:
    """Run HIS `conforms()` against this oracle and print the verdict.

    Deliberately not our own checklist: the contract is his, so the check has to be his function, or
    we are certifying against our reading of it rather than against it.
    """
    contract = _load_contract()
    if contract is None:
        log.error("CERTIFY: contract module not found under %s", PLUGIN_ENGINE_ROOT)
        log.error("  Install the tableau-fabric-skills plugin - it is the single canonical engine (#107).")
        return 2
    result = contract.conforms(oracle)
    for name, passed in sorted(result["checks"].items()):
        log.info("  %-20s %s", name, "PASS" if passed else "FAIL")
    for failure in result["failures"]:
        log.error("  %s", failure)
    log.info("CERTIFY: %s", "CONFORMS" if result["ok"] else "DOES NOT CONFORM")
    return 0 if result["ok"] else 1


def _stub_executor(dax: str) -> list[dict]:
    """An in-process executor that answers the trivial probe, for certifying with no Desktop.

    It certifies the PROTOCOL half - the shape, the guards, the marshalling - and nothing about a
    real model. That distinction is the point: it is honest about what an offline pass proves, which
    is why `--offline` says so in its own output.
    """
    return [{"[value]": 1}] if "ROW(" in dax.upper() or "ROW (" in dax.upper() else []


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pid", type=int, help="Power BI Desktop pid (authoritative; never widened)")
    parser.add_argument("--port", type=int, help="local Analysis Services port, if already known")
    parser.add_argument("--certify", action="store_true", help="check this oracle against the engine's contract")
    parser.add_argument("--offline", action="store_true", help="certify the protocol only, with NO Desktop")
    parser.add_argument("--query", help="run one DAX statement and print the JSON result, then exit")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    if args.offline:
        oracle = make_oracle(_stub_executor)
        if args.certify:
            log.info("CERTIFY (offline): protocol, guards and marshalling only - proves NOTHING about a model.")
            return certify(oracle)
        # Serving offline is what lets the FULL wiring - his `persistent_oracle` talking to this
        # process over NDJSON - be exercised in CI, on a machine with no Power BI Desktop. It proves
        # the plumbing, never a number.
        log.info("serving OFFLINE with a stub executor: plumbing only, answers are not from a model")
        return serve(oracle)

    if args.query and not (args.pid or args.port):
        parser.error("--query needs --pid or --port (or add --offline to exercise the plumbing)")

    if not args.pid and not args.port:
        parser.error("one of --pid / --port is required (or use --offline)")

    sys.path.insert(0, str(SKILL_SCRIPTS))
    # pylint: disable-next=import-outside-toplevel  # the skill dir is only on sys.path from here
    import probe_desktop_query as pdq  # noqa: PLC0415

    port = args.port or pdq.discover_port(args.pid)  # pylint: disable=no-member  # resolved at runtime
    log.info("bound to Power BI Desktop local AS on port %s", port)
    execute = adomd_executor(port)
    oracle = make_oracle(execute)
    try:
        if args.certify:
            return certify(oracle)
        if args.query:
            sys.stdout.write(json.dumps(oracle(args.query), indent=2) + "\n")
            return 0
        log.info('ready: one {"dax": ...} JSON request per line on stdin')
        return serve(oracle)
    finally:
        close = getattr(execute, "close", None)
        if close:
            close()


if __name__ == "__main__":
    sys.exit(main())
