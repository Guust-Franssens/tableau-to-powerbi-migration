r"""check_path_ceiling: the gate against a bundle a stock Windows box cannot open (#235).

No test creates a path over 259 characters (a stock runner could not create the fixture). Boundary
tests drive a LOW ceiling over a short, openable tree, which also proves the verdict is arithmetic
rather than an "can I open this?" probe.
"""

from __future__ import annotations

import codecs
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_path_ceiling.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_path_ceiling as cpc  # noqa: E402  # pylint: disable=wrong-import-position

COMBINING_NAME = "cafe\u0301.json"  # legal everywhere, unencodable in cp1252
ASTRAL = "\U0001f600"  # one code point, TWO UTF-16 code units
U16 = cpc._utf16_len  # pylint: disable=protected-access
_REAL_WALK = os.walk  # captured before any test patches the shared `os` module


def _tree(root: Path, *, filename: str = "visual.json", subdir: str = "visuals") -> Path:
    """Create <root>/<subdir>/<filename> and return the file path."""
    (root / subdir).mkdir(parents=True, exist_ok=True)
    (root / subdir / filename).write_text("{}", encoding="utf-8")
    return root / subdir / filename


def _under(target: Path) -> cpc.Limits:
    """A file ceiling one unit below `target`, with a directory ceiling out of the way."""
    return cpc.Limits(file_ceiling=len(str(target)) - 1, dir_ceiling=10_000)


def _walk_with(monkeypatch, *, extra_file: str | None = None, error_dir: str | None = "") -> None:
    """Wrap `os.walk` to inject an undecodable filename and/or an onerror PermissionError."""
    real_walk = _REAL_WALK

    def walk(top, onerror=None, **kwargs):
        if error_dir is not None and onerror is not None:
            error = PermissionError(13, "Permission denied")
            if error_dir:
                error.filename = str(Path(top) / error_dir)
            onerror(error)
        for dirpath, dirnames, filenames in real_walk(top, onerror=onerror, **kwargs):
            yield dirpath, dirnames, filenames + ([extra_file] if extra_file else [])

    monkeypatch.setattr(cpc.os, "walk", walk)


def test_shipped_constants_and_exit_ladder_are_pinned_to_literals():
    """The measured Desktop limits and the house exit ladder; a silent edit here is the worst change."""
    assert (cpc.FILE_CEILING, cpc.DIR_CEILING) == (259, 247)
    assert (cpc.DEFAULT_LIMITS.file_ceiling, cpc.DEFAULT_LIMITS.dir_ceiling) == (259, 247)
    assert cpc.DEFAULT_LIMITS.min_root_budget is None, "the portability gate must be opt-in"
    assert (cpc.EXIT_OK, cpc.EXIT_OVER_CEILING, cpc.EXIT_USAGE, cpc.EXIT_SKIPPED) == (0, 1, 2, 3)
    assert cpc.SHIPPING_ROOT_BUDGET_ADVISORY == 40


def test_ceiling_boundary_and_named_offenders(tmp_path):
    """At the ceiling is legal; one over is a finding, and every offender is NAMED, not counted."""
    target = _tree(tmp_path)
    at = cpc.scan(tmp_path, cpc.Limits(file_ceiling=len(str(target)), dir_ceiling=len(str(target))))
    assert at["status"] == cpc.STATUS_OK and at["counted"]["over_ceiling"] == 0
    over = cpc.scan(tmp_path, _under(target))
    assert over["status"] == cpc.STATUS_OVER_CEILING and over["counted"]["over_ceiling"] == 1
    assert (over["worst_offenders"][0]["path"], over["worst_offenders"][0]["kind"]) == (str(target), cpc.KIND_FILE)
    for name in ("b.json", "c.json"):
        _tree(tmp_path, filename=name)
    report = cpc.scan(tmp_path, cpc.Limits(file_ceiling=len(str(tmp_path)), dir_ceiling=10_000))
    assert {Path(o["path"]).name for o in report["worst_offenders"]} == {"visual.json", "b.json", "c.json"}


def test_directory_and_file_ceilings_apply_to_their_own_kind(tmp_path):
    """The stricter directory rule is separate; a file-only check would miss it, and vice versa."""
    target = _tree(tmp_path, filename="p.json")
    directory = target.parent
    dirs = cpc.scan(tmp_path, cpc.Limits(file_ceiling=len(str(target)), dir_ceiling=len(str(directory)) - 1))
    assert dirs["status"] == cpc.STATUS_OVER_CEILING
    assert {o["kind"] for o in dirs["worst_offenders"]} == {cpc.KIND_DIR}
    files = cpc.scan(tmp_path, cpc.Limits(file_ceiling=len(str(directory)) - 1, dir_ceiling=len(str(target)) + 100))
    assert {o["kind"] for o in files["worst_offenders"]} == {cpc.KIND_FILE}


@pytest.mark.parametrize(("registry", "git"), [(0, True), (1, False), (None, None)])
def test_host_opt_ins_are_reported_and_never_reach_the_verdict(tmp_path, monkeypatch, registry, git):
    """LongPathsEnabled and core.longpaths are host facts; neither rescues an over-ceiling or unknown path."""
    target = _tree(tmp_path)
    monkeypatch.setattr(cpc, "read_long_paths_enabled", lambda: registry)
    monkeypatch.setattr(cpc, "read_git_long_paths", lambda _root=None: git)
    report = cpc.scan(tmp_path, _under(target))
    assert (report["status"], report["counted"]["over_ceiling"]) == (cpc.STATUS_OVER_CEILING, 1)
    assert (report["host_long_paths_enabled"], report["host_git_long_paths"]) == (registry, git)
    text = cpc.render(report)
    assert "core.longpaths" in text
    expected = {True: None, False: "silently drops", None: "do not read as enabled"}[git]
    assert expected is None or expected in text
    if registry == 1:
        assert "NON-DEFAULT" in text and "does NOT affect Power BI Desktop" in text
    _walk_with(monkeypatch)
    unknown = cpc.scan(tmp_path)
    assert (unknown["status"], unknown["counted"]["unknown"]) == (cpc.STATUS_UNKNOWN_PATHS, 1)


def test_both_ceilings_are_rendered_in_both_framings(tmp_path):
    """259/247 longest-legal and 260/248 first-refused, so neither can start an off-by-one argument."""
    _tree(tmp_path)
    text = cpc.render(cpc.scan(tmp_path))
    assert "file <= 259" in text and "directory <= 247" in text
    assert "observed refusal at 260" in text and "refuses 248" in text and "LongPathsEnabled" in text


def test_lengths_are_utf16_code_units_not_code_points(tmp_path):
    """Desktop counts UTF-16 units; `len()` would let an astral-character path through."""
    assert (U16("abc"), U16(ASTRAL), U16("e\u0301")) == (3, 2, 2)
    target = _tree(tmp_path, filename=f"v{ASTRAL}.json")
    record = next(r for r in cpc.collect(tmp_path)[0] if r["path"] == str(target))
    assert record["length"] == len(str(target)) + 1
    report = cpc.scan(tmp_path, cpc.Limits(file_ceiling=len(str(target)), dir_ceiling=10_000))
    assert report["status"] == cpc.STATUS_OVER_CEILING
    assert report["worst_offenders"][0]["length"] == len(str(target)) + 1


def test_unassessable_input_is_unknown_never_clean(tmp_path, monkeypatch):
    """A surrogate name or an unreadable directory is UNKNOWN; a finding still outranks it."""
    target = _tree(tmp_path)
    _walk_with(monkeypatch, extra_file="bad\udcff.json", error_dir=None)
    report = cpc.scan(tmp_path)
    assert report["status"] == cpc.STATUS_UNKNOWN_PATHS
    assert any("bad" in u["path"] for u in report["unknown_paths"])
    payload = json.dumps(report)
    assert "\\udcff" in payload or "\\xff" in payload
    _walk_with(monkeypatch, error_dir="visuals")
    report = cpc.scan(tmp_path)
    assert (report["status"], report["counted"]["unknown"]) == (cpc.STATUS_UNKNOWN_PATHS, 1)
    assert "visuals" in report["unknown_paths"][0]["path"]
    report = cpc.scan(tmp_path, _under(target))
    assert (report["status"], report["counted"]["unknown"]) == (cpc.STATUS_OVER_CEILING, 1)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows filenames cannot hold undecodable bytes")
def test_real_undecodable_posix_filename_is_unknown(tmp_path):
    (tmp_path / "visuals").mkdir()
    with open(os.path.join(bytes(tmp_path / "visuals"), b"bad\xff.json"), "wb") as handle:
        handle.write(b"{}")
    assert cpc.scan(tmp_path)["status"] == cpc.STATUS_UNKNOWN_PATHS


def test_empty_target_cannot_be_judged_clean(tmp_path, capsys):
    report = cpc.scan(tmp_path)
    assert (report["status"], report["root_budget"]) == (cpc.STATUS_NO_PATHS, None)
    assert cpc.main([str(tmp_path)]) == 3
    capsys.readouterr()


def test_host_readers(tmp_path, monkeypatch):
    """`git config --get` exit 1 is git's default (False); a missing git is None; registry is int|None."""
    value = cpc.read_long_paths_enabled()
    assert value is None if sys.platform != "win32" else (value is None or isinstance(value, int))
    monkeypatch.setattr(cpc.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", ""))
    assert cpc.read_git_long_paths(tmp_path) is False

    def _boom(*_a, **_k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(cpc.subprocess, "run", _boom)
    assert cpc.read_git_long_paths(tmp_path) is None


def test_root_budget_is_portable_and_tight_roots_are_advisory(tmp_path, monkeypatch):
    """The budget is ceiling minus tail, independent of where the tree sits; tight is advisory only."""
    shallow, deep = tmp_path / "a", tmp_path / ("a" * 36)
    target = _tree(shallow)
    _tree(deep)
    report = cpc.scan(shallow)
    records, _ = cpc.collect(shallow)
    expected = min((cpc.DIR_CEILING if r["kind"] == cpc.KIND_DIR else cpc.FILE_CEILING) - r["tail"] for r in records)
    assert report["root_budget"] == expected == cpc.scan(deep)["root_budget"]
    assert report["longest_tail"] == U16(str(target)) - U16(str(shallow)) == cpc.scan(deep)["longest_tail"]
    assert report["root_budget_is_tight"] is False and "TIGHT ROOT BUDGET" not in cpc.render(report)
    monkeypatch.setattr(cpc, "SHIPPING_ROOT_BUDGET_ADVISORY", report["root_budget"] + 1)
    tight = cpc.scan(shallow)
    assert tight["root_budget_is_tight"] is True and tight["status"] == cpc.STATUS_OK
    assert "TIGHT ROOT BUDGET" in cpc.render(tight)


def test_a_short_filename_makes_the_directory_rule_decide_the_budget(tmp_path, capsys):
    """A blank PBIR page holds only `page.json`: a file-only budget overstates the root by 2."""
    target = _tree(tmp_path, subdir="pages", filename="page.json")
    file_only = cpc.FILE_CEILING - (U16(str(target)) - U16(str(tmp_path)))
    actual = cpc.DIR_CEILING - (U16(str(target.parent)) - U16(str(tmp_path)))
    report = cpc.scan(tmp_path)
    assert report["root_budget"] == actual == file_only - 2
    assert report["root_budget_binding"]["kind"] == cpc.KIND_DIR
    assert cpc.scan(tmp_path, cpc.Limits(min_root_budget=actual))["status"] == cpc.STATUS_OK
    assert cpc.scan(tmp_path, cpc.Limits(min_root_budget=actual + 1))["status"] == cpc.STATUS_OVER_CEILING
    assert cpc.main([str(tmp_path), "--min-root-budget", str(actual)]) == 0
    assert cpc.main([str(tmp_path), "--min-root-budget", str(file_only)]) == 1
    capsys.readouterr()


def test_near_ceiling_paths_are_advisory_and_do_not_fail(tmp_path):
    target = _tree(tmp_path)
    limits = cpc.Limits(file_ceiling=len(str(target)), dir_ceiling=10_000, warn_at=len(str(target)) - 1)
    report = cpc.scan(tmp_path, limits)
    assert report["status"] == cpc.STATUS_OK and report["counted"]["near_ceiling"] >= 1
    assert "advisory" in cpc.render(report)


def test_cli_exit_codes_json_and_multiple_targets(tmp_path, capsys):
    """Clean 0, finding 1, warn-only 0 but loud, usage 2, negative threshold refused, worst target wins."""
    clean, dirty = tmp_path / "clean", tmp_path / "dirty"
    _tree(clean)
    target = _tree(dirty)
    ceiling = str(len(str(target)) - 1)
    assert cpc.main([str(clean)]) == 0
    assert cpc.main([str(dirty), "--ceiling", ceiling, "--warn-only"]) == 0
    assert "OVER CEILING" in capsys.readouterr().out
    out = tmp_path / "reports" / "path-ceiling.json"
    assert cpc.main([str(clean), str(dirty), "--ceiling", ceiling]) == 1
    text = capsys.readouterr().out
    assert str(clean.resolve()) in text and str(dirty.resolve()) in text
    assert cpc.main([str(dirty), "--ceiling", ceiling, "--json", str(out)]) == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert (payload["status"], payload["file_ceiling"]) == (cpc.STATUS_OVER_CEILING, len(str(target)) - 1)
    assert "host_long_paths_enabled" in payload
    assert cpc.main([str(tmp_path / "nope")]) == 2
    with pytest.raises(SystemExit):
        cpc.main([str(clean), "--ceiling", "-1"])
    capsys.readouterr()


def _make_combining_fixture(root: Path) -> Path | None:
    """Create `<root>/visuals/cafe<combining-acute>.json`, or None if this filesystem refuses it."""
    directory = root / "visuals"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / COMBINING_NAME
    try:
        target.write_text("{}", encoding="utf-8")
    except (OSError, UnicodeError):  # pragma: no cover - only on an ASCII-locale filesystem
        return None
    return target if target.exists() and COMBINING_NAME in os.listdir(directory) else None


def _strict_cp1252_stream() -> tuple[object, io.BytesIO]:
    """A stream that really refuses non-cp1252 text and has neither `reconfigure` nor `.encoding`."""
    buffer = io.BytesIO()
    return codecs.getwriter("cp1252")(buffer), buffer


def test_json_is_written_before_the_console_is_touched(tmp_path, monkeypatch):
    """A rendering failure must not destroy the machine-readable artifact."""
    _tree(tmp_path)
    out = tmp_path / "reports" / "path-ceiling.json"

    def boom(_report):
        raise RuntimeError("console exploded")

    monkeypatch.setattr(cpc, "render", boom)
    with pytest.raises(RuntimeError):
        cpc.main([str(tmp_path), "--json", str(out)])
    assert json.loads(out.read_text(encoding="utf-8"))["status"] == cpc.STATUS_OK


def test_cp1252_console_subprocess_still_produces_json_and_exit_zero(tmp_path):
    """End to end: the measured crash exited 1 (the findings code) and wrote no JSON."""
    if _make_combining_fixture(tmp_path) is None:
        pytest.skip("filesystem will not store a combining-character filename unchanged")
    out = tmp_path / "out.json"
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    env.pop("PYTHONUTF8", None)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path), "--json", str(out)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert result.returncode == cpc.EXIT_OK, result.stderr
    assert "UnicodeEncodeError" not in result.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == cpc.STATUS_OK and payload["counted"]["files"] >= 1


@pytest.mark.parametrize(("ceiling", "expected"), [(None, 0), ("10", 1)], ids=["clean", "finding"])
def test_strict_unreconfigurable_stream_preserves_the_exit_code(tmp_path, monkeypatch, ceiling, expected):
    """Both verdicts survive a console that cannot encode the path; the report is escaped, not lost."""
    if _make_combining_fixture(tmp_path) is None:
        pytest.skip("filesystem will not store a combining-character filename unchanged")
    out = tmp_path / "out.json"
    stream, buffer = _strict_cp1252_stream()
    monkeypatch.setattr(sys, "stdout", stream)
    argv = [str(tmp_path), "--json", str(out)] + (["--ceiling", ceiling] if ceiling else [])
    assert cpc.main(argv) == expected
    assert out.exists()
    written = buffer.getvalue().decode("cp1252")
    assert "cafe" in written and "\\u0301" in written


def test_console_handling_never_reconfigures_a_shared_stream(tmp_path, monkeypatch, capsys):
    """stdout.errors is unchanged (also under --quiet); StringIO still prints; stderr usage is escaped."""
    _tree(tmp_path)
    before = sys.stdout.errors
    assert cpc.main([str(tmp_path)]) == cpc.EXIT_OK
    assert cpc.main([str(tmp_path), "--quiet"]) == cpc.EXIT_OK
    assert sys.stdout.errors == before
    sink = io.StringIO()
    monkeypatch.setattr(sys, "stdout", sink)
    assert cpc.main([str(tmp_path)]) == cpc.EXIT_OK
    assert "OK:" in sink.getvalue()
    stream, buffer = _strict_cp1252_stream()
    monkeypatch.setattr(sys, "stderr", stream)
    assert cpc.main([str(tmp_path / "cafe\u0301")]) == 2
    assert "cafe" in buffer.getvalue().decode("cp1252")
    capsys.readouterr()


def test_console_safe_degrades_only_the_unencodable_part(tmp_path):
    stream, _ = _strict_cp1252_stream()
    assert not hasattr(stream, "encoding")
    assert cpc._console_safe("cafe\u0301", stream) == "cafe\\u0301"  # pylint: disable=protected-access
    with open(tmp_path / "c.txt", "w", encoding="cp1252") as handle:
        escaped = cpc._console_safe("caf\u00e9 \u0301", handle)  # pylint: disable=protected-access
    assert "caf\u00e9" in escaped and "\\u0301" in escaped
