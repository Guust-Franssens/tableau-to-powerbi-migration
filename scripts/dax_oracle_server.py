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

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_SCRIPTS = REPO_ROOT / ".github" / "skills" / "pbip-model-refresh" / "scripts"

log = logging.getLogger("dax_oracle")

# Obligation 2, enforced. `EVALUATE`/`DEFINE` are the query surface; `SELECT` reaches the $SYSTEM
# DMVs, which is how the model is introspected. Everything else - and notably anything that could
# alter a model somebody is mid-migration on - is refused before it reaches the connection.
READ_ONLY_PREFIXES = ("EVALUATE", "DEFINE", "SELECT")

# Where the deterministic tier's contract module may live: the installed plugin first (what an agent
# actually runs), then a sibling clone (what a developer edits). Import is OPTIONAL - the server runs
# without it; only `--certify` needs it, because certification is his function, not our reimplementation.
CONTRACT_CANDIDATES = (
    Path.home()
    / ".copilot/installed-plugins/tableau-collection/tableau-fabric-skills/skills/tableau-migration/scripts",
    REPO_ROOT.parent / "tableau-fabric-skills/skills/tableau-migration/scripts",
)


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
    for candidate in CONTRACT_CANDIDATES:
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
        log.error("CERTIFY: contract module not found in %s", " | ".join(str(c) for c in CONTRACT_CANDIDATES))
        log.error("  Install the tableau-migration plugin, or clone the engine beside this repo.")
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
