"""Regression tests for the earned-clear path out of the credential gate (#346).

A successful probe is the ONLY way to earn a clear. Until 2026-08-27 it could not actually do so on
any real estate: `_lift_gate` passed a human-readable count ("2 live source(s)") as `--sources`,
`clear_block` diffed that against the marker's real source names, nothing matched, and the
partial-clear branch left the gate **armed** -- while still writing a `probe-cleared` audit entry
and exiting 0.

The failure was invisible from the probe's own output (it printed `PROBE: DATA_OK`) and fail-safe
for artifacts (nothing unvalidated could be written), which is exactly why it survived: the only
symptom was that humans on a real 44-unit estate found `authorize` was the only thing that worked,
and so permanently marked every build UNVALIDATED.

Two properties are pinned here, and the second matters more than the first:

1. proving EVERY live source earns a full clear;
2. proving a SUBSET must NOT clear -- a full clear on partial proof would lift a gate covering
   sources nobody ever contacted, which is the precise hole `run_probe`'s plural guarantee exists
   to close. Fixing (1) by blanket-omitting `--sources` would have opened (2).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import credential_gate as cg  # noqa: E402
import probe_live_source as pls  # noqa: E402

NAMED = ["snowflake:acme/PROD", "databricks:acme/wh"]


def _armed(tmp_path: Path) -> Path:
    """A migration whose gate is armed with NAMED sources -- what the real classifier produces."""
    d = tmp_path / "unit"
    (d / "fabric").mkdir(parents=True)
    cg.apply_block(d, list(NAMED), force_scope=True)
    assert (d / cg.MARKER).exists(), "fixture must start armed, or it proves nothing"
    return d


def _audit_actions(d: Path) -> list[str]:
    return [json.loads(line)["action"] for line in (d / cg.AUDIT).read_text(encoding="utf-8").splitlines()]


def test_proving_every_live_source_fully_clears_a_named_source_gate(tmp_path: Path) -> None:
    """The #346 regression. Before the fix this left the marker in place and status at 1."""
    d = _armed(tmp_path)
    pls._lift_gate(d, "2 live source(s)", proved_all=True)

    assert not (d / cg.MARKER).exists(), "gate must be fully lifted when every live source was proved"
    assert cg.status(d) == 0
    assert "probe-cleared" in _audit_actions(d)


def test_the_count_string_is_never_sent_as_a_source_name(tmp_path: Path) -> None:
    """Root cause, pinned directly.

    `what` is prose for humans and is already carried in `--reason`. If it ever returns to
    `--sources`, it matches no marker entry and the partial-clear branch silently re-arms the bug.
    """
    d = _armed(tmp_path)
    pls._lift_gate(d, "2 live source(s)", proved_all=True)

    detail = " ".join(
        json.loads(line).get("detail", "") for line in (d / cg.AUDIT).read_text(encoding="utf-8").splitlines()
    )
    assert "sources=['2 live source(s)']" not in detail
    assert "live source(s)" in detail, "the count should still appear, as the human-readable reason"


def test_proving_only_a_SUBSET_must_not_clear_the_gate(tmp_path: Path) -> None:
    """The safety property that makes the naive one-line fix wrong.

    `--source-index` narrows the probe to one source. Clearing in full there would lift a gate
    covering sources never contacted -- a worse defect than the one being fixed.
    """
    d = _armed(tmp_path)
    pls._lift_gate(d, "1 live source(s)", proved_all=False)

    assert (d / cg.MARKER).exists(), "a partial proof must leave the gate armed"
    assert cg.status(d) == 1
    assert "probe-cleared" not in _audit_actions(d), "a partial proof must not record earned evidence"


def test_run_probe_computes_proved_all_before_source_index_narrows_it(tmp_path: Path, monkeypatch) -> None:
    """`proved_all` is derived from the FULL live set, not from the narrowed one.

    Computing it after the `--source-index` assignment would make `set(live) >= set(all_live)`
    trivially true and silently restore the over-clear.
    """
    calls: list[bool] = []
    monkeypatch.setattr(pls, "_lift_gate", lambda _m, _w, proved_all: calls.append(proved_all))
    monkeypatch.setattr(pls, "_probe_one", lambda *_a, **_k: (0, "DATA_OK"))

    sources = [
        {"connection": {"powerbi_target": "live_source"}},
        {"connection": {"powerbi_target": "live_source"}},
    ]
    bundle = type("B", (), {"data_sources": sources, "kind": "spec", "label": "x", "migration_dir": tmp_path})()
    monkeypatch.setattr(pls, "load_bundle", lambda _p: bundle)

    assert pls.run_probe(tmp_path, None, 60, False) == 0
    assert calls == [True], "probing both live sources must report proved_all=True"

    calls.clear()
    assert pls.run_probe(tmp_path, 0, 60, False) == 0
    assert calls == [False], "probing only index 0 of two live sources must report proved_all=False"
