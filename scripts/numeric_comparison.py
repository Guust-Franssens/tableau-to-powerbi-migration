"""
purpose: compare held CSV and typed-v1 wire bytes using explicit text keys and exact numeric values.
usage:   from numeric_comparison import compare_numeric

``compare_numeric`` requires key_columns and value_columns as sequences of (CSV name, DAX name)
pairs, plus csv_max_payload_bytes and typed_max_payload_bytes. Both mappings together must cover
every column exactly once on each side. Keys are tuples of nonempty, unchanged UTF-8 text.

The typed success envelope has exactly schema_version (1), query_sha256 (64 lowercase hex digits),
columns ([{name, kind}]), and rows ([[{kind, value}]]). Column kinds are string, int32, int64, decimal;
cells retain that kind, or blank with value null. Other cell values are strings. Numbers use ASCII
``-?(?:0|[1-9][0-9]*)(?:\\.[0-9]+)?``; integer kinds additionally require integral syntax and range.
Typed decimals require a 96-bit unsigned coefficient and scale 0..28, including retained trailing
zeros. CSV numbers do not inherit that CLR domain. Equality ignores native kind and scale, never
uses floats, arithmetic, or a tolerance.

Budgets charge original CSV bytes and the entire received typed envelope, including JSON whitespace
and escaping. One terminal NDJSON LF (optionally CRLF) is framing, not envelope bytes. Nothing is
reserialized for admission. There are no independent row, column, cell, or digit policy ceilings;
native type representability is separate from the byte budgets.

Returns only EQUAL, DIFFERENT, EMPTY, or a fixed refusal. Both inputs are fully validated before a
relation is returned. This module does no I/O, execution, identity/certification admission, evidence
writing, or Phase-2 sign-off. In particular, query_sha256 is syntax-checked, not request-bound.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from decimal import Decimal

# bool is an int subclass; wire integers and budget integers must be actual integers.
# pylint: disable=unidiomatic-typecheck

_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", re.ASCII)
_INTEGER = re.compile(r"-?(?:0|[1-9][0-9]*)", re.ASCII)
_CLR_DECIMAL_MAX_COEFFICIENT = "79228162514264337593543950335"
_NUMERIC_KINDS = frozenset({"int32", "int64", "decimal"})
_COLUMN_KINDS = _NUMERIC_KINDS | {"string"}
_INTEGER_RANGES = {
    "int32": (Decimal("-2147483648"), Decimal("2147483647")),
    "int64": (Decimal("-9223372036854775808"), Decimal("9223372036854775807")),
}
_REFUSALS = frozenset(
    {
        "INPUT_INVALID",
        "PAYLOAD_LIMIT",
        "CSV_INVALID",
        "TYPED_INVALID",
        "COLUMNS_INVALID",
        "KEY_INVALID",
        "KEY_DUPLICATE",
        "VALUE_UNSUPPORTED",
    }
)

ColumnPairs = Sequence[tuple[str, str]]
RowMap = dict[tuple[str, ...], tuple[Decimal | None, ...]]


class _Refusal(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code if code in _REFUSALS else "INPUT_INVALID"
        super().__init__(self.code)


def _text(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _csv_records(blob: bytes) -> list[list[str]]:  # pylint: disable=too-many-branches
    # csv.reader has a process-global field_size_limit. A local state machine avoids both that
    # hidden ceiling and changing global parser state in what must remain a pure operation.
    try:
        text = blob.decode("utf-8")
    except UnicodeError:
        raise _Refusal("CSV_INVALID") from None
    records, row, field = [], [], []
    quoted = after_quote = False
    index = 0
    while index < len(text):
        char = text[index]
        if quoted:
            if char == '"':
                if index + 1 < len(text) and text[index + 1] == '"':
                    field.append('"')
                    index += 1
                else:
                    quoted, after_quote = False, True
            else:
                field.append(char)
        elif char == "," or char in "\r\n":
            row.append("".join(field))
            field = []
            after_quote = False
            if char != ",":
                records.append(row)
                row = []
                if char == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
                    index += 1
        elif char == '"' and not field and not after_quote:
            quoted = True
        elif char == '"' or after_quote:
            raise _Refusal("CSV_INVALID")
        else:
            field.append(char)
        index += 1
    if quoted:
        raise _Refusal("CSV_INVALID")
    if field or row or after_quote:
        records.append([*row, "".join(field)])
    if not records or records[0] == [""]:
        raise _Refusal("CSV_INVALID")
    if any(len(row) != len(records[0]) for row in records[1:]):
        raise _Refusal("CSV_INVALID")
    return records


def _json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise _Refusal("TYPED_INVALID")
        result[key] = value
    return result


def _json_constant(_value: str) -> None:
    raise _Refusal("TYPED_INVALID")


def _typed_envelope(blob: bytes) -> dict:
    try:
        payload = json.loads(blob.decode("utf-8"), object_pairs_hook=_json_object, parse_constant=_json_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise _Refusal("TYPED_INVALID") from None
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "query_sha256", "columns", "rows"}:
        raise _Refusal("TYPED_INVALID")
    query_hash = payload["query_sha256"]
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise _Refusal("TYPED_INVALID")
    if (
        not isinstance(query_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", query_hash)
        or not isinstance(payload["columns"], list)
        or not isinstance(payload["rows"], list)
    ):
        raise _Refusal("TYPED_INVALID")
    for column in payload["columns"]:
        if not isinstance(column, dict) or set(column) != {"name", "kind"}:
            raise _Refusal("TYPED_INVALID")
        if not isinstance(column["kind"], str):
            raise _Refusal("TYPED_INVALID")
        if column["kind"] not in _COLUMN_KINDS:
            raise _Refusal("VALUE_UNSUPPORTED")
    for row in payload["rows"]:
        if not isinstance(row, list) or len(row) != len(payload["columns"]):
            raise _Refusal("TYPED_INVALID")
        for column, cell in zip(payload["columns"], row):
            _typed_cell(column, cell)
    return payload


def _typed_cell(column: dict, cell: object) -> None:
    if not isinstance(cell, dict) or set(cell) != {"kind", "value"} or not isinstance(cell["kind"], str):
        raise _Refusal("TYPED_INVALID")
    kind, value = cell["kind"], cell["value"]
    if kind not in _COLUMN_KINDS | {"blank"}:
        raise _Refusal("VALUE_UNSUPPORTED")
    if kind == "blank":
        if value is not None:
            raise _Refusal("TYPED_INVALID")
    elif kind != column["kind"] or not _text(value):
        raise _Refusal("TYPED_INVALID")


def _column_names(names: list[str]) -> None:
    if not names or any(not _text(name) or not name for name in names) or len(set(names)) != len(names):
        raise _Refusal("COLUMNS_INVALID")


def _mapping(
    key_columns: ColumnPairs, value_columns: ColumnPairs, csv_names: list[str], typed_columns: list[dict]
) -> tuple[tuple[tuple[int, int], ...], tuple[tuple[int, int], ...]]:
    dax_names = [column["name"] for column in typed_columns]
    _column_names(csv_names)
    _column_names(dax_names)
    for group in (key_columns, value_columns):
        if type(group) not in (list, tuple) or not group:
            raise _Refusal("COLUMNS_INVALID")
        for pair in group:
            if type(pair) not in (list, tuple) or len(pair) != 2 or any(not _text(name) or not name for name in pair):
                raise _Refusal("COLUMNS_INVALID")
    pairs = [*key_columns, *value_columns]
    for names, mapped in ((csv_names, [pair[0] for pair in pairs]), (dax_names, [pair[1] for pair in pairs])):
        if len(mapped) != len(names) or len(set(mapped)) != len(mapped) or set(mapped) != set(names):
            raise _Refusal("COLUMNS_INVALID")
    indices = tuple((csv_names.index(left), dax_names.index(right)) for left, right in pairs)
    keys, values = indices[: len(key_columns)], indices[len(key_columns) :]
    if any(typed_columns[right]["kind"] != "string" for _, right in keys):
        raise _Refusal("KEY_INVALID")
    if any(typed_columns[right]["kind"] not in _NUMERIC_KINDS for _, right in values):
        raise _Refusal("VALUE_UNSUPPORTED")
    return keys, values


def _clr_decimal_text(text: str) -> bool:
    whole, _, fraction = text.removeprefix("-").partition(".")
    coefficient = (whole + fraction).lstrip("0") or "0"
    return len(fraction) <= 28 and (
        len(coefficient) < len(_CLR_DECIMAL_MAX_COEFFICIENT)
        or (len(coefficient) == len(_CLR_DECIMAL_MAX_COEFFICIENT) and coefficient <= _CLR_DECIMAL_MAX_COEFFICIENT)
    )


def _number(text: str, kind: str | None = None) -> Decimal:
    grammar = _INTEGER if kind in _INTEGER_RANGES else _NUMBER
    if not grammar.fullmatch(text) or (kind == "decimal" and not _clr_decimal_text(text)):
        raise _Refusal("VALUE_UNSUPPORTED")
    try:
        value = Decimal(text)
    except (ValueError, ArithmeticError):
        raise _Refusal("VALUE_UNSUPPORTED") from None
    if kind in _INTEGER_RANGES:
        lower, upper = _INTEGER_RANGES[kind]
        if not lower <= value <= upper:
            raise _Refusal("VALUE_UNSUPPORTED")
    return value


def _rows(rows: list[list], key_indices: tuple[int, ...], value_indices: tuple[int, ...], *, typed: bool) -> RowMap:
    indexed = {}
    for row in rows:
        key = tuple(row[index]["value"] if typed else row[index] for index in key_indices)
        if any(not isinstance(part, str) or not part for part in key):
            raise _Refusal("KEY_INVALID")
        if key in indexed:
            raise _Refusal("KEY_DUPLICATE")
        values = []
        for index in value_indices:
            if typed:
                cell = row[index]
                values.append(None if cell["kind"] == "blank" else _number(cell["value"], cell["kind"]))
            else:
                values.append(_number(row[index]))
        indexed[key] = tuple(values)
    return indexed


def compare_numeric(  # pylint: disable=too-many-arguments
    csv_bytes: bytes,
    typed_bytes: bytes,
    *,
    key_columns: ColumnPairs,
    value_columns: ColumnPairs,
    csv_max_payload_bytes: int,
    typed_max_payload_bytes: int,
) -> str:
    """Return an exact keyed relation or a fixed refusal, without emitting either operand."""
    if type(csv_bytes) is not bytes or type(typed_bytes) is not bytes:
        return "INPUT_INVALID"
    if any(type(limit) is not int or limit <= 0 for limit in (csv_max_payload_bytes, typed_max_payload_bytes)):
        return "INPUT_INVALID"
    if typed_bytes.endswith(b"\r\n"):
        typed_bytes = typed_bytes[:-2]
    elif typed_bytes.endswith(b"\n"):
        typed_bytes = typed_bytes[:-1]
    if len(csv_bytes) > csv_max_payload_bytes or len(typed_bytes) > typed_max_payload_bytes:
        return "PAYLOAD_LIMIT"
    try:
        csv = _csv_records(csv_bytes)
        typed = _typed_envelope(typed_bytes)
        keys, values = _mapping(key_columns, value_columns, csv[0], typed["columns"])
        left = _rows(csv[1:], tuple(pair[0] for pair in keys), tuple(pair[0] for pair in values), typed=False)
        right = _rows(typed["rows"], tuple(pair[1] for pair in keys), tuple(pair[1] for pair in values), typed=True)
    except _Refusal as refusal:
        return refusal.code
    if not left and not right:
        return "EMPTY"
    return "EQUAL" if left == right else "DIFFERENT"
