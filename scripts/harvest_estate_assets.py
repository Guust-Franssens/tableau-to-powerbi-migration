"""
purpose: download every workbook and published datasource on a Tableau site, then run BOTH tiers'
         parsers over them, to get a failure distribution the estate can be reasoned about — and
         from which upstream feature requests can be written with evidence instead of anecdote.
usage:   python scripts/harvest_estate_assets.py --out <dir> [--env .env] [--limit N]
                                                 [--skip-download] [--workbooks-only]
                                                 [--project NAME] [--project-id LUID]
                                                 [--allow-unignored-out]

Where the output goes
---------------------
`--out` is `_sweep` by convention (`.gitignore`: `/_sweep*/`; `_harvest*` belongs to the OTHER
harvester, `harvest_tableau_public.py`). Anything under `--out` is a real customer's workbooks and
their names, and THIS REPO IS PUBLIC, so when the target sits inside a git work tree this script
refuses to start unless git already ignores it -- see `unignored_output_paths` below. A target
outside any work tree is fine and runs unguarded.

What the guard does and does not cover (issue #374). It judges BOTH the path git's own working-tree
walk would see (`--out` made absolute, nothing expanded or followed) AND the path the bytes actually
land on (`~` expanded, every junction/symlink followed), refuses if EITHER is committable, and the
run then writes to the second one -- so the path that was checked is the path that is written. It
does NOT detect an `--out` that names a junction's TARGET directly: the write lands outside the
checkout, but a junction elsewhere in the checkout still exposes it to `git add -A`. Finding that
needs reparse-point enumeration of the whole checkout and is deliberately out of scope.

Why this exists
---------------
Both tiers are normally exercised on whatever workbook is in front of us, which selects for the
shapes we already know. An estate-wide pass selects for nothing, so it finds the shapes nobody
thought to try — and it is cheap: parsing is offline, needs no Power BI Desktop, no Fabric capacity
and no data-source credential (a LIVE connection is only contacted at refresh, never at parse).

It runs BOTH parsers on purpose. They answer different questions and their disagreements are the
interesting part:

* ours (`parse_tableau.py` -> `migration-spec.json`) is the FIDELITY spec — mark types, encodings,
  shelves, palettes: what the viz meant and looked like;
* his (`connection_to_m.describe_datasource`) is the CONVERSION descriptor — relations, columns,
  connection routing: what can be rebuilt.

A workbook one parses and the other refuses is a finding by construction, and which way round it
fails says which tier owns it (see `docs/migration-programme.md` §0).

⚠️ Downloads are the session-fragile part. Tableau Cloud drops a session intermittently and the
failure is a `401002` mid-loop, so each asset is fetched with its OWN sign-in rather than a shared
token: measured on this site, a shared token truncated a 58-asset run repeatedly while
fresh-per-asset completed 8/8. Slower, and the only thing that finishes.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
import traceback
from collections import deque
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# Windows defaults stdout/stderr to the legacy cp1252 codec, which cannot encode the non-ASCII
# characters (e.g. the warning glyph above) in this module's own docstring -- argparse's --help
# crashes with UnicodeEncodeError before printing anything. Force UTF-8 so --help and any print()
# of the same characters work the same on every platform. This runs BEFORE the import below so a
# failure there is reportable rather than itself crashing the encoder.
for _stream in (sys.stdout, sys.stderr):
    # pylint: disable-next=no-member  # astroid mis-infers TextIOWrapper.encoding as a class here
    if _stream is not None and _stream.encoding and _stream.encoding.lower() != "utf-8":
        _stream.reconfigure(encoding="utf-8")

from engine_source import EngineNotFoundError, engine_scripts_dir  # noqa: E402  # pylint: disable=wrong-import-position
from tableau_env import engine_child_env, pat_secret, redact, require, resolve_env  # noqa: E402  # pylint: disable=wrong-import-position

LOG = logging.getLogger("harvest_estate_assets")

# The FILES this script writes under `--out`, probed as files on purpose. Two reasons:
#   * a directory-only rule (`/_sweep*/`, or a hand-written `_myout/assets/`) is applied by
#     `git check-ignore` only to a path it knows is a directory, and a not-yet-created `--out` is
#     not - so probing `assets` as a bare name reports a false "not ignored" for exactly the rule
#     shape people write. A file path underneath makes every parent a directory by construction.
#   * it is the honest question: these are the paths a `git add -A` would stage.
# `assets/` holds the downloaded workbooks/datasources; the sweep files record every asset's NAME
# and LUID even under `--skip-download`, so all of them are checked, not just the downloads.
OUTPUT_ARTIFACTS = (
    "assets/harvested-workbook.twbx",
    "assets/harvested-datasource.tdsx",
    "parse-sweep.json",
    "parse-sweep.md",
)

# The remedy sentence, kept separate from the diagnosis so another tool can reuse the guard without
# advertising THIS tool's ignored folder convention. `provision_tableau_estate.py capture` writes a
# manifest naming every project/workbook/datasource on a live site plus the downloads themselves, so
# it needs the same refusal - and an operator told to `--out _sweep` there would be misdirected.
DEFAULT_UNIGNORED_HINT = (
    "Fix: use the ignored convention `--out _sweep` (any `_sweep*` variant works, e.g. "
    "`_sweep-2026-08-13`), point --out outside the checkout, or add a rule to .gitignore."
)


class OutputPathNotIgnoredError(RuntimeError):
    """`--out` is inside a git work tree that would commit it, or git cannot prove otherwise."""


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str] | None:
    """Run git in `cwd`. Returns None when git itself could not be run at all."""
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _lexical(path: Path) -> Path:
    """`path` made absolute with `.`/`..` collapsed TEXTUALLY, following no junction or symlink.

    `Path.absolute()` keeps `..` verbatim (measured on Python 3.11.9 and 3.13.2, Windows), and a
    surviving `..` then rides into `git check-ignore`, which answers about a directory the operator
    never named: `<repo>\\..\\outside\\out` still passes a `relative_to(<repo>)` test, so the
    documented "point --out outside the checkout" remedy was refused in its most natural relative
    spelling. `os.path.abspath` collapses it the way git itself does - and only textually, because
    `resolve()` here would dereference the junction and re-open the hole below.
    """
    return Path(os.path.abspath(path))


def _resolved(path: Path) -> Path:
    """`path` with `~` expanded and every junction/symlink followed. Isolated so the guard has one
    place to fail closed when a path cannot be resolved at all."""
    return path.expanduser().resolve()


def _same_dir(a: Path, b: Path) -> bool | None:
    """Do these two spellings name the SAME directory on disk? Never a string comparison.

    Three-valued on purpose. `None` means the FILESYSTEM could not answer - a permission failure, a
    disconnected share - which is not the same as "no", and collapsing it to `False` is how an
    unignored in-repository output was allowed through: with every comparison erroring, containment
    looked disproven rather than unproven and the guard proceeded (measured: `main()` exit 0, a
    customer workbook name written to `parse-sweep.json`, `git status` staging it).
    """
    try:
        return os.path.samefile(a, b)
    except OSError:
        return None


def _identity_relative(node: Path, root: Path) -> tuple[list[str] | None, bool]:
    """`(segments from root down to node, every comparison answered)` - by FILE IDENTITY, not text.

    The second element is what separates "walked the whole ancestry and this really is not inside
    `root`" from "could not tell". Only the first is safe to act on.

    Windows can spell one directory several ways that are not lexically relative to each other, and
    both `absolute()` and `resolve()` preserve every one of them, so comparing `--out` against
    `git rev-parse --show-toplevel` as TEXT let three spellings write into the checkout unrefused
    while the plain spelling of the same target was correctly refused:

        \\\\?\\<repo>\\leak                     extended-length prefix
        \\\\localhost\\c$\\...\\<repo>\\leak       UNC admin-share alias
        <8.3 repo>\\link-out\\leak             8.3 short name PLUS an outbound junction

    The third defeats the obvious fix: 8.3 alone is expanded by `resolve()` and caught, but combined
    with a junction the lexical form stays short and the resolved form lands outside, so BOTH forms
    pass. `os.path.samefile` answers the question the spellings obscure.
    """
    segments: list[str] = []
    current = node
    answered = True
    while True:
        same = _same_dir(current, root)
        if same is None:
            answered = False
        elif same:
            return list(reversed(segments)), answered
        if current.parent == current:
            return None, answered
        segments.append(current.name)
        current = current.parent


def _canonical_probe_target(out: Path) -> tuple[Path, Path] | None:
    """`(work tree root, out re-spelled beneath it)`, or None when no work tree holds `out`.

    Walks the LEXICAL ancestors, because an existing ancestor can be reached through a junction and
    `git rev-parse` run there answers about somewhere else entirely (issue #374). Measured on Windows
    with `mklink /J <repo>\\linkdir <outside>`: `rev-parse` with cwd=`<repo>\\linkdir` exits 128 "not
    a git repository" -- the OS resolved the junction for the child process's cwd -- so a guard
    anchored there concludes "outside any work tree" and passes, while `git add -A` at `<repo>`
    stages `linkdir/sweepout/...`. Asked from `<repo>` the same git answers correctly.

    Containment is then decided by `_identity_relative`, never by path spelling, and the probe is
    rebuilt from the ROOT's own spelling so git is asked about a path it can actually see. That
    rebuild is load-bearing: with `subst Z: <repo>`, `rev-parse` at `Z:\\` reports the long
    `C:\\...` toplevel and `check-ignore` answers the canonical path (exit 0) while refusing the
    `Z:\\` spelling outright (exit 128) - so probing what the caller typed would refuse a plainly
    ignored output.

    EVERY unanswerable probe here is a refusal, not a shrug. Three of them were fail-open:

    * a `.git` entry found with `exists()`, which FOLLOWS a reparse point - a dangling `.git`
      junction reported absent while `rev-parse` failed, so the broken checkout was skipped and an
      unignored output was written and staged. `os.path.lexists` asks about the ENTRY.
    * an identity comparison that errored, collapsed to "not contained" (see `_same_dir`).
    * an ancestry with no examinable directory at all - a nonexistent drive, a disconnected share -
      where nothing could be looked at and the run proceeded anyway.
    """
    lexical = _lexical(out)
    tail: list[str] = []
    node = lexical
    examined = False
    while True:
        if node.is_dir():
            examined = True
            probe = _git(["rev-parse", "--show-toplevel"], node)
            root = (
                Path(probe.stdout.strip())
                if probe is not None and probe.returncode == 0 and probe.stdout.strip()
                else None
            )
            if root is None:
                if os.path.lexists(node / ".git"):
                    detail = "git could not be run" if probe is None else (probe.stderr.strip() or "no work tree")
                    raise OutputPathNotIgnoredError(
                        f"{node} holds a .git entry but git could not identify a work tree there "
                        f"({detail}), so {lexical} cannot be proven ignored"
                    )
            else:
                relative, answered = _identity_relative(node, root)
                if relative is not None:
                    return root, root.joinpath(*relative, *reversed(tail))
                if not answered:
                    raise OutputPathNotIgnoredError(
                        f"the filesystem could not say whether {node} lies inside the work tree at "
                        f"{root}, so {lexical} cannot be proven ignored"
                    )
        if node.parent == node:
            break
        tail.append(node.name)
        node = node.parent
    if not examined:
        raise OutputPathNotIgnoredError(
            f"no directory in the ancestry of {lexical} could be examined, so it cannot be proven ignored"
        )
    return None


def unignored_output_paths(out: Path, artifacts: Sequence[str] = OUTPUT_ARTIFACTS) -> list[Path]:
    """Which artifacts under `out` git would offer to commit. Empty list means safe to write.

    `out` is judged EXACTLY as given, beyond collapsing `.`/`..`: the caller decides which form of
    the path to ask about, because the forms disagree and both matter - see `refuse_unignored_output`,
    which asks about all of them. The returned paths are the CANONICAL spelling git was asked about,
    which is the one that says where the bytes would actually be staged from.

    Two measured details decide the probe itself, and getting either wrong yields a guard that
    silently always passes - worse than no guard, because it also reassures:

    * **Ask about paths INSIDE `out`, never `out` itself.** `/_sweep*/` is a directory-only pattern,
      and `git check-ignore` applies such a pattern only to a path it knows is a directory - which,
      for an `--out` that does not exist yet, it does not (measured: `_assessment-x` -> exit 1,
      `_assessment-x/assets/f.twbx` -> exit 0, same rule, same repo). A trailing component makes the
      parent a directory by construction, so the rule applies without creating anything on disk.
    * **NEVER append a trailing slash to work around that.** On git 2.55.0.windows.3,
      `git check-ignore -- 'zzz_not_ignored/'` exits 0 reporting an EMPTY matched pattern: with a
      trailing slash EVERY path looks ignored.

    Raises `OutputPathNotIgnoredError` when git is present but cannot answer, is absent while a
    `.git` checkout is in scope, or finds a `.git` it cannot read: an unprovable path is treated as
    unsafe, never as safe.
    """
    found = _canonical_probe_target(out)
    if found is None:
        return []  # outside any work tree: nothing here can be committed by accident
    root, base = found

    unignored: list[Path] = []
    for artifact in artifacts:
        target = base / artifact
        probe = _git(["check-ignore", "-q", "--", str(target)], root)
        # 0 = ignored, 1 = not ignored, anything else (128, or no git) = no answer, so do not guess.
        if probe is None or probe.returncode not in (0, 1):
            detail = "git could not be run" if probe is None else (probe.stderr.strip() or f"exit {probe.returncode}")
            raise OutputPathNotIgnoredError(f"could not ask git whether {target} is ignored: {detail}")
        if probe.returncode == 1:
            unignored.append(target)
    return unignored


def output_path_forms(out: Path) -> list[Path]:
    """Every form of `--out` that must pass before customer content is written (issue #374).

    A guard that judges one form while the writer uses another proves nothing. Both of these are
    real, and each catches what the other cannot:

    * **lexical** - absolute, `.`/`..` collapsed, nothing dereferenced. This is the string git's own
      working-tree walk sees, so it is the honest answer to "would `git add -A` stage this?" for an
      `--out` that traverses a junction OUT of the checkout, and for a literal `~` (measured: from
      cmd.exe, a quoted PowerShell argument, or any programmatic call, `~` reaches argv unexpanded,
      and its absolute form is `<cwd>/~/x` - inside the checkout, unignored).
    * **resolved** - `~` expanded and every junction/symlink followed. This is where the bytes land,
      and it is what catches an `--out` that looks external but is a junction pointing INTO the
      checkout (measured: already refused before this function existed, precisely because the guard
      resolved - which is why the resolved form is kept rather than replaced).

    An unresolvable path raises rather than degrading to the lexical form alone: a form we cannot
    compute is a question we cannot answer, and unanswerable means unsafe.
    """
    lexical = _lexical(out)
    try:
        resolved = _resolved(out)
    except (OSError, RuntimeError, ValueError) as exc:
        raise OutputPathNotIgnoredError(f"cannot resolve {out}, so it cannot be proven ignored: {exc}") from exc
    return list(dict.fromkeys((lexical, resolved)))


def refuse_unignored_output(
    out: Path,
    allow_unignored: bool,
    *,
    artifacts: Sequence[str] = OUTPUT_ARTIFACTS,
    hint: str = DEFAULT_UNIGNORED_HINT,
) -> bool:
    """True when the run must STOP before downloading anything. Logs the reason either way.

    Takes `--out` AS THE OPERATOR GAVE IT, not a pre-normalised copy: normalising before the call
    discards the very form that catches a literal `~` (issue #374). Every form from
    `output_path_forms` must pass; the caller then writes to the resolved one.

    `artifacts` and `hint` exist so a second tool that downloads customer content can reuse this one
    implementation rather than growing a near-copy that drifts. Pass the FILES that tool writes: the
    probe must name a file, never a bare directory (see `unignored_output_paths`).
    """
    try:
        unignored = list(
            dict.fromkeys(path for form in output_path_forms(out) for path in unignored_output_paths(form, artifacts))
        )
    except OutputPathNotIgnoredError as exc:
        message = str(exc)
    else:
        if not unignored:
            return False
        message = (
            f"git does not ignore {', '.join(str(p) for p in unignored)}. This run downloads a real "
            "site's .twbx/.tdsx and records every workbook name, and this repo is PUBLIC, so a "
            f"`git add -A` would stage customer content (issue #125). {hint}"
        )
    if allow_unignored:
        LOG.warning("--allow-unignored-out: proceeding anyway, but %s", message)
        return False
    LOG.error("REFUSING to write customer content into %s: %s", out, message)
    LOG.error("Nothing was downloaded. Pass --allow-unignored-out to override this deliberately.")
    return True


def download(kind: str, luid: str, out_file: Path, env: dict[str, str], scripts: Path) -> tuple[bool, str]:
    """Fetch one asset BY LUID via the deterministic tier's fetcher. Returns (ok, detail).

    By LUID, never by name: Tableau permits duplicate names across projects, and name-keyed identity
    has already produced four separate defects in this codebase.
    """
    flag = "--workbook-luid" if kind == "workbook" else "--datasource-luid"
    cmd = [
        sys.executable,
        str(scripts / "fetch_tds.py"),
        "--server",
        env["TABLEAU_SERVER_URL"],
        "--site",
        env.get("TABLEAU_SITE", ""),
        flag,
        luid,
        "--include-extract",
        "--no-prompt",
        "--out",
        str(out_file),
    ]
    child = engine_child_env(env)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False, env=child)
    except subprocess.TimeoutExpired:
        return False, "timeout after 600s"
    if proc.returncode != 0:
        # Redact BEFORE truncating. Slicing first can cut through the secret and leave a suffix in
        # the retained text, which is then both logged and persisted -- measured: the full secret was
        # absent while its tail survived at the start of the slice. Order matters more than the
        # scrub itself here, because the wrong order still passes a test whose sentinel happens to
        # fall inside the window.
        raw = redact((proc.stderr or proc.stdout or "").strip(), pat_secret(env), env.get("TABLEAU_PAT_NAME", ""))
        return False, raw[-300:]
    return True, ""


def parse_ours(path: Path) -> dict[str, Any]:
    """Run OUR parser. Returns {ok, error, sheets, dashboards, calcs, data_sources}."""
    try:
        # Imported here, not at module scope, deliberately: an ImportError from the parser IS one of
        # the findings this sweep collects, so it must be caught by the handler below rather than
        # killing the process at import time.
        from parse_tableau import parse_workbook  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

        spec = parse_workbook(path)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        # A parser crash IS the finding this sweep exists to collect, so it must be recorded and
        # stepped over. Narrowing here would abort the run on the first interesting input.
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-800:]}
    return {
        "ok": True,
        "sheets": len(spec.get("worksheets") or []),
        "dashboards": len(spec.get("dashboards") or []),
        "calcs": sum(len(d.get("calculated_fields") or []) for d in spec.get("data_sources") or []),
        "data_sources": len(spec.get("data_sources") or []),
        "limitations": len(spec.get("limitations_encountered") or []),
    }


def parse_theirs(path: Path, scripts: Path) -> dict[str, Any]:
    """Run HIS descriptor over the same asset, in a subprocess so a hard failure cannot kill us."""
    # His public entry point is `parse_tds(xml_text)` (connection_to_m.py:1931) - it takes the XML
    # TEXT, so a packaged .tdsx/.twbx must be unzipped first. The first attempt guessed
    # `describe_datasource`, which reported "his parser failed 3/3" when nothing of his had run at
    # all. A harness error and a real finding are indistinguishable unless you read the error text.
    snippet = (
        "import json,sys,zipfile\n"
        f"sys.path.insert(0, r'{scripts}')\n"
        "import connection_to_m as cm\n"
        f"p = r'{path}'\n"
        "try:\n"
        "    if p.lower().endswith(('.twbx','.tdsx')):\n"
        "        z = zipfile.ZipFile(p)\n"
        "        inner = [n for n in z.namelist() if n.endswith(('.twb','.tds'))][0]\n"
        "        xml = z.read(inner).decode('utf-8','replace')\n"
        "    else:\n"
        "        xml = open(p, encoding='utf-8', errors='replace').read()\n"
        "    d = cm.parse_tds(xml)\n"
        "    rels = d.get('relations') or []\n"
        "    print(json.dumps({'ok':True,'relations':len(rels),"
        "'untyped':len([r for r in rels if not r.get('columns')]),"
        "'unsupported':d.get('unsupported_reasons') or [],"
        "'connection_class':d.get('connection_class'),"
        "'named_connection_count':d.get('named_connection_count')}))\n"
        "except Exception as e:\n"
        "    print(json.dumps({'ok':False,'error':'%s: %s' % (type(e).__name__, e)}))\n"
    )
    try:
        proc = subprocess.run([sys.executable, "-c", snippet], capture_output=True, text=True, timeout=180, check=False)
        return json.loads((proc.stdout or "").strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        # Same reason, one layer out: a harness fault must be labelled as such, never as a parser
        # verdict - reporting 'his parser failed' for our own bug already happened once here.
        return {"ok": False, "error": f"harness: {type(exc).__name__}: {exc}"}


def summarise(results: list[dict], out: Path) -> str:  # pylint: disable=too-many-locals
    """Group failures by SHAPE, because one root cause repeated 12 times is one feature request."""
    ours_fail = [r for r in results if r.get("ours", {}).get("ok") is False]
    theirs_fail = [r for r in results if r.get("theirs", {}).get("ok") is False]
    both_ok = [r for r in results if r.get("ours", {}).get("ok") and r.get("theirs", {}).get("ok")]

    lines = ["# Estate parse sweep", ""]
    lines.append(
        f"**{len(results)} asset(s)** — ours failed {len(ours_fail)}, his failed {len(theirs_fail)}, "
        f"both parsed {len(both_ok)}."
    )
    lines.append("")
    lines.append(
        "A failure on ONE side only is the interesting case: it says which tier owns the gap "
        "(`docs/migration-programme.md` §0). A failure on both is a genuinely hard input."
    )
    lines.append("")

    for title, rows, key in (
        ("## Our parser failed", ours_fail, "ours"),
        ("## The deterministic tier's descriptor failed", theirs_fail, "theirs"),
    ):
        lines.append(title)
        lines.append("")
        if not rows:
            lines.append("_none_")
            lines.append("")
            continue
        by_shape: dict[str, list[str]] = {}
        for r in rows:
            msg = str(r[key].get("error", "?"))
            head, _, tail = msg.partition(":")
            shape = f"{head}: {tail[:70]}"
            by_shape.setdefault(shape, []).append(r["name"])
        for shape, names in sorted(by_shape.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"- **{len(names)}x** `{shape}`")
            lines.append(f"  - {', '.join(names[:8])}{' …' if len(names) > 8 else ''}")
        lines.append("")

    unsupported: dict[str, list[str]] = {}
    for r in results:
        for reason in (r.get("theirs", {}) or {}).get("unsupported", []) or []:
            shape = str(reason).partition("'")[0].strip() or str(reason)[:60]
            unsupported.setdefault(shape, []).append(r["name"])
    if unsupported:
        lines.append("## Rebuild refusals (his `unsupported_reasons`), grouped by shape")
        lines.append("")
        for shape, names in sorted(unsupported.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"- **{len(names)}x** {shape}")
            lines.append(f"  - {', '.join(sorted(set(names))[:8])}")
        lines.append("")

    text = "\n".join(lines) + "\n"
    (out / "parse-sweep.md").write_text(text, encoding="utf-8")
    (out / "parse-sweep.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return text


def dependency_datasources(con: sqlite3.Connection, workbook_luids: list[str]) -> list[tuple[str, str]]:
    """The published datasources those workbooks bind to, even when they live in another project.

    LUID first; an edge the survey could not resolve to a LUID falls back to the normalized name and
    keeps EVERY candidate, because dropping one silently is the "empty report" failure this exists
    to prevent.
    """
    if not workbook_luids:
        return []
    return list(
        con.execute(
            f"""
            SELECT DISTINCT datasource.luid, datasource.name
            FROM datasource
            JOIN dependency ON dependency.datasource_luid = datasource.luid
                OR (
                    COALESCE(dependency.datasource_luid, '') = ''
                    AND LOWER(TRIM(dependency.datasource_name)) = LOWER(TRIM(datasource.name))
                )
            WHERE dependency.workbook_luid IN ({",".join("?" for _ in workbook_luids)})
            ORDER BY datasource.name, datasource.luid
            """,
            workbook_luids,
        )
    )


def scoped_todo(
    con: sqlite3.Connection, project_names: list[str], project_ids: list[str], workbooks_only: bool
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str]], int, int, int]:
    """Select everything IN the chosen projects, plus the published sources their edges require.

    Returns `(todo, selected_projects, workbooks, datasources_in_project, datasources_pulled_in)`.

    Both halves are load-bearing and neither substitutes for the other. Following dependency edges
    OUT of the project is what stops a report rebuilding against a model nobody migrated. Selecting
    the datasources that simply LIVE in the project is what makes the model-first phase-1 workflow
    work at all: the issue's own example, `--project "00 - Certified Sources"`, is 3 datasources and
    0 workbooks, so an edges-only scope selects nothing and exits 1 on `0 asset(s) to sweep`.
    """
    if not project_names and not project_ids:
        todo = []
        if not workbooks_only:
            todo.extend(
                ("datasource", luid, name)
                for luid, name in con.execute("SELECT luid, name FROM datasource ORDER BY name")
            )
        todo.extend(
            ("workbook", luid, name) for luid, name in con.execute("SELECT luid, name FROM workbook ORDER BY name")
        )
        return todo, [], 0, 0, 0

    selected = list(
        con.execute(
            f"SELECT luid, name FROM project WHERE name IN ({','.join('?' for _ in project_names) or 'NULL'}) "
            f"OR luid IN ({','.join('?' for _ in project_ids) or 'NULL'}) ORDER BY name, luid",
            [*project_names, *project_ids],
        )
    )
    if not selected:
        raise ValueError("no projects matched --project/--project-id")
    selected_ids = [row[0] for row in selected]
    placeholders = ",".join("?" for _ in selected_ids)
    workbooks = list(
        con.execute(
            f"SELECT luid, name FROM workbook WHERE project_luid IN ({placeholders}) ORDER BY name, luid", selected_ids
        )
    )
    in_project: list[tuple[str, str]] = []
    pulled_in: list[tuple[str, str]] = []
    if not workbooks_only:
        in_project = list(
            con.execute(
                f"SELECT luid, name FROM datasource WHERE project_luid IN ({placeholders}) ORDER BY name, luid",
                selected_ids,
            )
        )
        already = {luid for luid, _ in in_project}
        pulled_in = [row for row in dependency_datasources(con, [row[0] for row in workbooks]) if row[0] not in already]
    datasources = sorted(in_project + pulled_in, key=lambda row: (row[1], row[0]))
    todo = [("datasource", luid, name) for luid, name in datasources]
    todo.extend(("workbook", luid, name) for luid, name in workbooks)
    return todo, selected, len(workbooks), len(in_project), len(pulled_in)


def parse_asset(path: Path, scripts: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run both offline parsers while the main thread starts the next fresh session."""
    return parse_ours(path), parse_theirs(path, scripts)


def safe_component(text: str, limit: int | None = None) -> str:
    """Filename-safe form of a Tableau name or LUID -- `[A-Za-z0-9-_]` only, so it is glob-literal."""
    cleaned = "".join(char if char.isalnum() or char in "-_" else "_" for char in text)
    return cleaned[:limit] if limit is not None else cleaned


def asset_path(assets_dir: Path, kind: str, name: str, luid: str) -> Path:
    """The local filename to download INTO: `<luid>_<name><ext>`; display names are not unique.

    ⚠️ The LUID goes in FRONT on purpose. The engine's `migrate_estate.py::asset_name()` strips a
    leading canonical-UUID prefix (`_TRANSFER_UUID_PREFIX`, followed by `-`, `_` or a space), so a
    prefixed file keeps local identity WITHOUT the LUID reaching `bundle/pbip/<stem>/` or
    `migrations/<slug>/`. A trailing `--<luid>` is not stripped and does reach both -- verified
    against engine 2.126.0: `strip_transfer_uuid('<uuid>_Meridian_Revenue_by_Region')` ->
    `'Meridian_Revenue_by_Region'`, while `'Meridian_Revenue_by_Region--<uuid>'` is returned intact.
    """
    extension = ".twbx" if kind == "workbook" else ".tdsx"
    return assets_dir / f"{safe_component(luid)}_{safe_component(name, 60)}{extension}"


def landed_files(assets_dir: Path, stem: str) -> list[Path]:
    """Files that could be this asset's download, PACKAGED form first: a `.twbx` carries data."""
    found = sorted(assets_dir.glob(f"{stem}.tw*")) + sorted(assets_dir.glob(f"{stem}.td*"))
    return sorted(found, key=lambda path: (path.suffix.lower() not in (".twbx", ".tdsx"), path.name))


def existing_asset(assets_dir: Path, kind: str, name: str, luid: str) -> Path | None:
    """The file that ACTUALLY landed for this asset, or None. Never assume the requested extension.

    Two fallbacks, both measured, both load-bearing:

    * **extension.** The engine's `fetch_tds.py::save_outputs` writes `<base>.twb`/`<base>.tds`
      whenever the REST download is not a zip -- across three real full harvests the landed
      extensions were `{'.tdsx': 17, '.twb': 18, '.twbx': 20}`, i.e. **18 of 38 workbooks (47%)
      arrive as `.twb` where `.twbx` was requested**. Matching only the requested extension loses
      them, and the sweep still reports `ours failed 0, his failed 0`, because an asset that never
      reached a parser is not counted as a failure: silent data loss that reads as a clean run.
    * **legacy name.** Assets harvested before the LUID prefix are `<name><ext>`. Without this the
      first run after an upgrade re-downloads the whole estate at one fresh sign-in per asset --
      the opposite of the resume behaviour this is for. Reuse is only as ambiguous as the run that
      wrote it (two same-named assets already shared one legacy file); everything downloaded from
      here on is LUID-unique.
    """
    candidates = [asset_path(assets_dir, kind, name, luid)]
    for stem in (f"{safe_component(luid)}_{safe_component(name, 60)}", safe_component(name, 60)):
        candidates.extend(landed_files(assets_dir, stem))
    return next((path for path in candidates if path.exists()), None)


def progress(finished: int, total: int, started: float) -> str:
    """Elapsed, running average and ETA measured on FINISHED assets only.

    The divisor must be what has actually completed, never the loop index: the download loop and the
    parse drain each count from 1 while `elapsed` keeps accumulating, so `elapsed / index` reported
    `[1/6] ... elapsed=9s avg=9.1s ETA=46s` on a run with **0 s of work left**, and scaled to ~19
    hours announced on a 58-asset run. An ETA that big in front of a customer is worse than none.
    """
    elapsed = time.perf_counter() - started
    if finished <= 0:
        return f"elapsed={elapsed:.0f}s"
    average = elapsed / finished
    return f"elapsed={elapsed:.0f}s avg={average:.1f}s ETA={average * max(total - finished, 0):.0f}s"


def record_parse(
    entry: tuple[int, dict[str, Any], Future[tuple[dict[str, Any], dict[str, Any]]]],
    results: list[dict],
    total: int,
    started: float,
) -> None:
    """Collect one finished offline parse and log it, so progress interleaves with the downloads."""
    index, row, future = entry
    row["ours"], row["theirs"] = future.result()
    results.append(row)
    mark = "ok " if row["ours"].get("ok") and row["theirs"].get("ok") else "DIFF"
    LOG.info(
        "[%d/%d] %-46s %s ours=%s his=%s %s",
        index,
        total,
        row["name"][:46],
        mark,
        "ok" if row["ours"].get("ok") else "FAIL",
        "ok" if row["theirs"].get("ok") else "FAIL",
        progress(len(results), total, started),
    )


def main() -> int:  # pylint: disable=too-many-locals,too-many-statements  # one linear sweep
    """Harvest and sweep. Exit 2 when `--out` is committable, 1 when nothing could be assessed."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path, help="output directory (must be git-ignored, see below)")
    ap.add_argument("--env", type=Path, default=REPO_ROOT / ".env")
    ap.add_argument("--db", type=Path, help="assess_estate.py estate.db to take LUIDs from")
    ap.add_argument(
        "--project",
        action="append",
        default=[],
        help="project name to harvest (repeatable): its workbooks AND datasources, plus any "
        "datasource those workbooks depend on, wherever it lives",
    )
    ap.add_argument(
        "--project-id",
        action="append",
        default=[],
        help="project LUID to harvest (repeatable); same selection as --project, matched exactly",
    )
    ap.add_argument("--limit", type=int, help="stop after N assets (for a quick pass)")
    ap.add_argument("--skip-download", action="store_true", help="reuse whatever is already in --out/assets")
    ap.add_argument("--workbooks-only", action="store_true", help="skip published datasources; sweep workbooks only")
    ap.add_argument(
        "--allow-unignored-out",
        action="store_true",
        help="write to --out even when git does not ignore it (escape hatch; logs a warning instead)",
    )
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # Before the engine, the .env, the database and above all the download: a customer's workbooks
    # must never land somewhere this PUBLIC repo would commit them (issue #125). The guard is given
    # `--out` RAW, so it can judge the literal argv form as well as the resolved one; the write then
    # uses the resolved form, which the guard has just passed. Checking one form and writing another
    # is the whole of issue #374 -- measured: `--out ~/sweep` from cmd.exe was judged as
    # `%USERPROFILE%\sweep` (outside any work tree, so allowed) and written to `<checkout>\~\sweep`.
    if refuse_unignored_output(args.out, args.allow_unignored_out):
        return 2
    args.out = _resolved(args.out)
    # One resolver, no fallback: the installed plugin is the single canonical engine (issue #107).
    try:
        scripts = engine_scripts_dir()
    except EngineNotFoundError as exc:
        LOG.error("%s", exc)
        return 1

    env = resolve_env(args.env)
    if not args.skip_download:
        require(env)
    assets_dir = args.out / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    db = args.db or (REPO_ROOT / "_assessment" / "estate.db")
    con = sqlite3.connect(db)
    try:
        todo, selected, project_workbooks, project_datasources, pulled_datasources = scoped_todo(
            con, args.project, args.project_id, args.workbooks_only
        )
    except (sqlite3.OperationalError, ValueError) as exc:
        con.close()
        LOG.error("%s; run assess_estate.py with --survey again before using project scoping", exc)
        return 1
    con.close()
    if selected:
        LOG.info(
            "project(s) %s selected: %d workbook(s) and %d datasource(s) in project, plus %d datasource(s) "
            "pulled in because a selected workbook binds to them",
            ", ".join(f"'{name}' ({luid})" for luid, name in selected),
            project_workbooks,
            project_datasources,
            pulled_datasources,
        )
    if args.limit:
        todo = todo[: args.limit]
    LOG.info(
        "%d asset(s) to sweep (%d datasource, %d workbook)",
        len(todo),
        sum(1 for t in todo if t[0] == "datasource"),
        sum(1 for t in todo if t[0] == "workbook"),
    )

    results: list[dict] = []
    started = time.perf_counter()
    pending: deque[tuple[int, dict[str, Any], Future[tuple[dict[str, Any], dict[str, Any]]]]] = deque()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="offline-parse") as parser:
        for index, (kind, luid, name) in enumerate(todo, 1):
            target = asset_path(assets_dir, kind, name, luid)
            row: dict[str, Any] = {"name": name, "kind": kind, "luid": luid, "file": str(target)}

            # Ask what LANDED, not what was requested, on BOTH sides of the download: before it so an
            # asset already on disk (any extension, LUID-prefixed or legacy) is not re-fetched at a
            # fresh sign-in, and after it because the fetcher decides the extension, not us.
            actual = existing_asset(assets_dir, kind, name, luid)
            if not args.skip_download and actual is None:
                ok, detail = download(kind, luid, target, env, scripts)
                if not ok:
                    row["download_error"] = detail
                    results.append(row)
                    LOG.warning("[%d/%d] %-46s DOWNLOAD FAILED %s", index, len(todo), name[:46], detail[:80])
                    continue
                actual = existing_asset(assets_dir, kind, name, luid)

            if actual is None:
                row["download_error"] = "fetcher reported success but no file landed"
                results.append(row)
                continue
            row["file"] = str(actual)
            pending.append((index, row, parser.submit(parse_asset, actual, scripts)))
            LOG.info(
                "[%d/%d] %-46s downloaded %s", index, len(todo), name[:46], progress(len(results), len(todo), started)
            )
            # Drain whatever finished parsing while this asset was downloading, so a verdict lands
            # next to the download it belongs to instead of all of them arriving after the sweep.
            while pending and pending[0][2].done():
                record_parse(pending.popleft(), results, len(todo), started)

        while pending:
            record_parse(pending.popleft(), results, len(todo), started)

    args.out.mkdir(parents=True, exist_ok=True)
    text = summarise(results, args.out)
    LOG.info("\n%s", text[: text.index("## ") if "## " in text else len(text)])
    LOG.info(
        "swept %d asset(s) in %.0fs -> %s", len(results), time.perf_counter() - started, args.out / "parse-sweep.md"
    )
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
