"""Regression tests for probe_live_source's machine-readable verdict parsing."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# pylint: disable=import-outside-toplevel,protected-access,no-member

REPO = Path(__file__).resolve().parent.parent


def _import_probe_live_source():
    """Import the script module from the repo-local scripts folder."""
    scripts = str(REPO / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import probe_live_source  # noqa: PLC0415

    return probe_live_source


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


def test_accepts_genuine_data_ok_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the success path: an anchored DATA_OK verdict still marks the source reachable."""
    probe_live_source = _import_probe_live_source()
    monkeypatch.setattr(
        probe_live_source.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["refresh"],
            returncode=0,
            stdout="  data   : 1 row(s) in 'Orders'\nREFRESH: DATA_OK\n",
            stderr="",
        ),
    )

    assert probe_live_source._refresh_and_classify(123, "Orders", 1, network_fault_observed=False) == (  # noqa: SLF001
        0,
        "DATA_OK",
    )


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


def _import_child_refresh():
    """Import the child refresh module from the skill's own scripts folder."""
    scripts = str(REPO / ".github" / "skills" / "pbip-model-refresh" / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import refresh_pbip_model  # noqa: PLC0415

    return refresh_pbip_model


def test_probe_timeout_strictly_outlasts_child_ceiling() -> None:
    """#156: the parent's refresh bound MUST strictly outlast the child's own deadline.

    The inversion this guards against: with the old default of 180s the parent SIGKILLed the child
    150s BEFORE the child's own ceiling (300 + 30 = 330s) could fire, so the child's far better
    verdict (a credential re-check, then a TimeoutError naming the mashup-modal signature) was dead
    code from the probe path. Computed by importing BOTH sides so any future change to either
    constant re-checks the ordering - hard-coding 390/330 here would not catch a drift in the child.
    """
    probe_live_source = _import_probe_live_source()
    child = _import_child_refresh()
    child_ceiling = child.REFRESH_TIMEOUT_SECONDS + child.REFRESH_WALL_CLOCK_GRACE_SECONDS
    assert probe_live_source.PROBE_TIMEOUT_SECONDS > child_ceiling, (
        f"parent PROBE_TIMEOUT_SECONDS={probe_live_source.PROBE_TIMEOUT_SECONDS} must strictly exceed the "
        f"child's own deadline {child_ceiling}s (REFRESH_TIMEOUT_SECONDS + REFRESH_WALL_CLOCK_GRACE_SECONDS), "
        "or the child's deadline classification never fires before the parent kills it."
    )


def test_probe_timeout_is_derived_from_child_not_retyped() -> None:
    """#156: the parent's bound must equal the child's constants plus the documented margin.

    Enforcing derivation-by-construction is the point: a bound picked as an independent literal can
    silently drift back into the inversion the moment someone bumps the child's ceiling.
    """
    probe_live_source = _import_probe_live_source()
    child = _import_child_refresh()
    expected = (
        child.REFRESH_TIMEOUT_SECONDS
        + child.REFRESH_WALL_CLOCK_GRACE_SECONDS
        + probe_live_source.PROBE_KILL_MARGIN_SECONDS
    )
    assert probe_live_source.PROBE_TIMEOUT_SECONDS == expected
    # The parent must reuse the child's own objects, not a private copy that could diverge.
    assert probe_live_source.REFRESH_TIMEOUT_SECONDS == child.REFRESH_TIMEOUT_SECONDS
    assert probe_live_source.REFRESH_WALL_CLOCK_GRACE_SECONDS == child.REFRESH_WALL_CLOCK_GRACE_SECONDS


def test_probe_timeout_default_threads_all_the_way_to_run_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#156 (the WIRING, not just the constant): the derived default must be the value that actually
    reaches the refresh subprocess.

    The two tests above pin the constant and its derivation, but neither observes what ``main()`` hands
    to ``run_probe`` -> ``subprocess.run(..., timeout=timeout_sec)``. Reverting only the argparse default
    to 180 (leaving PROBE_TIMEOUT_SECONDS=390) restores the exact inversion #156 exists to prevent while
    both constant tests stay green. This drives ``main()`` with ``run_probe`` captured, so it fails the
    moment the default drifts off the constant. No live Desktop or real bundle: ``run_probe`` is replaced
    and an existing empty directory satisfies the path-exists guard.
    """
    probe_live_source = _import_probe_live_source()
    captured: dict[str, int] = {}

    def _capture(_bundle: Path, _source_index: int | None, timeout_sec: int, _keep: bool) -> int:
        captured["timeout_sec"] = timeout_sec
        return 0

    monkeypatch.setattr(probe_live_source, "run_probe", _capture)
    rc = probe_live_source.main(["--bundle", str(tmp_path)])

    assert rc == 0
    assert captured["timeout_sec"] == probe_live_source.PROBE_TIMEOUT_SECONDS, (
        f"main() handed run_probe timeout_sec={captured.get('timeout_sec')}, but the derived default is "
        f"{probe_live_source.PROBE_TIMEOUT_SECONDS}; the argparse default has drifted off the constant, so the "
        "child would be SIGKILLed before its own deadline can fire (issue #156 inversion)."
    )
