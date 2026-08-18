"""Regression tests for explicit Server intent in ``capture_tableau_reference.py``."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import capture_tableau_reference as capture  # noqa: E402  # pylint: disable=wrong-import-position


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
    server_records = [{"name": "Server dashboard", "states": []}]
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
