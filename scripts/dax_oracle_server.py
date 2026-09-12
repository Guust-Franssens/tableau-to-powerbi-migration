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

OPT-IN EXACT TRANSPORT
---------------------
Only an NDJSON request with result_format exactly "typed-v1" selects the additive typed path.
It requires dax and a positive integer max_payload_bytes, with no other request fields. Success
uses the same rows/error response union: {schema_version: 1, query_sha256, columns, rows}, where
columns contain {name, kind} and each row contains ordered {kind, value} cells. Kinds are string,
int32, int64, decimal, and blank (null, in cells only). Nonblank values are lossless strings.
Decimal cells require the actual CLR 96-bit coefficient and scale 0..28; trailing zeros are retained,
not trimmed to force an otherwise impossible representation into that domain.
The limit charges the exact UTF-8 success envelope, excluding its framing LF. Success is withheld
until EOF, NextResult() == False, and reader.Close() all succeed. No independent row/cell/digit
policy cap is imposed.

There is one duplicate-rejecting JSON parse before mode routing. Ambiguous, malformed, truncated,
or undecodable/deep frames return a fixed INPUT_INVALID, never a legacy echo or float result.
Only an established legacy request enters legacy marshalling. Native binding precedes readiness,
even for empty stdin. A startup failure is exposed only for established legacy requests or empty
stdin; typed/unestablished frames retain fixed errors and an unbound native server exits nonzero.

execute_typed(connection, dax, max_payload_bytes=...) is the native-qualification seam; the normal
--pid/--port NDJSON server wires it before legacy marshalling. Native qualification is mandatory
before merge; offline mocks do not qualify ADOMD's CLR conversions. --offline deliberately returns
TYPED_UNAVAILABLE for typed requests. This is transport, not receipt, identity, or completion proof.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from collections.abc import Iterator
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

_TYPED_CLR_KINDS = {
    "System.String": "string",
    "System.Int32": "int32",
    "System.Int64": "int64",
    "System.Decimal": "decimal",
}
_TYPED_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", re.ASCII)
_CLR_DECIMAL_MAX_COEFFICIENT = "79228162514264337593543950335"


class TypedResultError(ValueError):
    """Closed typed-mode diagnostics; never format cells, queries, or driver exceptions."""

    CODES = frozenset(
        {
            "INPUT_INVALID",
            "QUERY_INVALID",
            "PAYLOAD_LIMIT",
            "RESULT_SCHEMA",
            "RESULT_COLUMNS",
            "RESULT_TYPE",
            "RESULT_INCOMPLETE",
            "EXECUTION_FAILED",
            "TYPED_UNAVAILABLE",
        }
    )

    def __init__(self, code: str) -> None:
        self.code = code if isinstance(code, str) and code in self.CODES else "RESULT_SCHEMA"
        super().__init__(self.code)


def _typed_json(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _typed_query_args(dax: str, max_payload_bytes: int) -> None:
    if not isinstance(max_payload_bytes, int) or isinstance(max_payload_bytes, bool) or max_payload_bytes <= 0:
        raise TypedResultError("INPUT_INVALID")
    if not isinstance(dax, str) or not dax.strip():
        raise TypedResultError("QUERY_INVALID")
    try:
        dax.encode("utf-8")
    except UnicodeError:
        raise TypedResultError("QUERY_INVALID") from None
    if not is_read_only(dax):
        raise TypedResultError("QUERY_INVALID")


def _decimal_text(value: Any) -> str:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise TypedResultError("RESULT_TYPE")
        sign, digits, exponent = value.as_tuple()
        # Check the native domain before expanding an arbitrarily large Python exponent.
        if exponent < -28 or (any(digits) and len(digits) + max(exponent, 0) > 29):
            raise TypedResultError("RESULT_TYPE")
        text = ("-0" if sign else "0") if not any(digits) and exponent >= 0 else format(value, "f")
    elif type(value).__name__ == "Decimal" and hasattr(value, "ToString"):
        # Preserve CLR scale and digits without using the operator's decimal separator.
        from System.Globalization import CultureInfo  # pylint: disable=import-outside-toplevel,import-error

        text = str(value.ToString(CultureInfo.InvariantCulture))
    else:
        raise TypedResultError("RESULT_TYPE")
    if not _TYPED_NUMBER.fullmatch(text):
        raise TypedResultError("RESULT_TYPE")
    whole, _, fraction = text.removeprefix("-").partition(".")
    coefficient = (whole + fraction).lstrip("0") or "0"
    if (
        len(fraction) > 28
        or len(coefficient) > len(_CLR_DECIMAL_MAX_COEFFICIENT)
        or (len(coefficient) == len(_CLR_DECIMAL_MAX_COEFFICIENT) and coefficient > _CLR_DECIMAL_MAX_COEFFICIENT)
    ):
        raise TypedResultError("RESULT_TYPE")
    return text


def _typed_integer(value: Any, kind: str) -> str:
    native_name = "System.Int32" if kind == "int32" else "System.Int64"
    if not isinstance(value, int) or isinstance(value, bool):
        if not hasattr(value, "GetType") or value.GetType().FullName != native_name:
            raise TypedResultError("RESULT_TYPE")
        value = int(value)
    bits = 32 if kind == "int32" else 64
    if not -(2 ** (bits - 1)) <= value < 2 ** (bits - 1):
        raise TypedResultError("RESULT_TYPE")
    return str(value)


def _typed_value(value: Any, kind: str) -> dict:
    if value is None or type(value).__name__ == "DBNull":
        return {"kind": "blank", "value": None}
    if kind == "string" and isinstance(value, str):
        text = value
    elif kind in {"int32", "int64"}:
        text = _typed_integer(value, kind)
    elif kind == "decimal":
        text = _decimal_text(value)
    else:
        raise TypedResultError("RESULT_TYPE")
    try:
        text.encode("utf-8")
    except UnicodeError:
        raise TypedResultError("RESULT_TYPE") from None
    return {"kind": kind, "value": text}


def _typed_columns(reader: Any) -> list[dict]:
    columns, names = [], set()
    for index in range(reader.FieldCount):
        name = reader.GetName(index)
        if not isinstance(name, str) or not name or name in names:
            raise TypedResultError("RESULT_COLUMNS")
        try:
            name.encode("utf-8")
        except UnicodeError:
            raise TypedResultError("RESULT_COLUMNS") from None
        names.add(name)
        kind = _TYPED_CLR_KINDS.get(reader.GetFieldType(index).FullName)
        if kind is None:
            raise TypedResultError("RESULT_TYPE")
        columns.append({"name": name, "kind": kind})
    if not columns:
        raise TypedResultError("RESULT_COLUMNS")
    return columns


def _read_typed(reader: Any, query_hash: str, max_payload_bytes: int) -> bytes:
    columns = _typed_columns(reader)
    envelope = _typed_json({"schema_version": 1, "query_sha256": query_hash, "columns": columns, "rows": []})
    if len(envelope) > max_payload_bytes:
        raise TypedResultError("PAYLOAD_LIMIT")
    # Keep the exact bytes that will be sent, rather than estimating a smaller projection.
    buffer = bytearray(envelope[:-2])
    separator = b""
    while True:
        read = reader.Read()
        if read is False:
            break
        if read is not True:
            raise TypedResultError("RESULT_INCOMPLETE")
        row = [_typed_value(reader.GetValue(index), column["kind"]) for index, column in enumerate(columns)]
        encoded = _typed_json(row)
        if len(buffer) + len(separator) + len(encoded) + 2 > max_payload_bytes:
            raise TypedResultError("PAYLOAD_LIMIT")
        buffer.extend(separator)
        buffer.extend(encoded)
        separator = b","
    if reader.NextResult() is not False:
        raise TypedResultError("RESULT_INCOMPLETE")
    buffer.extend(b"]}")
    return bytes(buffer)


def execute_typed(connection: Any, dax: str, *, max_payload_bytes: int) -> bytes:
    """Return one complete typed-v1 success envelope on an already-open connection, or refuse.

    This public seam must also be qualified with native ADOMD before merge. Mock readers exercise
    control flow, not CLR conversion fidelity. Exceptions crossing this boundary carry fixed codes.
    """
    _typed_query_args(dax, max_payload_bytes)
    try:
        command = connection.CreateCommand()
        command.CommandText = dax
        reader = command.ExecuteReader()
    except Exception:  # pylint: disable=broad-exception-caught
        raise TypedResultError("EXECUTION_FAILED") from None
    try:
        try:
            return _read_typed(reader, hashlib.sha256(dax.encode("utf-8")).hexdigest(), max_payload_bytes)
        finally:
            reader.Close()
    except TypedResultError:
        raise
    except Exception:  # pylint: disable=broad-exception-caught
        raise TypedResultError("RESULT_INCOMPLETE") from None


def _request_object(pairs: list[tuple[str, object]]) -> dict:
    request = {}
    for key, value in pairs:
        if key in request:
            raise TypedResultError("INPUT_INVALID")
        request[key] = value
    return request


def _request_constant(_value: str) -> None:
    raise TypedResultError("INPUT_INVALID")


def _read_request(line: str | bytes) -> dict:
    # The decoder delivers ordered pairs, so duplicate mode fields cannot be lost before routing.
    # Its syntax, recursion and conversion failures establish NO mode and must never select legacy.
    try:
        request = json.loads(line, object_pairs_hook=_request_object, parse_constant=_request_constant)
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise TypedResultError("INPUT_INVALID") from None
    if not isinstance(request, dict):
        raise TypedResultError("INPUT_INVALID")
    return request


def _typed_response(request: dict, executor: Callable[[str, int], bytes] | None) -> bytes:
    try:
        if set(request) != {"dax", "result_format", "max_payload_bytes"}:
            raise TypedResultError("INPUT_INVALID")
        dax, limit = request["dax"], request["max_payload_bytes"]
        _typed_query_args(dax, limit)
        if executor is None:
            raise TypedResultError("TYPED_UNAVAILABLE")
        payload = executor(dax, limit)
        if not isinstance(payload, bytes) or not payload:
            raise TypedResultError("RESULT_SCHEMA")
        if len(payload) > limit:
            raise TypedResultError("PAYLOAD_LIMIT")
        return payload
    except TypedResultError as error:
        return _typed_json({"error": error.code})
    except Exception:  # pylint: disable=broad-exception-caught
        return _typed_json({"error": "EXECUTION_FAILED"})


def _write_typed(stdout: Any, payload: bytes) -> None:
    if hasattr(stdout, "buffer"):
        stdout.buffer.write(payload + b"\n")
        stdout.buffer.flush()
    else:
        stdout.write(payload.decode("utf-8") + "\n")
        stdout.flush()


def _input_lines(stdin: Any) -> Iterator[str | bytes | None]:
    lines = iter(stdin)
    while True:
        try:
            line = next(lines)
        except StopIteration:
            return
        except (UnicodeError, OSError):
            yield None
            return
        yield line


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
    execute.typed = lambda dax, limit: execute_typed(connection, dax, max_payload_bytes=limit)
    return execute


def serve(
    oracle: Callable[[str], dict],
    stdin=None,
    stdout=None,
    *,
    typed_executor: Callable[[str, int], bytes] | None = None,
    on_empty: Callable[[], None] | None = None,
) -> int:
    """The `persistent_oracle` protocol: one JSON request per line in, one response per line out.

    A malformed line answers with an error and keeps the session alive. Killing the process on bad
    input would turn one unreadable query into a lost model refresh, and the refresh is the
    expensive part.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    received = False
    for line in _input_lines(stdin):
        if not isinstance(line, (str, bytes)):
            received = True
            _write_typed(stdout, _typed_json({"error": "INPUT_INVALID"}))
            continue
        line = line.strip()
        if not line:
            continue
        received = True
        try:
            request = _read_request(line)
        except TypedResultError:
            _write_typed(stdout, _typed_json({"error": "INPUT_INVALID"}))
            continue
        if request.get("result_format") == "typed-v1":
            _write_typed(stdout, _typed_response(request, typed_executor))
            continue
        try:
            dax = request["dax"]
            if not isinstance(dax, str):
                raise TypeError
            dax.encode("utf-8")
        except (KeyError, TypeError, UnicodeError):
            _write_typed(stdout, _typed_json({"error": "INPUT_INVALID"}))
            continue
        stdout.write(json.dumps(oracle(dax)) + "\n")
        stdout.flush()
    if not received and on_empty is not None:
        on_empty()
    return 0


def _serve_native(resolve_port: Callable[[], int]) -> int:
    # Bind once before readiness, as the base server did. Hold a startup failure until the ONE
    # protocol loop establishes legacy intent (or empty input); never echo it for ambiguous input.
    execute = None
    bind_error = None
    try:
        execute = adomd_executor(resolve_port())
    except Exception as error:  # pylint: disable=broad-exception-caught
        bind_error = error
    else:
        log.info('ready: one {"dax": ...} JSON request per line on stdin')

    def legacy(dax: str) -> dict:
        if bind_error is not None:
            raise bind_error
        return make_oracle(execute)(dax)

    def typed(dax: str, limit: int) -> bytes:
        if bind_error is not None:
            raise TypedResultError("EXECUTION_FAILED") from None
        return execute.typed(dax, limit)

    def empty() -> None:
        if bind_error is not None:
            raise bind_error

    close_failed = False
    try:
        status = serve(legacy, typed_executor=typed, on_empty=empty)
    finally:
        if execute is not None:
            try:
                execute.close()
            except Exception:  # pylint: disable=broad-exception-caught
                close_failed = True
    return 1 if close_failed or execute is None else status


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

    if not args.certify and not args.query:
        return _serve_native(lambda: args.port or pdq.discover_port(args.pid))  # pylint: disable=no-member

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
