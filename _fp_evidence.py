"""
purpose: false-positive evidence for issue #258 - how much of the committed example corpus the new
         table-agreement check actually EXERCISES, and what (if anything) it now says.
usage:   python _fp_evidence.py            (scratch, deleted before commit)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

import check_field_bindings as cfb  # noqa: E402

REPO = Path(__file__).resolve().parent
TOTALS = {"visuals": 0, "multi": 0, "exempt": 0, "findings": 0}

for report_dir in sorted(REPO.glob("examples/*/fabric/*.Report")):
    model_dir = cfb.model_for_report(report_dir)
    if model_dir is None:
        continue
    model = cfb.parse_model(model_dir)
    queries = cfb.iter_visual_queries(report_dir)
    multi = 0
    exempt = 0
    findings = []
    for query in queries:
        grouped = cfb._grouping_tables(model, query)  # pylint: disable=protected-access
        raw = {r.entity for r in query.refs if r.kind != "Measure"}
        if len(raw) > len(grouped):
            exempt += 1
        if len(grouped) >= 2:
            multi += 1
        one = cfb.check_visual_coherence(model, query)
        if one:
            findings.append(one)
    TOTALS["visuals"] += len(queries)
    TOTALS["multi"] += multi
    TOTALS["exempt"] += exempt
    TOTALS["findings"] += len(findings)
    print(
        f"{report_dir.parent.parent.name:32s} tables={len(model.tables):3d} "
        f"rels={len(model.relationships):2d} exempt_tables={len(model.detached_ok):2d} "
        f"visuals={len(queries):3d} multi_table={multi:3d} exempt_visuals={exempt:3d} "
        f"FINDINGS={len(findings)}"
    )
    for one in findings:
        print(f"    {one['detail']}  fields={one['fields']}  visual={one['visual_type']}")

print(f"\nTOTAL {TOTALS}")
