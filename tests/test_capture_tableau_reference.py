"""Regression tests for explicit Server intent in ``capture_tableau_reference.py``."""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import capture_tableau_reference as capture  # noqa: E402  # pylint: disable=wrong-import-position


def _args(slug_dir: Path, *, server_rest: bool = False, structural_only: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        slug_dir=str(slug_dir),
        public_url=None,
        view=None,
        env=slug_dir / "unrelated.env",
        server_rest=server_rest,
        structural_only=structural_only,
        force=False,
    )


def test_stray_server_env_does_not_preempt_offline_capture_but_explicit_request_halts(
    tmp_path: Path, monkeypatch
) -> None:
    """An inherited URL is inert, while an explicit Server request retains exit 3."""
    slug_dir = tmp_path / "workbook"
    source_dir = slug_dir / "source"
    source_dir.mkdir(parents=True)
    png = base64.b64encode(b"\x89PNG\r\n\x1a\ncached-render").decode("ascii")
    (source_dir / "book.twb").write_text(
        f"<workbook><thumbnails><thumbnail name='Sheet 1'>{png}</thumbnail></thumbnails></workbook>",
        encoding="utf-8",
    )
    (slug_dir / "unrelated.env").write_text("TABLEAU_SERVER_URL=https://unrelated.invalid\n", encoding="utf-8")

    assert capture.resolve_and_capture(_args(slug_dir)) == 0
    manifest = json.loads((slug_dir / "reference" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["dashboards"][0]["states"][0]["provider"] == "embedded_thumbnail"

    monkeypatch.setattr(capture, "_run_providers", lambda *_args: [])
    assert capture.resolve_and_capture(_args(slug_dir, server_rest=True)) == 3


def test_structural_only_is_an_explicit_escape_from_requested_server_capture(tmp_path: Path, monkeypatch) -> None:
    """Structural-only must bypass even an explicit but unavailable Server provider."""
    slug_dir = tmp_path / "workbook"
    slug_dir.mkdir()

    def unexpected_server_call(_slug_dir: Path) -> None:
        raise AssertionError("structural-only must bypass an unavailable Server provider")

    monkeypatch.setattr(capture, "capture_server_rest", unexpected_server_call)

    assert capture.resolve_and_capture(_args(slug_dir, server_rest=True, structural_only=True)) == 0
    manifest = json.loads((slug_dir / "reference" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["dashboards"] == []
