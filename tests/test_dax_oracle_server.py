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

import importlib.util
import json
import os
import subprocess
import sys
from decimal import Decimal
from io import StringIO
from pathlib import Path

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


requires_engine = pytest.mark.skipif(_contract() is None, reason="deterministic tier not installed")


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
    import translation_reconcile as tr  # noqa: PLC0415

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
    import translation_reconcile as tr  # noqa: PLC0415

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
    import dax_oracle_server as module  # noqa: PLC0415

    def _absent():
        raise module.EngineNotFoundError("plugin not installed (simulated)")

    original = module.engine_scripts_dir
    module.engine_scripts_dir = _absent
    try:
        assert module._load_contract() is None
        assert module.certify(module.make_oracle(module._stub_executor)) == 2
    finally:
        module.engine_scripts_dir = original
