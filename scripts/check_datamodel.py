"""
purpose: Catch semantic-model defects BEFORE Power BI Desktop does: Power Query M plus TMDL structure.
usage:   python scripts/check_datamodel.py [<path to .SemanticModel or migration folder> ...]
         python scripts/check_datamodel.py --all        # every model in every migration tree

Why this exists
---------------
Desktop reports a broken model as:

    Issues were found
    There's a problem with the definition content in your Power BI Project.
    M Engine error: 'Microsoft.Data.Mashup.Preview; Token ',' expected.'

...and names NO file, NO line and NO expression. On a model with dozens of partitions that is
close to unactionable, which is why users report hitting it repeatedly and being unable to move.

The M in these models is authored by an agent, so the failures cluster into a few shapes that are
all detectable structurally, without evaluating anything:

  * a trailing comma before a closing brace - `{1, 2, }` - a JSON habit that M rejects outright,
    and the single most likely source of "Token ',' expected"
  * unbalanced (), [] or {} - usually a truncated or over-nested `#table(...)`
  * `let` with no matching `in`
  * an unterminated string or block comment
  * two values sitting side by side with no separator - `{"a" "b"}` - a dropped comma

This deliberately does NOT try to be a full M parser: it is string/comment aware and tracks
delimiters, which is enough to localise the error to `file:line:col` and quote the offending text.
That converts an opaque dialog into a fix.

The same "green structural gates, broken Desktop open" gap also exists outside M. A duplicated TMDL
scalar property (for example two `formatString:` entries on one measure) passes report validation and
the M checker, but Desktop rejects the semantic model. This checker folds those low-noise TMDL checks
into the existing model gate instead of adding another command for agents to remember.

Scope stays deliberately narrow because a false positive is worse than a miss:

  * duplicate scalar TMDL properties within one object
  * measure/column name collisions within one table file
  * a measure name repeated in a different table (Tabular measure names are unique model-wide, not
    per-table; ported from the drifted `tmdl_validate` example helpers - issue #413)
  * empty measure expressions
  * direct CALCULATE/CALCULATETABLE compact filters that compare a column to a measure
  * legacy BIFF8 `.xls` partitions with a resolvable local source: their navigation key and type
    conversion culture, which otherwise fail or silently corrupt rows at refresh

On top of those text checks it runs the TMDL ORACLE (`scripts/tmdl_oracle.py`, needs the .NET SDK),
which hands each model to `TmdlSerializer` - the parser Power BI Desktop itself uses - and reports
`TMDL_PARSER_REJECTED` when it refuses, with AMO's own document and line. That is the failure issue
#254 reported: Desktop names no file and no line, and the model does not open at all.

The oracle is MANDATORY. If it cannot run, this exits `EXIT_UNASSESSABLE` (3) rather than 0: "we
could not check" must never look like "clean". `--no-oracle` is the explicit opt-out.

It deliberately does NOT detect silent absorption (a property swallowed into an expression while the
document still parses). That is measurably undecidable from the parse - see scripts/tmdl_oracle.py
and issue #404.

A clean result does NOT prove the model refreshes; it only excludes these structural classes.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from check_empty_model import eval_m_path, model_parameters
from tmdl_checks import (
    TmdlFinding,
    check_tmdl_model,
    check_tmdl_text,
    find_compact_filters,
)
from tmdl_oracle import OracleUnavailable, check_models

# Re-exported so `from check_datamodel import ...` keeps working for callers and tests that
# predate the split of the TMDL half into tmdl_checks.
__all__ = [
    "TmdlFinding",
    "check_datamodel",
    "check_model",
    "check_model_counted",
    "check_tmdl_model",
    "check_tmdl_text",
    "find_compact_filters",
    "main",
]

REPO_ROOT = Path(__file__).resolve().parent.parent
TREES = ("examples", "migrations/workbooks", "migrations/datasources")

# A DISTINCT exit code for "the gate could not assess this", kept apart from 0 (clean) and 1
# (findings). `check_unit.py` has no fail-open fallthrough, so an exit code it does not recognise
# is recorded as NOT_CHECKED rather than PASS - which is exactly the meaning here.
EXIT_UNASSESSABLE = 3

log = logging.getLogger("check_datamodel")

PAIRS = {"(": ")", "[": "]", "{": "}"}
CLOSERS = {v: k for k, v in PAIRS.items()}
_NUMBER_RE = re.compile(r"0[xX][0-9a-fA-F]+|[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")
_BIFF8_MAGIC = b"\xd0\xcf\x11\xe0"
_FILE_CONTENTS_RE = re.compile(r"File\.Contents\s*\(")
_TYPE_CONVERSION_RE = re.compile(r"Table\.TransformColumnTypes\s*\(")
_EXCEL_ASSIGNMENT_RE = re.compile(r'(?m)^\s*(?P<name>#"[^"]+"|[A-Za-z_]\w*)\s*=\s*Excel\.Workbook\s*\(')
_BIFF8_NAVIGATION_DETAIL = "BIFF8 .xls navigation must use a Name= key (not Item=/Kind=)"
# The culture is the minimum fix, not the recommended one: pinning one bakes the build host's locale
# into the artifact, so name the locale-proof escape hatch the same gotcha section prescribes.
_BIFF8_CULTURE_DETAIL = (
    "BIFF8 .xls type conversion must pass an explicit culture; better, take the legacy reader out of "
    "the path - re-land the sheet as an invariant CSV and read it with Csv.Document"
)

# Keywords that legitimately introduce or continue an expression, so an identifier following one of
# them is NOT a missing separator (e.g. `type text`, `each Foo`, `otherwise null`).
M_KEYWORDS = {
    "and",
    "as",
    "catch",
    "each",
    "else",
    "error",
    "if",
    "in",
    "is",
    "let",
    "meta",
    "not",
    "nullable",
    "optional",
    "or",
    "otherwise",
    "section",
    "shared",
    "then",
    "try",
    "type",
}


@dataclass
class Finding:
    """One structural problem, located precisely enough to fix without opening Desktop."""

    path: Path
    line: int
    col: int
    kind: str
    detail: str
    snippet: str

    def render(self, root: Path) -> str:
        """One human-readable `file:line:col` block, which is the whole point of this tool."""
        try:
            where = self.path.relative_to(root)
        except ValueError:
            where = self.path
        return f"  {where}:{self.line}:{self.col}  {self.kind}\n      {self.detail}\n      | {self.snippet}"


@dataclass
class Token:
    """One coarse M token, carrying the position needed to localise a finding."""

    kind: str  # "string" | "ident" | "number" | "punct" | "keyword"
    text: str
    line: int
    col: int


class _Scanner:
    """Cursor over an M expression that keeps line/col in sync.

    Split out of the token loop so the scanning mechanics (positions, string escapes, comments)
    stay separate from the token classification, and neither grows unreadable.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.line = 1
        self.col = 1
        self.errors: list[tuple[str, int, int]] = []

    @property
    def done(self) -> bool:
        """True once the cursor has passed the end of the expression."""
        return self.pos >= len(self.text)

    def advance(self, count: int) -> None:
        """Move the cursor `count` characters, tracking newlines so positions stay reportable."""
        for ch in self.text[self.pos : self.pos + count]:
            if ch == "\n":
                self.line += 1
                self.col = 1
            else:
                self.col += 1
        self.pos += count

    def skip_trivia(self) -> bool:
        """Consume whitespace and comments. Returns False on an unterminated block comment."""
        text, i = self.text, self.pos
        if text[i] in " \t\r\n":
            self.advance(1)
            return True
        if text.startswith("//", i):
            end = text.find("\n", i)
            self.advance((len(text) if end == -1 else end) - i)
            return True
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end == -1:
                self.errors.append(("unterminated block comment", self.line, self.col))
                return False
            self.advance(end + 2 - i)
            return True
        return True

    def read_string(self) -> Token | None:
        """Read a `"..."` literal or a `#"quoted identifier"` as ONE token (`""` escapes a quote)."""
        text, i = self.text, self.pos
        start_line, start_col = self.line, self.col
        j = i + (1 if text[i] == "#" else 0) + 1
        while True:
            j = text.find('"', j)
            if j == -1:
                self.errors.append(("unterminated string literal", start_line, start_col))
                return None
            if j > i and text[j - 1] == "\\" and not _is_string_terminator(text, j + 1):
                self.errors.append(
                    (
                        'invalid JSON-style \\" escape; Power Query M uses doubled quotes ("")',
                        self.line,
                        self.col + j - i - 1,
                    )
                )
            if j + 1 < len(text) and text[j + 1] == '"':
                j += 2
                continue
            break
        token = Token("string", text[i : j + 1], start_line, start_col)
        self.advance(j + 1 - i)
        return token


def _is_string_terminator(text: str, index: int) -> bool:
    """Whether text after a quote can legally follow an M string literal."""
    while index < len(text) and text[index].isspace():
        index += 1
    if index == len(text) or text[index] in ",)]}&+-*/=<>&|?":
        return True
    for word in ("as", "catch", "else", "in", "is", "meta", "or", "otherwise", "then"):
        end = index + len(word)
        if text.startswith(word, index) and (end == len(text) or not (text[end].isalnum() or text[end] == "_")):
            return True
    return False


def _tokenize(text: str) -> tuple[list[Token], list[tuple[str, int, int]]]:
    """Split M into coarse tokens. Returns (tokens, fatal_scan_errors).

    Only as precise as the checks need: strings and comments must be skipped correctly or every
    other check produces nonsense (a `{` inside a string is not a delimiter).
    """
    tokens: list[Token] = []
    scanner = _Scanner(text)

    while not scanner.done:
        ch = text[scanner.pos]
        if ch in " \t\r\n" or text.startswith("//", scanner.pos) or text.startswith("/*", scanner.pos):
            if not scanner.skip_trivia():
                break
            continue
        if ch == '"' or text.startswith('#"', scanner.pos):
            token = scanner.read_string()
            if token is None:
                break
            tokens.append(token)
            continue
        if text.startswith('\\"', scanner.pos):
            scanner.errors.append(
                ('invalid JSON-style \\" escape; Power Query M uses doubled quotes ("")', scanner.line, scanner.col)
            )
        match = _NUMBER_RE.match(text, scanner.pos) if ch.isdigit() else None
        if match is None and (ch.isalpha() or ch == "_"):
            # A dotted path (Table.TransformColumnTypes, Int64.Type) is ONE token, so the
            # missing-separator check doesn't see `Int64` `.` `Type` as adjacent values.
            match = _IDENT_RE.match(text, scanner.pos)
        if match is not None:
            word = match.group(0)
            kind = "number" if ch.isdigit() else ("keyword" if word in M_KEYWORDS else "ident")
            tokens.append(Token(kind, word, scanner.line, scanner.col))
            scanner.advance(len(word))
            continue
        tokens.append(Token("punct", ch, scanner.line, scanner.col))
        scanner.advance(1)

    return tokens, scanner.errors


def _snippet(text: str, line: int) -> str:
    lines = text.splitlines()
    return lines[line - 1].strip()[:140] if 0 < line <= len(lines) else ""


def _check_delimiters(tokens: list[Token], add: Callable[[str, str, int, int], None], shift: int) -> None:
    """Balanced ()/[]/{} plus the trailing separator M rejects.

    Split out of `_check_expression` so each check stays independently readable; the balance stack
    and the trailing-comma test share one pass because both key off the closing token.
    """
    stack: list[Token] = []
    for idx, tok in enumerate(tokens):
        if tok.kind != "punct":
            continue
        if tok.text in PAIRS:
            stack.append(tok)
            continue
        if tok.text not in CLOSERS:
            continue
        if not stack:
            add("UNBALANCED", f"closing '{tok.text}' with nothing open", tok.line, tok.col)
        elif stack[-1].text != CLOSERS[tok.text]:
            opener = stack.pop()
            add(
                "UNBALANCED",
                f"'{opener.text}' opened at line {opener.line + shift} is closed by '{tok.text}'",
                tok.line,
                tok.col,
            )
        else:
            stack.pop()
        # M rejects a trailing separator: `{1, 2, }`. This is the classic JSON habit and the most
        # likely single cause of "Token ',' expected".
        prev = tokens[idx - 1] if idx else None
        if prev and prev.kind == "punct" and prev.text == ",":
            add(
                "TRAILING_COMMA",
                f"comma immediately before '{tok.text}' - M rejects a trailing separator "
                "(this is what Desktop reports as \"Token ',' expected\")",
                prev.line,
                prev.col,
            )
    for opener in stack:
        add("UNBALANCED", f"'{opener.text}' is never closed", opener.line, opener.col)


def _bracket_depths(tokens: list[Token]) -> list[int]:
    """Bracket nesting depth at each token, so `[...]` contents can be excluded from keyword counts."""
    depths: list[int] = []
    depth = 0
    for tok in tokens:
        if tok.kind == "punct" and tok.text == "[":
            depth += 1
        depths.append(depth)
        if tok.kind == "punct" and tok.text == "]":
            depth = max(0, depth - 1)
    return depths


def _check_let_in(tokens: list[Token], add: Callable[[str, str, int, int], None]) -> None:
    """Every `let` needs a matching `in`.

    Counted only at bracket depth 0: `let` and `in` are legal generalized FIELD NAMES inside `[...]`
    (`[let = 1]`, `each [in]`), and counting those produced a bogus LET_WITHOUT_IN on valid M.
    """
    depths = _bracket_depths(tokens)
    top = [t for t, d in zip(tokens, depths, strict=True) if d == 0 and t.kind == "keyword"]
    lets = sum(1 for t in top if t.text == "let")
    ins = sum(1 for t in top if t.text == "in")
    if lets > ins:
        first = next(t for t in top if t.text == "let")
        add("LET_WITHOUT_IN", f"{lets} 'let' but {ins} 'in' - every let needs a matching in", first.line, first.col)


def _check_expression(path: Path, text: str, offset_line: int = 0, first_col: int = 0) -> list[Finding]:
    """Structural checks over one M expression.

    `first_col` shifts columns on the expression's FIRST line, which in TMDL starts partway along
    (`source = let ...`). Without it a reported column is short by the length of that prefix, and
    localisation is the entire value of this tool.
    """
    findings: list[Finding] = []
    tokens, scan_errors = _tokenize(text)

    def add(kind: str, detail: str, line: int, col: int) -> None:
        shifted = col + first_col if line == 1 else col
        findings.append(Finding(path, line + offset_line, shifted, kind, detail, _snippet(text, line)))

    for message, line, col in scan_errors:
        if message.startswith("invalid JSON-style"):
            add("INVALID_STRING_ESCAPE", message, line, col)
        else:
            add("UNTERMINATED", f"{message} - the expression ends inside it", line, col)

    _check_delimiters(tokens, add, offset_line)
    _check_let_in(tokens, add)
    _check_transform_column_type_pairs(tokens, add)

    findings.extend(_check_missing_separator(path, text, tokens, offset_line, first_col))
    return findings


def _check_transform_column_type_pairs(tokens: list[Token], add: Callable[[str, str, int, int], None]) -> None:
    """Check that TransformColumnTypes receives a list of `{columnName, type}` pairs."""
    for index, token in enumerate(tokens[:-1]):
        if token.kind != "ident" or token.text != "Table.TransformColumnTypes" or tokens[index + 1].text != "(":
            continue
        arguments = _function_arguments(tokens, index + 1)
        if len(arguments) < 2:
            continue
        pairs = arguments[1]
        if len(pairs) < 2 or pairs[0].text != "{" or pairs[-1].text != "}":
            continue
        entries = pairs[1:-1]
        if not entries:
            continue
        for entry in _split_top_level(entries):
            if not entry or entry[0].text != "{":
                continue
            if len(entry) < 2 or entry[-1].text != "}":
                _add_invalid_transform_pair(add, entry[0], "each literal entry must be a `{columnName, type}` pair")
                continue
            values = _split_top_level(entry[1:-1])
            if len(values) != 2 or any(not value or value[0].text == "{" for value in values):
                _add_invalid_transform_pair(add, entry[0], "each entry must contain a column name and one type")


def _function_arguments(tokens: list[Token], open_index: int) -> list[list[Token]]:
    """Return top-level arguments for a balanced function call, or none when it is incomplete."""
    arguments: list[list[Token]] = [[]]
    stack = ["("]
    for token in tokens[open_index + 1 :]:
        if token.kind == "punct" and token.text in PAIRS:
            stack.append(token.text)
        elif token.kind == "punct" and token.text in CLOSERS:
            if not stack or stack[-1] != CLOSERS[token.text]:
                return []
            stack.pop()
            if not stack:
                return arguments
        if token.kind == "punct" and token.text == "," and len(stack) == 1:
            arguments.append([])
        else:
            arguments[-1].append(token)
    return []


def _split_top_level(tokens: list[Token]) -> list[list[Token]]:
    """Split comma-separated tokens, ignoring commas inside nested M expressions."""
    values: list[list[Token]] = [[]]
    depth = 0
    for token in tokens:
        if token.kind == "punct" and token.text in PAIRS:
            depth += 1
        elif token.kind == "punct" and token.text in CLOSERS:
            depth -= 1
        if token.kind == "punct" and token.text == "," and depth == 0:
            values.append([])
        else:
            values[-1].append(token)
    return values


def _add_invalid_transform_pair(add: Callable[[str, str, int, int], None], token: Token, detail: str) -> None:
    """Report the refresh-only TransformColumnTypes pair-shape failure at its entry."""
    add(
        "INVALID_TRANSFORM_COLUMN_TYPE_PAIR",
        f"Table.TransformColumnTypes {detail}; extra brace nesting passes syntax but fails refresh",
        token.line,
        token.col,
    )


def _check_missing_separator(
    path: Path, text: str, tokens: list[Token], offset_line: int, first_col: int = 0
) -> list[Finding]:
    """Two values side by side inside a list literal, i.e. a dropped comma.

    Deliberately scoped to `{...}` only. `[...]` is ambiguous in M: it is a record literal
    (`[a=1, b=2]`, separator required) but far more often FIELD ACCESS (`each [Quarter Number]`),
    where the field name legitimately contains spaces. Checking brackets produced false positives on
    known-good models, and a checker that cries wolf gets ignored - which would defeat the point.
    """
    findings: list[Finding] = []
    value_end = {"string", "ident", "number"}
    context: list[str] = []

    for idx, tok in enumerate(tokens):
        if tok.kind == "punct" and tok.text in PAIRS:
            context.append(tok.text)
            continue
        if tok.kind == "punct" and tok.text in CLOSERS:
            if context:
                context.pop()
            continue
        if not context or context[-1] != "{":
            continue
        prev = tokens[idx - 1] if idx else None
        if prev is None or prev.kind not in value_end:
            continue
        if tok.kind in value_end:
            findings.append(
                Finding(
                    path,
                    tok.line + offset_line,
                    tok.col + (first_col if tok.line == 1 else 0),
                    "MISSING_SEPARATOR",
                    f"'{prev.text}' is followed by '{tok.text}' with no comma between them inside a '{{' list literal",
                    _snippet(text, tok.line),
                )
            )
    return findings


def _collect_body(lines: list[str], idx: int, indent: int, metadata_re: re.Pattern[str]) -> tuple[list[str], int]:
    """Continuation lines of one TMDL expression, and where scanning should resume.

    A TMDL metadata key only ends the block when it sits at a SIBLING indent. Matching it anywhere
    truncated valid M that merely uses one as an identifier (`let mode = 1 in mode`), which then
    reported a bogus LET_WITHOUT_IN.
    """
    body: list[str] = []
    while idx < len(lines):
        current = lines[idx]
        if current.strip() and (len(current) - len(current.lstrip())) <= indent:
            break
        metadata = metadata_re.match(current)
        if metadata and len(metadata.group(1)) <= indent + 1:
            break
        body.append(current)
        idx += 1
    return body, idx


def _iter_m_blocks(path: Path) -> list[tuple[str, int, int]]:  # pylint: disable=too-many-locals
    """Every M expression in a .tmdl file, with the line it starts on, and its starting column.

    One cohesive line scanner; splitting it further would spread the TMDL shape knowledge across
    helpers that each only make sense together, so the local count is accepted deliberately.

    Two shapes carry M: a model-level `expression <Name> = <M>` (expressions.tmdl) and a table
    partition declared `partition <Name> = m` (tables/*.tmdl).

    A partition declared `= calculated` holds **DAX**, not M - different language, different rules
    (a DAX table constructor legitimately contains things this checker would call a missing
    separator). Reading those as M produced 64 false positives across the committed examples, so the
    partition's declared source type decides whether its `source =` is checked at all.
    """
    text = path.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff")
    lines = text.splitlines()
    blocks: list[tuple[str, int]] = []
    partition_re = re.compile(r"^\s*partition\s+.+?=\s*(\w+)\s*$")
    # `'quoted name'` first: TMDL requires quoting a name containing '=', and a non-greedy match
    # would otherwise stop at the wrong equals sign and feed the tail into the M scanner.
    starters = re.compile(r"^(\s*)(?:expression\s+(?:'[^']*'|[^=]+?)\s*=|source\s*=)\s*(.*)$")
    # TMDL metadata keys only terminate the block at the SAME indent as a sibling property. Matching
    # them anywhere truncated valid M that merely uses one as an identifier (`let mode = 1 in mode`),
    # which then reported a bogus LET_WITHOUT_IN.
    metadata_re = re.compile(r"^(\s*)(lineageTag|annotation|queryGroup|mode|dataType|isHidden)\b")

    idx = 0
    current_partition_kind: str | None = None
    while idx < len(lines):
        partition_match = partition_re.match(lines[idx])
        if partition_match:
            current_partition_kind = partition_match.group(1).lower()
            idx += 1
            continue
        match = starters.match(lines[idx])
        if not match:
            idx += 1
            continue
        is_source = lines[idx].lstrip().startswith("source")
        if is_source and current_partition_kind not in (None, "m"):
            idx += 1
            continue
        indent = len(match.group(1))
        # Column of the M text on the starter line, so a finding on it points at the real column.
        first_col = len(lines[idx]) - len(match.group(2))
        body = [match.group(2)]
        start = idx + 1
        idx += 1
        while idx < len(lines):
            current = lines[idx]
            if current.strip() and (len(current) - len(current.lstrip())) <= indent:
                break
            metadata = metadata_re.match(current)
            if metadata and len(metadata.group(1)) <= indent + 1:
                break
            body.append(current)
            idx += 1
        blocks.append(("\n".join(body), start, first_col))
    return blocks


def _model_dirs(target: Path) -> list[Path]:
    if target.name.endswith(".SemanticModel"):
        return [target]
    return sorted(target.glob("**/*.SemanticModel"))


def _call_arguments(text: str, start: int) -> list[str]:
    """Top-level arguments in a call whose opening parenthesis ends at ``start``."""
    depth, index, in_string, argument_start = 1, start, False, start
    arguments: list[str] = []
    while index < len(text) and depth:
        char = text[index]
        if in_string:
            if char == '"' and text[index : index + 2] == '""':
                index += 2
                continue
            if char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 1:
            arguments.append(text[argument_start:index])
            argument_start = index + 1
        if depth == 0:
            arguments.append(text[argument_start:index])
        index += 1
    return arguments if depth == 0 else []


def _m_code(text: str) -> str:
    """Remove M comments without mistaking their contents for live navigation expressions."""
    out: list[str] = []
    index, in_string = 0, False
    while index < len(text):
        char, next_char = text[index], text[index + 1 : index + 2]
        if in_string:
            out.append(char)
            if char == '"' and next_char == '"':
                out.append(next_char)
                index += 2
                continue
            if char == '"':
                in_string = False
        elif char == '"':
            out.append(char)
            in_string = True
        elif char == "/" and next_char == "/":
            end = text.find("\n", index)
            end = len(text) if end == -1 else end
            out.extend(" " for _ in text[index:end])
            index = end
            continue
        elif char == "/" and next_char == "*":
            end = text.find("*/", index + 2)
            end = len(text) if end == -1 else end + 2
            out.extend(char if char == "\n" else " " for char in text[index:end])
            index = end
            continue
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _m_code_without_strings(text: str) -> str:
    """Mask string literals while preserving `#"quoted identifiers"` and source offsets."""
    out = list(text)
    index = 0
    while index < len(text):
        if text[index] != '"':
            index += 1
            continue
        start = index
        index += 1
        while index < len(text):
            if text[index] == '"' and text[index : index + 2] == '""':
                index += 2
                continue
            if text[index] == '"':
                index += 1
                break
            index += 1
        if start and text[start - 1] == "#":
            continue
        for position in range(start, index):
            if out[position] != "\n":
                out[position] = " "
    return "".join(out)


def _is_biff8_xls(source: Path) -> bool:
    """True only when a `.xls` really is a legacy BIFF8/OLE2 workbook, decided by its magic bytes.

    The suffix alone is deliberately not enough, and that is the whole point of the gate: an `.xlsx`
    (or a CSV) merely *named* `.xls` is read by a different provider whose navigation table does have
    `Item`/`Kind` columns, so flagging it would be a false positive on correct M.
    """
    if source.suffix.lower() != ".xls":
        return False
    try:
        with source.open("rb") as handle:
            return handle.read(4) == _BIFF8_MAGIC
    except OSError:
        return False


def _navigation_key(executable_code: str, assignments: list[re.Match[str]], before: int) -> str | None:
    """The record key of the `<binding>{[...]}[Data]` navigation off the workbook bound before ``before``."""
    assignment = next((item for item in reversed(assignments) if item.end() <= before), None)
    name = assignment.group("name") if assignment else ""
    identifier = re.escape(name) if name.startswith('#"') else rf'(?<![A-Za-z0-9_"]){re.escape(name)}(?![A-Za-z0-9_"])'
    navigation = re.search(rf"{identifier}\s*\{{\s*\[(?P<key>[^\]]*)\]\s*\}}\s*\[\s*Data\s*\]", executable_code)
    return navigation.group("key") if navigation else None


def _lacks_explicit_culture(arguments: list[str]) -> bool:
    """A type conversion needs a non-null culture, including inside an options record.

    A literal `null` is NOT "explicit": it selects the ambient locale, which is exactly the silent
    decimal/date corruption this rule exists to stop.
    """
    if len(arguments) < 3 or arguments[2].strip().lower() == "null":
        return True
    culture = arguments[2].strip()
    if not (culture.startswith("[") and culture.endswith("]")):
        return False
    match = re.search(r"\bCulture\s*=", _m_code_without_strings(culture))
    if match is None:
        return True
    return re.match(r"null\b", culture[match.end() :].lstrip(), re.IGNORECASE) is not None


def _biff8_violations(
    code: str, executable_code: str, parameters: dict[str, str]
) -> Iterator[tuple[int, list[tuple[str, str]]]]:
    """Every legacy BIFF8 binding in one M block, as (offset, broken (kind, detail) rules).

    A partition with NO `Table.TransformColumnTypes` is silent on culture on purpose: the engine emits
    the typed step only when it has type pairs, and "pass a culture" is unactionable advice about a
    conversion that does not exist.
    """
    assignments = list(_EXCEL_ASSIGNMENT_RE.finditer(executable_code))
    conversions = [_call_arguments(code, call.end()) for call in _TYPE_CONVERSION_RE.finditer(executable_code)]
    implicit_culture = any(_lacks_explicit_culture(arguments) for arguments in conversions)
    for match in _FILE_CONTENTS_RE.finditer(executable_code):
        arguments = _call_arguments(code, match.end())
        resolved = eval_m_path(arguments[0], parameters) if arguments else None
        if resolved is None or not _is_biff8_xls(Path(resolved)):
            continue
        broken: list[tuple[str, str]] = []
        key = _navigation_key(executable_code, assignments, match.start())
        if key is None or not re.search(r"\bName\s*=", key):
            broken.append(("BIFF8_XLS_NAVIGATION_KEY", _BIFF8_NAVIGATION_DETAIL))
        if implicit_culture:
            broken.append(("BIFF8_XLS_CULTURE", _BIFF8_CULTURE_DETAIL))
        if broken:
            yield match.start(), broken


def _legacy_xls_findings(
    path: Path, text: str, offset_line: int, first_col: int, parameters: dict[str, str]
) -> list[Finding]:
    """Check the two BIFF8-only M requirements when the referenced local file is available."""
    code = _m_code(text)
    findings: list[Finding] = []
    for start, violations in _biff8_violations(code, _m_code_without_strings(code), parameters):
        line = offset_line + text.count("\n", 0, start) + 1
        col = start - text.rfind("\n", 0, start)
        if line == offset_line + 1:
            col += first_col
        snippet = _snippet(text, line - offset_line)
        findings.extend(Finding(path, line, col, kind, detail, snippet) for kind, detail in violations)
    return findings


def check_model(model_dir: Path) -> list[Finding]:
    """Every M expression in one .SemanticModel."""
    return check_model_counted(model_dir)[0]


def check_model_counted(model_dir: Path) -> tuple[list[Finding], int]:
    """Findings plus HOW MANY M expressions were actually scanned.

    The count matters: "no findings" and "nothing was checked" are the same output otherwise, and a
    missing/empty/unreadable model would report a reassuring "M syntax OK" while proving nothing.
    """
    findings: list[Finding] = []
    scanned = 0
    definition = model_dir / "definition"
    parameters = model_parameters(model_dir)
    files = sorted(definition.glob("expressions.tmdl")) + sorted(definition.glob("tables/*.tmdl"))
    for tmdl in files:
        for block, start_line, first_col in _iter_m_blocks(tmdl):
            if block.strip():
                scanned += 1
                findings.extend(_check_expression(tmdl, block, offset_line=start_line - 1, first_col=first_col))
                findings.extend(_legacy_xls_findings(tmdl, block, start_line - 1, first_col, parameters))
    return findings, scanned


def check_datamodel(model_dir: Path) -> tuple[list[Finding], int, list[TmdlFinding], int]:
    """Run the complete dependency-free model gate over one .SemanticModel."""
    m_findings, m_scanned = check_model_counted(model_dir)
    tmdl_findings, tmdl_scanned = check_tmdl_model(model_dir)
    return m_findings, m_scanned, tmdl_findings, tmdl_scanned


class Unassessable(RuntimeError):
    """The TMDL oracle could not run, so the model's loadability is UNKNOWN - never 'clean'."""


def _run_oracle(targets: list[Path], skip: bool) -> tuple[int, bool]:
    """Run the TMDL oracle over every target; returns (problems reported, ran to completion).

    The oracle is MANDATORY by default. "Could not run" raises Unassessable, which `main` turns
    into EXIT_UNASSESSABLE - never 0. That is deliberate and it is the one behaviour here with a
    field history: while it was merely a warning, `check_datamodel.py <model>` exited 0 on
    parser-fatal TMDL whenever `dotnet` was missing, and `check_unit.py` recorded a PASS for a model
    Desktop cannot open. Unassessable collapsing into clean is the exact defect class this whole
    repo keeps finding; a gate of record must not contain it.
    """
    if skip:
        log.warning(
            "TMDL ORACLE SKIPPED (--no-oracle) - whether these models LOAD was NOT checked. "
            "This is an explicit opt-out, not a clean result."
        )
        return 0, False
    findings, inspected = check_models(targets)
    by_model: dict[Path, list[TmdlFinding]] = {}
    for finding in findings:
        by_model.setdefault(finding.file, []).append(finding)
    for _, group in sorted(by_model.items()):
        log.error("TMDL PARSER ERRORS")
        for finding in group:
            log.error("%s", finding.render(REPO_ROOT))
    if inspected == 0:
        raise Unassessable("no definition/ folder in any target, so no model was handed to the parser")
    log.info("TMDL oracle: %d model(s) handed to TmdlSerializer (AMO), %d problem(s).", inspected, len(findings))
    return len(findings), True


def main(argv: list[str] | None = None) -> int:  # pylint: disable=too-many-locals,too-many-branches
    """CLI entry point: check every requested model and exit non-zero if anything is structurally wrong."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path, help="a .SemanticModel, or a folder containing some")
    parser.add_argument("--all", action="store_true", help="check every model in every migration tree")
    parser.add_argument(
        "--no-oracle",
        action="store_true",
        help="skip the TMDL oracle - whether the models LOAD is then not checked at all",
    )
    args = parser.parse_args(argv)

    targets: list[Path] = []
    if args.all or not args.paths:
        for tree in TREES:
            targets.extend(sorted((REPO_ROOT / tree).glob("*/fabric/*.SemanticModel")))
    for raw in args.paths:
        targets.extend(_model_dirs(raw.resolve()))

    if not targets:
        log.error("No .SemanticModel folders found - nothing was checked. This is NOT a pass.")
        return 2

    total = 0
    m_scanned_total = 0
    tmdl_scanned_total = 0
    empty_m: list[Path] = []
    empty_tmdl: list[Path] = []
    for model in targets:
        m_findings, m_scanned, tmdl_findings, tmdl_scanned = check_datamodel(model)
        m_scanned_total += m_scanned
        tmdl_scanned_total += tmdl_scanned
        if m_scanned == 0:
            empty_m.append(model)
        if tmdl_scanned == 0:
            empty_tmdl.append(model)
        if m_findings:
            total += len(m_findings)
            log.error("M SYNTAX ERRORS in %s", model.relative_to(REPO_ROOT) if REPO_ROOT in model.parents else model)
            for finding in m_findings:
                log.error("%s", finding.render(REPO_ROOT))
        if tmdl_findings:
            total += len(tmdl_findings)
            log.error(
                "TMDL STRUCTURAL ERRORS in %s", model.relative_to(REPO_ROOT) if REPO_ROOT in model.parents else model
            )
            for finding in tmdl_findings:
                log.error("%s", finding.render(REPO_ROOT))

    if empty_m:
        # "clean" and "nothing was checked" must never look the same - an agent would read the
        # reassuring line as proof its model is fine.
        log.warning("NOT CHECKED - no M expressions found in %d target(s):", len(empty_m))
        for model in empty_m:
            log.warning("  %s", model)
        log.warning("  (missing definition/, no `= m` partitions, or an unreadable path)")
    if empty_tmdl:
        log.warning("NOT CHECKED - no TMDL documents found in %d target(s):", len(empty_tmdl))
        for model in empty_tmdl:
            log.warning("  %s", model)

    try:
        oracle_problems, oracle_ran = _run_oracle(targets, args.no_oracle)
    except (OracleUnavailable, Unassessable) as exc:
        log.error(
            "TMDL ORACLE COULD NOT RUN, so whether these models LOAD is UNKNOWN. This is NOT a "
            "pass and NOT a finding - it is unassessable (exit %d).\n  %s\n  Fix the oracle, or pass "
            "--no-oracle to state explicitly that you are skipping it.",
            EXIT_UNASSESSABLE,
            exc,
        )
        return EXIT_UNASSESSABLE
    total += oracle_problems
    if total:
        log.error(
            "\n%d problem(s) across %d model(s).\nThese are structural checks for defects that "
            "otherwise surface later as opaque Power BI Desktop model-load failures.",
            total,
            len(targets),
        )
        return 1
    if m_scanned_total == 0 or tmdl_scanned_total == 0:
        log.warning(
            "NOTHING CHECKED - scanned %d M expression(s) and %d TMDL document(s) across %d target(s).",
            m_scanned_total,
            tmdl_scanned_total,
            len(targets),
        )
        return 1
    log.info(
        "Data model OK - %d M expression(s) and %d TMDL document(s) across %d model(s), no structural "
        "problems found.\n  NOTE: structural checks only%s; a clean result does not prove the model "
        "refreshes.",
        m_scanned_total,
        tmdl_scanned_total,
        len(targets),
        "" if oracle_ran else ", and the TMDL oracle did NOT run",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
