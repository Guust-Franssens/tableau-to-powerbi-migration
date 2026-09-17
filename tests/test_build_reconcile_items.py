"""Numeric consumer controls for #669, using only tiny authored CSV byte strings."""

# pylint: disable=use-implicit-booleaness-not-comparison  # Assert JSON array shape, not just truthiness.

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_reconcile_items as builder  # noqa: E402  # pylint: disable=wrong-import-position

ROLES = {"Workbook": {"Region": "DIMENSION", "Amount": "MEASURE", "Other": "MEASURE"}}


def _write_oracle(root: Path, *payloads: bytes) -> None:
    views = []
    for index, payload in enumerate(payloads):
        filename = f"view-{index}.csv"
        (root / filename).write_bytes(payload)
        views.append(
            {
                "view_luid": f"00000000-0000-4000-8000-{index:012d}",
                "view_name": f"View {index}",
                "workbook_name": "Workbook",
                "data": {"status": "ok", "path": filename, "certification": "certified", "row_count": 1},
            }
        )
    (root / "oracle-manifest.json").write_text(
        json.dumps({"schema": "tableau-oracle/1", "views": views}), encoding="utf-8"
    )


def test_unique_headers_keep_each_value_and_caption_identity(tmp_path: Path) -> None:
    """The 1 and 999 control must produce two distinct, correctly attributed items."""
    _write_oracle(tmp_path, b"Region,Amount,Other,Note\nWest,1,999,unmapped\n")

    result = builder.build(tmp_path, ROLES)

    assert result["schema"] == "tableau-reconcile-items/1"
    assert result["item_count"] == 2
    assert result["skipped_views"] == []
    assert result["unmapped_columns"] == [{"view": "View 0", "column": "Note"}]
    assert [(item["name"], item["tableau_value"]) for item in result["items"]] == [("Amount", 1.0), ("Other", 999.0)]
    for item, raw in zip(result["items"], ("1", "999"), strict=True):
        assert item["grain_filters"] == {"Region": "West"}
        assert item["filter_context_known"] is False
        assert item["source"] == {
            "view_luid": "00000000-0000-4000-8000-000000000000",
            "view_name": "View 0",
            "workbook_name": "Workbook",
            "row": 0,
            "raw": raw,
            "normalisation": "number",
        }


@pytest.mark.parametrize(
    "payload",
    [
        b"Region,Amount,Amount,Other\nWest,1,999,7\n",
        b"Region,Amount,Amount,Other\nWest,999,1,7\n",
        b"Region,Region,Amount\nWest,East,1\n",
        b"Region,Unknown,Unknown,Amount\nWest,1,999,7\n",
        b"Region,Amount, Amount,Other\nWest,1,999,7\n",
        b"Region,Amount,Amount ,Other\nWest,1,999,7\n",
        b"Region,Amount,\tAmount\t,Other\nWest,1,999,7\n",
        b"Region, Amount ,Amount,Other\nWest,999,1,7\n",
        b"Region, Amount ,\tAmount\t,Other\nWest,1,999,7\n",
        b"Region,\tRegion ,Amount,Other\nWest,East,1,7\n",
        b"Region,Unknown,\tUnknown ,Amount\nWest,1,999,7\n",
    ],
    ids=[
        "measure-first-order",
        "measure-reversed",
        "dimension",
        "unmapped",
        "leading-space-alias",
        "trailing-space-alias",
        "tab-alias",
        "reversed-alias",
        "two-aliases",
        "dimension-alias",
        "unmapped-alias",
    ],
)
def test_duplicate_headers_refuse_the_entire_view(tmp_path: Path, payload: bytes) -> None:
    """Raw duplicates and aliases of one role key refuse even neighboring unique measures."""
    _write_oracle(tmp_path, payload)

    result = builder.build(tmp_path, ROLES)

    assert result["items"] == []
    assert result["item_count"] == 0
    assert result["unmapped_columns"] == []
    assert result["skipped_views"] == [
        {"view": "View 0", "reason": "duplicate CSV headers; caption-keyed field roles are ambiguous"}
    ]


@pytest.mark.parametrize(
    "payload",
    [
        b"Region,Amount,Amount\nWest,1,999\n",
        b"Region,Amount, Amount \nWest,1,999\n",
        b"Region,Amount,\tAmount\t\nWest,1,999\n",
        b"Region, Amount ,\tAmount\t\nWest,1,999\n",
    ],
    ids=["raw-duplicate", "space-alias", "tab-alias", "two-aliases"],
)
def test_a_duplicate_view_does_not_discard_neighboring_unique_views(tmp_path: Path, payload: bytes) -> None:
    """Refusal is view-scoped, not a silent partial mapping or an estate-wide stop."""
    _write_oracle(
        tmp_path,
        b"Region,Amount,Other\nWest,1,999\n",
        payload,
        b"Region,Amount,Other\nEast,2,998\n",
    )

    result = builder.build(tmp_path, ROLES)

    assert result["item_count"] == 4
    assert [
        (item["source"]["view_name"], item["grain_filters"], item["name"], item["tableau_value"])
        for item in result["items"]
    ] == [
        ("View 0", {"Region": "West"}, "Amount", 1.0),
        ("View 0", {"Region": "West"}, "Other", 999.0),
        ("View 2", {"Region": "East"}, "Amount", 2.0),
        ("View 2", {"Region": "East"}, "Other", 998.0),
    ]
    assert result["skipped_views"] == [
        {"view": "View 1", "reason": "duplicate CSV headers; caption-keyed field roles are ambiguous"}
    ]
    assert result["unmapped_columns"] == []


@pytest.mark.parametrize("caption", [b" Amount ", b"\tAmount\t", b"amount"])
@pytest.mark.parametrize("role", ["MEASURE", "DIMENSION"])
def test_distinct_exact_caption_keys_keep_their_roles(tmp_path: Path, caption: bytes, role: str) -> None:
    """Exact role keys take precedence; whitespace and case are not blanket-normalized."""
    _write_oracle(tmp_path, b"Region,Amount," + caption + b",Other\nWest,1,999,7\n")
    name = caption.decode("utf-8")
    roles = {"Workbook": {**ROLES["Workbook"], name: role}}

    result = builder.build(tmp_path, roles)

    expected_items = [("Amount", 1.0), ("Other", 7.0)]
    expected_grain = {"Region": "West"}
    if role == "MEASURE":
        expected_items.insert(1, (name, 999.0))
    else:
        expected_grain[name] = "999"
    assert [(item["name"], item["tableau_value"]) for item in result["items"]] == expected_items
    assert result["item_count"] == len(expected_items)
    assert all(item["grain_filters"] == expected_grain for item in result["items"])
    assert result["skipped_views"] == []
    assert result["unmapped_columns"] == []


def test_empty_exact_role_still_uses_the_stripped_caption_key(tmp_path: Path) -> None:
    """A present but empty role retains classification's existing stripped-key fallback."""
    _write_oracle(tmp_path, b"Region,Amount, Amount ,Other\nWest,1,999,7\n")
    roles = {"Workbook": {**ROLES["Workbook"], " Amount ": ""}}

    result = builder.build(tmp_path, roles)

    assert result["items"] == []
    assert result["item_count"] == 0
    assert result["unmapped_columns"] == []
    assert result["skipped_views"] == [
        {"view": "View 0", "reason": "duplicate CSV headers; caption-keyed field roles are ambiguous"}
    ]


@pytest.mark.parametrize("caption", [b" Amount ", b"\tAmount\t"])
def test_unique_stripped_caption_still_resolves_without_renaming(tmp_path: Path, caption: bytes) -> None:
    """A whitespace alias is not ambiguous unless another header resolves to the same key."""
    _write_oracle(tmp_path, b"Region," + caption + b",Other\nWest,1,999\n")

    result = builder.build(tmp_path, ROLES)

    assert result["item_count"] == 2
    assert [(item["name"], item["tableau_value"]) for item in result["items"]] == [
        (caption.decode("utf-8"), 1.0),
        ("Other", 999.0),
    ]
    assert all(item["grain_filters"] == {"Region": "West"} for item in result["items"])
    assert result["skipped_views"] == []
    assert result["unmapped_columns"] == []


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"Region,Amount\nAB,1\n", "AB"),
        (b'Region,Amount\n"A\nB",1\n', "A\nB"),
        (b'Region,Amount\r\n"A\r\nB",1\r\n', "A\r\nB"),
        (b'Region,Amount\r"A\rB",1\r', "A\rB"),
        (b'Region,Amount\r\n" A, ""B"" \r\nC ",1\r\n', ' A, "B" \r\nC '),
    ],
    ids=["plain", "lf", "crlf", "cr", "quoted-with-spaces"],
)
def test_grain_text_survives_csv_parsing_exactly(tmp_path: Path, payload: bytes, expected: str) -> None:
    """Literal expected text, not another invocation of the consumer, is the oracle."""
    _write_oracle(tmp_path, payload)

    result = builder.build(tmp_path, ROLES)

    assert result["item_count"] == 1
    assert result["skipped_views"] == []
    assert result["items"][0]["grain_filters"] == {"Region": expected}
    assert result["items"][0]["tableau_value"] == 1.0
    assert result["items"][0]["filter_context_known"] is False


@pytest.mark.parametrize(
    ("cell", "expected", "normalisation"),
    [
        (b"1.23", 1.23, "number"),
        (b"0", 0.0, "number"),
        (b"0.123", 0.123, "number"),
        (b"-.5", -0.5, "number"),
        (b"1234.56", 1234.56, "number"),
        (b"1,234", 1234.0, "number"),
        (b"123,456.78", 123456.78, "number"),
        (b"-12,345,678.90", -12345678.9, "number"),
        (b"0%", 0.0, "percent"),
        (b"19.5%", 0.195, "percent"),
        (b"-1,234.5 %", -12.345, "percent"),
        (b"$0", 0.0, "currency"),
        (b"$1,234.56", 1234.56, "currency"),
        (b" - $ 12.50 ", -12.5, "currency"),
        (b"\xc2\xa312.50", 12.5, "currency"),
        (b"\xe2\x82\xac0.123", 0.123, "currency"),
        (b"\xe2\x82\xac1,234.56", 1234.56, "currency"),
        (b"\xc2\xa512", 12.0, "currency"),
    ],
)
def test_supported_numbers_still_emit_items(tmp_path: Path, cell: bytes, expected: float, normalisation: str) -> None:
    """Invariant decimals and US comma grouping work in all three existing format arms."""
    _write_oracle(tmp_path, b'Region,Amount\nWest,"' + cell + b'"\n')

    result = builder.build(tmp_path, ROLES)

    assert result["item_count"] == 1
    item = result["items"][0]
    assert item["tableau_value"] == expected
    assert item["source"]["normalisation"] == normalisation
    assert item["source"]["raw"] == cell.decode("utf-8")
    assert item["filter_context_known"] is False


@pytest.mark.parametrize(
    "cell",
    [
        b"1,23",
        b"19,5%",
        b"\xe2\x82\xac1,23",
        b"1,234,56",
        b"12,34.56",
        b"1,,234",
        b",123",
        b"123,",
        b"1e3",
        b"NaN",
        b"Infinity",
        b"\xd9\xa1.\xd9\xa2\xd9\xa3",
        b"-0,123",
        b"-0,123%",
        b"-$0,123",
    ],
)
def test_unsupported_numbers_stay_text_and_emit_no_item(tmp_path: Path, cell: bytes) -> None:
    """Comma decimals are not locale-inferred or silently scaled into plausible numbers."""
    raw = cell.decode("utf-8")
    assert builder.normalise_value(raw) == (raw, "text")
    _write_oracle(tmp_path, b'Region,Amount\nWest,"' + cell + b'"\n')

    result = builder.build(tmp_path, ROLES)

    assert result["items"] == []
    assert result["item_count"] == 0
    assert result["skipped_views"] == []


@pytest.mark.parametrize(
    "number",
    [b"0,123", b"0,000", b"00,123", b"000,001", b"01,234", b"012,345", b"000,001,234.56"],
)
@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [(b"", b""), (b"", b"%"), (b"\xe2\x82\xac", b"")],
    ids=["plain", "percent", "currency"],
)
def test_zero_prefixed_grouped_numbers_stay_text(tmp_path: Path, number: bytes, prefix: bytes, suffix: bytes) -> None:
    """Every format arm refuses a leading zero before comma grouping, including zero itself."""
    cell = prefix + number + suffix
    raw = cell.decode("utf-8")
    assert builder.normalise_value(raw) == (raw, "text")
    _write_oracle(tmp_path, b'Region,Amount\nWest,"' + cell + b'"\n')

    result = builder.build(tmp_path, ROLES)

    assert result["items"] == []
    assert result["item_count"] == 0
    assert result["skipped_views"] == []


@pytest.mark.parametrize(
    ("payload", "expected_exit", "expected_count"),
    [
        (b"Region,Amount,Other\nWest,1,999\n", 0, 2),
        (b"Region,Amount,Amount\nWest,1,999\n", 1, 0),
        (b"Region,Amount,\tAmount \nWest,1,999\n", 1, 0),
        (b'Region,Amount\nWest,"1,23"\n', 1, 0),
        (b'Region,Amount\nWest,"0,123"\n', 1, 0),
        (b'Region,Amount\nWest,"0,123%"\n', 1, 0),
        (b'Region,Amount\nWest,"\xe2\x82\xac0,123"\n', 1, 0),
    ],
    ids=[
        "unique",
        "duplicate",
        "alias",
        "unsupported",
        "zero-prefix-plain",
        "zero-prefix-percent",
        "zero-prefix-currency",
    ],
)
def test_cli_reports_whether_any_numeric_item_was_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes, expected_exit: int, expected_count: int
) -> None:
    """An empty batch retains the existing non-success exit and JSON result shape."""
    _write_oracle(tmp_path, payload)
    roles_path = tmp_path / "roles.json"
    roles_path.write_text(json.dumps(ROLES), encoding="utf-8")
    output = tmp_path / "items.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["build_reconcile_items.py", "--oracle", str(tmp_path), "--roles", str(roles_path), "--out", str(output)],
    )

    assert builder.main() == expected_exit
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["item_count"] == expected_count
    assert len(result["items"]) == expected_count
