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
  * empty measure expressions
  * direct CALCULATE/CALCULATETABLE compact filters that compare a column to a measure

A clean result does NOT prove the model opens; it only excludes these structural classes.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TREES = ("examples", "migrations/workbooks", "migrations/datasources")

log = logging.getLogger("check_datamodel")

PAIRS = {"(": ")", "[": "]", "{": "}"}
CLOSERS = {v: k for k, v in PAIRS.items()}
_NUMBER_RE = re.compile(r"0[xX][0-9a-fA-F]+|[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")

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


@dataclass
class TmdlFinding:
    """One TMDL/data-model problem, located precisely enough to fix without hunting."""

    code: str
    message: str
    file: Path
    line: int

    def render(self, root: Path) -> str:
        """Format as `path:line CODE`, matching the M finding output style."""
        try:
            shown = self.file.relative_to(root)
        except ValueError:
            shown = self.file
        return f"  {shown}:{self.line}  {self.code}\n      {self.message}"


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
            if j + 1 < len(text) and text[j + 1] == '"':
                j += 2
                continue
            break
        token = Token("string", text[i : j + 1], start_line, start_col)
        self.advance(j + 1 - i)
        return token


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
        add("UNTERMINATED", f"{message} - the expression ends inside it", line, col)

    _check_delimiters(tokens, add, offset_line)
    _check_let_in(tokens, add)

    findings.extend(_check_missing_separator(path, text, tokens, offset_line, first_col))
    return findings


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


_OBJECT_RE = re.compile(
    r"^(?P<indent>\s*)(?P<kind>table|measure|column|partition|hierarchy|level|role|"
    r"relationship|culture|expression|calculationGroup|calculationItem|perspective|model|database)\b"
    r"(?P<rest>.*)$"
)
_PROPERTY_RE = re.compile(r"^(?P<indent>\s*)(?P<name>[A-Za-z][A-Za-z0-9_]*)\s*:\s*(?P<value>.*)$")
_REPEATABLE_PROPERTIES = {"annotation", "extendedProperty", "changedProperty", "ref", "variation", "levels"}
_COLUMN_EQ_MEASURE_RE = re.compile(r"^'?[^'\[\]]+'?\s*\[[^\]]+\]\s*(?:=|<>|<=|>=|<|>)\s*\[[^\]]+\]$")
_CALCULATE_RE = re.compile(r"\bCALCULATE(?:TABLE)?\s*\(", re.IGNORECASE)


def _split_top_level_args(text: str) -> list[str]:
    """Split a function argument list on commas that are not nested inside another expression."""
    args: list[str] = []
    depth = 0
    start = 0
    in_string = False
    idx = 0
    while idx < len(text):
        ch = text[idx]
        if ch == '"':
            if in_string and idx + 1 < len(text) and text[idx + 1] == '"':
                idx += 2
                continue
            in_string = not in_string
        elif not in_string:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth = max(0, depth - 1)
            elif ch == "," and depth == 0:
                args.append(text[start:idx])
                start = idx + 1
        idx += 1
    args.append(text[start:])
    return args


def find_compact_filters(expression: str) -> bool:
    """Return True for direct CALCULATE filters like `'T'[Col] = [Measure]`.

    The same predicate is legal inside FILTER(...), so the check only inspects direct filter
    arguments after the first CALCULATE/CALCULATETABLE expression argument.
    """
    for match in _CALCULATE_RE.finditer(expression):
        depth = 1
        end: int | None = None
        in_string = False
        idx = match.end()
        while idx < len(expression):
            ch = expression[idx]
            if ch == '"':
                if in_string and idx + 1 < len(expression) and expression[idx + 1] == '"':
                    idx += 2
                    continue
                in_string = not in_string
            elif not in_string:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        end = idx
                        break
            idx += 1
        if end is None:
            continue
        for arg in _split_top_level_args(expression[match.end() : end])[1:]:
            if _COLUMN_EQ_MEASURE_RE.match(arg.strip()):
                return True
    return False


def _strip_tmdl_comment(line: str) -> str:
    """Remove a TMDL description/comment line so prose cannot be mistaken for a property."""
    return "" if line.lstrip().startswith("///") else line


def _object_name(rest: str) -> str | None:
    """Extract a TMDL object's name from the remainder of its header line."""
    head = rest.split("=", 1)[0].strip()
    if head.startswith("'"):
        end = head.find("'", 1)
        return head[1:end] if end > 0 else None
    parts = head.split()
    return parts[0] if parts else None


def _expression_on_measure_header(rest: str) -> bool:
    """Whether a measure header carries a non-empty expression after `=`."""
    return "=" in rest and bool(rest.split("=", 1)[1].strip())


def _nearest_context(seen: dict[int, dict[str, int]], indent: int) -> int | None:
    """Return the nearest open TMDL object context for a property indent."""
    candidates = [depth for depth in seen if depth <= indent]
    return min(candidates, key=lambda depth: indent - depth) if candidates else None


def _empty_measure_finding(path: Path, pending_measure: tuple[str, int]) -> TmdlFinding:
    """Build the repeated empty-measure finding once so narrowing stays simple."""
    name, line = pending_measure
    return TmdlFinding("EMPTY_EXPRESSION", f"measure '{name}' has no expression after '='.", path, line)


def _append_name_collisions(
    findings: list[TmdlFinding], path: Path, table_name: str, measures: dict[str, int], columns: dict[str, int]
) -> None:
    """Report measure/column collisions for one table scope."""
    for name, line in measures.items():
        if name in columns:
            findings.append(
                TmdlFinding(
                    "NAME_COLLISION",
                    f"measure '{name}' in table '{table_name}' has the same name as a column in "
                    f"that table; Tabular names must be unique within a table. Column first seen "
                    f"at line {columns[name]}.",
                    path,
                    line,
                )
            )


def check_tmdl_text(path: Path, text: str) -> list[TmdlFinding]:  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    """Structurally check one TMDL document."""
    findings: list[TmdlFinding] = []
    seen: dict[int, dict[str, int]] = {}
    context_line: dict[int, int] = {}
    current_table = ""
    measures: dict[str, int] = {}
    columns: dict[str, int] = {}
    pending_measure: tuple[str, int] | None = None

    for number, raw in enumerate(text.splitlines(), start=1):
        line = _strip_tmdl_comment(raw)
        if not line.strip():
            continue

        if pending_measure and _OBJECT_RE.match(line):
            findings.append(_empty_measure_finding(path, pending_measure))
            pending_measure = None

        if find_compact_filters(line):
            findings.append(
                TmdlFinding(
                    "COMPACT_FILTER",
                    "direct CALCULATE filter compares a column to a measure; DAX requires a constant "
                    "on the right of a compact predicate. Hoist the measure into a VAR or wrap the "
                    "predicate in FILTER(...).",
                    path,
                    number,
                )
            )

        obj = _OBJECT_RE.match(line)
        if obj:
            indent = len(obj.group("indent").expandtabs(4))
            for depth in [depth for depth in seen if depth > indent]:
                del seen[depth]
            seen[indent + 1] = {}
            context_line[indent + 1] = number
            kind = obj.group("kind")
            rest = obj.group("rest")
            name = _object_name(rest)
            if kind == "table" and name:
                _append_name_collisions(findings, path, current_table, measures, columns)
                current_table = name
                measures = {}
                columns = {}
            elif kind == "measure" and name and current_table:
                measures.setdefault(name, number)
                pending_measure = None if _expression_on_measure_header(rest) else (name, number)
            elif kind == "column" and name and current_table:
                columns.setdefault(name, number)
            continue

        prop = _PROPERTY_RE.match(line)
        if prop:
            indent = len(prop.group("indent").expandtabs(4))
            name = prop.group("name")
            if name not in _REPEATABLE_PROPERTIES:
                bucket = _nearest_context(seen, indent)
                if bucket is not None:
                    if name in seen[bucket]:
                        findings.append(
                            TmdlFinding(
                                "DUPLICATE_PROPERTY",
                                f"'{name}' appears more than once in the object opened at line "
                                f"{context_line[bucket]}; TMDL rejects duplicated scalar properties. "
                                f"First seen at line {seen[bucket][name]}.",
                                path,
                                number,
                            )
                        )
                    else:
                        seen[bucket][name] = number
            if pending_measure:
                findings.append(_empty_measure_finding(path, pending_measure))
                pending_measure = None
            continue

        if pending_measure and line.strip():
            pending_measure = None

    if pending_measure:
        findings.append(_empty_measure_finding(path, pending_measure))

    _append_name_collisions(findings, path, current_table, measures, columns)
    return findings


def _model_dirs(target: Path) -> list[Path]:
    if target.name.endswith(".SemanticModel"):
        return [target]
    return sorted(target.glob("**/*.SemanticModel"))


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
    files = sorted(definition.glob("expressions.tmdl")) + sorted(definition.glob("tables/*.tmdl"))
    for tmdl in files:
        for block, start_line, first_col in _iter_m_blocks(tmdl):
            if block.strip():
                scanned += 1
                findings.extend(_check_expression(tmdl, block, offset_line=start_line - 1, first_col=first_col))
    return findings, scanned


def check_tmdl_model(model_dir: Path) -> tuple[list[TmdlFinding], int]:
    """Every TMDL document in one .SemanticModel."""
    findings: list[TmdlFinding] = []
    definition = model_dir / "definition"
    files = sorted(definition.rglob("*.tmdl")) if definition.exists() else []
    for tmdl in files:
        text = tmdl.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff")
        findings.extend(check_tmdl_text(tmdl, text))
    return findings, len(files)


def check_datamodel(model_dir: Path) -> tuple[list[Finding], int, list[TmdlFinding], int]:
    """Run the complete dependency-free model gate over one .SemanticModel."""
    m_findings, m_scanned = check_model_counted(model_dir)
    tmdl_findings, tmdl_scanned = check_tmdl_model(model_dir)
    return m_findings, m_scanned, tmdl_findings, tmdl_scanned


def main(argv: list[str] | None = None) -> int:  # pylint: disable=too-many-locals,too-many-branches
    """CLI entry point: check every requested model and exit non-zero if anything is structurally wrong."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path, help="a .SemanticModel, or a folder containing some")
    parser.add_argument("--all", action="store_true", help="check every model in every migration tree")
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

    if total:
        log.error(
            "\n%d problem(s) across %d model(s).\nThese are dependency-free structural checks for "
            "defects that otherwise surface later as opaque Power BI Desktop model-load failures.",
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
        "problems found.\n  NOTE: structural checks only; a clean result does not prove the model opens.",
        m_scanned_total,
        tmdl_scanned_total,
        len(targets),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
