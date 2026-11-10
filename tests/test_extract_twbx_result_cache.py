"""Regression tests for the `.twbx` embedded result-cache decoder."""

from __future__ import annotations

import importlib.util
import struct
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("etrc", REPO / "scripts" / "extract_twbx_result_cache.py")
etrc = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(etrc)

TAG_INT, TAG_NUMBER, TAG_STRING = 0, 1, 2


def _rec(tag: int, payload: bytes) -> bytes:
    return struct.pack("<HHI", tag, 0xFFFF, len(payload)) + payload


def _meta(alias: str, local_type: str) -> bytes:
    xml = (
        f"<metadata-record class='column'><remote-alias>{alias}</remote-alias>"
        f"<local-type>{local_type}</local-type></metadata-record>"
    )
    return _rec(TAG_STRING, xml.encode("utf-16-le"))


def _blob(*records: bytes) -> bytes:
    return b"\x00" * 16 + b"".join(records)


def test_int64_cells_decode_as_integers_and_keep_rows_aligned() -> None:
    """The decode bug that made the oracle silently WRONG, pinned. Measured 2026-08-08 (`book_5-2-LOD`): the first
    decoder handled only tag 1 (double) and tag 2 (string)."""
    blob = _blob(
        _meta("Cohort Year", "integer"),
        _meta("Region", "string"),
        _meta("Sales", "real"),
        _rec(TAG_INT, struct.pack("<q", 2018)),
        _rec(TAG_STRING, "West".encode("utf-16-le")),
        _rec(TAG_NUMBER, struct.pack("<d", 725457.82)),
        _rec(TAG_INT, struct.pack("<q", 2019)),
        _rec(TAG_STRING, "East".encode("utf-16-le")),
        _rec(TAG_NUMBER, struct.pack("<d", 678781.24)),
    )

    entry = etrc.decode_bin(blob)

    assert [c["alias"] for c in entry["columns"]] == ["Cohort Year", "Region", "Sales"]
    assert entry["rows"] == [[2018, "West", 725457.82], [2019, "East", 678781.24]], (
        "int64 cells must occupy their own column slot - dropping them shifts every later row"
    )
    assert isinstance(entry["rows"][0][0], int) and entry["rows"][0][0] == 2018, (
        "a year must decode as 2018, never as the 0.0-ish denormal a double-read produces"
    )
    assert not entry["ragged_tail_dropped"]
    # Control: the type guard must not cry wolf on a clean decode, or it gets ignored when it matters.
    assert etrc._typecheck(entry) == []


def test_typecheck_catches_a_column_decoded_as_the_wrong_python_type() -> None:
    """The guard that turns 'the numbers look plausible' into a checkable claim. If the container format ever drifts,
    `_typecheck` is what makes the drift LOUD instead of letting a mistyped column through as ground truth."""
    blob = _blob(
        _meta("Cohort Year", "integer"),
        _rec(TAG_NUMBER, struct.pack("<d", 2018.0)),
    )

    problems = etrc._typecheck(etrc.decode_bin(blob))

    assert problems, "a declared-integer column decoded as float must be reported, not accepted"
    assert "Cohort Year" in problems[0] and "integer" in problems[0]


def test_a_truncated_final_tuple_is_dropped_not_emitted_as_a_short_row() -> None:
    """A partial tuple must never reach the oracle as if it were a complete observation. The cache is written by a
    live Desktop session, so the tail can legitimately be mid-write."""
    blob = _blob(
        _meta("Region", "string"),
        _meta("Sales", "real"),
        _rec(TAG_STRING, "West".encode("utf-16-le")),
        _rec(TAG_NUMBER, struct.pack("<d", 725457.82)),
        _rec(TAG_STRING, "East".encode("utf-16-le")),  # truncated: no Sales cell follows
    )

    entry = etrc.decode_bin(blob)

    assert entry["rows"] == [["West", 725457.82]]
    assert entry["ragged_tail_dropped"] is True, "the caller must be told a tuple was discarded"
