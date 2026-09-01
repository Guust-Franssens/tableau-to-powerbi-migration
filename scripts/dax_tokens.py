"""
purpose: tokenise DAX, and match a token stream against CLOSED, FULLY-CONSUMED templates - the
         lexical half of the running-total axis gate (#218), with no knowledge of TMDL or PBIR.
usage:   import dax_tokens; dax_tokens.tokenize(text); dax_tokens.match(TEMPLATE, tokens)

internal-reason: rounds 1-6 of blind review on #218 found the same class of defect eleven times, and
every one of them was a REGEX READING RAW DAX TEXT. Round 6 measured two that are worth writing
down because they point in opposite directions at once, both on the shipped code:

    canonical engine measure over a table named 'Orders--Archive'
      -> an identifier-unaware comment strip truncated at `--`
      -> the measure vanished: NOT_APPLICABLE, exit 0        (a REAL cumulative measure ERASED)

    ordinary measure SUM('WINDOW()'[Sales])
      -> a `\\bWINDOW\\s*\\(` search matched inside the quoted table NAME
      -> classified window_orderby: OK, exit 0                (a FAKE one SYNTHESISED)

    ordinary measure SUM('Orders'[WINDOW(1, ABS, 0, REL, ORDERBY('Date'[Date], ASC))])
      -> the same search matched inside a bracketed COLUMN name
      -> MISMATCH, exit 1                                     (a defect INVENTED on correct DAX)

So this module exists to make that class of error UNREPRESENTABLE rather than to patch it again:

1. **A lexer, not a parser.** A DAX lexer is finite and total - every character lands in exactly one
   token. Quoted table names (`'...'`), bracketed column names (`[...]`) and string literals
   (`"..."`) are ATOMIC: a `--` or a `WINDOW(` inside one is part of the identifier's text and can
   never be seen by anything that looks for code. Comments are recognised by the LEXER, so there is
   no separate comment pass to be identifier-unaware.
2. **Whole-expression templates, not searches.** `match()` anchors at token 0, and succeeds only
   when it has consumed the LAST token. A dead `VAR`, a wrapper, an extra branch or any residual
   token means no match - which the caller must report as unassessable, never as a verdict. That is
   the structural answer to round 6 finding 2 (`VAR _dead = <a WINDOW> VAR _answer = SUM(...)
   RETURN _answer` reported MISMATCH although the returned value is a plain sum).
3. **Holes are declared, bounded and checked.** A hole is not "whatever is left"; it is a balanced
   token run ending at the template's next literal token, and `<agg>` runs additionally have to be
   INERT (see `is_inert`). So "fully consumed" means every token of the expression is either a
   template literal or inside a declared hole - never unaccounted for.

The template SET is the engine's, read out of the deterministic tier rather than imagined:
`skills/tableau-migration/scripts/calc_to_dax.py` `_emit_table_calc` (~3466), `_orderby_clause`
(~3236, always emits `<table>[<col>], ASC|DESC`), `_partitionby_clause` (~3341),
`translate_difference_to_dax` (~3738) and `translate_percent_difference_to_dax` (~3686). Its
window/offset calls deliberately OMIT the `<relation>` argument (`calc_to_dax.py:279`), which is
exactly why the visual's own grain is the contract this gate checks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

KIND_NAME = "name"
KIND_NUMBER = "number"
KIND_STRING = "string"
KIND_TABLE = "table"
KIND_COLUMN = "column"
KIND_OP = "op"
KIND_COMMENT = "comment"
KIND_SPACE = "space"
KIND_UNKNOWN = "unknown"

# Trivia: lexed, kept for offsets, and never shown to a matcher.
_TRIVIA = frozenset({KIND_SPACE, KIND_COMMENT})

_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_NUMBER_RE = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_SPACE_RE = re.compile(r"[ \t\r\n\f\v]+")

# Longest first, so `<=` never lexes as `<` then `=` and `<>` is ONE token. That single fact retires
# a whole round-2 finding: DAX's not-equal can no longer be read as the `<` it begins with.
_OPERATORS = ("<=", ">=", "<>", "&&", "||", ":=", "(", ")", ",", "+", "-", "*", "/", "^", "&", "=", "<", ">", ".")

ORDERING_OPERATORS = frozenset({"<", "<=", ">", ">="})

# The window family whose `<relation>` argument is OPTIONAL, so an omitted relation makes the
# VISUAL the ordering domain. MOVINGAVERAGE/RUNNINGSUM are absent on purpose: their relation is
# REQUIRED, so the visual never decides their domain and this gate has no standing to judge them.
WINDOW_FUNCTIONS = frozenset({"WINDOW", "OFFSET", "INDEX", "RANK", "ROWNUMBER"})
CLAUSE_FUNCTIONS = frozenset({"ORDERBY", "PARTITIONBY", "MATCHBY"})
PERIOD_TO_DATE_FUNCTIONS = frozenset({"TOTALYTD", "TOTALMTD", "TOTALQTD", "DATESYTD", "DATESMTD", "DATESQTD"})

# The iterator heads `_emit_table_calc` folds a window frame with (`_TABLECALC_X`,
# `_TABLECALC_WINDOW_X`, `_TABLECALC_COUNT_X`, `_TABLECALC_STAT_X` in the engine).
WINDOW_ITERATORS = frozenset(
    {"SUMX", "AVERAGEX", "MINX", "MAXX", "COUNTX", "MEDIANX", "STDEVX.S", "STDEVX.P", "VARX.S", "VARX.P"}
)

# Scalar aggregates that cannot modify filter context, used for the ONE hole (`<sagg>`) whose
# contents would otherwise be able to change the verdict rather than only the value.
SIMPLE_AGGREGATES = frozenset(
    {"SUM", "AVERAGE", "MIN", "MAX", "COUNT", "COUNTA", "COUNTROWS", "DISTINCTCOUNT", "COUNTBLANK"}
)


@dataclass(frozen=True)
class Token:
    """One lexed DAX token. `text` is the source slice; `value` is the identifier it denotes."""

    kind: str
    text: str
    value: str
    start: int
    end: int

    @property
    def uppercase(self) -> str:
        """The token's text upper-cased - DAX function names are case-insensitive.

        Named `uppercase` rather than `upper` on purpose: `token.upper` and `token.upper()` differ
        only by two characters, and the second silently returns a bound method that is truthy
        everywhere a comparison expects a string.
        """
        return self.text.upper()


@dataclass(frozen=True)
class ColumnRef:
    """One `'Table'[Column]` reference, with the table left None when DAX did not qualify it."""

    table: str | None
    column: str

    def qualified(self) -> str:
        """The reference as a human would write it back into DAX."""
        return f"'{self.table}'[{self.column}]" if self.table else f"[{self.column}]"

    def key(self) -> tuple[str, str]:
        """Case-insensitive identity, so a casing-only difference does not invent a finding."""
        return ((self.table or "").casefold(), self.column.casefold())


@dataclass(frozen=True)
class WindowSpec:
    """The `ORDERBY(...)[, PARTITIONBY(...)]` addressing tail every engine window call carries."""

    ordered_by: tuple[ColumnRef, ...]
    partition_by: tuple[ColumnRef, ...]


@dataclass
class Lexed:
    """A whole expression, lexed once: every token, the code tokens, and what could not be lexed."""

    tokens: list[Token] = field(default_factory=list)
    unterminated: str = ""

    @property
    def code(self) -> list[Token]:
        """Every token a matcher may see - whitespace and comments removed, identifiers intact."""
        return [token for token in self.tokens if token.kind not in _TRIVIA]

    @property
    def has_unknown(self) -> bool:
        """Whether any character could not be lexed, which makes every read of this text a guess."""
        return any(token.kind == KIND_UNKNOWN for token in self.tokens)


def _read_delimited(text: str, start: int, closer: str, kind: str) -> tuple[Token, str]:
    """One atomic `'...'`, `[...]` or `"..."` run, honouring DAX's doubled-delimiter escape.

    ATOMIC is the whole point. `'Orders--Archive'` is ONE token, so no comment scanner can truncate
    it; `'WINDOW()'` is ONE token, so no function search can find a call inside it.
    """
    index = start + 1
    length = len(text)
    parts: list[str] = []
    while index < length:
        if text[index] == closer:
            if index + 1 < length and text[index + 1] == closer:
                parts.append(closer)
                index += 2
                continue
            token = Token(kind, text[start : index + 1], "".join(parts), start, index + 1)
            return token, ""
        parts.append(text[index])
        index += 1
    token = Token(KIND_UNKNOWN, text[start:length], "".join(parts), start, length)
    return token, f"unterminated {closer!r} starting at offset {start}"


def _read_comment(text: str, start: int) -> Token:
    """One `--`/`//` line comment or `/* */` block comment, recognised by the LEXER itself."""
    length = len(text)
    if text.startswith("/*", start):
        end = text.find("*/", start + 2)
        end = length if end < 0 else end + 2
    else:
        end = text.find("\n", start)
        end = length if end < 0 else end
    return Token(KIND_COMMENT, text[start:end], "", start, end)


def _read_operator(text: str, start: int) -> Token | None:
    """The longest operator that starts here, or None when no operator does."""
    for operator in _OPERATORS:
        if text.startswith(operator, start):
            return Token(KIND_OP, operator, operator, start, start + len(operator))
    return None


def tokenize(text: str) -> Lexed:
    """Lex a DAX expression. Total: every character lands in exactly one token.

    This replaces the identifier-unaware `strip_comments` + `mask_noncode` pair that round 6 finding
    1 defeated in both directions on the shipped code. There is no ordering hazard left to get
    wrong, because there is only one pass.
    """
    lexed = Lexed()
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        space = _SPACE_RE.match(text, index)
        if space:
            lexed.tokens.append(Token(KIND_SPACE, space.group(0), "", index, space.end()))
            index = space.end()
            continue
        if char in ("'", "[", '"'):
            closer = {"'": "'", "[": "]", '"': '"'}[char]
            kind = {"'": KIND_TABLE, "[": KIND_COLUMN, '"': KIND_STRING}[char]
            token, problem = _read_delimited(text, index, closer, kind)
            lexed.tokens.append(token)
            if problem and not lexed.unterminated:
                lexed.unterminated = problem
            index = token.end
            continue
        if text.startswith("--", index) or text.startswith("//", index) or text.startswith("/*", index):
            token = _read_comment(text, index)
            lexed.tokens.append(token)
            index = token.end
            continue
        number = _NUMBER_RE.match(text, index)
        if number:
            lexed.tokens.append(Token(KIND_NUMBER, number.group(0), number.group(0), index, number.end()))
            index = number.end()
            continue
        name = _NAME_RE.match(text, index)
        if name:
            lexed.tokens.append(Token(KIND_NAME, name.group(0), name.group(0), index, name.end()))
            index = name.end()
            continue
        operator = _read_operator(text, index)
        if operator is not None:
            lexed.tokens.append(operator)
            index = operator.end
            continue
        lexed.tokens.append(Token(KIND_UNKNOWN, char, char, index, index + 1))
        index += 1
    return lexed


def calls_named(code: list[Token], names: frozenset[str]) -> list[int]:
    """Indexes of every NAME token in `names` that is actually CALLED - i.e. followed by `(`.

    The `(` requirement is not decoration. `Index[Value]` names a table called `Index`; without the
    check it would raise a window-function signal and make an ordinary measure unassessable.
    """
    found = []
    for position, token in enumerate(code):
        if token.kind != KIND_NAME or token.uppercase not in names:
            continue
        following = code[position + 1] if position + 1 < len(code) else None
        if following is not None and following.kind == KIND_OP and following.text == "(":
            found.append(position)
    return found


def call_body(code: list[Token], call_index: int) -> list[Token]:
    """The tokens between the parentheses of the call whose NAME token is at `call_index`."""
    depth = 0
    body: list[Token] = []
    for position in range(call_index + 1, len(code)):
        token = code[position]
        if token.kind == KIND_OP and token.text == "(":
            depth += 1
            if depth == 1:
                continue
        elif token.kind == KIND_OP and token.text == ")":
            depth -= 1
            if depth == 0:
                return body
        if depth >= 1:
            body.append(token)
    return body


def has_ordering_comparison(tokens: list[Token]) -> bool:
    """Whether an ORDERING operator appears among these CODE tokens.

    `<>` cannot reach this: the lexer emits it as one token, so DAX's not-equal is not the `<` it
    begins with. Neither can a `<` inside a table name such as `'a<b'`, which is one TABLE token.
    """
    return any(token.kind == KIND_OP and token.text in ORDERING_OPERATORS for token in tokens)


def is_inert(tokens: list[Token]) -> bool:
    """Whether a captured `<agg>` run can be admitted without judging what is inside it.

    An `<agg>` hole is the aggregate the engine folds over a window frame (`inner[0]` in
    `_emit_table_calc`), and it is arbitrary translated DAX - it cannot be enumerated. What CAN be
    decided, from tokens alone, is that it carries no SECOND mechanism: no window-family call, no
    addressing clause, no period-to-date call, no as-of `FILTER(... <= ...)`, and nothing unlexable.

    That is what makes admitting the run honest. The judged property is whether the window's
    ORDERBY column is projected by the visual - a property of the FRAME - and the frame is fixed
    before the inner is evaluated per row, so the inner changes the value, never the frame. An
    inner carrying its own window or period-to-date call WOULD add a second grain, and that is
    exactly the case excluded here.
    """
    if not tokens:
        return False
    if any(token.kind == KIND_UNKNOWN for token in tokens):
        return False
    if calls_named(tokens, WINDOW_FUNCTIONS) or calls_named(tokens, CLAUSE_FUNCTIONS):
        return False
    if calls_named(tokens, PERIOD_TO_DATE_FUNCTIONS):
        return False
    return not any(
        has_ordering_comparison(call_body(tokens, index)) for index in calls_named(tokens, frozenset({"FILTER"}))
    )


@dataclass(frozen=True)
class Template:
    """One whole-expression shape the engine emits, compiled to literal items and holes."""

    name: str
    source: str

    @property
    def items(self) -> tuple[str, ...]:
        """The template's items, in order. Holes are `<...>`; everything else is a literal token."""
        return tuple(self.source.split())


@dataclass
class TemplateMatch:
    """What a whole-expression match bound, plus the receipt that it consumed EVERY token."""

    template: str
    specs: list[WindowSpec] = field(default_factory=list)
    anchors: list[ColumnRef] = field(default_factory=list)
    consumed: bool = False


class _Cursor:
    """A position in the code-token list, with the small reads every hole and literal needs."""

    def __init__(self, code: list[Token]) -> None:
        self.code = code
        self.index = 0

    def at_end(self) -> bool:
        """Whether every token has been consumed."""
        return self.index >= len(self.code)

    def peek(self, offset: int = 0) -> Token | None:
        """The token `offset` ahead, or None past the end."""
        position = self.index + offset
        return self.code[position] if 0 <= position < len(self.code) else None

    def take_literal(self, literal: str) -> bool:
        """Consume one literal template token - a name (case-insensitively) or an operator."""
        token = self.peek()
        if token is None:
            return False
        if token.kind == KIND_NAME and token.uppercase == literal.upper():
            self.index += 1
            return True
        if token.kind in (KIND_OP, KIND_NUMBER) and token.text == literal:
            self.index += 1
            return True
        return False

    def take_name_in(self, names: frozenset[str]) -> Token | None:
        """Consume one NAME token drawn from a closed set."""
        token = self.peek()
        if token is not None and token.kind == KIND_NAME and token.uppercase in names:
            self.index += 1
            return token
        return None

    def take_number(self) -> bool:
        """Consume one optionally-signed numeric literal."""
        token = self.peek()
        if token is not None and token.kind == KIND_OP and token.text in ("+", "-"):
            token = self.peek(1)
            if token is not None and token.kind == KIND_NUMBER:
                self.index += 2
                return True
            return False
        if token is not None and token.kind == KIND_NUMBER:
            self.index += 1
            return True
        return False

    def take_column(self) -> ColumnRef | None:
        """Consume one TABLE-QUALIFIED column reference, `'Table'[Column]` or `Table[Column]`."""
        table = self.peek()
        column = self.peek(1)
        if table is None or column is None or column.kind != KIND_COLUMN:
            return None
        if table.kind not in (KIND_TABLE, KIND_NAME):
            return None
        self.index += 2
        return ColumnRef(table=table.value, column=column.value)

    def take_balanced_until(self, literal: str) -> list[Token] | None:
        """Consume a balanced run that ends where the template's next LITERAL token begins.

        Depth is counted from the hole's start, so the run stops at the first `,` or `)` that
        belongs to the enclosing call rather than to something nested inside the hole.
        """
        depth = 0
        taken: list[Token] = []
        while not self.at_end():
            token = self.code[self.index]
            if token.kind == KIND_OP and token.text == "(":
                depth += 1
            elif token.kind == KIND_OP and token.text == ")":
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and token.kind == KIND_OP and token.text == literal:
                break
            taken.append(token)
            self.index += 1
        if depth != 0 or not taken:
            return None
        token = self.peek()
        if token is None or token.kind != KIND_OP or token.text != literal:
            return None
        return taken


_ORDERBY = frozenset({"ORDERBY"})
_DIRECTIONS = frozenset({"ASC", "DESC"})
_MODES = frozenset({"ABS", "REL"})


def _take_column_list(cursor: _Cursor, directions: bool) -> list[ColumnRef] | None:
    """Consume `<col>[, DIR][, <col>[, DIR]]...`, the body shared by ORDERBY and PARTITIONBY.

    A sort direction is optional: it is consumed when present and cannot change the judged property,
    which is only whether the ordering COLUMN is projected by the visual.
    """
    columns: list[ColumnRef] = []
    while True:
        reference = cursor.take_column()
        if reference is None:
            return None
        columns.append(reference)
        if not cursor.take_literal(","):
            return columns
        if directions and cursor.take_name_in(_DIRECTIONS) is not None and not cursor.take_literal(","):
            return columns


def _at_partition_tail(cursor: _Cursor) -> bool:
    """Whether the cursor sits on the `, PARTITIONBY` that continues an addressing spec."""
    token, following = cursor.peek(), cursor.peek(1)
    if token is None or token.kind != KIND_OP or token.text != ",":
        return False
    return following is not None and following.kind == KIND_NAME and following.uppercase == "PARTITIONBY"


def _take_spec(cursor: _Cursor) -> WindowSpec | None:
    """Consume `ORDERBY(<col>[, ASC|DESC][, ...])[, PARTITIONBY(<col>[, ...])]`, exactly.

    Every part must be TABLE-QUALIFIED, because that is what `_orderby_clause`/`_partitionby_clause`
    emit and because an unqualified name cannot be compared against a visual's projection.
    """
    if cursor.take_name_in(_ORDERBY) is None or not cursor.take_literal("("):
        return None
    ordered = _take_column_list(cursor, directions=True)
    if ordered is None or not cursor.take_literal(")"):
        return None
    partition: list[ColumnRef] = []
    if _at_partition_tail(cursor):
        cursor.index += 2
        if not cursor.take_literal("("):
            return None
        taken = _take_column_list(cursor, directions=False)
        if taken is None or not cursor.take_literal(")"):
            return None
        partition = taken
    return WindowSpec(ordered_by=tuple(ordered), partition_by=tuple(partition))


def _take_qcol(cursor: _Cursor, _next: str, result: TemplateMatch) -> bool:
    """A table-qualified column reference the caller needs to keep - a period-to-date anchor."""
    reference = cursor.take_column()
    if reference is None:
        return False
    result.anchors.append(reference)
    return True


def _take_simple_aggregate(cursor: _Cursor, _next: str, _result: TemplateMatch) -> bool:
    """`SUM('T'[C])` and its siblings - the ONE hole shape that provably cannot alter filters."""
    if cursor.take_name_in(SIMPLE_AGGREGATES) is None or not cursor.take_literal("("):
        return False
    return cursor.take_column() is not None and cursor.take_literal(")")


def _take_addressing(cursor: _Cursor, _next: str, result: TemplateMatch) -> bool:
    """The `ORDERBY(...)[, PARTITIONBY(...)]` spec, recorded for the gate to judge."""
    spec = _take_spec(cursor)
    if spec is None:
        return False
    result.specs.append(spec)
    return True


def _take_inert_run(cursor: _Cursor, next_literal: str, _result: TemplateMatch) -> bool:
    """The aggregate the engine folds over a frame: a balanced run, checked inert by `is_inert`."""
    taken = cursor.take_balanced_until(next_literal)
    return taken is not None and is_inert(taken)


# Every hole a template may declare, and nothing else. A template naming an unlisted hole cannot
# match anything, which is what keeps the vocabulary closed rather than open-ended.
_HOLES = {
    "<num>": lambda cursor, _next, _result: cursor.take_number(),
    "<mode>": lambda cursor, _next, _result: cursor.take_name_in(_MODES) is not None,
    "<aggx>": lambda cursor, _next, _result: cursor.take_name_in(WINDOW_ITERATORS) is not None,
    "<qcol>": _take_qcol,
    "<sagg>": _take_simple_aggregate,
    "<spec>": _take_addressing,
    "<agg>": _take_inert_run,
}


def match(template: Template, code: list[Token]) -> TemplateMatch | None:
    """Match a WHOLE expression against one template, or return None.

    Anchored at token 0 and required to end on the last token. A wrapper, a dead `VAR`, an extra
    argument or one residual token means None - and a caller that has no match may not form a
    definite verdict. That is the whole contract, and it is what makes "the tokeniser consumed the
    input" a property of the code rather than a claim about test coverage.
    """
    cursor = _Cursor(code)
    result = TemplateMatch(template=template.name)
    items = template.items
    for position, item in enumerate(items):
        if item.startswith("<"):
            following = items[position + 1] if position + 1 < len(items) else ""
            hole = _HOLES.get(item)
            if hole is None or not hole(cursor, following, result):
                return None
            continue
        if not cursor.take_literal(item):
            return None
    if not cursor.at_end():
        return None
    result.consumed = True
    return result


def match_any(templates: tuple[Template, ...], code: list[Token]) -> TemplateMatch | None:
    """The first template that consumes the whole expression, or None when none does."""
    for template in templates:
        found = match(template, code)
        if found is not None:
            return found
    return None
