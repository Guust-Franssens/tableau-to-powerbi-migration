"""Tests for scripts/disarm_credential_gate.py: parser seam, injected-runner walk, one real ACL."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import disarm_credential_gate as dg  # noqa: E402

ME = "someuser"
GATE = "(OI)(CI)(DENY)(WD,AD,WA)"
DENY_ARG = "(OI)(CI)(WD,AD,WA)"  # what the gate passed to icacls /deny
INHERITED_TAIL = [
    "BUILTIN\\Administrators:(I)(OI)(CI)(F)",
    "NT AUTHORITY\\SYSTEM:(I)(OI)(CI)(F)",
    "NT AUTHORITY\\Authenticated Users:(I)(OI)(CI)(IO)(M)",
    "",
    "Successfully processed 1 files; Failed processing 0 files",
]


def listing(target: str, *aces: str) -> str:
    """Shape real `icacls` output: target echoed before the first ACE, the rest indented."""
    lines = [*aces, *INHERITED_TAIL]
    pad = " " * (len(target) + 1)
    return "\n".join([f"{target} {lines[0]}", *(pad + line if line else line for line in lines[1:])])


# --- (a) the ACE-line parser seam -----------------------------------------------------------------


@pytest.mark.parametrize("principal", [f"DOMAIN\\{ME}", ME, f"domain\\{ME.upper()}"])
def test_explicit_gate_deny_for_current_user_is_armed(principal: str) -> None:
    target = "C:\\runs\\a b (1)\\fabric"
    assert dg.classify(listing(target, f"{principal}:{GATE}"), target, ME) == "armed"


@pytest.mark.parametrize(
    "ace",
    [
        f"DOMAIN\\{ME}:(I)(OI)(CI)(DENY)(WD,AD,WA)",  # inherited from a parent anchor
        f"DOMAIN\\other:{GATE}",  # another user
        f"DOMAIN\\{ME}x:{GATE}",  # name that merely starts with ours
        f"DOMAIN\\x{ME}:{GATE}",  # name that merely ends with ours
        f"DOMAIN\\{ME}:(OI)(CI)(WD,AD,WA)",  # ALLOW of the same shape
        f"DOMAIN\\{ME}:(OI)(CI)(DENY)(WD,AD)",  # fewer rights
        f"DOMAIN\\{ME}:(OI)(CI)(DENY)(WD,AD,WA,DE)",  # more rights
        f"DOMAIN\\{ME}:(OI)(CI)(DENY)(W)",  # generic write, not the gate set
        f"DOMAIN\\{ME}:(OI)(DENY)(WD,AD,WA)",  # missing CI
        f"DOMAIN\\{ME}:(CI)(DENY)(WD,AD,WA)",  # missing OI
        f"DOMAIN\\{ME}:(OI)(CI)(NP)(DENY)(WD,AD,WA)",  # extra propagation flag
        f"DOMAIN\\{ME}:(OI)(CI)(IO)(DENY)(WD,AD,WA)",  # inherit-only
    ],
)
def test_non_gate_aces_are_clean(ace: str) -> None:
    target = "C:\\runs\\fabric"
    assert dg.classify(listing(target, ace), target, ME) == "clean"


def test_domain_qualified_user_argument_requires_exact_principal() -> None:
    target = "C:\\runs\\fabric"
    assert dg.classify(listing(target, f"DOMAIN\\{ME}:{GATE}"), target, f"DOMAIN\\{ME}") == "armed"
    assert dg.classify(listing(target, f"OTHER\\{ME}:{GATE}"), target, f"DOMAIN\\{ME}") == "clean"


def test_empty_user_never_matches() -> None:
    assert not dg.is_current_user("DOMAIN\\", "")
    assert not dg.is_current_user("someone", "")


def test_other_principals_explicit_aces_are_tolerated() -> None:
    target = "C:\\runs\\fabric"
    text = listing(target, "BUILTIN\\Users:(OI)(CI)(RX)", f"DOMAIN\\other:{GATE}", f"DOMAIN\\{ME}:{GATE}")
    assert dg.classify(text, target, ME) == "armed"


def test_second_explicit_deny_of_ours_is_a_conflict() -> None:
    target = "C:\\runs\\fabric"
    text = listing(target, f"DOMAIN\\{ME}:(DENY)(DE)", f"DOMAIN\\{ME}:{GATE}")
    assert dg.classify(text, target, ME) == "conflict"


def test_inherited_extra_deny_is_not_a_conflict() -> None:
    target = "C:\\runs\\fabric"
    text = listing(target, f"DOMAIN\\{ME}:{GATE}", f"DOMAIN\\{ME}:(I)(DENY)(DE)")
    assert dg.classify(text, target, ME) == "armed"


# --- (b) the walk and the apply/dry-run decisions, with an injected runner --------------------------


class FakeIcacls:
    """In-memory icacls: per-path listings, call log, optional failures."""

    def __init__(self, armed: set[Path], **listings: str) -> None:
        self.armed = {str(p) for p in armed}
        self.extra = listings
        self.calls: list[list[str]] = []
        self.remove_code = 0
        self.remove_works = True
        self.fail_listing: set[str] = set()

    def __call__(self, args: list[str]) -> tuple[int, str]:
        self.calls.append(list(args))
        target = args[0]
        if args[1:] == ["/remove:d", ME]:
            if self.remove_code == 0 and self.remove_works:
                self.armed.discard(target)
            return self.remove_code, "processed file"
        assert len(args) == 1, f"unexpected icacls call {args}"
        if target in self.fail_listing:
            return 5, "Access is denied."
        if target in self.extra:
            return 0, self.extra[target]
        ace = f"DOMAIN\\{ME}:{GATE}" if target in self.armed else "BUILTIN\\Users:(I)(OI)(CI)(RX)"
        return 0, listing(target, ace)

    @property
    def removals(self) -> list[str]:
        return [call[0] for call in self.calls if "/remove:d" in call]


@pytest.fixture(name="tree")
def tree_fixture(tmp_path: Path) -> dict[str, Path]:
    paths = {
        "fabric": tmp_path / "run" / "fabric",
        "pbip": tmp_path / "run" / "pbip",
        "model": tmp_path / "run" / "Sales.SemanticModel",
        "report": tmp_path / "run" / "unit" / "Sales.Report",
        "plain": tmp_path / "run" / "notes",
        "nested": tmp_path / "run" / "fabric" / "inner" / "pbip",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "run" / ".credential-gate-BLOCKED.json").write_text("{}", encoding="utf-8")
    return paths


@pytest.fixture(autouse=True)
def _pretend_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The injected-runner tests exercise the Windows path on any host."""
    monkeypatch.setattr(dg, "is_windows", lambda: True)


def _run(root: Path, runner: FakeIcacls, *, apply: bool, out: list[str] | None = None) -> int:
    sink = out if out is not None else []
    return dg.disarm(root, apply=apply, runner=runner, user=ME, out=sink.append)


def test_dry_run_lists_armed_anchors_and_never_removes(tree: dict[str, Path], tmp_path: Path) -> None:
    runner = FakeIcacls({tree["fabric"], tree["model"], tree["report"], tree["nested"], tree["plain"]})
    out: list[str] = []
    assert _run(tmp_path, runner, apply=False, out=out) == dg.EXIT_ARMED
    assert runner.removals == []
    assert all(len(call) == 1 for call in runner.calls)
    listed = {line.split(None, 1)[1] for line in out if line.startswith("ARMED")}
    assert listed == {str(tree[key]) for key in ("fabric", "model", "report", "nested")}
    assert str(tree["plain"]) not in {call[0] for call in runner.calls}, "non-anchor folders are not queried"


def test_dry_run_on_a_clean_tree_exits_zero(tree: dict[str, Path], tmp_path: Path) -> None:
    runner = FakeIcacls(set())
    assert _run(tmp_path, runner, apply=False) == dg.EXIT_CLEAN
    assert runner.removals == []


def test_apply_removes_only_armed_anchors_and_is_idempotent(tree: dict[str, Path], tmp_path: Path) -> None:
    armed = {tree["fabric"], tree["report"], tree["nested"]}
    runner = FakeIcacls(armed | {tree["plain"]})
    assert _run(tmp_path, runner, apply=True) == dg.EXIT_CLEAN
    assert sorted(runner.removals) == sorted(str(p) for p in armed)
    removal_calls = sorted(tuple(call) for call in runner.calls if "/remove:d" in call)
    assert removal_calls == sorted((str(p), "/remove:d", ME) for p in armed)
    runner.calls.clear()
    assert _run(tmp_path, runner, apply=True) == dg.EXIT_CLEAN
    assert runner.removals == []


def test_apply_exits_one_when_icacls_remove_fails(tree: dict[str, Path], tmp_path: Path) -> None:
    runner = FakeIcacls({tree["fabric"]})
    runner.remove_code = 5
    assert _run(tmp_path, runner, apply=True) == dg.EXIT_FAILED


def test_apply_exits_one_when_the_deny_survives_a_zero_exit(tree: dict[str, Path], tmp_path: Path) -> None:
    runner = FakeIcacls({tree["fabric"]})
    runner.remove_works = False
    assert _run(tmp_path, runner, apply=True) == dg.EXIT_FAILED


def test_conflicting_anchor_is_skipped_not_removed(tree: dict[str, Path], tmp_path: Path) -> None:
    target = str(tree["model"])
    runner = FakeIcacls(
        {tree["fabric"]}, **{target: listing(target, f"DOMAIN\\{ME}:(DENY)(DE)", f"DOMAIN\\{ME}:{GATE}")}
    )
    assert _run(tmp_path, runner, apply=True) == dg.EXIT_FAILED
    assert runner.removals == [str(tree["fabric"])]


def test_unreadable_acl_exits_one(tree: dict[str, Path], tmp_path: Path) -> None:
    runner = FakeIcacls(set())
    runner.fail_listing.add(str(tree["pbip"]))
    assert _run(tmp_path, runner, apply=False) == dg.EXIT_FAILED


def test_non_windows_is_a_no_op(tree: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dg, "is_windows", lambda: False)
    runner = FakeIcacls({tree["fabric"]})
    out: list[str] = []
    assert dg.disarm(tmp_path, apply=True, runner=runner, user=ME, out=out.append) == dg.EXIT_CLEAN
    assert runner.calls == []
    assert "nothing to do" in out[0]


def test_unknown_user_or_missing_root_exits_one(tree: dict[str, Path], tmp_path: Path) -> None:
    runner = FakeIcacls({tree["fabric"]})
    assert dg.disarm(tmp_path, apply=True, runner=runner, user="", out=[].append) == dg.EXIT_FAILED
    assert _run(tmp_path / "missing", runner, apply=True) == dg.EXIT_FAILED
    assert runner.calls == []


def test_root_that_is_itself_an_anchor_is_included(tree: dict[str, Path]) -> None:
    runner = FakeIcacls({tree["fabric"]})
    assert _run(tree["fabric"], runner, apply=True) == dg.EXIT_CLEAN
    assert str(tree["fabric"]) in runner.removals


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows reparse point")
def test_walk_never_enters_a_junction(tmp_path: Path) -> None:
    outside = tmp_path / "outside" / "fabric"
    outside.mkdir(parents=True)
    root = tmp_path / "root"
    root.mkdir()
    link = root / "linked"
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside.parent)], capture_output=True, check=False
    )
    assert made.returncode == 0, made.stderr
    try:
        runner = FakeIcacls({link / "fabric", outside})
        assert _run(root, runner, apply=True) == dg.EXIT_CLEAN
        assert runner.calls == []
    finally:
        os.rmdir(link)


# --- (c) one real ACL, Windows only ------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="the gate only ever set ACLs on Windows")
def test_real_gate_deny_is_removed_and_writes_work_again(tmp_path: Path) -> None:
    user = os.environ["USERNAME"]
    anchor = tmp_path / "run" / "fabric"
    anchor.mkdir(parents=True)
    probe = anchor / "written.txt"
    applied = subprocess.run(["icacls", str(anchor), "/deny", f"{user}:{DENY_ARG}"], check=False)
    try:
        assert applied.returncode == 0
        with pytest.raises(PermissionError):
            probe.write_text("x", encoding="utf-8")

        out: list[str] = []
        assert dg.disarm(tmp_path, apply=False, out=out.append) == dg.EXIT_ARMED
        with pytest.raises(PermissionError):
            probe.write_text("x", encoding="utf-8")

        assert dg.disarm(tmp_path, apply=True, out=out.append) == dg.EXIT_CLEAN
        code, text = dg.run_icacls([str(anchor)])
        assert code == 0
        assert dg.classify(text, str(anchor), user) == "clean"
        probe.write_text("x", encoding="utf-8")
        assert probe.read_text(encoding="utf-8") == "x"

        assert dg.disarm(tmp_path, apply=True, out=out.append) == dg.EXIT_CLEAN
    finally:
        subprocess.run(["icacls", str(anchor), "/remove:d", user], capture_output=True, check=False)
