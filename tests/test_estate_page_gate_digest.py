"""Tool-boundary tests for `tests/estate_page_gate_digest.py` (issue #562).

Scope, deliberately narrow: this file covers the ONE production consumer of `check_unit`'s direct
target-bearing helpers. The measurement itself is pinned by `tests/estate_page_gate_expected.json`
against a real estate; what is asserted here is the boundary contract that estate run depends on -
a staged unit whose package boundary cannot be established must not produce a clean digest, and no
supplied path component may travel out of the refusal.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import estate_page_gate_digest as epgd  # noqa: E402  # pylint: disable=wrong-import-position

UNIT_LUID = "adc431bb-aeeb-43fe-8ecb-092d4bae8bfa"


def _write_unit(pbip_unit: Path, specs: Path, name: str) -> None:
    """One engine `pbip/<unit>` plus its parsed spec: a single dashboard page carrying one visual."""
    page = pbip_unit / "Book.Report" / "definition" / "pages" / "p1"
    (page / "visuals" / "v0").mkdir(parents=True)
    (page / "visuals" / "v0" / "visual.json").write_text(json.dumps({"name": "v0"}), encoding="utf-8")
    (page / "page.json").write_text(
        json.dumps({"name": "p1", "displayName": "Revenue", "width": 1600, "height": 900}), encoding="utf-8"
    )
    (page.parent / "pages.json").write_text(json.dumps({"pageOrder": ["p1"]}), encoding="utf-8")
    specs.mkdir(parents=True, exist_ok=True)
    (specs / f"{name}.json").write_text(
        json.dumps(
            {
                "source": {"file_name": f"{UNIT_LUID}_Book.twbx"},
                "dashboards": [{"id": "dash.0", "name": "Revenue"}],
                "worksheets": [],
            }
        ),
        encoding="utf-8",
    )


def _write_oracle(oracle: Path) -> None:
    """A shared Tableau-Server capture with one certified render + data leg for `Revenue`."""
    (oracle / "images").mkdir(parents=True, exist_ok=True)
    (oracle / "data").mkdir(parents=True, exist_ok=True)
    (oracle / "images" / "Revenue__0.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 64)
    (oracle / "data" / "Revenue__0.csv").write_text("a\n1\n", encoding="utf-8")
    (oracle / "oracle-manifest.json").write_text(
        json.dumps(
            {
                "views": [
                    {
                        "view_name": "Revenue",
                        "view_type": "dashboard",
                        "workbook_luid": UNIT_LUID,
                        "data": {
                            "status": "ok",
                            "certification": "certified",
                            "path": "data/Revenue__0.csv",
                            "row_count": 1,
                        },
                        "image": {"status": "ok", "path": "images/Revenue__0.png"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _estate(tmp_path: Path, unit_name: str = "Unit") -> tuple[Path, Path, Path]:
    bundle = tmp_path / "bundle"
    specs = tmp_path / "specs"
    oracle = tmp_path / "oracle"
    _write_unit(bundle / "pbip" / unit_name, specs, unit_name)
    (bundle / "handover").mkdir(parents=True, exist_ok=True)
    _write_oracle(oracle)
    return bundle, specs, oracle


def _run(bundle: Path, specs: Path, oracle: Path, work: Path, out: Path) -> subprocess.CompletedProcess[str]:
    """Run the tool exactly as documented, as a subprocess, so stderr is a real channel."""
    return subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tests" / "estate_page_gate_digest.py"),
            "--bundle",
            str(bundle),
            "--specs",
            str(specs),
            "--oracle",
            str(oracle),
            "--work",
            str(work),
            "--json",
            str(out),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_safe_staged_estate_measures_exactly_what_it_measured_before(tmp_path: Path) -> None:
    """The positive control: an ordinary staged unit's summary and digest are pinned literally.

    Pinned as a whole dict rather than field-by-field, because the claim being protected is that the
    direct-helper guard changed NOTHING for a safe target - a summary that merely still had the right
    shape would not establish it. The four catching helpers all run here: `load_exemptions`,
    `page_expectation` and `check_page_parity` through the verdict, `check_oracle_coverage` twice.

    ⚠️ Scope: `oracle_layer_facts` is deliberately NOT exercised. Its synthetic kind-less probe
    passes `workbook=None` into `WorkbookIdentity.attribute`, which raises `AttributeError` on the
    current `object_identity` - a pre-existing defect unrelated to this boundary and outside this
    change's closed surface, reproduced in the PR report rather than fixed here.
    """
    bundle, specs, oracle = _estate(tmp_path)
    units, unstaged = epgd.stage(bundle, specs, tmp_path / "work")

    summary = epgd.measure(units, oracle)

    assert unstaged == []
    assert summary == {
        "units": 1,
        "verdicts": {"PASS": 1},
        "dispositions": {},
        "blank_pages": 0,
        "unaccounted_extra_pages": 0,
        "contested_names": 0,
        "disagreements": [],
    }
    assert epgd.digest(summary) == "8e2866d250bd56b0b4b7a0705ff34b162bc3ca1f1ff59fd9f245dd9bc2c39934"


def test_a_staged_unit_whose_boundary_is_unproven_exits_unmeasurable_without_leaking_a_component(
    tmp_path: Path,
) -> None:
    """Kills: removing the tool's typed-exception catch, and any leak through stderr.

    Staged into a `packages/` directory - the real shape an operator produces by pointing `--work` at
    a run's package root - every staged unit is lexically package-shaped with no
    `package-manifest.json`, so its boundary is unproven. A measurement that could not be made must
    not report a clean digest, and the message is a CONSTANT plus the classifier's stable code: the
    unit here is named for a customer, which is exactly the component that must not travel.
    """
    bundle, specs, oracle = _estate(tmp_path, unit_name="Contoso-Secret")
    work = tmp_path / "run" / "packages"
    out = tmp_path / "result.json"

    completed = _run(bundle, specs, oracle, work, out)

    assert completed.returncode == epgd.EXIT_UNMEASURABLE, completed.stdout + completed.stderr
    assert epgd.REFUSED_BOUNDARY_MESSAGE in completed.stderr
    assert not out.exists(), "an unmeasurable run must not write a digest"
    everywhere = completed.stdout + completed.stderr
    for supplied in (str(work), str(bundle), str(tmp_path), "Contoso-Secret"):
        assert supplied not in everywhere, supplied
    assert "Traceback" not in everywhere


def test_the_tool_catches_only_the_boundary_refusal_and_not_every_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kills: broadening the catch to `Exception`.

    A blanket catch would turn any unrelated failure in the oracle layer into a tidy exit 3 - the
    unassessable-collapsing-into-clean shape this whole gate exists to remove, inverted. An unrelated
    error must still propagate.
    """
    bundle, specs, oracle = _estate(tmp_path)
    monkeypatch.chdir(tmp_path)

    def unrelated(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("unrelated")

    monkeypatch.setattr(epgd, "oracle_layer_facts", unrelated)

    with pytest.raises(RuntimeError, match="unrelated"):
        epgd.main(
            [
                "--bundle",
                str(bundle),
                "--specs",
                str(specs),
                "--oracle",
                str(oracle),
                "--work",
                str(tmp_path / "work"),
            ]
        )
