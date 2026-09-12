"""Tests for scripts/dax_oracle_server.py - our executor for the engine's `fabric_oracle` socket.

Two layers, deliberately:

* **contract** - certified with the ENGINE'S OWN ``fabric_oracle.conforms``, not a local re-reading
  of it. If his contract tightens, these fail, which is the point. Skipped when the engine is not
  installed, since it is an optional peer.
* **obligations** - each of his three stated obligations gets a test that FAILS when the guard is
  removed. A guard nobody has ever seen bite is a comment.

The third obligation carries the weight: *"a fabricated zero is indistinguishable from a real one and
would produce a false ``verified``, which is the single worst outcome in this system."*
"""

# Preserve legacy regression node IDs and exercise private adapters/native method names.
# pylint: disable=invalid-name,protected-access,missing-function-docstring,too-few-public-methods

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from decimal import Decimal, localcontext
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dax_oracle_server", REPO / "scripts" / "dax_oracle_server.py")
dos = importlib.util.module_from_spec(spec)
sys.modules["dax_oracle_server"] = dos
spec.loader.exec_module(dos)

SIMULATE_ENGINE_ABSENT = "T2P_SIMULATE_ENGINE_ABSENT_FOR_TESTS"


def _contract():
    """The engine's contract module, or None when the deterministic tier is not installed."""
    if os.environ.get(SIMULATE_ENGINE_ABSENT):
        return None
    return dos._load_contract()


ENGINE_SKIP_REASON = "deterministic tier not installed"


def requires_engine(test):
    """Mark an engine-dependent test and skip it when the canonical engine is absent."""
    test = pytest.mark.engine_dependency(expected_skip_reason=ENGINE_SKIP_REASON)(test)
    return pytest.mark.skipif(_contract() is None, reason=ENGINE_SKIP_REASON)(test)


# --- the contract, certified by HIS function --------------------------------------------------


@requires_engine
def test_our_oracle_conforms_to_the_engines_own_contract():
    """Certification must be his `conforms()`, or we certify against our reading of the contract."""
    result = _contract().conforms(dos.make_oracle(dos._stub_executor))
    assert result["ok"], result["failures"]


@requires_engine
def test_the_full_wiring_works_over_a_real_subprocess():
    """His `persistent_oracle` client -> our server -> NDJSON -> back. The loop, end to end.

    Runs the server for real rather than with an injected spawn, because the failures this catches
    (a stray print on stdout, an unflushed buffer, a crash on line 1) only exist across a real pipe.
    """
    contract = _contract()
    cmd = [sys.executable, str(REPO / "scripts" / "dax_oracle_server.py"), "--offline"]
    with contract.persistent_oracle(cmd) as oracle:
        assert contract.conforms(oracle)["ok"]


@requires_engine
def test_reconcile_reaches_BOTH_verified_and_mismatch_through_us():
    """The socket's whole purpose. A wiring that can only say `verified` proves nothing."""
    contract = _contract()
    sys.path.insert(0, str(Path(contract.__file__).parent))
    import translation_reconcile as tr  # noqa: PLC0415  # pylint: disable=import-outside-toplevel,import-error

    cmd = [sys.executable, str(REPO / "scripts" / "dax_oracle_server.py"), "--offline"]
    with contract.persistent_oracle(cmd) as oracle:
        agreed = tr.reconcile("M", "SUM('T'[X])", fabric_oracle=oracle, tableau_value=1)
        differed = tr.reconcile("M", "SUM('T'[X])", fabric_oracle=oracle, tableau_value=999)
    assert agreed["state"] == contract.VERIFIED
    assert differed["state"] == contract.MISMATCH


# --- obligation 1: never raise -------------------------------------------------------------------


def test_an_exploding_executor_becomes_an_error_not_an_exception():
    def boom(_dax):
        raise RuntimeError("connection lost")

    result = dos.make_oracle(boom)('EVALUATE ROW("v", 1)')
    assert result["error"].startswith("RuntimeError: connection lost")


@pytest.mark.parametrize("bad", [None, "", "   ", 42])
def test_junk_input_is_reported_never_raised(bad):
    assert "error" in dos.make_oracle(lambda _d: [{"v": 1}])(bad)


# --- obligation 2: pure read ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        'EVALUATE ROW("v", 1)',
        "DEFINE MEASURE 'T'[M] = 1 EVALUATE ROW(\"v\", [M])",
        "SELECT * FROM $SYSTEM.TMSCHEMA_TABLES",
        '  \n evaluate row("v", 1)',
        '// a comment\nEVALUATE ROW("v", 1)',
    ],
)
def test_read_only_statements_are_allowed(query):
    assert dos.is_read_only(query)


@pytest.mark.parametrize(
    "query",
    [
        "DROP TABLE Orders",
        "<Batch><Alter/></Batch>",
        "CREATE MEASURE 'T'[M] = 1",
        "ALTER CUBE",
        'EXECUTE("EVALUATE ROW(\\"v\\",1)")',
    ],
)
def test_write_shaped_statements_are_REFUSED_before_the_connection(query):
    """The oracle is pointed at a model somebody is mid-migration on. Intent is not a control."""
    assert not dos.is_read_only(query)
    result = dos.make_oracle(lambda _d: pytest.fail("executor must never be reached"))(query)
    assert "refused" in result["error"]


# --- obligation 3: absence is never zero ---------------------------------------------------------


def test_an_empty_result_set_is_an_ERROR_never_zero():
    """The false-green his contract calls the single worst outcome in the system.

    Returning 0 for "nothing came back" would let a measure that evaluates to nothing be labelled
    `verified` against a genuine 0.
    """
    result = dos.make_oracle(lambda _d: [])('EVALUATE ROW("v", 1)')
    assert result.get("error"), "an empty result set must not be reported as a value"
    assert "rows" not in result


def test_a_real_BLANK_survives_as_null_not_zero():
    result = dos.make_oracle(lambda _d: [{"[v]": None}])('EVALUATE ROW("v", 1)')
    assert result["rows"] == [{"[v]": None}]


@requires_engine
def test_extract_scalar_reads_our_row_shape():
    """Our shape has to be one HIS parser already reads - that function IS the contract."""
    sys.path.insert(0, str(Path(_contract().__file__).parent))
    import translation_reconcile as tr  # noqa: PLC0415  # pylint: disable=import-outside-toplevel,import-error

    value, error = tr.extract_scalar(dos.make_oracle(lambda _d: [{"[value]": 12.5}])('EVALUATE ROW("value", 1)'))
    assert (value, error) == (12.5, None)


# --- the marshalling trap ------------------------------------------------------------------------


class _FakeDBNull:
    """Stands in for System.DBNull, which is matched by type NAME (no .NET runtime in CI)."""

    __name__ = "DBNull"


_FakeDBNull.__qualname__ = "DBNull"


def test_decimal_survives_as_a_NUMBER_not_a_string():
    """Currency/Fixed-Decimal columns return System.Decimal, which json.dumps cannot serialise.

    Unhandled it raises inside the response path AND corrupts the NDJSON stream, so one bad column
    kills the session rather than the query. Stringifying it instead would be worse-but-quiet: the
    upstream comparison would become a STRING comparison and mislabel a correct translation.
    """
    marshalled = dos._json_safe(Decimal("12.50"))
    assert marshalled == 12.5
    assert isinstance(marshalled, float)
    assert json.dumps(marshalled) == "12.5"


def test_dbnull_marshals_to_null_not_zero():
    fake = _FakeDBNull()
    type(fake).__name__ = "DBNull"
    assert dos._json_safe(fake) is None


@pytest.mark.parametrize("value", [3, 2.5, True, False, "x", None])
def test_plain_values_pass_through_unchanged(value):
    assert dos._json_safe(value) is value or dos._json_safe(value) == value
    json.dumps(dos._json_safe(value))


def test_an_unserialisable_value_degrades_to_a_string_rather_than_crashing():
    class Foreign:
        """Legacy foreign values intentionally retain their string fallback."""

        def __str__(self):
            return "2026-08-07"

    assert dos._json_safe(Foreign()) == "2026-08-07"


# --- the NDJSON protocol -------------------------------------------------------------------------


def test_serve_answers_one_json_document_per_line():
    out = StringIO()
    dos.serve(dos.make_oracle(lambda _d: [{"[value]": 7}]), StringIO('{"dax": "EVALUATE ROW(\\"v\\",1)"}\n'), out)
    assert json.loads(out.getvalue().strip()) == {"rows": [{"[value]": 7}]}


def test_a_malformed_line_does_not_kill_the_SESSION():
    """The refresh is the expensive part; one unreadable query must not cost it."""
    out = StringIO()
    stdin = StringIO('not json\n{"nope": 1}\n{"dax": "EVALUATE ROW(\\"v\\",1)"}\n')
    dos.serve(dos.make_oracle(lambda _d: [{"[value]": 7}]), stdin, out)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert len(lines) == 3, "every request must get exactly one response"
    assert "error" in lines[0] and "error" in lines[1]
    assert lines[2] == {"rows": [{"[value]": 7}]}


def test_blank_lines_are_skipped_not_answered():
    out = StringIO()
    dos.serve(dos.make_oracle(lambda _d: [{"v": 1}]), StringIO("\n\n"), out)
    assert out.getvalue() == ""


# --- the CLI's own refusals ----------------------------------------------------------------------


def test_serving_without_a_target_is_refused_rather_than_guessed():
    """`discover_port` refuses to widen to 'any msmdsrv'; the CLI must not undo that by guessing."""
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "dax_oracle_server.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "--pid" in proc.stderr


def test_offline_mode_says_plainly_that_it_proves_nothing_about_a_model():
    """An offline pass is plumbing evidence only. It must never read as model verification.

    The disclaimer is asserted unconditionally because it is printed BEFORE certification runs, so
    it holds with or without the engine installed. The exit code is only meaningful when the engine
    IS present - without it, `--certify` correctly exits 2 ("contract module not found"), and
    asserting 0 there tests the machine rather than the code (measured: this failed on CI, which
    has no deterministic tier).
    """
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "dax_oracle_server.py"), "--certify", "--offline"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "PROVES NOTHING" in proc.stderr.upper()
    if _contract() is not None:
        assert proc.returncode == 0, proc.stderr


def test_certify_without_the_engine_refuses_rather_than_passing_vacuously():
    """No contract module must mean "cannot certify", never "certified".

    The dangerous failure here is the silent one: if a missing engine degraded to a pass, every run
    on a machine without the deterministic tier would report CONFORMS having checked nothing.

    The engine now resolves through the ONE canonical resolver (`engine_source`, issue #107), so the
    absence is simulated by making that resolver raise - which is exactly what it does when the
    plugin is not installed. It no longer searches a candidate list, so there is no list to blank.
    """
    import dax_oracle_server as module  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    def _absent():
        raise module.EngineNotFoundError("plugin not installed (simulated)")

    original = module.engine_scripts_dir
    module.engine_scripts_dir = _absent
    try:
        assert module._load_contract() is None
        assert module.certify(module.make_oracle(module._stub_executor)) == 2
    finally:
        module.engine_scripts_dir = original


# --- typed-v1: independent fake reader controls, NOT native ADOMD qualification -------------------

TYPED_QUERY = "EVALUATE 'T'"
SENTINELS = ("SENSITIVE_QUERY", "SENSITIVE_COLUMN", "SENSITIVE_VALUE", "SENSITIVE_ENDPOINT", "SENSITIVE_CREDENTIAL")


def _native_failure() -> None:
    raise RuntimeError(" ".join(SENTINELS))


class _TypedReader:
    """A controllable reader with explicit EOF, second-result, and close observations."""

    def __init__(self, columns=None, rows=None, extra=False, fault=None):
        self.columns = (
            [
                ("[key]", "System.String"),
                ("[small]", "System.Int32"),
                ("[amount]", "System.Decimal"),
                ("[big]", "System.Int64"),
            ]
            if columns is None
            else columns
        )
        self.rows = iter(
            [
                ["\u00e9", 7, Decimal("9007199254740993.1250"), 9223372036854775807],
                ["\u00e9", 7, Decimal("9007199254740993.1250"), 9223372036854775807],
                ["z", -2147483648, None, -9223372036854775808],
            ]
            if rows is None
            else rows
        )
        self.FieldCount = len(self.columns)
        self.state = SimpleNamespace(closed=False, reads=0, next_results=0)
        self.extra = extra
        self.fault = fault
        self.current = None

    def GetName(self, index: int) -> str:
        return self.columns[index][0]

    def GetFieldType(self, index: int) -> SimpleNamespace:
        return SimpleNamespace(FullName=self.columns[index][1])

    def Read(self) -> bool | None:
        self.state.reads += 1
        if self.fault == "read" and self.state.reads > 1:
            _native_failure()
        self.current = next(self.rows, None)
        if self.current is None and self.fault == "eof":
            return None
        return self.current is not None

    def GetValue(self, index: int) -> object:
        if self.fault == "value" and self.state.reads > 1:
            _native_failure()
        return self.current[index]

    def NextResult(self) -> bool | None:
        self.state.next_results += 1
        if self.fault == "next":
            _native_failure()
        return self.extra

    def Close(self) -> None:
        self.state.closed = True
        if self.fault == "close":
            _native_failure()


def _typed_connection(reader: _TypedReader) -> tuple[SimpleNamespace, SimpleNamespace]:
    command = SimpleNamespace(CommandText=None, ExecuteReader=lambda: reader)
    return SimpleNamespace(CreateCommand=lambda: command), command


def _expected_typed(query: str = TYPED_QUERY) -> dict:
    # Hand-authored cells and metadata: no production serializer/normalizer builds the expected side.
    return {
        "schema_version": 1,
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "columns": [
            {"name": "[key]", "kind": "string"},
            {"name": "[small]", "kind": "int32"},
            {"name": "[amount]", "kind": "decimal"},
            {"name": "[big]", "kind": "int64"},
        ],
        "rows": [
            [
                {"kind": "string", "value": "\u00e9"},
                {"kind": "int32", "value": "7"},
                {"kind": "decimal", "value": "9007199254740993.1250"},
                {"kind": "int64", "value": "9223372036854775807"},
            ],
            [
                {"kind": "string", "value": "\u00e9"},
                {"kind": "int32", "value": "7"},
                {"kind": "decimal", "value": "9007199254740993.1250"},
                {"kind": "int64", "value": "9223372036854775807"},
            ],
            [
                {"kind": "string", "value": "z"},
                {"kind": "int32", "value": "-2147483648"},
                {"kind": "blank", "value": None},
                {"kind": "int64", "value": "-9223372036854775808"},
            ],
        ],
    }


def _expected_wire(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _serve_typed(executor=None, request=None) -> bytes:
    if request is None:
        request = {"dax": TYPED_QUERY, "result_format": "typed-v1", "max_payload_bytes": 100_000}
    output = StringIO()
    dos.serve(
        lambda _query: pytest.fail("typed requests must not enter the legacy oracle"),
        StringIO(json.dumps(request) + "\n"),
        output,
        typed_executor=executor,
    )
    return output.getvalue().encode("utf-8")


def _reader_executor(reader: _TypedReader):
    connection, _ = _typed_connection(reader)
    return lambda query, limit: dos.execute_typed(connection, query, max_payload_bytes=limit)


def test_typed_reader_keeps_complete_order_multiplicity_and_native_kinds() -> None:
    """Every column and duplicate row survives, including exact Decimal scale and BLANK."""
    reader = _TypedReader()
    connection, command = _typed_connection(reader)
    got = dos.execute_typed(connection, TYPED_QUERY, max_payload_bytes=100_000)
    assert got == _expected_wire(_expected_typed())
    assert command.CommandText == TYPED_QUERY
    assert reader.state.reads == 4
    assert reader.state.next_results == 1
    assert reader.state.closed


def test_typed_query_hash_covers_exact_unrewritten_utf8() -> None:
    """Whitespace, comments, accents, and line endings are hashed exactly as executed."""
    query = "\n// \u00e9 comment\r\n  EVALUATE 'T' \r\n"
    reader = _TypedReader()
    connection, command = _typed_connection(reader)
    got = dos.execute_typed(connection, query, max_payload_bytes=100_000)
    assert command.CommandText == query
    assert json.loads(got)["query_sha256"] == hashlib.sha256(query.encode("utf-8")).hexdigest()
    assert json.loads(got)["query_sha256"] != hashlib.sha256(query.strip().encode("utf-8")).hexdigest()
    assert json.loads(got)["query_sha256"] != hashlib.sha256(TYPED_QUERY.encode()).hexdigest()


def test_typed_decimal_never_enters_legacy_float_marshalling(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lossy legacy normalizer is unreachable in typed mode."""
    monkeypatch.setattr(dos, "_json_safe", lambda _value: pytest.fail("legacy marshaller reached"))
    assert _serve_typed(_reader_executor(_TypedReader())) == _expected_wire(_expected_typed()) + b"\n"


def test_clr_decimal_uses_invariant_formatting_without_float(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLR formatting seam is independently mocked; real pythonnet remains a native gate."""
    invariant = object()
    seen = []

    def to_string(_self, provider):
        seen.append(provider)
        return "12345678901234567890.1200"

    native_decimal = type(
        "Decimal", (), {"ToString": to_string, "__float__": lambda _self: pytest.fail("float used")}
    )()
    monkeypatch.setitem(
        sys.modules, "System.Globalization", SimpleNamespace(CultureInfo=SimpleNamespace(InvariantCulture=invariant))
    )
    reader = _TypedReader(columns=[("v", "System.Decimal")], rows=[[native_decimal]])
    result = json.loads(_reader_executor(reader)(TYPED_QUERY, 1000))
    assert result["rows"] == [[{"kind": "decimal", "value": "12345678901234567890.1200"}]]
    assert seen == [invariant]


def test_clr_dbnull_is_blank_not_a_fabricated_zero() -> None:
    """A native-shaped DBNull and Python None have the same lossless transport meaning."""
    dbnull = type("DBNull", (), {})()
    reader = _TypedReader(columns=[("v", "System.Decimal")], rows=[[dbnull], [None]])
    result = json.loads(_reader_executor(reader)(TYPED_QUERY, 1000))
    assert result["rows"] == [[{"kind": "blank", "value": None}], [{"kind": "blank", "value": None}]]


@pytest.mark.parametrize("kind,value", [("Int32", 2147483647), ("Int64", 9223372036854775807)])
def test_native_integral_wrappers_keep_the_declared_kind(kind: str, value: int) -> None:
    """An integral CLR wrapper does not require a float or a kind inference."""
    full_name = f"System.{kind}"
    native = type(
        kind, (), {"GetType": lambda _self: SimpleNamespace(FullName=full_name), "__int__": lambda _self: value}
    )()
    reader = _TypedReader(columns=[("v", full_name)], rows=[[native]])
    assert json.loads(_reader_executor(reader)(TYPED_QUERY, 1000))["rows"] == [
        [{"kind": kind.lower(), "value": str(value)}]
    ]


def test_typed_zero_rows_are_allowed_only_after_verified_eof() -> None:
    """A complete empty result is transport success, not legacy's empty-result error."""
    reader = _TypedReader(rows=[])
    expected = _expected_typed()
    expected["rows"] = []
    assert _reader_executor(reader)(TYPED_QUERY, 1000) == _expected_wire(expected)
    assert reader.state.reads == reader.state.next_results == 1
    assert reader.state.closed


@pytest.mark.parametrize("fault", ["read", "value", "next", "eof", "close"])
def test_typed_read_failures_never_return_partial_success(fault: str) -> None:
    """Even a late read/EOF/close failure must discard every collected row."""
    reader = _TypedReader(fault=fault)
    assert _serve_typed(_reader_executor(reader)) == b'{"error":"RESULT_INCOMPLETE"}\n'
    assert reader.state.closed


@pytest.mark.parametrize("extra", [True, None])
def test_second_result_or_unestablished_result_termination_refuses(extra: bool | None) -> None:
    """Only an explicit False from NextResult completes the admitted single result set."""
    reader = _TypedReader(extra=extra)
    assert _serve_typed(_reader_executor(reader)) == b'{"error":"RESULT_INCOMPLETE"}\n'
    assert reader.state.next_results == 1
    assert reader.state.closed


@pytest.mark.parametrize("columns", [[], [("", "System.Int64")], [("v", "System.Int64"), ("v", "System.Int64")]])
def test_typed_missing_or_duplicate_columns_refuse(columns: list[tuple[str, str]]) -> None:
    """Column ambiguity must not be hidden by dict construction."""
    reader = _TypedReader(columns=columns, rows=[])
    assert _serve_typed(_reader_executor(reader)) == b'{"error":"RESULT_COLUMNS"}\n'
    assert reader.state.closed


def test_case_distinct_column_names_are_preserved_not_folded() -> None:
    """Transport names are exact, not a case-insensitive identity guess."""
    reader = _TypedReader(columns=[("a", "System.Int32"), ("A", "System.Int32")], rows=[[1, 2]])
    got = json.loads(_reader_executor(reader)(TYPED_QUERY, 1000))
    assert got["columns"] == [{"name": "a", "kind": "int32"}, {"name": "A", "kind": "int32"}]
    assert got["rows"] == [[{"kind": "int32", "value": "1"}, {"kind": "int32", "value": "2"}]]


@pytest.mark.parametrize("native_kind", ["System.Boolean", "System.Double", "System.DateTime", "System.Object"])
def test_unsupported_native_column_kind_refuses_even_for_zero_rows(native_kind: str) -> None:
    """An empty result does not turn unsupported type metadata into admitted evidence."""
    reader = _TypedReader(columns=[("v", native_kind)], rows=[])
    assert _serve_typed(_reader_executor(reader)) == b'{"error":"RESULT_TYPE"}\n'


@pytest.mark.parametrize(
    "native_kind,value",
    [
        ("System.Int32", True),
        ("System.Int32", 1.0),
        ("System.Int32", 2147483648),
        ("System.Int64", 9223372036854775808),
        ("System.Decimal", 1.0),
        ("System.Decimal", "1.00"),
        ("System.Decimal", Decimal("NaN")),
        ("System.Decimal", Decimal("Infinity")),
        ("System.String", 1),
        ("System.String", "\ud800"),
    ],
)
def test_unsupported_or_lossy_cells_are_not_stringified(native_kind: str, value: object) -> None:
    """The typed path has no legacy foreign-value fallback."""
    reader = _TypedReader(columns=[("v", native_kind)], rows=[[value]])
    assert _serve_typed(_reader_executor(reader)) == b'{"error":"RESULT_TYPE"}\n'
    assert reader.state.closed


def test_typed_payload_budget_is_the_exact_success_envelope() -> None:
    """Use independently encoded expected bytes for both the accepted and refused boundary."""
    expected = _expected_wire(_expected_typed())
    reader = _TypedReader()
    assert _reader_executor(reader)(TYPED_QUERY, len(expected)) == expected
    request = {"dax": TYPED_QUERY, "result_format": "typed-v1", "max_payload_bytes": len(expected) - 1}
    reader = _TypedReader()
    assert _serve_typed(_reader_executor(reader), request) == b'{"error":"PAYLOAD_LIMIT"}\n'
    assert reader.state.closed
    assert len(expected.decode("utf-8")) < len(expected)


def test_typed_empty_header_also_consumes_budget() -> None:
    """A budget too small for metadata cannot yield an empty success."""
    reader = _TypedReader(rows=[])
    request = {"dax": TYPED_QUERY, "result_format": "typed-v1", "max_payload_bytes": 1}
    assert _serve_typed(_reader_executor(reader), request) == b'{"error":"PAYLOAD_LIMIT"}\n'
    assert reader.state.reads == 0
    assert reader.state.closed


@pytest.mark.parametrize("limit", [None, True, 0, -1, 1.5, "100"])
def test_typed_request_budget_is_required_and_not_coerced(limit: object) -> None:
    """Reject malformed budgets before the executor can be called."""
    request = {"dax": TYPED_QUERY, "result_format": "typed-v1", "max_payload_bytes": limit}
    assert _serve_typed(lambda *_args: pytest.fail("executor reached"), request) == b'{"error":"INPUT_INVALID"}\n'


@pytest.mark.parametrize("variant", ["missing_budget", "missing_query", "extra"])
def test_typed_request_fields_are_closed(variant: str) -> None:
    """No request identity, receipt, or execution-policy fields enter through this branch."""
    request = {"dax": TYPED_QUERY, "result_format": "typed-v1", "max_payload_bytes": 1000}
    if variant == "missing_budget":
        del request["max_payload_bytes"]
    elif variant == "missing_query":
        del request["dax"]
    else:
        request["extra"] = "SENSITIVE_CREDENTIAL"
    assert _serve_typed(lambda *_args: pytest.fail("executor reached"), request) == b'{"error":"INPUT_INVALID"}\n'


def test_duplicate_typed_request_members_are_refused_without_echo() -> None:
    """The legacy JSON parser must not silently decide an ambiguous typed budget."""
    request = (
        '{"result_format":"typed-v1","dax":"EVALUATE \'SENSITIVE_QUERY\'",'
        '"max_payload_bytes":1000,"max_payload_bytes":2000}\n'
    )
    output = StringIO()
    dos.serve(lambda _query: pytest.fail("legacy reached"), StringIO(request), output)
    assert output.getvalue() == '{"error":"INPUT_INVALID"}\n'


@pytest.mark.parametrize("query", [None, "", "  ", "\ud800", "DROP TABLE SENSITIVE_QUERY"])
def test_typed_invalid_queries_refuse_without_native_execution(query: object) -> None:
    """Validation never rewrites a query or sends an unsupported statement to ADOMD."""
    request = {"dax": query, "result_format": "typed-v1", "max_payload_bytes": 1000}
    assert _serve_typed(lambda *_args: pytest.fail("executor reached"), request) == b'{"error":"QUERY_INVALID"}\n'


def test_typed_execution_failure_is_a_fixed_union_error() -> None:
    """Connection/command exceptions carry no driver message into the typed response."""
    connection = SimpleNamespace(CreateCommand=_native_failure)

    def execute(query: str, limit: int) -> bytes:
        return dos.execute_typed(connection, query, max_payload_bytes=limit)

    assert _serve_typed(execute) == b'{"error":"EXECUTION_FAILED"}\n'


def test_no_typed_executor_means_unavailable_not_legacy_stub_rows() -> None:
    """Offline plumbing never fabricates a typed success."""
    assert _serve_typed() == b'{"error":"TYPED_UNAVAILABLE"}\n'


@pytest.mark.parametrize("fault", ["read", "value", "next", "close"])
def test_sensitive_sentinels_are_absent_from_typed_diagnostics(
    fault: str, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """Late failures may contain sensitive driver text; only the fixed code may escape."""
    reader = _TypedReader(
        columns=[("SENSITIVE_COLUMN", "System.String")],
        rows=[["SENSITIVE_VALUE"], ["SENSITIVE_CREDENTIAL"]],
        fault=fault,
    )
    request = {"dax": "EVALUATE 'SENSITIVE_QUERY'", "result_format": "typed-v1", "max_payload_bytes": 1000}
    output = _serve_typed(_reader_executor(reader), request)
    assert output == b'{"error":"RESULT_INCOMPLETE"}\n'
    captured = capsys.readouterr()
    diagnostics = output.decode() + captured.out + captured.err + caplog.text
    assert all(sentinel not in diagnostics for sentinel in SENTINELS)


@pytest.mark.parametrize("discriminator", [None, False, "Typed-v1", "typed-v2", "typed-v1 ", {"kind": "typed-v1"}])
def test_only_the_exact_discriminator_changes_the_legacy_response(discriminator: object) -> None:
    """Other requests retain byte-identical legacy output, even with an invalid typed budget."""
    request = {"dax": TYPED_QUERY, "result_format": discriminator, "max_payload_bytes": False}
    output = StringIO()
    dos.serve(
        dos.make_oracle(lambda _query: [{"v": 12.5, "blank": None, "text": "\u00e9"}]),
        StringIO(json.dumps(request) + "\n"),
        output,
        typed_executor=lambda *_args: pytest.fail("typed executor reached"),
    )
    assert output.getvalue().encode() == b'{"rows": [{"v": 12.5, "blank": null, "text": "\\u00e9"}]}\n'


def test_legacy_malformed_missing_and_empty_requests_keep_exact_bytes() -> None:
    """The opt-in branch does not redact, reformat, or otherwise change legacy responses."""
    output = StringIO()
    requests = 'not json\n{"other":1}\n{"dax":""}\n{"dax":"EVALUATE \'T\'"}\n'
    dos.serve(dos.make_oracle(lambda _query: []), StringIO(requests), output)
    assert output.getvalue() == (
        '{"error": "expected {\'dax\': ...}, got not json"}\n'
        '{"error": "expected {\'dax\': ...}, got {\\"other\\":1}"}\n'
        '{"error": "empty DAX query"}\n'
        '{"error": "query returned no rows"}\n'
    )


def test_typed_failure_does_not_break_following_legacy_requests() -> None:
    """One typed refusal must leave the persistent legacy socket usable."""
    output = StringIO()
    requests = (
        '{"dax":"EVALUATE \'T\'","result_format":"typed-v1","max_payload_bytes":1000}\n{"dax":"EVALUATE \'T\'"}\n'
    )
    dos.serve(dos.make_oracle(lambda _query: [{"v": 7}]), StringIO(requests), output)
    assert output.getvalue() == '{"error":"TYPED_UNAVAILABLE"}\n{"rows": [{"v": 7}]}\n'


def test_native_server_seam_routes_before_legacy_marshalling(monkeypatch: pytest.MonkeyPatch) -> None:
    """The actual --port route reaches execute_typed on a mocked connection, not a new runtime."""
    reader = _TypedReader()
    connection, command = _typed_connection(reader)
    calls = []
    connection.Open = lambda: calls.append("open")
    connection.Close = lambda: calls.append("close")

    def connect(connection_string: str):
        assert connection_string == "Data Source=localhost:45678"
        return connection

    monkeypatch.setitem(sys.modules, "probe_desktop_query", SimpleNamespace(_load_adomd=lambda: connect))
    monkeypatch.setattr(dos, "_json_safe", lambda _value: pytest.fail("legacy marshaller reached"))
    request = {"dax": TYPED_QUERY, "result_format": "typed-v1", "max_payload_bytes": 100_000}
    output = StringIO()
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(request) + "\n"))
    monkeypatch.setattr(sys, "stdout", output)
    assert dos.main(["--port", "45678"]) == 0
    assert output.getvalue().encode("utf-8") == _expected_wire(_expected_typed()) + b"\n"
    assert command.CommandText == TYPED_QUERY
    assert reader.state.closed
    assert calls == ["open", "close"]


@pytest.mark.parametrize("phase", ["discovery", "connection"])
def test_typed_native_startup_failures_do_not_leak_or_enter_legacy_diagnostics(
    phase: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Mocked discovery/open failures must cross the same sanitized typed response boundary."""
    pdq = SimpleNamespace(discover_port=lambda _pid: _native_failure())
    monkeypatch.setitem(sys.modules, "probe_desktop_query", pdq)
    monkeypatch.setattr(dos, "adomd_executor", lambda _port: _native_failure())
    request = {"dax": "EVALUATE 'SENSITIVE_QUERY'", "result_format": "typed-v1", "max_payload_bytes": 1000}
    output = StringIO()
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(request) + "\n"))
    monkeypatch.setattr(sys, "stdout", output)
    with caplog.at_level("INFO"):
        assert dos.main(["--pid", "45678"] if phase == "discovery" else ["--port", "45678"]) == 0
    assert output.getvalue() == '{"error":"EXECUTION_FAILED"}\n'
    assert all(sentinel not in caplog.text + output.getvalue() for sentinel in SENTINELS)
    assert "45678" not in caplog.text


def test_offline_cli_refuses_typed_results_but_keeps_legacy_plumbing() -> None:
    """This is a real subprocess, but explicitly no Desktop/ADOMD qualification."""
    requests = (
        '{"dax":"EVALUATE \'T\'","result_format":"typed-v1","max_payload_bytes":1000}\n'
        '{"dax":"EVALUATE ROW(\\"v\\",1)"}\n'
    )
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "dax_oracle_server.py"), "--offline"],
        input=requests,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout == '{"error":"TYPED_UNAVAILABLE"}\n{"rows": [{"[value]": 1}]}\n'


def test_typed_decimal_transport_does_not_use_context_precision() -> None:
    """Scale and digits survive even when the caller has an unusually small Decimal context."""
    expected = _expected_wire(_expected_typed())
    with localcontext() as context:
        context.prec = 1
        context.clear_flags()
        assert _reader_executor(_TypedReader())(TYPED_QUERY, len(expected)) == expected
        assert context.prec == 1
        assert not any(context.flags.values())


def test_typed_transport_has_no_inherited_row_limit() -> None:
    """The frozen candidate's 100,000-row ceiling is deliberately not part of this slice."""
    reader = _TypedReader(columns=[("v", "System.Int32")], rows=([1] for _ in range(100_001)))
    expected = {
        "schema_version": 1,
        "query_sha256": hashlib.sha256(TYPED_QUERY.encode()).hexdigest(),
        "columns": [{"name": "v", "kind": "int32"}],
        "rows": [[{"kind": "int32", "value": "1"}]] * 100_001,
    }
    wire = _expected_wire(expected)
    assert _reader_executor(reader)(TYPED_QUERY, len(wire)) == wire
    assert reader.state.reads == 100_002
    assert reader.state.closed


def test_typed_writer_emits_exact_utf8_bytes_without_text_newline_translation() -> None:
    """A real stdout buffer receives UTF-8 and one LF, regardless of its text encoding."""
    from io import BytesIO  # pylint: disable=import-outside-toplevel

    output = SimpleNamespace(buffer=BytesIO(), write=lambda _text: pytest.fail("text writer used"))
    expected = _expected_wire(_expected_typed())
    dos.serve(
        lambda _query: pytest.fail("legacy reached"),
        StringIO('{"dax":"EVALUATE \'T\'","result_format":"typed-v1","max_payload_bytes":100000}\n'),
        output,
        typed_executor=_reader_executor(_TypedReader()),
    )
    assert output.buffer.getvalue() == expected + b"\n"
