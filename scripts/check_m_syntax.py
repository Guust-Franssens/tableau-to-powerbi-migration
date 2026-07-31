"""
purpose: Catch Power Query M syntax errors in a semantic model BEFORE Power BI Desktop does.
usage:   python scripts/check_m_syntax.py [<path to .SemanticModel or migration folder> ...]
         python scripts/check_m_syntax.py --all        # every model in every migration tree

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

log = logging.getLogger("check_m_syntax")

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

    # `in` closes a `let` step list, so `B = A,` followed by `in` is the same trailing-separator
    # defect - and it is the shape agent-generated M actually produces most often (an extra step is
    # deleted, its comma is left behind). It was missed for a long time because the loop above only
    # considers PUNCT closers, and `in` tokenises as a keyword. Measured: seeding exactly this into a
    # real model passed `check_m_syntax` clean while Power BI Desktop rejected the file.
    # Depth 0 only: `in` is a legal generalized field name inside `[...]` (`each [in]`).
    depths = _bracket_depths(tokens)
    for idx, (tok, depth) in enumerate(zip(tokens, depths, strict=True)):
        if depth or tok.kind != "keyword" or tok.text != "in" or idx == 0:
            continue
        prev = tokens[idx - 1]
        if prev.kind == "punct" and prev.text == ",":
            add(
                "TRAILING_COMMA",
                "comma immediately before 'in' - M rejects a trailing separator at the end of a "
                "let step list (this is what Desktop reports as \"Token ',' expected\")",
                prev.line,
                prev.col,
            )


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


def main() -> int:
    """CLI entry point: check every requested model and exit 1 if anything is structurally wrong."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path, help="a .SemanticModel, or a folder containing some")
    parser.add_argument("--all", action="store_true", help="check every model in every migration tree")
    args = parser.parse_args()

    targets: list[Path] = []
    if args.all or not args.paths:
        for tree in TREES:
            targets.extend(sorted((REPO_ROOT / tree).glob("*/fabric/*.SemanticModel")))
    for raw in args.paths:
        targets.extend(_model_dirs(raw.resolve()))

    if not targets:
        # Exiting 0 here was a FALSE GREEN in a mandatory gate: an agent that mistypes the model path
        # (or points at the .pbip folder's parent) got "clean" and handed the model on unchecked.
        # A gate that cannot find its subject has not passed - it has not run.
        log.error(
            "No .SemanticModel folders found under: %s. Nothing was checked - this is NOT a pass. "
            "Point this at the folder containing <Name>.SemanticModel, or run with --all.",
            ", ".join(str(p) for p in args.paths) or "(the repo's migration trees)",
        )
        return 2

    total = 0
    scanned_total = 0
    empty: list[Path] = []
    for model in targets:
        findings, scanned = check_model_counted(model)
        scanned_total += scanned
        if scanned == 0:
            empty.append(model)
        if findings:
            total += len(findings)
            log.error("M SYNTAX ERRORS in %s", model.relative_to(REPO_ROOT) if REPO_ROOT in model.parents else model)
            for finding in findings:
                log.error("%s", finding.render(REPO_ROOT))

    if empty:
        # "clean" and "nothing was checked" must never look the same - an agent would read the
        # reassuring line as proof its model is fine.
        log.warning("NOT CHECKED - no M expressions found in %d target(s):", len(empty))
        for model in empty:
            log.warning("  %s", model)
        log.warning("  (missing definition/, no `= m` partitions, or an unreadable path)")

    if total:
        log.error(
            "\n%d problem(s) across %d model(s).\nThese are what Power BI Desktop reports as the "
            "unlocalised \"M Engine error: Token ',' expected\" - fix them before opening the .pbip.",
            total,
            len(targets),
        )
        return 1
    if scanned_total == 0:
        log.warning("NOTHING CHECKED - 0 M expressions scanned across %d target(s).", len(targets))
        return 1
    log.info(
        "M syntax OK - %d M expression(s) across %d model(s), no structural problems found.\n"
        "  NOTE: structural checks only (delimiters, separators, let/in, strings). This is not a full "
        "M parser - a clean result does not prove the model opens.",
        scanned_total,
        len(targets),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
