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

# `e` + COMBINING ACUTE ACCENT. A perfectly ordinary filename that cp1252 cannot encode, so it
# reproduces the console hazard WITHOUT being an invalid or unrepresentable name on any filesystem
# under test. Deliberately used only in a fixture's LEAF name - never in a directory the test itself
# has to print - so the test cannot become the thing it is testing.
COMBINING_NAME = "cafe\u0301.json"


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
    assert "observed refusal at 260" in text and "refuses 248" in text


# --------------------------------------------------------------------------------------------
# HIGH 1 - Desktop counts UTF-16 code units; Python's len() counts code points. Every non-BMP
# character is 1 here and 2 there, so a code-point measurement lets an over-long path through.
# --------------------------------------------------------------------------------------------

ASTRAL = "\U0001f600"  # one code point, TWO UTF-16 code units


def test_lengths_are_measured_in_utf16_code_units_not_code_points(tmp_path):
    target = _tree(tmp_path, filename=f"v{ASTRAL}{ASTRAL}.json")
    records, _ = cpc.collect(tmp_path)
    record = next(r for r in records if r["path"] == str(target))
    assert record["length"] == len(str(target)) + 2, "two astral chars cost two EXTRA UTF-16 units"
    assert record["length"] == len(str(target).encode("utf-16-le")) // 2


def test_astral_path_over_the_utf16_ceiling_is_a_finding_even_though_len_says_it_fits(tmp_path):
    target = _tree(tmp_path, filename=f"v{ASTRAL}.json")
    code_points = len(str(target))
    # A ceiling exactly at the code-point length: clean under the old measurement, over under UTF-16.
    report = cpc.scan(tmp_path, cpc.Limits(file_ceiling=code_points, dir_ceiling=10_000))
    assert report["status"] == cpc.STATUS_OVER_CEILING
    assert report["counted"]["over_ceiling"] == 1
    assert report["worst_offenders"][0]["length"] == code_points + 1


def test_utf16_helper_matches_dotnet_string_length_semantics():
    assert cpc._utf16_len("abc") == 3  # pylint: disable=protected-access
    assert cpc._utf16_len(ASTRAL) == 2  # pylint: disable=protected-access
    assert cpc._utf16_len("e\u0301") == 2  # pylint: disable=protected-access  # combining mark


# --------------------------------------------------------------------------------------------
# HIGH 1 (second half) - a name UTF-16 cannot represent must be UNKNOWN, never clean. `os.walk`
# hands back lone surrogates for undecodable POSIX filenames under surrogateescape.
# --------------------------------------------------------------------------------------------


def test_surrogate_name_is_unknown_never_clean(tmp_path, monkeypatch):
    real_walk = cpc.os.walk

    def walk_with_surrogate(top, onerror=None, **kwargs):
        for dirpath, dirnames, filenames in real_walk(top, onerror=onerror, **kwargs):
            yield dirpath, dirnames, filenames + ["bad\udcff.json"]

    _tree(tmp_path)
    monkeypatch.setattr(cpc.os, "walk", walk_with_surrogate)
    report = cpc.scan(tmp_path)
    assert report["status"] == cpc.STATUS_UNKNOWN_PATHS
    assert report["counted"]["unknown"] >= 1
    assert any("bad" in u["path"] for u in report["unknown_paths"])


def test_unknown_paths_survive_json_serialisation(tmp_path, monkeypatch):
    """A lone surrogate in the report must not blow up `--json`; it is escaped, not carried raw."""
    real_walk = cpc.os.walk

    def walk_with_surrogate(top, onerror=None, **kwargs):
        for dirpath, dirnames, filenames in real_walk(top, onerror=onerror, **kwargs):
            yield dirpath, dirnames, filenames + ["bad\udcff.json"]

    _tree(tmp_path)
    monkeypatch.setattr(cpc.os, "walk", walk_with_surrogate)
    payload = json.dumps(cpc.scan(tmp_path))
    assert "\\udcff" in payload or "\\xff" in payload


@pytest.mark.skipif(sys.platform == "win32", reason="Windows filenames cannot hold undecodable bytes")
def test_real_undecodable_posix_filename_is_unknown(tmp_path):
    (tmp_path / "visuals").mkdir()
    with open(os.path.join(bytes(tmp_path / "visuals"), b"bad\xff.json"), "wb") as handle:
        handle.write(b"{}")
    report = cpc.scan(tmp_path)
    assert report["status"] == cpc.STATUS_UNKNOWN_PATHS
    assert report["counted"]["unknown"] >= 1


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


def test_root_budget_is_the_minimum_across_both_ceilings(tmp_path):
    target = _tree(tmp_path)
    records, _ = cpc.collect(tmp_path)
    expected = min((cpc.DIR_CEILING if r["kind"] == cpc.KIND_DIR else cpc.FILE_CEILING) - r["tail"] for r in records)
    report = cpc.scan(tmp_path)
    assert report["root_budget"] == expected
    assert report["longest_tail"] == cpc._utf16_len(str(target)) - cpc._utf16_len(  # pylint: disable=protected-access
        str(tmp_path)
    )


def test_a_short_filename_makes_the_directory_rule_decide_the_budget(tmp_path):
    """HIGH 2 - the PBIR shape, not a contrived one: a blank page holds only `page.json`.

    `file_ceiling - longest_tail` overstates the budget by exactly 2 here, because the file tail is
    10 longer than its directory's while the ceilings differ by 12. At the overstated root length the
    page DIRECTORY would breach DIR_CEILING and the bundle would not open.
    """
    target = _tree(tmp_path, subdir="pages", filename="page.json")
    directory = target.parent
    file_tail = cpc._utf16_len(str(target)) - cpc._utf16_len(str(tmp_path))  # pylint: disable=protected-access
    dir_tail = cpc._utf16_len(str(directory)) - cpc._utf16_len(str(tmp_path))  # pylint: disable=protected-access

    report = cpc.scan(tmp_path)
    file_only_budget = cpc.FILE_CEILING - file_tail
    actual = cpc.DIR_CEILING - dir_tail
    assert actual == file_only_budget - 2
    assert report["root_budget"] == actual, "the stricter directory rule must win"
    assert report["root_budget_binding"]["kind"] == cpc.KIND_DIR


def test_min_root_budget_gate_uses_the_binding_ceiling_not_the_file_one(tmp_path, capsys):
    target = _tree(tmp_path, subdir="pages", filename="page.json")
    file_tail = cpc._utf16_len(str(target)) - cpc._utf16_len(str(tmp_path))  # pylint: disable=protected-access
    overstated = cpc.FILE_CEILING - file_tail
    # A file-only budget would call `overstated` achievable; the directory rule says it is not.
    assert cpc.main([str(tmp_path), "--min-root-budget", str(overstated)]) == 1
    capsys.readouterr()


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


# --------------------------------------------------------------------------------------------
# `--json` is a MACHINE-READABLE CONTRACT and must not depend on the console's codec.
#
# Measured before the fix: on a Windows cp1252 console, a tree containing a legal filename with a
# combining character made `print(render(report))` raise UnicodeEncodeError. The run exited 1 - which
# is also the "findings" code, so a consumer could not even tell a crash from a real finding - and
# because the artifact was written AFTER printing, `--json out.json` produced NO FILE AT ALL.
# --------------------------------------------------------------------------------------------


def _make_combining_fixture(root: Path) -> Path | None:
    """Create `<root>/visuals/cafe<combining-acute>.json`, or None if this filesystem refuses it."""
    directory = root / "visuals"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / COMBINING_NAME
    try:
        target.write_text("{}", encoding="utf-8")
    except (OSError, UnicodeError):  # pragma: no cover - only on an ASCII-locale filesystem
        return None
    # A filesystem may normalise the name (HFS+ does). Only proceed if it round-trips unchanged.
    return target if target.exists() and COMBINING_NAME in os.listdir(directory) else None


def test_json_is_written_before_the_console_is_touched(tmp_path):
    """The ordering fix, proven without needing a hostile console.

    If rendering runs first, an exception from it destroys the requested artifact. Making `render`
    raise is a deterministic stand-in for `UnicodeEncodeError` and works identically on every OS.
    """
    _tree(tmp_path)
    out = tmp_path / "reports" / "path-ceiling.json"

    def boom(_report):
        raise RuntimeError("console exploded")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cpc, "render", boom)
        with pytest.raises(RuntimeError):
            cpc.main([str(tmp_path), "--json", str(out)])

    assert out.exists(), "the JSON contract must survive a rendering failure"
    assert json.loads(out.read_text(encoding="utf-8"))["status"] == cpc.STATUS_OK


def test_cp1252_console_still_produces_json_and_the_expected_exit_code(tmp_path):
    """End-to-end, in a real subprocess with a cp1252 stdout - the shape that actually broke.

    Runs on Windows AND Linux: the codec is available on every platform, and the fixture name is
    valid on both. It is skipped only if the filesystem genuinely will not store the name, which is
    reported rather than silently passing.
    """
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

    assert result.returncode == cpc.EXIT_OK, f"expected a clean verdict, got {result.returncode}:\n{result.stderr}"
    assert "UnicodeEncodeError" not in result.stderr
    assert out.exists(), "`--json` produced no artifact on a console that cannot encode the path"
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == cpc.STATUS_OK
    assert payload["counted"]["files"] >= 1


def test_cp1252_console_preserves_a_finding_exit_code(tmp_path):
    """The degraded console must not change the verdict, only how it is spelled."""
    if _make_combining_fixture(tmp_path) is None:
        pytest.skip("filesystem will not store a combining-character filename unchanged")
    out = tmp_path / "out.json"

    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    env.pop("PYTHONUTF8", None)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path), "--ceiling", "10", "--json", str(out)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )

    assert result.returncode == cpc.EXIT_OVER_CEILING, result.stderr
    assert out.exists()
    assert json.loads(out.read_text(encoding="utf-8"))["status"] == cpc.STATUS_OVER_CEILING


def test_stdout_errors_is_unchanged_after_main(tmp_path, capsys):
    """The caller's stream is SHARED. Fixing our own output must not reconfigure it.

    Measured on the first attempt at this fix: a clean `--quiet` run returned 0 and left
    `sys.stdout.errors` switched from `surrogateescape` to `backslashreplace`, silently escaping an
    unrelated caller's later output. `--quiet` and a non-TTY are checked too, because the mutation
    happened there as well.
    """
    _tree(tmp_path)
    before = sys.stdout.errors
    assert cpc.main([str(tmp_path)]) == cpc.EXIT_OK
    assert sys.stdout.errors == before
    assert cpc.main([str(tmp_path), "--quiet"]) == cpc.EXIT_OK
    assert sys.stdout.errors == before
    capsys.readouterr()


def _strict_cp1252_stream() -> tuple[object, io.BytesIO]:
    """A stream that REALLY refuses non-cp1252 text and has no `reconfigure`.

    `io.StringIO` is the obvious stand-in and it is worthless here: it accepts every `str`, so it can
    only ever prove the happy path. Measured, `codecs.getwriter("cp1252")(...)` exposes no
    `.encoding` attribute either, so it also catches a fix that keys off `stream.encoding`.
    """
    buffer = io.BytesIO()
    return codecs.getwriter("cp1252")(buffer), buffer


@pytest.mark.parametrize(
    ("ceiling", "expected"),
    [(None, 0), ("10", 1)],
    ids=["clean-verdict", "finding-verdict"],
)
def test_strict_unreconfigurable_stream_preserves_the_exit_code(tmp_path, monkeypatch, ceiling, expected):
    """Both verdicts must survive a console that cannot encode the path.

    Before the fix this raised `UnicodeEncodeError` and the process exited 1 - which is also the
    findings code, so a consumer could not tell a crash from a real over-ceiling result.
    """
    if _make_combining_fixture(tmp_path) is None:
        pytest.skip("filesystem will not store a combining-character filename unchanged")
    out = tmp_path / "out.json"
    stream, buffer = _strict_cp1252_stream()
    monkeypatch.setattr(sys, "stdout", stream)

    argv = [str(tmp_path), "--json", str(out)] + (["--ceiling", ceiling] if ceiling else [])
    assert cpc.main(argv) == expected

    assert out.exists()
    written = buffer.getvalue().decode("cp1252")
    assert "cafe" in written, "the report must still be printed, merely escaped"
    assert "\\u0301" in written


def test_stream_without_an_encoding_attribute_still_prints(tmp_path, monkeypatch, capsys):
    """A stream that accepts everything (StringIO) must keep working - the no-op path."""
    _tree(tmp_path)
    sink = io.StringIO()
    monkeypatch.setattr(sys, "stdout", sink)
    assert cpc.main([str(tmp_path)]) == cpc.EXIT_OK
    assert "OK:" in sink.getvalue()
    capsys.readouterr()


def test_console_safe_escapes_to_ascii_when_the_stream_hides_its_encoding():
    stream, _ = _strict_cp1252_stream()
    assert not hasattr(stream, "encoding"), "this test exists because the attribute is absent"
    escaped = cpc._console_safe("cafe\u0301", stream)  # pylint: disable=protected-access
    assert escaped == "cafe\\u0301"
    escaped.encode("cp1252")  # must not raise


def test_console_safe_uses_the_stream_encoding_when_it_is_declared(tmp_path):
    with open(tmp_path / "c.txt", "w", encoding="cp1252") as handle:
        escaped = cpc._console_safe("caf\u00e9 \u0301", handle)  # pylint: disable=protected-access
    assert "caf\u00e9" in escaped, "cp1252 CAN encode e-acute; only the unencodable part degrades"
    assert "\\u0301" in escaped


def test_usage_error_on_an_unencodable_path_does_not_crash(tmp_path, monkeypatch, capsys):
    """The `not a directory` message carries caller-supplied paths and used a raw print too."""
    stream, buffer = _strict_cp1252_stream()
    monkeypatch.setattr(sys, "stderr", stream)
    assert cpc.main([str(tmp_path / "cafe\u0301")]) == 2
    assert "cafe" in buffer.getvalue().decode("cp1252")
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
