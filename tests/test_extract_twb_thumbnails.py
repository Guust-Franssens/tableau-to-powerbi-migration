"""Regression tests for scripts/extract_twb_thumbnails.py.

These build their own fixtures in ``tmp_path`` rather than editing the shared
``tests/fixtures/minimal.twb``, so they cannot collide with the parser tests.

The behaviour under test exists because a real migration signed off a wrong chart type after every
agent concluded "no reference images exist" for an offline ``.twbx``. Tableau embeds a PNG render per
worksheet; these tests pin that we find them, in both container formats, and that we refuse to write
anything that is not actually a PNG.
"""

from __future__ import annotations

import base64
import struct
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "extract_twb_thumbnails.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from extract_twb_thumbnails import extract, read_twb_bytes  # noqa: E402


def _png_bytes(width: int = 2, height: int = 2) -> bytes:
    """A minimal but genuinely valid 8-bit RGB PNG (no Pillow dependency)."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def _twb_xml(thumbnails: dict[str, str]) -> str:
    """A skeleton .twb carrying a <thumbnails> block, as Tableau writes it."""
    entries = "".join(
        f"<thumbnail height='192' name='{name}' width='192'>\n{payload}\n</thumbnail>"
        for name, payload in thumbnails.items()
    )
    return (
        "<?xml version='1.0' encoding='utf-8' ?>\n"
        "<workbook version='18.1'>"
        "<worksheets />"
        f"<thumbnails>{entries}</thumbnails>"
        "</workbook>"
    )


@pytest.fixture()
def png_b64() -> str:
    return base64.b64encode(_png_bytes()).decode("ascii")


def test_extracts_one_png_per_worksheet_from_twb(tmp_path: Path, png_b64: str) -> None:
    twb = tmp_path / "wb.twb"
    twb.write_text(_twb_xml({"Bump": png_b64, "Moving Average": png_b64}), encoding="utf-8")
    out = tmp_path / "reference"

    written = extract(twb, out)

    assert len(written) == 2
    assert {name for name, _, _ in written} == {"Bump", "Moving Average"}
    # Worksheet names are sanitised for the filesystem but stay readable.
    assert (out / "Bump.png").exists()
    assert (out / "Moving_Average.png").exists()
    # And what we wrote is a real PNG, not base64 text.
    assert (out / "Bump.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_reads_through_a_packaged_twbx(tmp_path: Path, png_b64: str) -> None:
    """The common engagement case: an offline .twbx, no Tableau Server, no credential."""
    twbx = tmp_path / "wb.twbx"
    with zipfile.ZipFile(twbx, "w") as zf:
        zf.writestr("Book1.twb", _twb_xml({"Running Total (2)": png_b64}))
        zf.writestr("Data/orders.xls", b"not really an xls")

    written = extract(twbx, tmp_path / "reference")

    assert [name for name, _, _ in written] == ["Running Total (2)"]
    # '(' ')' and spaces are not filesystem-hostile everywhere, but we normalise anyway.
    # `_safe` also strips leading/trailing separators, so the closing ')' leaves no trailing '_'.
    assert (tmp_path / "reference" / "Running_Total__2.png").exists()


def test_twbx_reader_finds_the_root_twb_not_a_nested_one(tmp_path: Path, png_b64: str) -> None:
    twbx = tmp_path / "wb.twbx"
    with zipfile.ZipFile(twbx, "w") as zf:
        zf.writestr("Data/backup/old.twb", _twb_xml({"Stale": png_b64}))
        zf.writestr("Book1.twb", _twb_xml({"Live": png_b64}))

    assert b"name='Live'" in read_twb_bytes(twbx)


def test_non_png_payload_is_skipped_not_written(tmp_path: Path) -> None:
    """A decodable but non-PNG payload must never reach disk as a .png."""
    twb = tmp_path / "wb.twb"
    twb.write_text(
        _twb_xml({"Bogus": base64.b64encode(b"GIF89a-not-a-png").decode("ascii")}),
        encoding="utf-8",
    )
    out = tmp_path / "reference"

    assert extract(twb, out) == []
    assert list(out.glob("*.png")) == []


def test_empty_thumbnail_element_is_skipped(tmp_path: Path) -> None:
    twb = tmp_path / "wb.twb"
    twb.write_text(_twb_xml({"NeverDisplayed": ""}), encoding="utf-8")

    assert extract(twb, tmp_path / "reference") == []


def test_duplicate_sanitised_names_do_not_overwrite_each_other(tmp_path: Path, png_b64: str) -> None:
    """'Sales/Region' and 'Sales:Region' both sanitise to 'Sales_Region'."""
    twb = tmp_path / "wb.twb"
    twb.write_text(_twb_xml({"Sales/Region": png_b64, "Sales:Region": png_b64}), encoding="utf-8")
    out = tmp_path / "reference"

    written = extract(twb, out)

    assert len(written) == 2
    assert len({path for _, path, _ in written}) == 2, "second thumbnail clobbered the first"


def test_workbook_without_thumbnails_returns_empty_and_strict_exits_1(tmp_path: Path) -> None:
    twb = tmp_path / "wb.twb"
    twb.write_text(
        "<?xml version='1.0' encoding='utf-8' ?><workbook version='18.1'><worksheets /></workbook>",
        encoding="utf-8",
    )
    out = tmp_path / "reference"

    assert extract(twb, out) == []

    lenient = subprocess.run(
        [sys.executable, str(SCRIPT), str(twb), "-o", str(out)],
        capture_output=True,
        text=True,
    )
    assert lenient.returncode == 0
    assert "no thumbnails embedded" in lenient.stderr

    strict = subprocess.run(
        [sys.executable, str(SCRIPT), str(twb), "-o", str(out), "--strict"],
        capture_output=True,
        text=True,
    )
    assert strict.returncode == 1


def test_cli_reports_count_and_the_resolution_caveat(tmp_path: Path, png_b64: str) -> None:
    """The caveat is load-bearing: over-claiming pixel fidelity from a 192px render is the failure mode."""
    twb = tmp_path / "wb.twb"
    twb.write_text(_twb_xml({"Bump": png_b64}), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(twb), "-o", str(tmp_path / "reference")],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "[OK] 1 worksheet thumbnail(s)" in result.stdout
    assert "NOT for fonts, exact colours or pixel parity" in result.stdout


def test_missing_workbook_exits_2(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "nope.twbx"), "-o", str(tmp_path / "r")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
