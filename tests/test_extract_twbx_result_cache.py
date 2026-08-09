"""Regression tests for the `.twbx` embedded result-cache decoder.

`scripts/extract_twbx_result_cache.py` recovers **Tableau Desktop's own computed values** from the
`TwbxExternalCache/TwbxResultsCacheV3/` entries inside a packaged workbook. That makes offline
numeric ground-truthing possible when `capture_tableau_oracle.py` cannot help (no live Tableau
Cloud/Server site). Because the container format is *reverse-engineered*, a silent format drift is
the one failure that would quietly turn the oracle into a fabricator - so these tests build byte
streams by hand rather than depending on any customer `.twbx`.

Every record is `<uint16 tag><uint16 0xFFFF><uint32 len><payload>`, after a 16-byte header.
"""

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
    """The decode bug that made the oracle silently WRONG, pinned.

    Measured 2026-08-08 (`book_5-2-LOD`): the first decoder handled only tag 1 (double) and tag 2
    (string). Integer measures and date-part derivations arrive as **tag 0 / int64**, so every such
    cell was dropped - and dropping a cell does not raise, it *shifts every subsequent row one
    column left*. The cohort and row-count queries came back plausibly shaped and completely wrong.

    Worse, the obvious "just read it as a double" repair fails silently too: an int64 like 2018
    reinterpreted as an IEEE double is a denormal that prints as `0.000000` rather than raising. A
    wrong grain that returns a number is exactly the failure this whole oracle exists to catch, so
    the decoder must be pinned on a stream that mixes all three tags.
    """
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


def test_typecheck_catches_a_column_decoded_as_the_wrong_python_type() -> None:
    """The guard that turns 'the numbers look plausible' into a checkable claim.

    If the container format ever drifts, `_typecheck` is what makes the drift LOUD instead of
    letting a mistyped column through as ground truth. Here an `integer` column is fed a double
    payload, which is precisely what the original bug's naive repair would have produced.
    """
    blob = _blob(
        _meta("Cohort Year", "integer"),
        _rec(TAG_NUMBER, struct.pack("<d", 2018.0)),
    )

    problems = etrc._typecheck(etrc.decode_bin(blob))

    assert problems, "a declared-integer column decoded as float must be reported, not accepted"
    assert "Cohort Year" in problems[0] and "integer" in problems[0]


def test_a_clean_decode_reports_no_typecheck_problems() -> None:
    """Control: the guard must not cry wolf, or it gets ignored on the run that matters."""
    blob = _blob(
        _meta("Cohort Year", "integer"),
        _meta("Sales", "real"),
        _rec(TAG_INT, struct.pack("<q", 2018)),
        _rec(TAG_NUMBER, struct.pack("<d", 725457.82)),
    )

    assert etrc._typecheck(etrc.decode_bin(blob)) == []


def test_a_truncated_final_tuple_is_dropped_not_emitted_as_a_short_row() -> None:
    """A partial tuple must never reach the oracle as if it were a complete observation.

    The cache is written by a live Desktop session, so the tail can legitimately be mid-write. A
    short row silently read as a full one would put a real number under the wrong column heading -
    the same class of error as the int64 shift, arriving from the other end of the stream.
    """
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
