"""Focused controls for grouped-manifest Tableau oracle recovery (#652)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import capture_tableau_oracle as oracle  # noqa: E402  # pylint: disable=wrong-import-position
import group_oracle_by_workbook as grp  # noqa: E402  # pylint: disable=wrong-import-position

LUID_1 = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
LUID_2 = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
WB_1 = "11111111-1111-4111-8111-111111111111"
UPDATED = "2026-09-01T00:00:00Z"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
CSV = b"region,sales\nEast,12\n"


class _Session:
    version = "3.29"
    site_id = "site-id"
    reauth_count = 0
    retry_count = 0

    def __init__(self) -> None:
        self.signins = 0
        self.signouts = 0

    def sign_in(self) -> None:
        self.signins += 1

    def sign_out(self) -> None:
        self.signouts += 1

    @staticmethod
    def reflected_credential(_payload: bytes) -> None:
        return None

    @staticmethod
    def redact_text(text: str) -> str:
        return text


def _run_dir(tmp_path: Path) -> Path:
    run = tmp_path / "_runs" / "001-recovery"
    run.mkdir(parents=True)
    (run / "run.json").write_text(
        json.dumps(
            {
                "run": 1,
                "unit_key": "recovery",
                "allocated_dir_name": run.name,
                "allocated_abs_path": str(run),
            }
        ),
        encoding="utf-8",
    )
    return run


def _view(luid: str, *, data: dict | None = None, image: dict | None = None) -> dict:
    view = {
        "view_luid": luid,
        "view_name": f"View {luid[:1]}",
        "workbook_luid": WB_1,
        "workbook_name": "Workbook",
        "updated_at": UPDATED,
        "max_age_minutes": 17,
    }
    if data is not None:
        view["data"] = data
    if image is not None:
        view["image"] = image
    return view


def _ok_data(luid: str) -> dict:
    return {
        "status": "ok",
        "certification": "certified",
        "path": f"data/{luid}.csv",
        "sha256": __import__("hashlib").sha256(CSV).hexdigest(),
        "row_count": 1,
    }


def _ok_image(luid: str) -> dict:
    return {
        "status": "ok",
        "format": "png",
        "path": f"images/{luid}.png",
        "sha256": __import__("hashlib").sha256(PNG).hexdigest(),
    }


def _failed(status: str = "transient") -> dict:
    return {"status": status, "detail": "HTTP 0: read operation timed out", "retry_reasons": ["old failure"]}


def _grouped_reference(root: Path, views: list[dict], *, server: str = "https://example.test") -> Path:
    ref = root / "reference"
    (ref / "data").mkdir(parents=True)
    (ref / "images").mkdir()
    for view in views:
        if (view.get("data") or {}).get("status") == "ok":
            (ref / "data" / f"{view['view_luid']}.csv").write_bytes(CSV)
        if (view.get("image") or {}).get("status") == "ok":
            (ref / "images" / f"{view['view_luid']}.png").write_bytes(PNG)
    manifest = {
        "schema": "tableau-oracle-workbook/1",
        "server": server,
        "site": "site",
        "rest_api_version": "3.29",
        "max_age_minutes": 17,
        "workbook_name": "Workbook",
        "workbook_luid": WB_1,
        "view_count": len(views),
        "requested_renders": ["png"],
        "render_capability": {"selected_tier": "png_high", "selected_api_version": None},
        "views": views,
    }
    (ref / grp.MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return ref


def _current_view(luid: str) -> dict:
    return {
        "id": luid,
        "name": f"View {luid[:1]}",
        "updatedAt": UPDATED,
        "workbook": {"id": WB_1, "name": "Workbook"},
        "project": {"name": "Project"},
    }


def _configure(monkeypatch, tmp_path: Path, session: _Session, grouped: Path, out: Path, run: Path) -> None:
    monkeypatch.setattr(
        oracle,
        "resolve_env",
        lambda _path: {
            "TABLEAU_SERVER_URL": "https://example.test",
            "TABLEAU_SITE": "site",
            "TABLEAU_PAT_NAME": "pat",
            "TABLEAU_PAT_SECRET": "secret",
            "TABLEAU_REST_API_VERSION": "3.29",
        },
    )
    monkeypatch.setattr(oracle, "TableauSession", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(oracle, "select_views", lambda *_args, **_kwargs: ([_current_view(LUID_1), _current_view(LUID_2)], {}))
    monkeypatch.setattr(oracle.tableau_view_types, "resolve_and_stamp", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "capture_tableau_oracle.py",
            "--run",
            str(run),
            "--retry-failed-from",
            str(grouped),
            "--out",
            str(out),
            "--workers",
            "2",
        ],
    )
    assert tmp_path


def test_recovery_exports_only_eligible_failed_legs(monkeypatch, tmp_path):
    run = _run_dir(tmp_path)
    grouped = _grouped_reference(
        tmp_path / "migrations" / "workbooks" / "workbook",
        [
            _view(LUID_1, data=_failed(), image=_ok_image(LUID_1)),
            _view(LUID_2, data=_ok_data(LUID_2), image=_failed("truncated")),
        ],
    )
    out = run / "oracle-retry"
    session = _Session()
    data_exports = []
    render_exports = []

    def data(_session, view_luid, out_dir, stem, *, max_age):
        data_exports.append((view_luid, max_age))
        path = out_dir / "data" / f"{stem}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(CSV)
        return {"status": "ok", "certification": "certified", "path": f"data/{stem}.csv", "sha256": __import__("hashlib").sha256(CSV).hexdigest(), "row_count": 1, "elapsed_sec": 0.0, "max_age_minutes": max_age}

    def render(_session, view_luid, path, kind, options):
        render_exports.append((view_luid, kind, options.max_age))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PNG)
        return {"status": "ok", "format": kind, "path": f"images/{path.name}", "sha256": __import__("hashlib").sha256(PNG).hexdigest(), "elapsed_sec": 0.0, "max_age_minutes": options.max_age}

    _configure(monkeypatch, tmp_path, session, grouped, out, run)
    monkeypatch.setattr(oracle, "_capture_data", data)
    monkeypatch.setattr(oracle, "_capture_render", render)

    assert oracle.main() == 3

    manifest = json.loads((out / "oracle-manifest.json").read_text(encoding="utf-8"))
    assert data_exports == [(LUID_1, 17)]
    assert render_exports == [(LUID_2, "png", 17)]
    assert [view["view_luid"] for view in manifest["views"]] == [LUID_1, LUID_2]
    assert manifest["views"][0]["data"]["status"] == "ok"
    assert "image" not in manifest["views"][0], "successful prior PNG must not be exported again"
    assert "data" not in manifest["views"][1], "successful prior data must not be exported again"
    assert manifest["views"][1]["image"]["status"] == "ok"
    assert manifest["recovery"]["eligible_legs"] == 2
    assert session.signins == 1 and session.signouts == 1


def test_zero_eligible_recovery_does_not_sign_in_or_create_batch(monkeypatch, tmp_path):
    run = _run_dir(tmp_path)
    grouped = _grouped_reference(
        tmp_path / "migrations" / "workbooks" / "workbook",
        [_view(LUID_1, data=_ok_data(LUID_1), image=_ok_image(LUID_1))],
    )
    out = run / "oracle-retry"
    session = _Session()
    _configure(monkeypatch, tmp_path, session, grouped, out, run)

    assert oracle.main() == 0

    assert session.signins == 0
    assert not out.exists()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda manifest: manifest.update({"schema": "tableau-oracle/1"}), "grouped schema"),
        (lambda manifest: manifest.update({"view_count": 99}), "view_count"),
        (lambda manifest: manifest["views"][0]["image"].update({"sha256": "0" * 64}), "digest changed"),
        (lambda manifest: manifest.update({"server": "https://evil.example"}), "configured Tableau server/site"),
    ],
)
def test_recovery_refuses_malformed_or_transplanted_grouped_evidence(monkeypatch, tmp_path, mutate, message):
    run = _run_dir(tmp_path)
    grouped = _grouped_reference(
        tmp_path / "migrations" / "workbooks" / "workbook",
        [_view(LUID_1, data=_failed(), image=_ok_image(LUID_1))],
    )
    manifest_path = grouped / grp.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutate(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _configure(monkeypatch, tmp_path, _Session(), grouped, run / "oracle-retry", run)

    with pytest.raises(oracle.OracleRecoveryRefusal, match=message):
        oracle.main()


def test_recovery_requires_absolute_run_and_in_run_empty_output(monkeypatch, tmp_path):
    run = _run_dir(tmp_path)
    grouped = _grouped_reference(
        tmp_path / "migrations" / "workbooks" / "workbook",
        [_view(LUID_1, data=_failed(), image=_ok_image(LUID_1))],
    )
    outside = tmp_path / "outside"
    _configure(monkeypatch, tmp_path, _Session(), grouped, outside, run)

    with pytest.raises(oracle.OracleRecoveryRefusal, match="must be below"):
        oracle.main()
