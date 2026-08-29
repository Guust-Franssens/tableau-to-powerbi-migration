r"""Tests for scripts/check_path_ceiling.py - the gate against a bundle a stock Windows box cannot open.

Two design choices here are load-bearing, and both exist because of how this check can go wrong.

**No test creates a path longer than 259 characters.** That would be the obvious way to exercise a
MAX_PATH check, and it is exactly the way to make CI red on the machines this issue is about: a
stock Windows runner (`LongPathsEnabled = 0`) cannot create such a path at all, so the fixture, not
the assertion, would fail. Every boundary test instead drives a *low* ceiling over a short tree -
`Limits(file_ceiling=<len of a real path>)` - which walks the identical comparison code with none of
the filesystem risk, and works the same on Linux.

That substitution buys a second, better property. The files in these fixtures are short, real, and
perfectly openable on the host running the tests, and the check still reports them over ceiling.
That IS the proof that the verdict is arithmetic rather than an "can I open this?" probe - which is
the whole reason issue #235 went unnoticed for so long on a machine with `LongPathsEnabled = 1`.

The shipped constants (259 / 247) are pinned by a separate, filesystem-free test, because they are a
measurement (see the module docstring of the script) and a silent edit to either is the single most
damaging change anyone could make to this file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_path_ceiling as cpc  # noqa: E402  # pylint: disable=wrong-import-position


def _tree(root: Path, *, filename: str = "visual.json", subdir: str = "visuals") -> Path:
    """Create <root>/<subdir>/<filename> and return the file path."""
    directory = root / subdir
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    target.write_text("{}", encoding="utf-8")
    return target


# --------------------------------------------------------------------------------------------
# The measured constants. These are the answer to issue #235's "is Power BI Desktop long-path
# aware?" and come verbatim from the PathTooLongException Desktop raised on a 268-char visual.json.
# --------------------------------------------------------------------------------------------


def test_shipped_ceilings_are_the_measured_desktop_limits():
    assert cpc.FILE_CEILING == 259, "Desktop: 'fully qualified file name must be less than 260 characters'"
    assert cpc.DIR_CEILING == 247, "Desktop: 'the directory name must be less than 248 characters'"


def test_default_limits_use_the_shipped_ceilings():
    assert cpc.DEFAULT_LIMITS.file_ceiling == cpc.FILE_CEILING
    assert cpc.DEFAULT_LIMITS.dir_ceiling == cpc.DIR_CEILING
    assert cpc.DEFAULT_LIMITS.min_root_budget is None, "the portability gate must be opt-in"


# --------------------------------------------------------------------------------------------
# Boundary. A path exactly AT the ceiling is legal; one character over is a finding.
# --------------------------------------------------------------------------------------------


def test_path_exactly_at_the_ceiling_is_clean(tmp_path):
    target = _tree(tmp_path)
    report = cpc.scan(tmp_path, cpc.Limits(file_ceiling=len(str(target)), dir_ceiling=len(str(target))))
    assert report["status"] == cpc.STATUS_OK
    assert report["counted"]["over_ceiling"] == 0


def test_path_one_char_over_the_ceiling_is_a_finding(tmp_path):
    target = _tree(tmp_path)
    report = cpc.scan(tmp_path, cpc.Limits(file_ceiling=len(str(target)) - 1, dir_ceiling=len(str(target))))
    assert report["status"] == cpc.STATUS_OVER_CEILING
    assert report["counted"]["over_ceiling"] == 1
    assert report["worst_offenders"][0]["path"] == str(target)
    assert report["worst_offenders"][0]["kind"] == cpc.KIND_FILE


def test_offenders_are_named_not_merely_counted(tmp_path):
    # Issue #235 asked for the file to be NAMED, because a count alone is not actionable.
    for name in ("a.json", "b.json", "c.json"):
        _tree(tmp_path, filename=name)
    report = cpc.scan(tmp_path, cpc.Limits(file_ceiling=len(str(tmp_path)), dir_ceiling=10_000))
    assert report["counted"]["over_ceiling"] == 3
    named = {Path(o["path"]).name for o in report["worst_offenders"]}
    assert named == {"a.json", "b.json", "c.json"}


# --------------------------------------------------------------------------------------------
# The directory rule is separate and stricter, and a file-only check would miss it.
# --------------------------------------------------------------------------------------------


def test_directory_over_its_own_ceiling_is_a_finding_even_when_every_file_fits(tmp_path):
    target = _tree(tmp_path, filename="p.json")
    directory = target.parent
    report = cpc.scan(
        tmp_path,
        cpc.Limits(file_ceiling=len(str(target)), dir_ceiling=len(str(directory)) - 1),
    )
    assert report["status"] == cpc.STATUS_OVER_CEILING
    kinds = {o["kind"] for o in report["worst_offenders"]}
    assert kinds == {cpc.KIND_DIR}, "only the directory should be flagged; the file fits"


def test_directory_ceiling_is_applied_to_directories_not_the_file_ceiling(tmp_path):
    target = _tree(tmp_path, filename="p.json")
    directory = target.parent
    report = cpc.scan(
        tmp_path,
        cpc.Limits(file_ceiling=len(str(directory)) - 1, dir_ceiling=len(str(target)) + 100),
    )
    flagged = {o["kind"] for o in report["worst_offenders"]}
    assert cpc.KIND_DIR not in flagged, "a generous dir ceiling must not be overridden by the file ceiling"
    assert flagged == {cpc.KIND_FILE}


# --------------------------------------------------------------------------------------------
# The verdict must NOT inherit the host. This is the defect issue #235 is actually about.
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("registry_value", [0, 1, None])
def test_verdict_is_identical_whatever_the_host_registry_says(tmp_path, monkeypatch, registry_value):
    target = _tree(tmp_path)
    monkeypatch.setattr(cpc, "read_long_paths_enabled", lambda: registry_value)
    report = cpc.scan(tmp_path, cpc.Limits(file_ceiling=len(str(target)) - 1, dir_ceiling=10_000))
    assert report["status"] == cpc.STATUS_OVER_CEILING
    assert report["counted"]["over_ceiling"] == 1
    assert report["host_long_paths_enabled"] == registry_value


def test_registry_value_is_reported_so_a_reader_is_never_fooled(tmp_path, monkeypatch):
    _tree(tmp_path)
    monkeypatch.setattr(cpc, "read_long_paths_enabled", lambda: 1)
    text = cpc.render(cpc.scan(tmp_path))
    assert "LongPathsEnabled" in text
    assert "NON-DEFAULT" in text
    # The correction that matters: Desktop enforces its own limit in managed code (EnsureNotLong),
    # so the registry opt-in never rescues it. A reader must not infer "we are protected".
    assert "does NOT affect Power BI Desktop" in text


def test_both_ceilings_are_reported_separately_and_unambiguously(tmp_path):
    """#235 asked for the file AND directory limits, stated so neither can be misread.

    259/247 (longest legal) and 260/248 (first refused) are the same fact in two framings, and the
    issue was written in the second. Printing both is what stops an off-by-one argument.
    """
    _tree(tmp_path)
    text = cpc.render(cpc.scan(tmp_path))
    assert "file <= 259" in text and "directory <= 247" in text
    assert "refuses 260" in text and "refuses 248" in text


# --------------------------------------------------------------------------------------------
# The SECOND opt-in. One being set while the other was not is how #235 stayed invisible, so both
# are reported - and neither may reach the verdict.
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("git_value", [True, False, None])
def test_git_long_paths_is_reported_and_never_changes_the_verdict(tmp_path, monkeypatch, git_value):
    target = _tree(tmp_path)
    monkeypatch.setattr(cpc, "read_git_long_paths", lambda _root=None: git_value)
    report = cpc.scan(tmp_path, cpc.Limits(file_ceiling=len(str(target)) - 1, dir_ceiling=10_000))
    assert report["host_git_long_paths"] is git_value
    assert report["status"] == cpc.STATUS_OVER_CEILING
    assert report["counted"]["over_ceiling"] == 1
    assert "core.longpaths" in cpc.render(report)


@pytest.mark.parametrize("git_value", [True, False, None])
def test_git_long_paths_cannot_rescue_an_unknown_path_either(tmp_path, monkeypatch, git_value):
    """The `unknown` branch needs its own proof, and mutation testing is why.

    A mutant that made the unknown branch depend on `read_git_long_paths` SURVIVED the first pass:
    on this machine `core.longpaths` is unset, so the extra condition was inert and the covering
    test only exercised the over-ceiling branch. It is a live defect on any machine that HAS set
    `core.longpaths=true` - exactly the "it works here" trap #235 is about.
    """
    _tree(tmp_path)
    monkeypatch.setattr(cpc, "read_git_long_paths", lambda _root=None: git_value)
    real_walk = cpc.os.walk

    def exploding_walk(top, onerror=None, **kwargs):
        if onerror is not None:
            onerror(PermissionError(13, "Permission denied"))
        yield from real_walk(top, onerror=onerror, **kwargs)

    monkeypatch.setattr(cpc.os, "walk", exploding_walk)
    report = cpc.scan(tmp_path)
    assert report["status"] == cpc.STATUS_UNKNOWN_PATHS
    assert report["counted"]["unknown"] == 1


def test_git_default_is_reported_as_the_silent_data_loss_risk(tmp_path, monkeypatch):
    """Measured: an overlong DIRECTORY makes `git add` skip its contents and still exit 0."""
    _tree(tmp_path)
    monkeypatch.setattr(cpc, "read_git_long_paths", lambda _root=None: False)
    text = cpc.render(cpc.scan(tmp_path))
    assert "git default" in text
    assert "silently drops" in text


def test_unknown_git_config_is_not_read_as_enabled(tmp_path, monkeypatch):
    _tree(tmp_path)
    monkeypatch.setattr(cpc, "read_git_long_paths", lambda _root=None: None)
    text = cpc.render(cpc.scan(tmp_path))
    assert "do not read as enabled" in text


def test_git_reader_treats_unset_as_false_not_unknown(tmp_path, monkeypatch):
    """`git config --get` exits 1 when unset; that is git's documented default, not an unknown."""

    class _Proc:  # pylint: disable=too-few-public-methods
        returncode = 1
        stdout = ""

    monkeypatch.setattr(cpc.subprocess, "run", lambda *a, **k: _Proc())
    assert cpc.read_git_long_paths(tmp_path) is False


def test_git_reader_returns_none_when_git_is_unavailable(tmp_path, monkeypatch):
    def _boom(*_a, **_k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(cpc.subprocess, "run", _boom)
    assert cpc.read_git_long_paths(tmp_path) is None


# --------------------------------------------------------------------------------------------
# The shipping question: a short root can hide an unshippable tail.
#
# Note the arithmetic, because it decides how these tests must be written: root_budget is
# `ceiling - tail`, and a tree is clean only when `ceiling >= root_len + tail`. So a CLEAN tree
# always has `root_budget >= root_len`. The advisory can therefore only fire for a bundle built at
# a SHORT root - a CI runner, `C:\b\`, a container - which is precisely the blind spot: such a
# bundle passes the absolute check and is still unshippable. `tmp_path` is ~80 characters, far above
# the 40-char advisory, so the threshold is moved rather than the fixture.
# --------------------------------------------------------------------------------------------


def test_tight_root_budget_is_flagged_even_when_every_absolute_path_is_clean(tmp_path, monkeypatch):
    _tree(tmp_path)
    clean = cpc.scan(tmp_path)
    assert clean["counted"]["over_ceiling"] == 0
    monkeypatch.setattr(cpc, "SHIPPING_ROOT_BUDGET_ADVISORY", clean["root_budget"] + 1)
    report = cpc.scan(tmp_path)
    assert report["counted"]["over_ceiling"] == 0, "the absolute check must still be clean"
    assert report["root_budget_is_tight"] is True
    assert "TIGHT ROOT BUDGET" in cpc.render(report)


def test_tight_root_budget_is_advisory_not_a_finding(tmp_path, monkeypatch):
    _tree(tmp_path)
    monkeypatch.setattr(cpc, "SHIPPING_ROOT_BUDGET_ADVISORY", cpc.FILE_CEILING)
    report = cpc.scan(tmp_path)
    assert report["root_budget_is_tight"] is True
    assert report["status"] == cpc.STATUS_OK, "advisory only - --min-root-budget is the gate"


def test_a_roomy_bundle_is_not_flagged_as_tight(tmp_path):
    _tree(tmp_path)
    report = cpc.scan(tmp_path)
    assert report["root_budget"] >= cpc.SHIPPING_ROOT_BUDGET_ADVISORY
    assert report["root_budget_is_tight"] is False
    assert "TIGHT ROOT BUDGET" not in cpc.render(report)


def test_shipping_advisory_is_documented_and_below_the_ceiling():
    assert 0 < cpc.SHIPPING_ROOT_BUDGET_ADVISORY < cpc.FILE_CEILING
    assert cpc.SHIPPING_ROOT_BUDGET_ADVISORY == 40, r"derived from C:\Users\<name>\Documents\ + one folder"


def test_over_ceiling_is_reported_for_files_this_host_can_open_perfectly_well(tmp_path):
    # The files below are short and readable right now. The check still calls them over ceiling,
    # which proves it measures the string rather than asking the OS.
    target = _tree(tmp_path)
    assert target.read_text(encoding="utf-8") == "{}"
    report = cpc.scan(tmp_path, cpc.Limits(file_ceiling=len(str(target)) - 1, dir_ceiling=10_000))
    assert report["status"] == cpc.STATUS_OVER_CEILING


@pytest.mark.skipif(sys.platform == "win32", reason="off-Windows behaviour of the registry read")
def test_registry_read_returns_none_off_windows():
    assert cpc.read_long_paths_enabled() is None


@pytest.mark.skipif(sys.platform != "win32", reason="reads the Windows registry")
def test_registry_read_returns_an_int_or_none_on_windows():
    value = cpc.read_long_paths_enabled()
    assert value is None or isinstance(value, int)


# --------------------------------------------------------------------------------------------
# Unassessable input never lands in the clean bucket.
# --------------------------------------------------------------------------------------------


def test_unreadable_directory_is_unknown_never_ok(tmp_path, monkeypatch):
    _tree(tmp_path)
    real_walk = cpc.os.walk

    def exploding_walk(top, onerror=None, **kwargs):
        error = PermissionError(13, "Permission denied")
        error.filename = str(Path(top) / "visuals")
        if onerror is not None:
            onerror(error)
        yield from real_walk(top, onerror=onerror, **kwargs)

    monkeypatch.setattr(cpc.os, "walk", exploding_walk)
    report = cpc.scan(tmp_path)
    assert report["status"] == cpc.STATUS_UNKNOWN_PATHS
    assert report["counted"]["unknown"] == 1
    assert "visuals" in report["unknown_paths"][0]["path"]


def test_findings_outrank_unknowns_because_they_are_actionable(tmp_path, monkeypatch):
    target = _tree(tmp_path)
    real_walk = cpc.os.walk

    def exploding_walk(top, onerror=None, **kwargs):
        if onerror is not None:
            onerror(PermissionError(13, "Permission denied"))
        yield from real_walk(top, onerror=onerror, **kwargs)

    monkeypatch.setattr(cpc.os, "walk", exploding_walk)
    report = cpc.scan(tmp_path, cpc.Limits(file_ceiling=len(str(target)) - 1, dir_ceiling=10_000))
    assert report["status"] == cpc.STATUS_OVER_CEILING
    assert report["counted"]["unknown"] == 1


def test_empty_target_cannot_be_judged_clean(tmp_path):
    report = cpc.scan(tmp_path)
    assert report["status"] == cpc.STATUS_NO_PATHS
    assert report["root_budget"] is None


# --------------------------------------------------------------------------------------------
# The portable number: tail length and the install-root budget it leaves.
# --------------------------------------------------------------------------------------------


def test_root_budget_is_the_ceiling_minus_the_longest_tail(tmp_path):
    target = _tree(tmp_path)
    tail = len(str(target)) - len(str(tmp_path))
    report = cpc.scan(tmp_path)
    assert report["longest_tail"] == tail
    assert report["root_budget"] == cpc.FILE_CEILING - tail


def test_tail_is_independent_of_where_the_tree_sits(tmp_path):
    shallow = tmp_path / "a"
    deep = tmp_path / "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    _tree(shallow)
    _tree(deep)
    assert cpc.scan(shallow)["longest_tail"] == cpc.scan(deep)["longest_tail"]
    assert cpc.scan(shallow)["root_budget"] == cpc.scan(deep)["root_budget"]


def test_min_root_budget_gate_fails_a_tree_that_cannot_be_relocated(tmp_path):
    target = _tree(tmp_path)
    tail = len(str(target)) - len(str(tmp_path))
    budget = cpc.FILE_CEILING - tail
    assert cpc.scan(tmp_path, cpc.Limits(min_root_budget=budget))["status"] == cpc.STATUS_OK
    assert cpc.scan(tmp_path, cpc.Limits(min_root_budget=budget + 1))["status"] == cpc.STATUS_OVER_CEILING


# --------------------------------------------------------------------------------------------
# Advisory band - reported, never a finding.
# --------------------------------------------------------------------------------------------


def test_near_ceiling_paths_are_advisory_and_do_not_fail(tmp_path):
    target = _tree(tmp_path)
    report = cpc.scan(
        tmp_path,
        cpc.Limits(file_ceiling=len(str(target)), dir_ceiling=10_000, warn_at=len(str(target)) - 1),
    )
    assert report["status"] == cpc.STATUS_OK
    assert report["counted"]["near_ceiling"] >= 1
    assert "advisory" in cpc.render(report)


# --------------------------------------------------------------------------------------------
# Exit-code ladder. Gates are judged by exit code, never by printed text - so the ladder itself is
# pinned to LITERALS here. Asserting `== cpc.EXIT_OVER_CEILING` would be self-referential: an edit
# that silently made the failure code 0 would move the assertion with it and nothing would notice.
# --------------------------------------------------------------------------------------------


def test_exit_code_ladder_matches_the_house_contract():
    assert cpc.EXIT_OK == 0
    assert cpc.EXIT_OVER_CEILING == 1
    assert cpc.EXIT_USAGE == 2
    assert cpc.EXIT_SKIPPED == 3, "3 = could not evaluate, per the repo's checkers"


def test_exit_ok_on_a_clean_tree(tmp_path, capsys):
    _tree(tmp_path)
    assert cpc.main([str(tmp_path)]) == 0
    capsys.readouterr()


def test_exit_over_ceiling_on_a_finding(tmp_path, capsys):
    target = _tree(tmp_path)
    assert cpc.main([str(tmp_path), "--ceiling", str(len(str(target)) - 1)]) == 1
    capsys.readouterr()


def test_exit_usage_when_the_target_is_not_a_directory(tmp_path, capsys):
    assert cpc.main([str(tmp_path / "nope")]) == 2
    capsys.readouterr()


def test_exit_skipped_when_nothing_could_be_measured(tmp_path, capsys):
    assert cpc.main([str(tmp_path)]) == 3
    capsys.readouterr()


def test_warn_only_reports_but_never_fails(tmp_path, capsys):
    target = _tree(tmp_path)
    argv = [str(tmp_path), "--ceiling", str(len(str(target)) - 1), "--warn-only"]
    assert cpc.main(argv) == 0
    assert "OVER CEILING" in capsys.readouterr().out


def test_min_root_budget_flag_fails_by_exit_code(tmp_path, capsys):
    target = _tree(tmp_path)
    budget = cpc.FILE_CEILING - (len(str(target)) - len(str(tmp_path)))
    assert cpc.main([str(tmp_path), "--min-root-budget", str(budget + 1)]) == 1
    assert cpc.main([str(tmp_path), "--min-root-budget", str(budget)]) == 0
    capsys.readouterr()


def test_json_report_is_written_and_machine_readable(tmp_path, capsys):
    target = _tree(tmp_path)
    out = tmp_path / "reports" / "path-ceiling.json"
    cpc.main([str(tmp_path), "--ceiling", str(len(str(target)) - 1), "--json", str(out)])
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == cpc.STATUS_OVER_CEILING
    assert payload["counted"]["over_ceiling"] == 1
    assert payload["file_ceiling"] == len(str(target)) - 1
    assert "host_long_paths_enabled" in payload
    capsys.readouterr()


def test_multiple_targets_are_all_reported_and_the_worst_decides_the_exit(tmp_path, capsys):
    clean = tmp_path / "clean"
    dirty = tmp_path / "dirty"
    _tree(clean)
    target = _tree(dirty)
    argv = [str(clean), str(dirty), "--ceiling", str(len(str(target)) - 1)]
    assert cpc.main(argv) == 1
    out = capsys.readouterr().out
    assert str(clean.resolve()) in out and str(dirty.resolve()) in out


def test_negative_threshold_is_rejected(tmp_path, capsys):
    _tree(tmp_path)
    with pytest.raises(SystemExit):
        cpc.main([str(tmp_path), "--ceiling", "-1"])
    capsys.readouterr()
