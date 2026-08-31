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
# A correct window beside an as-of on a column the visual does not project: two mechanisms in one
# measure, which the reader chain used to judge as one.
WINDOW_PLUS_AS_OF = (
    "MAXX(WINDOW(1, ABS, 0, REL, ORDERBY('Orders'[Order_Date], ASC)), 1) + "
    "CALCULATE(SUM('Orders'[Sales]), FILTER(ALL('Orders'[Order_Month]), "
    "'Orders'[Order_Month] <= MAX('Orders'[Order_Month])))"
)
# A safe period-to-date on the marked date table beside a defective one on the fact table.
TWO_PERIOD_TO_DATE = (
    "TOTALYTD(SUM('Orders'[Sales]), 'Date'[Date]) + CALCULATE(SUM('Orders'[Sales]), DATESYTD('Orders'[Order_Date]))"
)
# The reviewer's round-3 predicates, both orders. Semantically identical.
START_THEN_ASOF = "'Orders'[Order_Date] >= DATE(2024,1,1) && 'Orders'[Order_Date] <= MAX('Orders'[Order_Date])"
ASOF_THEN_START = "'Orders'[Order_Date] <= MAX('Orders'[Order_Date]) && 'Orders'[Order_Date] >= DATE(2024,1,1)"
UNRELATED_REMOVAL = "CALCULATE(MAX('Orders'[Order_Date]), REMOVEFILTERS('Orders'[Region]))"
WHOLE_TABLE_REMOVAL = "MAXX(ALL('Orders'), 'Orders'[Order_Date])"
# Round 4: the bound clears the VISUAL'S OWN grain, so the cutoff is fixed across axis buckets.
AXIS_REMOVAL = "CALCULATE(MAX('Orders'[Order_Date]), REMOVEFILTERS('Orders'[Order Month Label]))"
# Round 4: one moving and one foreign MAX-like call in a single bound - `_context_bound_kinds` must
# yield one kind for each, never stop at the first.
TWO_MAX_BOUND = "MIN(MAX('Orders'[Order_Date]), MAX('Cutoff'[Date]))"
# Round 4: the arguments of a window call carrying two ORDERBY clauses, already split.
TWO_ORDERBY_ARGS = ["1", "ABS", "0", "REL", "ORDERBY('Orders'[Order_Date], ASC)", "ORDERBY('Orders'[Region], ASC)"]


def facts() -> dg.ModelFacts:
    """Model facts with one derived text bin and one transitive chain, as the engine emits them."""
    built = dg.ModelFacts()
    built.calc_expressions[("orders", "order month label")] = "FORMAT('Orders'[Order_Date], \"yyyy-MM\")"
    built.calc_expressions[("orders", "order quarter no")] = "QUARTER('Orders'[Order_Date])"
    built.calc_expressions[("orders", "order quarter")] = "\"Q\" & 'Orders'[Order Quarter No]"
    return built


def cumulative(**overrides: Any) -> dg.Cumulative:
    """A blank `Cumulative` a classifier can fill in, or a pre-filled one for a judge.

    `ordered_by`/`partition_by`/`compared` are read-only PROPERTIES derived from the call lists, so
    they cannot be set here - which is the point: a verdict must never be formed from a union.
    """
    base = dg.Cumulative(table="_Measures", name="Probe", shape="unknown", tmdl="t.tmdl", line=1)
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def as_of_call(cleared: list[dg.ColumnRef] | None = None) -> dg.AsOfCall:
    """One as-of restriction on `ANCHOR`, clearing whatever it is told to clear."""
    return dg.AsOfCall(compared=ANCHOR, cleared_columns=list(cleared or [ANCHOR]))


def window_call(ordered: list[dg.ColumnRef] | None = None, reason: str = "") -> dg.WindowCall:
    """One window call - readable with ordering keys, or unreadable when given a reason."""
    return dg.WindowCall(func="WINDOW", ordered_by=list(ordered or []), assessable=not reason, reason=reason)


def period_call(anchor: dg.ColumnRef | None = None) -> dg.PeriodToDateCall:
    """One period-to-date call, anchored on `ANCHOR` unless told otherwise."""
    return dg.PeriodToDateCall(func="TOTALYTD", anchor=anchor or ANCHOR)


class Member:  # pylint: disable=too-few-public-methods
    """The duck-typed TMDL member `dax_grain.classify` reads, without parsing a model."""

    def __init__(self, expression: str) -> None:
        self.table = "_Measures"
        self.name = "Probe"
        self.expression = expression
        self.tmdl = Path("_Measures.tmdl")
        self.line = 1
        self.annotations: dict[str, str] = {}


def member(expression: str) -> Member:
    """One measure, ready for `classify()`."""
    return Member(expression)


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
