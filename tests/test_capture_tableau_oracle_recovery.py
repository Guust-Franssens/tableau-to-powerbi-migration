"""Focused controls for grouped-manifest Tableau oracle recovery (#652)."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import subprocess
import sys
import traceback
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"><text x="1" y="12">12</text></svg>'
REFUSAL_VALUE = "SYNTHETIC_REFUSAL_VALUE_654"


class _Session:
    version = "3.29"
    site_id = "site-id"
    reauth_count = 0
    retry_count = 0

    def __init__(self) -> None:
        self.signins = 0
        self.signouts = 0

    def sign_in(self) -> None:
        """Count sign-in without contacting a tenant."""
        self.signins += 1

    def sign_out(self) -> None:
        """Count finally-path sign-out."""
        self.signouts += 1

    @staticmethod
    def reflected_credential(_payload: bytes) -> None:
        """No credentials occur in this fixture's export payloads."""
        return None

    @staticmethod
    def redact_text(text: str) -> str:
        """Preserve this fixture's non-secret diagnostics."""
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
        "max_age_minutes": 17,
        "rest_api_version": "3.29",
        "certification": "certified",
        "path": f"data/{luid}.csv",
        "sha256": hashlib.sha256(CSV).hexdigest(),
        "row_count": 1,
    }


def _ok_image(luid: str) -> dict:
    return {
        "status": "ok",
        "max_age_minutes": 17,
        "rest_api_version": "3.29",
        "format": "png",
        "path": f"images/{luid}.png",
        "sha256": hashlib.sha256(PNG).hexdigest(),
    }


def _failed(status: str = "transient") -> dict:
    return {
        "status": status,
        "detail": "HTTP 0: read operation timed out",
        "retry_reasons": ["old failure"],
        "max_age_minutes": 17,
        "rest_api_version": "3.29",
    }


def _grouped_reference(
    root: Path,
    views: list[dict],
    *,
    server: str = "https://example.test",
    requested_renders: list[str] | None = None,
    render_capability: dict | None = None,
) -> Path:
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
        "requested_renders": ["png"] if requested_renders is None else requested_renders,
        "render_capability": (
            {"selected_tier": "png_high", "selected_api_version": None, "configured_api_version": "3.29"}
            if render_capability is None
            else render_capability
        ),
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


def _configure(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    monkeypatch, tmp_path: Path, session: _Session, grouped: Path, out: Path, run: Path
) -> None:
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
    monkeypatch.setattr(
        oracle, "select_views", lambda *_args, **_kwargs: ([_current_view(LUID_1), _current_view(LUID_2)], {})
    )
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
    """Retry independently failed legs without exporting successful siblings."""
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
        return {
            "status": "ok",
            "certification": "certified",
            "path": f"data/{stem}.csv",
            "sha256": hashlib.sha256(CSV).hexdigest(),
            "row_count": 1,
            "elapsed_sec": 0.0,
            "max_age_minutes": max_age,
        }

    def render(_session, view_luid, path, kind, options):
        render_exports.append((view_luid, kind, options.max_age))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PNG)
        return {
            "status": "ok",
            "format": kind,
            "path": f"images/{path.name}",
            "sha256": hashlib.sha256(PNG).hexdigest(),
            "elapsed_sec": 0.0,
            "max_age_minutes": options.max_age,
        }

    _configure(monkeypatch, tmp_path, session, grouped, out, run)
    monkeypatch.setattr(oracle, "_capture_data", data)
    monkeypatch.setattr(oracle, "_capture_render", render)

    assert oracle.main() == 0

    manifest = json.loads((out / "oracle-manifest.json").read_text(encoding="utf-8"))
    assert data_exports == [(LUID_1, 17)]
    assert render_exports == [(LUID_2, "png", 17)]
    assert [view["view_luid"] for view in manifest["views"]] == [LUID_1, LUID_2]
    assert manifest["views"][0]["data"]["status"] == "ok"
    assert "image" not in manifest["views"][0], "successful prior PNG must not be exported again"
    assert "data" not in manifest["views"][1], "successful prior data must not be exported again"
    assert manifest["views"][1]["image"]["status"] == "ok"
    assert manifest["recovery"]["eligible_legs"] == 2
    assert manifest["captured_complete"] == 2
    assert manifest["failed"] == 0
    assert session.signins == 1 and session.signouts == 1


def test_zero_eligible_recovery_does_not_sign_in_or_create_batch(monkeypatch, tmp_path):
    """A verified all-success grouped manifest is genuine no-work."""
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


def test_data_truncated_is_not_a_recovery_target(monkeypatch, tmp_path):
    """Only render truncation belongs to the retry-eligible vocabulary."""
    run = _run_dir(tmp_path)
    grouped = _grouped_reference(
        tmp_path / "migrations" / "workbooks" / "workbook",
        [_view(LUID_1, data=_failed("truncated"))],
    )
    out = run / "oracle-retry"
    session = _Session()
    _configure(monkeypatch, tmp_path, session, grouped, out, run)

    assert oracle.main() == 0

    assert session.signins == 0
    assert not out.exists()


def test_svg_recovery_uses_recorded_api_override_without_serverinfo_probe(monkeypatch, tmp_path):
    """Preserve the selected SVG API without spending a capability probe."""
    run = _run_dir(tmp_path)
    view = _view(LUID_1, data=_ok_data(LUID_1))
    view["svg"] = _failed()
    grouped = _grouped_reference(
        tmp_path / "migrations" / "workbooks" / "workbook",
        [view],
        requested_renders=["svg"],
        render_capability={"selected_tier": "svg", "selected_api_version": "3.29"},
    )
    out = run / "oracle-retry"
    session = _Session()
    render_calls = []

    def no_serverinfo(*_args, **_kwargs):
        pytest.fail("recovery must not call server_info/capability probing")

    def render(_session, view_luid, path, kind, options):
        render_calls.append((view_luid, kind, options.api))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"<svg></svg>")
        return {
            "status": "ok",
            "format": kind,
            "path": f"images/{path.name}",
            "sha256": hashlib.sha256(b"<svg></svg>").hexdigest(),
            "elapsed_sec": 0.0,
            "max_age_minutes": options.max_age,
        }

    _configure(monkeypatch, tmp_path, session, grouped, out, run)
    monkeypatch.setattr(oracle.capability, "server_info", no_serverinfo)
    monkeypatch.setattr(oracle, "_capture_render", render)

    assert oracle.main() == 0
    assert render_calls == [(LUID_1, "svg", "3.29")]


def test_recovery_redacts_reused_warnings_in_actual_logs_and_manifest(monkeypatch, tmp_path, caplog):
    """Reused strings are untrusted, even when shaped like an ordinary capability warning."""
    pat = "SYNTHETIC_R2_PAT_77499d0419a14e33913aa1d6"
    token = "SYNTHETIC_R2_SESSION_7c6d3ed0ba8d4e07858032af"
    guidance = "The selected tier is provisional; inspect the retained probe before treating it as a ceiling."
    session = oracle.TableauSession(
        oracle.SiteCredentials("https://example.test", "site", "synthetic-pat-name", pat, "3.29")
    )
    session.token, session.site_id = token, "fixture-site"
    run = _run_dir(tmp_path)
    view = _view(LUID_1, data=_ok_data(LUID_1))
    view["svg"] = _failed()
    grouped = _grouped_reference(
        tmp_path / "input",
        [view],
        requested_renders=["svg"],
        render_capability={
            "selected_tier": "svg",
            "selected_api_version": "3.29",
            "warnings": [guidance, f"Unrecognised prior warning containing {pat} and {token}"],
        },
    )
    out = run / "retry"
    _configure(monkeypatch, tmp_path, session, grouped, out, run)
    monkeypatch.setattr(session, "sign_in", lambda: None)
    monkeypatch.setattr(session, "sign_out", lambda: None)
    monkeypatch.setattr(session, "export", lambda _path, **_kwargs: (SVG, 0.01, {}))

    with caplog.at_level(logging.INFO, logger=oracle.LOG.name):
        assert oracle.main() == 0

    written = (out / grp.MANIFEST_NAME).read_text(encoding="utf-8")
    for secret in (pat, token):
        assert secret not in caplog.text, "reused warning leaked a current credential through the actual recovery log"
        assert secret not in written, "whole-manifest scrubbing must remain active"
    assert f"! {guidance}" in caplog.messages
    assert guidance in written
    assert any("! Unrecognised prior warning containing [REDACTED]" in message for message in caplog.messages)
    assert json.loads(written)["render_capability"]["probe_performed"] is False


@pytest.mark.parametrize("warnings", [None, REFUSAL_VALUE, 17, {REFUSAL_VALUE: 1}, [REFUSAL_VALUE, {}], [None]])
def test_malformed_reused_warnings_refuse_before_signin(monkeypatch, tmp_path, caplog, warnings):
    """No list/string coercion may turn malformed warning metadata into raw diagnostic text."""
    run = _run_dir(tmp_path)
    grouped = _grouped_reference(
        tmp_path / "input",
        [_view(LUID_1, data=_failed())],
        render_capability={"warnings": warnings},
    )
    session = _Session()
    _configure(monkeypatch, tmp_path, session, grouped, run / "retry", run)
    with pytest.raises(oracle.OracleRecoveryRefusal, match="warnings must be an array of strings") as excinfo:
        oracle.main()
    assert REFUSAL_VALUE not in caplog.text + "".join(traceback.format_exception(excinfo.value))
    assert session.signins == 0 and not (run / "retry").exists()


@pytest.mark.parametrize("version", ["3.29", "3.29.1", "3.30.0.2", " 3.29.1 ", "3", "3.29-beta", REFUSAL_VALUE])
def test_recovery_api_grammar_is_the_capability_grammar(version):
    """Use the existing parser, including patch components, rather than a recovery-only grammar."""
    if oracle.capability.api_tuple(version) is not None:
        assert oracle.validated_render_capability({"configured_api_version": version}) == {
            "configured_api_version": version
        }
    else:
        with pytest.raises(oracle.OracleRecoveryRefusal, match="numeric REST API version") as excinfo:
            oracle.validated_render_capability({"configured_api_version": version})
        assert version not in str(excinfo.value)


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
    """Refuse malformed evidence, stale bytes and a different configured source."""
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
    """Reject an output outside the explicit allocated run."""
    run = _run_dir(tmp_path)
    grouped = _grouped_reference(
        tmp_path / "migrations" / "workbooks" / "workbook",
        [_view(LUID_1, data=_failed(), image=_ok_image(LUID_1))],
    )
    outside = tmp_path / "outside"
    _configure(monkeypatch, tmp_path, _Session(), grouped, outside, run)

    with pytest.raises(oracle.OracleRecoveryRefusal, match="must be below"):
        oracle.main()


class _ExportSession(_Session):
    """Script only the export boundary; use the real capture writers and manifest sink."""

    version = "3.21"

    def __init__(self, outcomes: dict[tuple[str, str], str]) -> None:
        super().__init__()
        self.outcomes = outcomes
        self.calls = []

    def export(self, path: str, **options) -> tuple[bytes, float, dict]:
        """Record actual API/cache arguments and return the scripted final leg outcome."""
        route = urlsplit(path)
        luid = route.path.split("/")[-2]
        kind = "data" if route.path.endswith("/data") else "svg"
        age = int(parse_qs(route.query)["maxAge"][0])
        self.calls.append((luid, kind, options.get("api") or self.version, age))
        assert (luid, kind) in self.outcomes, "an unselected successful sibling was exported again"
        status = self.outcomes[luid, kind]
        if status != "ok":
            raise oracle.ExportFailed("synthetic export refusal", status, "synthetic final failure")
        return (
            CSV if kind == "data" else SVG,
            0.01,
            {"content_type": "text/csv", "response_framing": oracle.FRAMING_CONTENT_LENGTH},
        )


def _pipeline_cli(
    monkeypatch, session: _ExportSession, views: list[dict], args: list[str], *, api_pin: str | None = "3.21"
) -> int:
    """Exercise main(), not a manually assembled capture manifest."""
    env = {
        "TABLEAU_SERVER_URL": "https://example.test",
        "TABLEAU_SITE": "site",
        "TABLEAU_PAT_NAME": "synthetic-pat",
        "TABLEAU_PAT_SECRET": "synthetic-secret",
    }
    if api_pin is not None:
        env["TABLEAU_REST_API_VERSION"] = api_pin

    def configured_session(creds, *_args, **_kwargs):
        session.version = creds.version
        return session

    monkeypatch.setattr(oracle, "resolve_env", lambda _path: env)
    monkeypatch.setattr(oracle, "TableauSession", configured_session)
    monkeypatch.setattr(oracle, "select_views", lambda *_args, **_kwargs: (views, {WB_1: "Workbook"}))
    monkeypatch.setattr(oracle.tableau_view_types, "resolve_and_stamp", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sys, "argv", ["capture_tableau_oracle.py", "--workers", "1", *args])
    return oracle.main()


def _ordinary_batch(  # pylint: disable=too-many-arguments
    monkeypatch, run: Path, name: str, outcomes: dict, *, age: int = 5, renders=True, svg_api: str = "3.29"
) -> Path:
    batch = run / name
    session = _ExportSession(outcomes)
    views = [_current_view(luid) for luid in dict.fromkeys(luid for luid, _kind in outcomes)]
    report = {
        "configured_api_version": "3.21",
        "selected_tier": "svg",
        "selected_api_version": svg_api,
        "capability_complete": True,
        "max_age_minutes": age,
        "probe_views_tried": 1,
        "probe_view_luids": [views[0]["id"]],
        "server": {"rest_api_version": "3.29"},
    }
    monkeypatch.setattr(oracle.capability, "probe_render_capability", lambda *_args, **_kwargs: report)
    args = ["--out", str(batch), "--max-age", str(age)]
    if renders:
        args.append("--reference-best")
    assert _pipeline_cli(monkeypatch, session, views, args) in {0, 1, 3, 5}
    manifest = json.loads((batch / grp.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["schema"] == "tableau-oracle/1"
    assert all(call[2:] == (("3.21" if call[1] == "data" else svg_api), age) for call in session.calls)
    return batch


def _merge_batches(tmp_path: Path, batches: list[Path]) -> Path:
    migrations = tmp_path / "migrations" / "workbooks"
    (migrations / "workbook").mkdir(parents=True, exist_ok=True)
    assert grp.run(batches, migrations, dry_run=False) == 0
    return migrations / "workbook" / "reference"


def _retry_batch(  # pylint: disable=too-many-arguments
    monkeypatch, run: Path, grouped: Path, name: str, outcomes: dict, *, api_pin: str | None = "3.21"
) -> tuple[int, _ExportSession]:
    def no_probe(*_args, **_kwargs):
        pytest.fail("recovery must reuse recorded policy, never probe capability or serverinfo")

    monkeypatch.setattr(oracle.capability, "probe_render_capability", no_probe)
    monkeypatch.setattr(oracle.capability, "server_info", no_probe)
    session = _ExportSession(outcomes)
    code = _pipeline_cli(
        monkeypatch,
        session,
        [_current_view(LUID_1), _current_view(LUID_2)],
        ["--run", str(run), "--retry-failed-from", str(grouped), "--out", str(run / name)],
        api_pin=api_pin,
    )
    return code, session


@pytest.mark.parametrize("api_pin", [None, "3.29", "3.29.1"])
def test_ordinary_effective_api_survives_grouping_and_recovery(monkeypatch, tmp_path, api_pin):
    """An omitted pin really exports at 3.21; explicit and patch-version pins retain their value."""
    run = _run_dir(tmp_path)
    initial = run / "initial"
    session = _ExportSession({(LUID_1, "data"): "transient"})
    expected = "3.21" if api_pin is None else api_pin
    assert (
        _pipeline_cli(
            monkeypatch, session, [_current_view(LUID_1)], ["--out", str(initial), "--max-age", "5"], api_pin=api_pin
        )
        == 3
    )
    assert session.calls == [(LUID_1, "data", expected, 5)]
    captured = json.loads((initial / grp.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert captured["rest_api_version"] == expected, "the producer lost its effective client API"
    grouped = _merge_batches(tmp_path, [initial])
    policy = json.loads((grouped / grp.MANIFEST_NAME).read_text(encoding="utf-8"))["views"][0]["data"]
    assert policy["rest_api_version"] == expected
    assert policy["rest_api_version_source"] == "capture_configuration"
    code, retried = _retry_batch(monkeypatch, run, grouped, "retry", {(LUID_1, "data"): "ok"}, api_pin=api_pin)
    assert code == 0 and retried.calls == [(LUID_1, "data", expected, 5)]


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("capability_config", [None, "3.28"])
def test_legacy_null_or_missing_api_is_derived_from_its_own_capture(monkeypatch, tmp_path, missing, capability_config):
    """Legacy inference is labelled, and a recorded capability pin wins over the producer default."""
    run = _run_dir(tmp_path)
    initial = run / "initial"
    outcomes = {(LUID_1, "data"): "transient"}
    args = ["--out", str(initial), "--max-age", "5"]
    if capability_config is not None:
        outcomes[LUID_1, "svg"] = "transient"
        args.append("--reference-best")
        monkeypatch.setattr(
            oracle.capability,
            "probe_render_capability",
            lambda *_args, **_kwargs: {
                "configured_api_version": capability_config,
                "selected_tier": "svg",
                "selected_api_version": "3.29",
                "probe_views_tried": 1,
            },
        )
    assert _pipeline_cli(
        monkeypatch, _ExportSession(outcomes), [_current_view(LUID_1)], args, api_pin=capability_config
    ) in {3, 5}
    path = initial / grp.MANIFEST_NAME
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if missing:
        manifest.pop("rest_api_version")
    else:
        manifest["rest_api_version"] = None
        manifest["views"][0]["data"]["rest_api_version"] = None
    path.write_text(json.dumps(manifest), encoding="utf-8")
    grouped = _merge_batches(tmp_path, [initial])
    view = json.loads((grouped / grp.MANIFEST_NAME).read_text(encoding="utf-8"))["views"][0]
    expected = capability_config or "3.21"
    assert view["data"]["rest_api_version"] == expected
    assert view["data"]["rest_api_version_source"] == (
        "capability_configuration" if capability_config else "legacy_producer_default"
    )
    if capability_config:
        assert view["svg"]["rest_api_version"] == "3.29"
        assert view["svg"]["rest_api_version_source"] == "selected_render_api"
    code, retried = _retry_batch(
        monkeypatch, run, grouped, "retry", dict.fromkeys(outcomes, "ok"), api_pin=capability_config
    )
    assert code == 0
    assert retried.calls[0] == (LUID_1, "data", expected, 5)
    if capability_config:
        assert retried.calls[1] == (LUID_1, "svg", "3.29", 5)


@pytest.mark.parametrize("sibling_status", ["ok", "transient"])
def test_only_selected_failed_leg_api_differences_can_block_recovery(monkeypatch, tmp_path, sibling_status):
    """Ordinary grouping is per leg; only selected incompatible policies constrain a retry."""
    run = _run_dir(tmp_path)
    first = _ordinary_batch(monkeypatch, run, "first", {(LUID_1, "data"): "ok", (LUID_1, "svg"): "transient"})
    later = _ordinary_batch(
        monkeypatch,
        run,
        "later",
        {(LUID_2, "data"): "ok", (LUID_2, "svg"): sibling_status},
        svg_api="3.30",
    )
    grouped = _merge_batches(tmp_path, [first, later])
    manifest = json.loads((grouped / grp.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["render_capability"] is None, "differing prior reports must not become one authoritative policy"
    by_luid = {view["view_luid"]: view for view in manifest["views"]}
    assert by_luid[LUID_1]["svg"]["rest_api_version"] == "3.29"
    assert by_luid[LUID_2]["svg"]["rest_api_version"] == "3.30"
    if sibling_status == "transient":
        monkeypatch.setattr(_ExportSession, "sign_in", lambda _self: pytest.fail("policy refusal must precede sign-in"))
        with pytest.raises(oracle.OracleRecoveryRefusal, match="incompatible REST API"):
            _retry_batch(monkeypatch, run, grouped, "retry", {})
        assert not (run / "retry").exists()
        return
    code, retried = _retry_batch(monkeypatch, run, grouped, "retry", {(LUID_1, "svg"): "transient"})
    assert code == 3 and retried.calls == [(LUID_1, "svg", "3.29", 5)]
    grouped = _merge_batches(tmp_path, [first, later, run / "retry"])
    code, retried = _retry_batch(monkeypatch, run, grouped, "retry-2", {(LUID_1, "svg"): "ok"})
    assert code == 0 and retried.calls == [(LUID_1, "svg", "3.29", 5)]


def test_failed_leg_overrides_an_older_unambiguous_probe_report(monkeypatch, tmp_path):
    """A retained probe describes its own capture, not a later explicit SVG export."""
    run = _run_dir(tmp_path)
    first = _ordinary_batch(monkeypatch, run, "first", {(LUID_1, "data"): "ok", (LUID_1, "svg"): "transient"})
    later = run / "later"
    session = _ExportSession({(LUID_1, "data"): "ok", (LUID_1, "svg"): "transient"})
    monkeypatch.setattr(oracle.capability, "server_info", lambda *_args, **_kwargs: {})
    assert (
        _pipeline_cli(
            monkeypatch,
            session,
            [_current_view(LUID_1)],
            ["--out", str(later), "--svg", "--max-age", "5"],
            api_pin="3.30",
        )
        == 3
    )
    grouped = _merge_batches(tmp_path, [first, later])
    manifest = json.loads((grouped / grp.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["render_capability"]["selected_api_version"] == "3.29"
    assert manifest["views"][0]["svg"]["rest_api_version"] == "3.30"
    code, retried = _retry_batch(monkeypatch, run, grouped, "retry", {(LUID_1, "svg"): "ok"})
    assert code == 0 and retried.calls == [(LUID_1, "svg", "3.30", 5)]


def test_recovery_omits_different_prior_reports_without_blocking_compatible_selected_legs(monkeypatch, tmp_path):
    """Different historical client pins across workbooks need not constrain identical SVG retries."""
    run = _run_dir(tmp_path)
    views, args = [], ["--run", str(run), "--out", str(run / "retry")]
    for index, luid in enumerate((LUID_1, LUID_2)):
        view = _view(luid, data=_ok_data(luid))
        view["svg"] = _failed()
        grouped = _grouped_reference(
            tmp_path / f"workbook-{index}",
            [view],
            requested_renders=["svg"],
            render_capability={
                "selected_tier": "svg",
                "selected_api_version": "3.29",
                "configured_api_version": "3.21" if index == 0 else "3.28",
            },
        )
        path = grouped / grp.MANIFEST_NAME
        manifest = json.loads(path.read_text(encoding="utf-8"))
        workbook = WB_1 if index == 0 else "22222222-2222-4222-8222-222222222222"
        manifest["workbook_luid"] = manifest["views"][0]["workbook_luid"] = workbook
        path.write_text(json.dumps(manifest), encoding="utf-8")
        current = _current_view(luid)
        current["workbook"]["id"] = workbook
        views.append(current)
        args.extend(["--retry-failed-from", str(grouped)])
    session = _ExportSession({(LUID_1, "svg"): "ok", (LUID_2, "svg"): "ok"})
    assert _pipeline_cli(monkeypatch, session, views, args) == 0
    assert session.calls == [(LUID_1, "svg", "3.29", 17), (LUID_2, "svg", "3.29", 17)]
    manifest = json.loads((run / "retry" / grp.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["render_capability"] is None
    assert manifest["captured_complete"] == 2


def test_ordinary_group_retry_group_retry_retains_svg_policy_and_success_bytes(monkeypatch, tmp_path):
    """One SVG gap deliberately persists across the first recovery and regrouping."""
    run = _run_dir(tmp_path)
    initial = _ordinary_batch(
        monkeypatch,
        run,
        "initial",
        {(LUID_1, "data"): "ok", (LUID_1, "svg"): "transient", (LUID_2, "data"): "ok", (LUID_2, "svg"): "transient"},
    )
    original = {path: path.read_bytes() for path in initial.rglob("*") if path.is_file()}
    grouped = _merge_batches(tmp_path, [initial])
    code, first = _retry_batch(
        monkeypatch, run, grouped, "retry-1", {(LUID_1, "svg"): "ok", (LUID_2, "svg"): "transient"}
    )
    assert code == 1
    assert first.calls == [(LUID_1, "svg", "3.29", 5), (LUID_2, "svg", "3.29", 5)]
    first_manifest = json.loads((run / "retry-1" / grp.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert isinstance(first_manifest["render_capability"], dict), "recovery lost the reused capability policy"
    assert first_manifest["render_capability"]["probe_performed"] is False
    grouped = _merge_batches(tmp_path, [initial, run / "retry-1"])
    code, second = _retry_batch(monkeypatch, run, grouped, "retry-2", {(LUID_2, "svg"): "ok"})
    assert code == 0
    assert second.calls == [(LUID_2, "svg", "3.29", 5)], "regrouping lost the selected SVG API override"
    grouped = _merge_batches(tmp_path, [initial, run / "retry-1", run / "retry-2"])
    final = json.loads((grouped / grp.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert final["render_capability"]["selected_tier"] == "svg", "package-facing capability metadata was erased"
    assert final["render_capability"]["selected_api_version"] == "3.29"
    assert final["render_capability"]["probe_views_tried"] == 1
    assert final["render_capability"]["probe_performed"] is False
    assert final["render_capability"]["reused_from_grouped"]["inputs"]
    assert final["requested_renders"] == ["svg"]
    assert all(view["data"]["status"] == view["svg"]["status"] == "ok" for view in final["views"])
    assert all(path.read_bytes() == payload for path, payload in original.items())
    assert (grouped / "images" / f"{LUID_1}.svg").read_bytes() == SVG


def test_grouped_retry_uses_failed_leg_max_age_not_newest_batch(monkeypatch, tmp_path):
    """Grouping must not substitute a newer data batch's cache policy for a failed render."""
    run = _run_dir(tmp_path)
    initial = _ordinary_batch(monkeypatch, run, "initial", {(LUID_1, "data"): "ok", (LUID_1, "svg"): "transient"})
    newer = _ordinary_batch(monkeypatch, run, "newer", {(LUID_1, "data"): "ok"}, age=60, renders=False)
    grouped = _merge_batches(tmp_path, [initial, newer])
    before = json.loads((grouped / grp.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert before["max_age_minutes"] == 60
    assert before["views"][0]["svg"]["max_age_minutes"] == 5
    code, session = _retry_batch(monkeypatch, run, grouped, "retry", {(LUID_1, "svg"): "ok"})
    assert code == 0
    assert session.calls[0][3] == 5, "recovery substituted an unrelated newer batch's maxAge"
    retried = json.loads((run / "retry" / grp.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert retried["max_age_minutes"] == retried["views"][0]["svg"]["max_age_minutes"] == 5


def test_render_only_recovery_logs_attempted_leg_success(monkeypatch, tmp_path, caplog):
    """The real CLI reports successful SVG work without a synthetic data failure."""
    run = _run_dir(tmp_path)
    initial = _ordinary_batch(monkeypatch, run, "initial", {(LUID_1, "data"): "ok", (LUID_1, "svg"): "transient"})
    grouped = _merge_batches(tmp_path, [initial])
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=oracle.LOG.name):
        code, _session = _retry_batch(monkeypatch, run, grouped, "retry", {(LUID_1, "svg"): "ok"})
    assert code == 0
    progress = [message for message in caplog.messages if "1/1" in message and "View a" in message]
    assert len(progress) == 1
    assert "FAILED" not in progress[0], "successful render-only recovery logged a synthetic data failure"
    assert "svg=ok" in progress[0]
    manifest = json.loads((run / "retry" / grp.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert "data" not in manifest["views"][0]


@pytest.mark.parametrize("status", ["mystery", 17, None, [], {}])
def test_unknown_or_malformed_status_cannot_be_successful_no_work(monkeypatch, tmp_path, status):
    """Unknown or malformed final statuses are refusals, never successful empty work."""
    run = _run_dir(tmp_path)
    initial = _ordinary_batch(monkeypatch, run, "initial", {(LUID_1, "data"): "transient"}, renders=False)
    grouped = _merge_batches(tmp_path, [initial])
    path = grouped / grp.MANIFEST_NAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["views"][0]["data"]["status"] = status
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(oracle.OracleRecoveryRefusal, match="status"):
        _retry_batch(monkeypatch, run, grouped, "retry", {})
    assert not (run / "retry").exists()


def test_pre_session_manifest_refusal_never_echoes_unverified_identity(monkeypatch, tmp_path):
    """A duplicate unverified identifier must not escape through the refusal traceback."""
    run = _run_dir(tmp_path)
    initial = _ordinary_batch(monkeypatch, run, "initial", {(LUID_1, "data"): "transient"}, renders=False)
    grouped = _merge_batches(tmp_path, [initial])
    path = grouped / grp.MANIFEST_NAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["views"][0]["view_luid"] = REFUSAL_VALUE
    payload["views"].append(dict(payload["views"][0]))
    payload["view_count"] = 2
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(oracle.OracleRecoveryRefusal) as excinfo:
        _retry_batch(monkeypatch, run, grouped, "retry", {})
    assert REFUSAL_VALUE not in "".join(traceback.format_exception(excinfo.value)), "refusal disclosed manifest text"
    assert "manifest" in str(excinfo.value) and "view" in str(excinfo.value)


def test_metadata_mismatch_refuses_before_exports_without_echoing_identity(monkeypatch, tmp_path):
    """The post-sign-in revision check must not expose an identifier missing from current metadata."""
    run = _run_dir(tmp_path)
    initial = _ordinary_batch(monkeypatch, run, "initial", {(LUID_1, "data"): "transient"}, renders=False)
    grouped = _merge_batches(tmp_path, [initial])
    path = grouped / grp.MANIFEST_NAME
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["views"][0]["view_luid"] = REFUSAL_VALUE
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(oracle.OracleRecoveryRefusal, match="updatedAt") as excinfo:
        _retry_batch(monkeypatch, run, grouped, "retry", {})
    assert REFUSAL_VALUE not in "".join(traceback.format_exception(excinfo.value))
    assert not (run / "retry").exists()


@pytest.mark.parametrize(
    "status",
    [
        "failed",
        "source_credential",
        "credential_reflected",
        "unsupported_api_version",
        "format_mismatch",
        "not_attempted",
        "not_captured",
        "absent",
        "stale_revision",
        "not_copied",
    ],
)
def test_known_nonretryable_statuses_ignore_retry_history(monkeypatch, tmp_path, status):
    """Known refusals and unattempted legs remain valid no-work, even with retryable history."""
    run = _run_dir(tmp_path)
    view = _view(LUID_1, data={**_failed(status), "retry_reasons": ["transient", "session_lost"]})
    grouped = _grouped_reference(tmp_path / "input", [view], requested_renders=[])
    session = _Session()
    _configure(monkeypatch, tmp_path, session, grouped, run / "retry", run)
    assert oracle.main() == 0
    assert session.signins == 0 and not (run / "retry").exists()


@pytest.mark.parametrize("value", [None, True, 0, "5", 5.0])
def test_selected_leg_cache_policy_requires_its_own_valid_integer(monkeypatch, tmp_path, value):
    """A valid unrelated top-level value cannot license an absent/malformed selected-leg value."""
    run = _run_dir(tmp_path)
    initial = _ordinary_batch(monkeypatch, run, "initial", {(LUID_1, "data"): "transient"}, renders=False)
    grouped = _merge_batches(tmp_path, [initial])
    path = grouped / grp.MANIFEST_NAME
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if value is None:
        manifest["views"][0]["data"].pop("max_age_minutes")
    else:
        manifest["views"][0]["data"]["max_age_minutes"] = value
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(oracle.OracleRecoveryRefusal, match="max_age_minutes"):
        _retry_batch(monkeypatch, run, grouped, "retry", {})
    assert not (run / "retry").exists()


def test_actual_grouping_of_incompatible_selected_cache_policies_refuses_before_signin(monkeypatch, tmp_path):
    """No splitting/scheduling workaround: one invocation requires one established cache policy."""
    run = _run_dir(tmp_path)
    first = _ordinary_batch(monkeypatch, run, "first", {(LUID_1, "data"): "transient"}, renders=False)
    later = _ordinary_batch(monkeypatch, run, "later", {(LUID_2, "data"): "transient"}, age=60, renders=False)
    grouped = _merge_batches(tmp_path, [first, later])
    monkeypatch.setattr(_ExportSession, "sign_in", lambda _self: pytest.fail("policy refusal must precede sign-in"))
    with pytest.raises(oracle.OracleRecoveryRefusal, match="incompatible max_age"):
        _retry_batch(monkeypatch, run, grouped, "retry", {})


@pytest.mark.parametrize("policy", ["missing-api", "wrong-api", "different-data-api", "boolean-count", "bad-intent"])
def test_consumed_policy_or_count_uncertainty_refuses_before_network(monkeypatch, tmp_path, policy):
    """Refuse unestablished/incompatible API policy and malformed count/intent fields."""
    run = _run_dir(tmp_path)
    grouped = _grouped_reference(tmp_path / "input", [_view(LUID_1, data=_failed())], requested_renders=[])
    path = grouped / grp.MANIFEST_NAME
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if policy == "missing-api":
        manifest["views"][0]["data"].pop("rest_api_version")
    elif policy == "wrong-api":
        manifest["views"][0]["data"]["rest_api_version"] = REFUSAL_VALUE
    elif policy == "different-data-api":
        manifest["views"][0]["data"]["rest_api_version"] = "3.21"
    elif policy == "boolean-count":
        manifest["view_count"] = True
    else:
        manifest["requested_renders"] = [REFUSAL_VALUE]
    path.write_text(json.dumps(manifest), encoding="utf-8")
    session = _Session()
    _configure(monkeypatch, tmp_path, session, grouped, run / "retry", run)
    with pytest.raises(oracle.OracleRecoveryRefusal) as excinfo:
        oracle.main()
    assert REFUSAL_VALUE not in str(excinfo.value)
    assert session.signins == 0 and not (run / "retry").exists()


def test_new_source_credential_refusal_is_final_for_recovery(monkeypatch, tmp_path):
    """A new credential refusal is not converted into another retry target."""
    run = _run_dir(tmp_path)
    initial = _ordinary_batch(monkeypatch, run, "initial", {(LUID_1, "data"): "transient"}, renders=False)
    grouped = _merge_batches(tmp_path, [initial])
    code, session = _retry_batch(monkeypatch, run, grouped, "retry", {(LUID_1, "data"): "source_credential"})
    assert code == 2 and session.calls == [(LUID_1, "data", "3.21", 5)]
    grouped = _merge_batches(tmp_path, [initial, run / "retry"])
    code, session = _retry_batch(monkeypatch, run, grouped, "no-work", {})
    assert code == 0 and session.signins == 0 and not (run / "no-work").exists()


@pytest.mark.parametrize(
    "remote",
    [
        r"\\synthetic.invalid\share\reference",
        r"\\?\UNC\synthetic.invalid\share\reference",
        r"\\.\C:\reference",
        "https://synthetic.invalid/reference",
    ],
)
def test_remote_source_is_lexically_refused_before_any_filesystem_call(monkeypatch, tmp_path, remote):
    """Intercept remote filesystem calls so a failed guard cannot access a real remote share."""
    run = _run_dir(tmp_path)
    session = _Session()
    _configure(monkeypatch, tmp_path, session, Path(remote), run / "retry", run)

    def intercept(original):
        def checked(path, *args, **kwargs):
            assert (
                "synthetic.invalid" not in str(path) and not str(path).startswith(r"\\.") and "https:" not in str(path)
            ), "remote input reached a filesystem operation before lexical refusal"
            return original(path, *args, **kwargs)

        return checked

    with monkeypatch.context() as guards:
        for name in ("read_bytes", "lstat", "resolve", "exists", "is_file"):
            guards.setattr(Path, name, intercept(getattr(Path, name)))
        with pytest.raises(oracle.OracleRecoveryRefusal, match="local|remote|path"):
            oracle.main()
    assert session.signins == 0


@pytest.mark.skipif(os.name != "nt", reason="real local Windows junction boundary")
@pytest.mark.parametrize("boundary", ["run", "run-ancestor", "source", "artifact", "output", "output-ancestor"])
def test_recovery_refuses_real_junction_before_following_it(monkeypatch, tmp_path, boundary):
    """Matching run.json and artifact bytes do not authorize following a Windows junction."""
    run = _run_dir(tmp_path)
    initial = _ordinary_batch(monkeypatch, run, "initial", {(LUID_1, "data"): "ok", (LUID_1, "svg"): "ok"})
    grouped = _merge_batches(tmp_path, [initial])
    out = run / "retry"
    if boundary == "run":
        alias, target = tmp_path / "001-recovery", run
        run, out = alias, alias / "retry"
    elif boundary == "run-ancestor":
        alias, target = tmp_path / "alias-runs", run.parent
        run = alias / run.name
        out = run / "retry"
    elif boundary == "source":
        alias, target = tmp_path / "alias-reference", grouped
        grouped = alias
    elif boundary == "artifact":
        alias, target = grouped / "images", grouped / "real-images"
        alias.rename(target)
    else:
        target = run / "real-output"
        target.mkdir()
        alias = run / "alias-output"
        out = alias if boundary == "output" else alias / "retry"
    made = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(alias), str(target)], capture_output=True, check=False
    )
    assert made.returncode == 0, "the local junction fixture must exist to test a no-follow boundary"
    if boundary in {"run", "run-ancestor"}:
        marker = target / "run.json" if boundary == "run" else target / run.name / "run.json"
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["allocated_abs_path"] = str(run)
        marker.write_text(json.dumps(payload), encoding="utf-8")
    session = _Session()
    _configure(monkeypatch, tmp_path, session, grouped, out, run)
    try:
        with pytest.raises(oracle.OracleRecoveryRefusal, match="reparse|junction"):
            oracle.main()
        assert session.signins == 0
    finally:
        alias.rmdir()
