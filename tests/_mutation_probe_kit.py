"""Fixtures the running-total mutation harness's PLUGIN-TIME probes are written against (#218).

Underscore-prefixed so pytest never collects it: this is a library for
``tests/mutation_harness_running_total_axis.py``, not a test module.

Why it exists
-------------
Blind review found a mutation that referenced ``crta._DATE_TYPES`` after the symbol had moved to
``dax_grain``. The patch statement itself succeeded (it only *read* the attribute inside a function
body), the identity assertion passed, the patched function then raised ``AttributeError`` on every
call, pytest reported the expected test as ``FAILED``, and the harness scored ``CAUGHT`` -- **a named
test failure indistinguishable from a genuine catch, with the intended mutation never exercised.**

An identity assertion proves a patch was *applied*. Only calling the patched object proves it
*runs*, and only asserting a mutated-specific result proves it does what the table claims. Every
probe therefore CALLS the patched object and asserts an outcome the unmutated code does not
produce -- which is exactly the check that would have failed loudly on the stale symbol.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import check_running_total_axis as crta
import dax_grain as dg

ANCHOR = dg.ColumnRef("Orders", "Order_Date")
COARSE = dg.ColumnRef("Orders", "Order_Month")
DERIVED = dg.ColumnRef("Orders", "Order Month Label")

AS_OF = (
    "CALCULATE(SUM('Orders'[Sales]), FILTER(ALL('Orders'[Order_Date]), "
    "'Orders'[Order_Date] <= MAX('Orders'[Order_Date])))"
)
# A safe call (clears the coarse bin too) followed by a defective one (clears only the anchor).
TWO_AS_OF_CALLS = (
    "CALCULATE(SUM('Orders'[Sales]), FILTER(ALL('Orders'[Order_Date], 'Orders'[Order_Month]), "
    "'Orders'[Order_Date] <= MAX('Orders'[Order_Date]))) + " + AS_OF
)
TWO_WINDOWS = (
    "MAXX(WINDOW(1, ABS, 0, REL, ORDERBY('Orders'[Order_Date], ASC)), 1) + "
    "MAXX(WINDOW(1, ABS, 0, REL, ORDERBY('Orders'[Region], ASC)), 1)"
)
PARTITIONED_WINDOW = (
    "MAXX(WINDOW(1, ABS, 0, REL, ORDERBY('Orders'[Order_Date], ASC), PARTITIONBY('Orders'[Region])), 1)"
)


def facts() -> dg.ModelFacts:
    """Model facts with one derived text bin and one transitive chain, as the engine emits them."""
    built = dg.ModelFacts()
    built.calc_expressions[("orders", "order month label")] = "FORMAT('Orders'[Order_Date], \"yyyy-MM\")"
    built.calc_expressions[("orders", "order quarter no")] = "QUARTER('Orders'[Order_Date])"
    built.calc_expressions[("orders", "order quarter")] = "\"Q\" & 'Orders'[Order Quarter No]"
    return built


def cumulative(**overrides: Any) -> dg.Cumulative:
    """A blank `Cumulative` a classifier can fill in, or a pre-filled one for a judge."""
    base = dg.Cumulative(table="_Measures", name="Probe", shape="unknown", tmdl="t.tmdl", line=1)
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def as_of_call(cleared: list[dg.ColumnRef] | None = None) -> dg.AsOfCall:
    """One as-of restriction on `ANCHOR`, clearing whatever it is told to clear."""
    return dg.AsOfCall(compared=ANCHOR, cleared_columns=list(cleared or [ANCHOR]))


def field_ref(entity: str, prop: str, kind: str = "Column") -> crta.FieldRef:
    """One PBIR projection reference, as `check_field_bindings` would have produced it."""
    return crta.FieldRef(kind=kind, entity=entity, prop=prop, file=Path("visual.json"))


def visual(
    grouping: dict[str, list[crta.FieldRef]] | None = None,
    roles: dict[str, list[crta.FieldRef]] | None = None,
    visual_type: str = "lineChart",
) -> crta.VisualBinding:
    """A `VisualBinding` with its grouping set stated directly, bypassing the PBIR reader."""
    return crta.VisualBinding(
        file=Path("visual.json"),
        visual="probe",
        visual_type=visual_type,
        roles=roles if roles is not None else (grouping or {}),
        grouping=grouping or {},
    )


def aggregated_role(entity: str, prop: str) -> dict[str, Any]:
    """A role body whose ONLY projection is an `Aggregation` wrapping a column - 377 of these ship
    in the estate + `examples/` corpus, and every one wraps a `Column`."""
    return {
        "projections": [
            {
                "field": {
                    "Aggregation": {
                        "Expression": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}},
                        "Function": 3,
                    }
                },
                "queryRef": f"Max({entity}.{prop})",
            }
        ]
    }
