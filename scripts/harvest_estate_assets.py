"""
purpose: download every workbook and published datasource on a Tableau site, then run BOTH tiers'
         parsers over them, to get a failure distribution the estate can be reasoned about — and
         from which upstream feature requests can be written with evidence instead of anecdote.
usage:   python scripts/harvest_estate_assets.py --out <dir> [--env .env] [--limit N]
                                                 [--skip-download] [--workbooks-only]
                                                 [--allow-unignored-out]

Where the output goes
---------------------
`--out` is `_sweep` by convention (`.gitignore`: `/_sweep*/`; `_harvest*` belongs to the OTHER
harvester, `harvest_tableau_public.py`). Anything under `--out` is a real customer's workbooks and
their names, and THIS REPO IS PUBLIC, so when the target sits inside a git work tree this script
refuses to start unless git already ignores it -- see `unignored_output_paths` below. A target
outside any work tree is fine and runs unguarded.

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
import sqlite3
import subprocess
import sys
import time
import traceback
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


class OutputPathNotIgnoredError(RuntimeError):
    """`--out` is inside a git work tree that would commit it, or git cannot prove otherwise."""


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str] | None:
    """Run git in `cwd`. Returns None when git itself could not be run at all."""
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _existing_ancestor(path: Path) -> Path | None:
    """Nearest existing directory at or above `path` - git needs a real directory to run in."""
    return next((p for p in (path, *path.parents) if p.is_dir()), None)


def unignored_output_paths(out: Path) -> list[Path]:
    """Which artifacts under `out` git would offer to commit. Empty list means safe to write.

    Two measured details decide this implementation, and getting either wrong yields a guard that
    silently always passes - worse than no guard, because it also reassures:

    * **Ask about paths INSIDE `out`, never `out` itself.** `/_sweep*/` is a directory-only pattern,
      and `git check-ignore` applies such a pattern only to a path it knows is a directory - which,
      for an `--out` that does not exist yet, it does not (measured: `_assessment-x` -> exit 1,
      `_assessment-x/assets/f.twbx` -> exit 0, same rule, same repo). A trailing component makes the
      parent a directory by construction, so the rule applies without creating anything on disk.
    * **NEVER append a trailing slash to work around that.** On git 2.55.0.windows.3,
      `git check-ignore -- 'zzz_not_ignored/'` exits 0 reporting an EMPTY matched pattern: with a
      trailing slash EVERY path looks ignored.

    Raises `OutputPathNotIgnoredError` when git is present but cannot answer, or is absent while a
    `.git` checkout is in scope: an unprovable path is treated as unsafe, never as safe.
    """
    out = out.expanduser().resolve()
    anchor = _existing_ancestor(out)
    if anchor is None:  # pragma: no cover - a drive/filesystem root always exists
        return []

    inside = _git(["rev-parse", "--is-inside-work-tree"], anchor)
    if inside is None:
        # git being un-runnable is not evidence that no repository is here, so look for the checkout
        # directly rather than letting a missing binary quietly disable the guard.
        if any((parent / ".git").exists() for parent in (anchor, *anchor.parents)):
            raise OutputPathNotIgnoredError(
                f"cannot run git, but {out} is inside a .git checkout, so it cannot be proven ignored"
            )
        return []
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return []  # outside any work tree: nothing here can be committed by accident

    unignored: list[Path] = []
    for artifact in OUTPUT_ARTIFACTS:
        target = out / artifact
        probe = _git(["check-ignore", "-q", "--", str(target)], anchor)
        # 0 = ignored, 1 = not ignored, anything else (128, or no git) = no answer, so do not guess.
        if probe is None or probe.returncode not in (0, 1):
            detail = "git could not be run" if probe is None else (probe.stderr.strip() or f"exit {probe.returncode}")
            raise OutputPathNotIgnoredError(f"could not ask git whether {target} is ignored: {detail}")
        if probe.returncode == 1:
            unignored.append(target)
    return unignored


def refuse_unignored_output(out: Path, allow_unignored: bool) -> bool:
    """True when the run must STOP before downloading anything. Logs the reason either way."""
    try:
        unignored = unignored_output_paths(out)
    except OutputPathNotIgnoredError as exc:
        message = str(exc)
    else:
        if not unignored:
            return False
        message = (
            f"git does not ignore {', '.join(str(p) for p in unignored)}. This run downloads a real "
            "site's .twbx/.tdsx and records every workbook name, and this repo is PUBLIC, so a "
            "`git add -A` would stage customer content (issue #125). Fix: use the ignored convention "
            "`--out _sweep` (any `_sweep*` variant works, e.g. `_sweep-2026-08-13`), point --out "
            "outside the checkout, or add a rule to .gitignore."
        )
    if allow_unignored:
        LOG.warning("--allow-unignored-out: proceeding anyway, but %s", message)
        return False
    LOG.error("REFUSING to harvest into %s: %s", out, message)
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


def scoped_todo(
    con: sqlite3.Connection, project_names: list[str], project_ids: list[str], workbooks_only: bool
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str]], int, int]:
    """Select project workbooks and the published sources their survey edges require."""
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
        return todo, [], 0, 0

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
    datasource_rows: list[tuple[str, str]] = []
    if not workbooks_only:
        datasource_rows = (
            list(
                con.execute(
                    f"""
                SELECT DISTINCT datasource.luid, datasource.name
                FROM datasource
                JOIN dependency ON dependency.datasource_luid = datasource.luid
                    OR (
                        COALESCE(dependency.datasource_luid, '') = ''
                        AND LOWER(TRIM(dependency.datasource_name)) = LOWER(TRIM(datasource.name))
                    )
                WHERE dependency.workbook_luid IN ({",".join("?" for _ in workbooks)})
                ORDER BY datasource.name, datasource.luid
                """,
                    [row[0] for row in workbooks],
                )
            )
            if workbooks
            else []
        )
    todo = [("datasource", luid, name) for luid, name in datasource_rows]
    todo.extend(("workbook", luid, name) for luid, name in workbooks)
    return todo, selected, len(workbooks), len(datasource_rows)


def parse_asset(path: Path, scripts: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run both offline parsers while the main thread starts the next fresh session."""
    return parse_ours(path), parse_theirs(path, scripts)


def asset_path(assets_dir: Path, kind: str, name: str, luid: str) -> Path:
    """Return an identity-specific local filename; Tableau display names are not unique."""
    safe_name = "".join(char if char.isalnum() or char in "-_" else "_" for char in name)[:60]
    safe_luid = "".join(char if char.isalnum() or char in "-_" else "_" for char in luid)
    extension = ".twbx" if kind == "workbook" else ".tdsx"
    return assets_dir / f"{safe_name}--{safe_luid}{extension}"


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
        help="project name to harvest (repeatable; includes required datasources)",
    )
    ap.add_argument(
        "--project-id",
        action="append",
        default=[],
        help="project LUID to harvest (repeatable; includes required datasources)",
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
    # must never land somewhere this PUBLIC repo would commit them (issue #125).
    if refuse_unignored_output(args.out, args.allow_unignored_out):
        return 2
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
        todo, selected, project_workbooks, pulled_datasources = scoped_todo(
            con, args.project, args.project_id, args.workbooks_only
        )
    except (sqlite3.OperationalError, ValueError) as exc:
        con.close()
        LOG.error("%s; run assess_estate.py with --survey again before using project scoping", exc)
        return 1
    con.close()
    if selected:
        LOG.info(
            "project(s) %s selected: %d workbook(s), plus %d datasource(s) pulled in by dependency",
            ", ".join(f"'{name}' ({luid})" for luid, name in selected),
            project_workbooks,
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
    pending: list[tuple[int, dict[str, Any], Future[tuple[dict[str, Any], dict[str, Any]]]]] = []
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="offline-parse") as parser:
        for index, (kind, luid, name) in enumerate(todo, 1):
            target = asset_path(assets_dir, kind, name, luid)
            row: dict[str, Any] = {"name": name, "kind": kind, "luid": luid, "file": str(target)}

            if not args.skip_download and not target.exists():
                ok, detail = download(kind, luid, target, env, scripts)
                if not ok:
                    row["download_error"] = detail
                    results.append(row)
                    LOG.warning("[%d/%d] %-46s DOWNLOAD FAILED %s", index, len(todo), name[:46], detail[:80])
                    continue

            candidates = [target]
            actual = next((c for c in candidates if c.exists()), None)
            if actual is None:
                row["download_error"] = "fetcher reported success but no file landed"
                results.append(row)
                continue
            row["file"] = str(actual)
            pending.append((index, row, parser.submit(parse_asset, actual, scripts)))
            elapsed = time.perf_counter() - started
            average = elapsed / index
            LOG.info(
                "[%d/%d] %-46s downloaded elapsed=%.0fs avg=%.1fs ETA=%.0fs",
                index,
                len(todo),
                name[:46],
                elapsed,
                average,
                average * (len(todo) - index),
            )

        for index, row, future in pending:
            row["ours"], row["theirs"] = future.result()
            results.append(row)
            mark = "ok " if row["ours"].get("ok") and row["theirs"].get("ok") else "DIFF"
            elapsed = time.perf_counter() - started
            average = elapsed / index
            LOG.info(
                "[%d/%d] %-46s %s ours=%s his=%s elapsed=%.0fs avg=%.1fs ETA=%.0fs",
                index,
                len(todo),
                row["name"][:46],
                mark,
                "ok" if row["ours"].get("ok") else "FAIL",
                "ok" if row["theirs"].get("ok") else "FAIL",
                elapsed,
                average,
                average * (len(todo) - index),
            )

    args.out.mkdir(parents=True, exist_ok=True)
    text = summarise(results, args.out)
    LOG.info("\n%s", text[: text.index("## ") if "## " in text else len(text)])
    LOG.info(
        "swept %d asset(s) in %.0fs -> %s", len(results), time.perf_counter() - started, args.out / "parse-sweep.md"
    )
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
