"""Regression tests for explicit Server intent in ``capture_tableau_reference.py``.

Also covers the ``manual`` provider — the whole of the bring-your-own-screenshots route (issue #519).
Everything a user drops into ``reference/`` must be ACCOUNTED FOR: adopted, or named in the output
with the reason it was not. A rejected file that produces no artifact, no log line and no exit-code
difference is this repository's dominant defect class, and it fires on the first thing a first-time
user tries.
"""

from __future__ import annotations

import base64
import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import capture_tableau_reference as capture  # noqa: E402  # pylint: disable=wrong-import-position


def _png(path: Path, width: int = 1440, height: int = 900, payload: int = 900) -> Path:
    """A structurally valid PNG of a chosen pixel size and a chosen (small) byte size.

    ``payload`` defaults FAR below the 20,000-byte floor `collect_manual` used to apply, because that
    is the case under test: PNG is lossless, so a legible flat-fill dashboard really is this small
    (measured 7,154 bytes for a plain 1440x900 single-page mock).
    """
    header = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + b"\x00" * payload)
    return path


def _spec(slug_dir: Path, dashboards: tuple[str, ...] = ("Detail",), worksheets: tuple[str, ...] = ()) -> None:
    slug_dir.mkdir(parents=True, exist_ok=True)
    (slug_dir / "migration-spec.json").write_text(
        json.dumps(
            {
                "dashboards": [{"name": name} for name in dashboards],
                "worksheets": [{"name": name} for name in worksheets],
            }
        ),
        encoding="utf-8",
    )


def _write_embedded_thumbnail(slug_dir: Path) -> None:
    """Create a minimal workbook carrying an embedded thumbnail."""
    source_dir = slug_dir / "source"
    source_dir.mkdir(parents=True)
    png = base64.b64encode(b"\x89PNG\r\n\x1a\ncached-render").decode("ascii")
    (source_dir / "book.twb").write_text(
        f"<workbook><thumbnails><thumbnail name='Sheet 1'>{png}</thumbnail></thumbnails></workbook>",
        encoding="utf-8",
    )


def test_process_server_env_does_not_preempt_offline_cli_capture(tmp_path: Path, monkeypatch) -> None:
    """Inherited process credentials are inert without the CLI intent flag."""
    slug_dir = tmp_path / "workbook"
    _write_embedded_thumbnail(slug_dir)
    monkeypatch.setenv("TABLEAU_SERVER_URL", "https://unrelated.invalid")
    monkeypatch.delenv("TABLEAU_PAT_SECRET", raising=False)

    assert capture.main([str(slug_dir)]) == 0
    manifest = json.loads((slug_dir / "reference" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["dashboards"][0]["states"][0]["provider"] == "embedded_thumbnail"


def test_explicit_server_cli_request_does_not_reuse_existing_manifest(tmp_path: Path, monkeypatch) -> None:
    """An existing offline manifest cannot bypass an explicit Server request."""
    slug_dir = tmp_path / "workbook"
    reference_dir = slug_dir / "reference"
    reference_dir.mkdir(parents=True)
    (reference_dir / "manifest.json").write_text('{"dashboards": [{"name": "stale"}]}\n', encoding="utf-8")

    def unavailable_server(_slug_dir: Path) -> list[dict] | None:
        raise NotImplementedError("not wired")

    monkeypatch.setattr(capture, "capture_server_rest", unavailable_server)

    assert capture.main([str(slug_dir), "--server-rest"]) == 3


def test_explicit_server_capture_fails_closed_on_empty_result(tmp_path: Path, monkeypatch) -> None:
    """A provider that returns no records must not fall through to offline providers."""
    slug_dir = tmp_path / "workbook"
    _write_embedded_thumbnail(slug_dir)
    monkeypatch.setattr(capture, "capture_server_rest", lambda _slug_dir: None)

    assert capture.main([str(slug_dir), "--server-rest"]) == 3
    assert not (slug_dir / "reference" / "manifest.json").exists()


def test_explicit_server_capture_fails_closed_on_provider_error(tmp_path: Path, monkeypatch) -> None:
    """Provider failures become the documented terminal exit instead of escaping."""
    slug_dir = tmp_path / "workbook"
    slug_dir.mkdir()

    def failed_server(_slug_dir: Path) -> list[dict] | None:
        raise ConnectionError("unreachable")

    monkeypatch.setattr(capture, "capture_server_rest", failed_server)

    assert capture.main([str(slug_dir), "--server-rest"]) == 3


def test_explicit_server_capture_writes_returned_records(tmp_path: Path, monkeypatch) -> None:
    """Successful Server records are written without invoking lower-fidelity providers."""
    slug_dir = tmp_path / "workbook"
    slug_dir.mkdir()
    server_records = [
        {
            "name": "Server Dashboard",
            "states": [
                {
                    "state_slug": "default",
                    "state": {},
                    "image": "server/dashboard.png",
                    "provider": "server_rest",
                    "capabilities": [
                        capture.CAP_LAYOUT,
                        capture.CAP_TEXT,
                        capture.CAP_STATE,
                        capture.CAP_REVISION,
                        capture.CAP_VALIDATION,
                    ],
                    "dimensions": {"w": 1600, "h": 1100, "dpr": 2},
                    "sha256": "server-render-sha",
                    "numeric_oracle": None,
                }
            ],
        }
    ]
    monkeypatch.setattr(capture, "capture_server_rest", lambda _slug_dir: server_records)

    def unexpected_offline_call(*_args) -> list[dict]:
        raise AssertionError("offline providers must not run after Server success")

    monkeypatch.setattr(capture, "_run_providers", unexpected_offline_call)

    assert capture.main([str(slug_dir), "--server-rest"]) == 0
    manifest = json.loads((slug_dir / "reference" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["dashboards"] == server_records


def test_structural_only_is_an_explicit_escape_from_requested_server_capture(tmp_path: Path, monkeypatch) -> None:
    """Structural-only must bypass even an explicit but unavailable Server provider."""
    slug_dir = tmp_path / "workbook"
    slug_dir.mkdir()

    def unexpected_server_call(_slug_dir: Path) -> None:
        raise AssertionError("structural-only must bypass an unavailable Server provider")

    monkeypatch.setattr(capture, "capture_server_rest", unexpected_server_call)

    assert capture.main([str(slug_dir), "--server-rest", "--structural-only"]) == 0
    manifest = json.loads((slug_dir / "reference" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["dashboards"] == []


# ---------------------------------------------------------------------------------------------
# The manual provider: bring-your-own-screenshots (issue #519)
# ---------------------------------------------------------------------------------------------


def test_a_small_but_valid_screenshot_is_adopted_and_never_silently_dropped(tmp_path: Path) -> None:
    """The headline regression: a legible dashboard PNG under the old 20 KB byte floor.

    Before the fix this produced NOTHING - no manifest, no log line - and the tool then told the
    operator to drop `tableau-<name>.png` into the very directory holding the file it had discarded.
    """
    slug_dir = tmp_path / "workbook"
    _spec(slug_dir)
    dropped = _png(slug_dir / "reference" / "tableau-Detail.png")
    assert dropped.stat().st_size < 20000, "fixture must sit under the OLD byte floor or it proves nothing"

    assert capture.main([str(slug_dir)]) == 0
    manifest = json.loads((slug_dir / "reference" / "manifest.json").read_text(encoding="utf-8"))
    state = manifest["dashboards"][0]["states"][0]
    assert state["provider"] == "manual"
    assert state["image"] == "tableau-Detail.png"


def test_every_rejected_candidate_is_named_with_a_reason(tmp_path: Path) -> None:
    """Adopted or reported - there is no third outcome for a file a human put in `reference/`."""
    reference = tmp_path / "reference"
    _png(reference / "tableau-too-small.png", width=40, height=40)
    (reference / "tableau-broken.png").write_bytes(b"not a png at all")
    (reference / "tableau-empty.png").write_bytes(b"")
    _png(reference / "tableau-page.jpg")
    _png(reference / "dashboard-overview.png")

    scan = capture.collect_manual(reference)

    assert not scan.adopted
    reasons = {path.name: reason for path, reason in scan.rejected}
    assert set(reasons) == {
        "tableau-too-small.png",
        "tableau-broken.png",
        "tableau-empty.png",
        "tableau-page.jpg",
        "dashboard-overview.png",
    }
    assert "40x40" in reasons["tableau-too-small.png"]
    assert "PNG" in reasons["tableau-broken.png"]
    assert "0 bytes" in reasons["tableau-empty.png"]
    assert ".jpg" in reasons["tableau-page.jpg"]
    assert capture.MANUAL_PREFIX in reasons["dashboard-overview.png"]


def test_our_own_outputs_are_not_reported_as_failed_candidates(tmp_path: Path) -> None:
    """`powerbi-*.png` and `manifest.json` are this toolkit's own files, not a user's rejected input."""
    reference = tmp_path / "reference"
    _png(reference / "powerbi-page1.png")
    (reference / "manifest.json").write_text("{}", encoding="utf-8")
    (reference / "notes.txt").write_text("not an image", encoding="utf-8")

    scan = capture.collect_manual(reference)

    assert not scan.adopted
    assert not scan.rejected


def test_the_fail_closed_error_names_the_files_it_rejected(tmp_path: Path, caplog) -> None:
    """ "Drop a screenshot in" must never be the whole answer when a screenshot is already in."""
    slug_dir = tmp_path / "workbook"
    slug_dir.mkdir()
    _png(slug_dir / "reference" / "tableau-tiny.png", width=32, height=32)

    with caplog.at_level("ERROR"):
        assert capture.main([str(slug_dir)]) == 1

    assert "tableau-tiny.png" in caplog.text
    assert "REJECTED, not missing" in caplog.text


def test_a_manual_screenshot_is_not_preempted_by_an_embedded_thumbnail(tmp_path: Path) -> None:
    """An automatic fallback may not silently outrank an explicit operator act.

    Embedded thumbnails were found in 17/17 workbooks of one estate, so this was the normal case for
    anything saved out of Tableau Desktop: the dropped dashboard PNG was never even looked at.
    """
    slug_dir = tmp_path / "workbook"
    _write_embedded_thumbnail(slug_dir)
    _png(slug_dir / "reference" / "tableau-Detail.png")

    assert capture.main([str(slug_dir)]) == 0
    manifest = json.loads((slug_dir / "reference" / "manifest.json").read_text(encoding="utf-8"))
    providers = [dashboard["states"][0]["provider"] for dashboard in manifest["dashboards"]]
    assert "manual" in providers, "the operator's own screenshot must be recorded"
    assert "embedded_thumbnail" in providers, "thumbnails are per-worksheet evidence, still worth keeping"


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ("tableau-Detail", "dashboard"),
        ("tableau-detail", "dashboard"),  # case-insensitive, like the gate's own name join
        ("tableau-Trend", "worksheet"),
        ("tableau-not-in-the-workbook", None),
    ],
)
def test_the_object_type_is_derived_from_the_parsed_workbook(tmp_path: Path, stem: str, expected: str | None) -> None:
    """Without a `view_type` the ENTRY gate reports `scope unknown cannot satisfy a dashboard page`."""
    slug_dir = tmp_path / "workbook"
    _spec(slug_dir, dashboards=("Detail",), worksheets=("Trend",))
    _png(slug_dir / "reference" / f"{stem}.png")

    assert capture.main([str(slug_dir)]) == 0
    manifest = json.loads((slug_dir / "reference" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["dashboards"][0]["states"][0].get("view_type") == expected


def test_an_object_type_can_be_declared_when_the_name_does_not_match(tmp_path: Path) -> None:
    """The escape hatch for a screenshot whose filename cannot carry the exact source object name."""
    slug_dir = tmp_path / "workbook"
    _spec(slug_dir, dashboards=("Detail",))
    _png(slug_dir / "reference" / "tableau-whatever.png")

    assert capture.main([str(slug_dir), "--manual-object-type", "dashboard"]) == 0
    manifest = json.loads((slug_dir / "reference" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["dashboards"][0]["states"][0]["view_type"] == "dashboard"


def test_an_ambiguous_name_is_refused_rather_than_guessed(tmp_path: Path) -> None:
    """A name claimed by two kinds must not silently pick a winner."""
    slug_dir = tmp_path / "workbook"
    _spec(slug_dir, dashboards=("Ops",), worksheets=("Ops",))

    assert capture.object_kinds(slug_dir) == {}
