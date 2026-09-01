"""Where a string literal can reach a console at RUNTIME, and how to find one that would break it.

Not named ``test_*``: it is the scanner the console gate uses, kept importable so each of its branches
can be mutated INDEPENDENTLY. That is the round-8 lesson in one sentence -- the gate scanned four call
shapes and had ONE positive control, so three branches were asserted by construction rather than
proved, and four idiomatic writes inside its claimed coverage were missed:

===============================  ========================================================
missed                           why
===============================  ========================================================
``print("x", end="\u26a0")``          keyword values were not scanned at all
``print("x", "y", sep="\u26a0")``     same
``LOG.warning(msg="\u26a0")``         logging's message can be a keyword
``LOG.log(30, "\u26a0")``             ``log`` was absent from the method set
===============================  ========================================================

⚠️ **Why a console gate exists at all.** A default Windows console is CP1252. Measured on this
repository twice in one day: the census printed ``VERDICT: CANNOT-TELL`` and then died with
``UnicodeEncodeError``, exit 1 -- destroying the exit-2 guarantee at the last line -- and a sibling
crashed ``--help`` when docstring glyphs became argparse's ``description``.

⚠️ **The rule, and its deliberate limit.** Non-ASCII is fine in comments, in docstrings that are
never printed, and in test names; it is not safe in anything written to a console at runtime. The
scanner only sees **literals in argument position**, so a string built elsewhere and passed in, a
``sys.stdout.write``, or a third-party exception's text remain outside it. That residual is real and
is stated rather than implied.
"""

from __future__ import annotations

import ast
from pathlib import Path

#: Calls whose positional arguments are written out.
RUNTIME_WRITES = {"print", "SystemExit"}

#: Logging methods whose first positional argument is the message. ``log`` takes a level first, but
#: scanning every positional argument covers both shapes without special-casing.
LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log"}

#: ``print``'s output-bearing keywords. They are printed verbatim, so a glyph here is as fatal as one
#: in the message -- and being separators they are exactly where a decorative character gets used.
PRINT_KEYWORDS = {"end", "sep"}

#: ``logging``'s message keyword. ``LOG.warning(msg="...")`` is ordinary and was invisible.
LOG_KEYWORDS = {"msg"}

#: argparse text that reaches the console through ``--help``.
ARGPARSE_TEXT = {"description", "help", "epilog"}


def runtime_non_ascii(path: Path) -> list[str]:
    """Every non-ASCII string literal that can reach a console at runtime, as ``file:line via how``."""
    findings: list[str] = []

    def scan(node: ast.AST, where: str) -> None:
        for part in ast.walk(node):
            if isinstance(part, ast.Constant) and isinstance(part.value, str) and not part.value.isascii():
                findings.append(f"{path.name}:{part.lineno} via {where}: {part.value[:60]!r}")

    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if name in RUNTIME_WRITES or name in LOG_METHODS:
            for arg in node.args:
                scan(arg, name)
        for keyword in node.keywords:
            if keyword.arg in ARGPARSE_TEXT:
                scan(keyword.value, f"argparse {keyword.arg}")
            elif name == "print" and keyword.arg in PRINT_KEYWORDS:
                scan(keyword.value, f"print {keyword.arg}=")
            elif name in LOG_METHODS and keyword.arg in LOG_KEYWORDS:
                scan(keyword.value, f"{name} {keyword.arg}=")
    return findings
