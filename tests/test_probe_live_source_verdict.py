"""Regression tests for probe_live_source's machine-readable verdict parsing.

Two seams are pinned here:
  * #152 - the parent (``probe_live_source``) must accept the verdict family the child
    (``refresh_pbip_model`` via ``_verdict``) actually emits for the argv the parent actually builds,
    honouring the child's exit code. The old suite hand-wrote ``REFRESH: DATA_OK`` - output the
    production caller CANNOT produce - so it stayed green while the gate was un-liftable.
  * #153 - the child's reassuring no-dialog banner must not classify as ``NO_CREDENTIAL``.
"""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
from pathlib import Path

import pytest

# pylint: disable=import-outside-toplevel,protected-access

REPO = Path(__file__).resolve().parent.parent


def _import_probe_live_source():
    """Import the script module from the repo-local scripts folder."""
    scripts = str(REPO / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import probe_live_source  # noqa: PLC0415

    return probe_live_source


def _import_skill_modules():
    """Import the CHILD-side modules the probe shells out to, from the SAME dir the probe resolves.

    Derived from ``probe_live_source.SKILL_SCRIPTS`` so the seam test binds to the real child, not a
    hand-picked path. Returns (refresh_pbip_model, _verdict, _credential_modal).
    """
    probe_live_source = _import_probe_live_source()
    skill_scripts = str(probe_live_source.SKILL_SCRIPTS)
    if skill_scripts not in sys.path:
        sys.path.insert(0, skill_scripts)
    import _credential_modal  # noqa: PLC0415
    import _verdict  # noqa: PLC0415
    import refresh_pbip_model  # noqa: PLC0415

    return refresh_pbip_model, _verdict, _credential_modal


def test_requires_data_ok_verdict_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stray DATA_OK substring after CREDENTIAL_MISSING must not clear the live-source gate."""
    probe_live_source = _import_probe_live_source()
    stdout = (
        "PREFLIGHT: CREDENTIAL_MISSING pid=111; window title='(empty title)'\n"
        "PREFLIGHT: DATA_OK_FROM_WORKER_AFTER_RELEASE\n"
    )
    monkeypatch.setattr(
        probe_live_source.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(args=["refresh"], returncode=1, stdout=stdout, stderr=""),
    )

    assert probe_live_source._refresh_and_classify(123, "Orders", 1, network_fault_observed=False) == (  # noqa: SLF001
        1,
        "NO_CREDENTIAL",
    )


def test_probe_argv_fed_through_real_emitter_is_accepted_by_the_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    """SEAM (#152): the argv the probe ACTUALLY builds, parsed by refresh_pbip_model's REAL parser and
    run through _verdict's REAL emitter on a successful single-row refresh, must produce a verdict line
    _has_data_ok_verdict accepts.

    This is the test CI was missing. The old fixture hand-wrote ``REFRESH: DATA_OK``, which the
    production caller CANNOT elicit - it always passes ``--tables`` and never ``--canaries``, so the
    child emits ``TABLES_OK``. Deriving the fixture from BOTH real sides is the only way to catch a
    vocabulary desync across the parent/child seam.
    """
    probe_live_source = _import_probe_live_source()
    refresh_pbip_model, verdict_mod, _ = _import_skill_modules()

    # 1. Capture the EXACT argv the production caller builds, without running a real refresh.
    captured: dict[str, list[str]] = {}

    def _capture(cmd, *_a, **_k):  # noqa: ANN001, ANN002, ANN003
        captured["argv"] = list(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(probe_live_source.subprocess, "run", _capture)
    probe_live_source._refresh_and_classify(123, "shipment", 1, network_fault_observed=False)
    argv = captured["argv"]

    # The exact desync #152 named: the probe always narrows with --tables and never asks for --canaries.
    assert "--tables" in argv and "--canaries" not in argv

    # 2. Parse the child flags (everything after `python refresh_pbip_model.py`) with the REAL parser.
    child_flags = argv[2:]
    args = refresh_pbip_model._build_arg_parser().parse_args(child_flags)
    assert args.tables == ["shipment"]
    (table,) = args.tables

    # 3. Run the REAL emitter on a successful single-row refresh; capture its verdict line verbatim.
    implicit = not verdict_mod._canary_tables(args)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exit_code = verdict_mod._emit_data_verdict(None, 0.0, args, [(table, 1)], implicit)
    emitted = buffer.getvalue()

    assert exit_code == 0, f"the child returns success on a good refresh; got exit {exit_code}"
    assert "TABLES_OK" in emitted, f"sanity: the production argv elicits TABLES_OK, not DATA_OK: {emitted!r}"
    # 4. The parent MUST accept what the child actually emits, for the table it actually probed.
    assert probe_live_source._has_data_ok_verdict(emitted, table), (
        f"verdict-vocabulary desync across the seam: child emitted {emitted!r}, parent rejected it"
    )


def test_probe_clears_gate_on_verbatim_live_databricks_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #152 reproduction, VERBATIM from the 2026-08-14 live Databricks run: child exit 0 and a
    ``TABLES_OK`` for the probed table. This exact input returned NO_CREDENTIAL at exit 1 before the fix.
    """
    probe_live_source = _import_probe_live_source()
    stdout = "  refresh: refreshed shipment\n  data   : 1 row(s) in 'shipment'\nREFRESH: TABLES_OK 'shipment'\n"
    monkeypatch.setattr(
        probe_live_source.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(args=["refresh"], returncode=0, stdout=stdout, stderr=""),
    )

    assert probe_live_source._refresh_and_classify(123, "shipment", 1, network_fault_observed=False) == (  # noqa: SLF001
        0,
        "DATA_OK",
    )


def test_has_data_ok_verdict_accepts_model_level_data_ok() -> None:
    """A whole-model ``DATA_OK`` stays a valid clear for any probed table (it certifies every source)."""
    probe_live_source = _import_probe_live_source()
    assert probe_live_source._has_data_ok_verdict("REFRESH: DATA_OK\n", "anything")
    assert probe_live_source._has_data_ok_verdict("REFRESH: DATA_OK + PERSISTED\n", "anything")


def test_scoped_table_verdicts_clear_only_for_the_probed_table() -> None:
    """#152 + #115: TABLE_OK/TABLES_OK clear the gate for the table the probe asked to refresh, and ONLY
    that one. A verdict naming some OTHER table is a false certificate and must not count."""
    probe_live_source = _import_probe_live_source()
    # Positive - the probed table is the one certified (single, multi-table membership, and TABLE_OK).
    assert probe_live_source._has_data_ok_verdict("REFRESH: TABLES_OK 'shipment'\n", "shipment")
    assert probe_live_source._has_data_ok_verdict("REFRESH: TABLES_OK 'a', 'shipment' + PERSISTED\n", "shipment")
    assert probe_live_source._has_data_ok_verdict("REFRESH: TABLE_OK 'shipment'\n", "shipment")
    # Negative control - a TABLES_OK naming a DIFFERENT table must NOT clear the gate for 'shipment'.
    assert not probe_live_source._has_data_ok_verdict("REFRESH: TABLES_OK 'some_other_table'\n", "shipment")
    # The anchoring lesson still holds: a stray verdict-looking substring in prose is not a verdict line.
    assert not probe_live_source._has_data_ok_verdict("note: TABLES_OK 'shipment' happened earlier\n", "shipment")


def test_nonzero_exit_is_never_read_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """#152: honour the child's exit code - a non-zero exit must not clear the gate even when the text
    carries a success-looking verdict line for the probed table."""
    probe_live_source = _import_probe_live_source()
    monkeypatch.setattr(
        probe_live_source.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            args=["refresh"], returncode=1, stdout="REFRESH: TABLES_OK 'shipment'\n", stderr=""
        ),
    )

    exit_code, verdict = probe_live_source._refresh_and_classify(123, "shipment", 1, network_fault_observed=False)
    assert exit_code == 1 and verdict != "DATA_OK"


def test_wrong_table_tables_ok_does_not_clear_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """#115 end-to-end: even at exit 0, a TABLES_OK for a DIFFERENT table must leave the gate closed."""
    probe_live_source = _import_probe_live_source()
    monkeypatch.setattr(
        probe_live_source.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            args=["refresh"], returncode=0, stdout="REFRESH: TABLES_OK 'some_other_table'\n", stderr=""
        ),
    )

    exit_code, verdict = probe_live_source._refresh_and_classify(123, "shipment", 1, network_fault_observed=False)
    assert exit_code == 1 and verdict != "DATA_OK"


@pytest.mark.parametrize(
    ("banner_func", "args", "sanity_substr"),
    [
        pytest.param("print_refresh_banner", (111, 180, 60), "No blocking dialog", id="healthy-no-dialog"),
        pytest.param(
            "print_refresh_unknown_banner",
            (111, 180, 60, "owner window is minimized"),
            "UNKNOWN",
            id="indeterminate-unknown",
        ),
    ],
)
def test_refresh_banners_do_not_classify_as_no_credential(banner_func: str, args: tuple, sanity_substr: str) -> None:
    """#153: NEITHER refresh banner may fabricate a credential stop from its own reassuring prose.

    Both banners are printed on non-failure paths and captured verbatim by the classifier.
    ``print_refresh_banner`` is the healthy no-dialog path; ``print_refresh_unknown_banner`` is the
    minimized-owner UNKNOWN path (issue #154) and was a SECOND instance of the same self-poisoning
    defect. Parametrized over BOTH on purpose rather than hard-coding one string: a third banner that
    reintroduces a sentinel token in prose is caught the moment it is added to this list.
    """
    probe_live_source = _import_probe_live_source()
    _, _, credential_modal = _import_skill_modules()

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        getattr(credential_modal, banner_func)(*args)
    banner = buffer.getvalue()

    assert sanity_substr in banner, f"sanity: {banner_func} should render {sanity_substr!r}; got {banner!r}"
    verdict, _ = probe_live_source._classify_failure(banner, network_fault_observed=False)
    assert verdict != "NO_CREDENTIAL", f"{banner_func} must not fabricate a credential stop; got {verdict}: {banner!r}"


def test_free_text_credential_marker_without_a_verdict_line_still_stops() -> None:
    """A revoked Databricks PAT returns a 403/socket-reset with NO modal and NO verdict line, so the
    deliberately-unanchored CREDENTIAL_MARKERS path is the only thing that catches it. Pin that #153's
    structural change did NOT remove that free-text path."""
    probe_live_source = _import_probe_live_source()
    verdict, _ = probe_live_source._classify_failure(
        "DataSource.Error: the connection was forcibly closed by the remote host (10054)",
        network_fault_observed=False,
    )
    assert verdict == "NO_CREDENTIAL"


def test_blocked_by_dialog_does_not_clear_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A generic Desktop dialog is a human-blocked source, not reachable data."""
    probe_live_source = _import_probe_live_source()
    monkeypatch.setattr(
        probe_live_source.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["refresh"],
            returncode=1,
            stdout="REFRESH: BLOCKED_BY_DIALOG pid=111; window title='(empty title)'\n",
            stderr="",
        ),
    )

    assert probe_live_source._refresh_and_classify(123, "Orders", 1, network_fault_observed=False) == (  # noqa: SLF001
        1,
        "NO_CREDENTIAL",
    )
