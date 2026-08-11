"""
purpose: download every workbook and published datasource on a Tableau site, then run BOTH tiers'
         parsers over them, to get a failure distribution the estate can be reasoned about — and
         from which upstream feature requests can be written with evidence instead of anecdote.
usage:   python scripts/harvest_estate_assets.py --out <dir> [--env .env] [--limit N]
                                                 [--skip-download] [--workbooks-only]

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
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from tableau_env import engine_child_env, load_env  # noqa: E402

LOG = logging.getLogger("harvest_estate_assets")

ENGINE_SCRIPTS = (
    Path.home()
    / ".copilot/installed-plugins/tableau-collection/tableau-fabric-skills/skills/tableau-migration/scripts",
    REPO_ROOT.parent / "tableau-fabric-skills/skills/tableau-migration/scripts",
)


def engine_scripts_dir() -> Path | None:
    """The deterministic tier's scripts folder: installed plugin first, then a sibling clone."""
    for candidate in ENGINE_SCRIPTS:
        if (candidate / "fetch_tds.py").is_file():
            return candidate
    return None


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
        return False, (proc.stderr or proc.stdout or "").strip()[-300:]
    return True, ""


def _os_environ() -> dict[str, str]:
    return dict(os.environ)


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


def main() -> int:  # pylint: disable=too-many-locals,too-many-statements  # one linear sweep
    """Harvest and sweep. Exit 1 only when nothing could be assessed at all."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path, help="output directory (should be git-ignored)")
    ap.add_argument("--env", type=Path, default=REPO_ROOT / ".env")
    ap.add_argument("--db", type=Path, help="assess_estate.py estate.db to take LUIDs from")
    ap.add_argument("--limit", type=int, help="stop after N assets (for a quick pass)")
    ap.add_argument("--skip-download", action="store_true", help="reuse whatever is already in --out/assets")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    scripts = engine_scripts_dir()
    if scripts is None:
        LOG.error("deterministic tier not found; install the tableau-migration plugin or clone it beside this repo")
        return 1

    env = {**_os_environ(), **load_env(args.env)}
    assets_dir = args.out / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    db = args.db or (REPO_ROOT / "_assessment" / "estate.db")
    con = sqlite3.connect(db)
    todo: list[tuple[str, str, str]] = []
    for luid, name in con.execute("SELECT luid, name FROM datasource ORDER BY name"):
        todo.append(("datasource", luid, name))
    for luid, name in con.execute("SELECT luid, name FROM workbook ORDER BY name"):
        todo.append(("workbook", luid, name))
    con.close()
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
    for index, (kind, luid, name) in enumerate(todo, 1):
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:60]
        ext = ".twbx" if kind == "workbook" else ".tdsx"
        target = assets_dir / f"{safe}{ext}"
        row: dict[str, Any] = {"name": name, "kind": kind, "luid": luid, "file": str(target)}

        if not args.skip_download and not target.exists():
            ok, detail = download(kind, luid, target, env, scripts)
            if not ok:
                row["download_error"] = detail
                results.append(row)
                LOG.warning("[%d/%d] %-46s DOWNLOAD FAILED %s", index, len(todo), name[:46], detail[:80])
                continue

        candidates = [target] + sorted(assets_dir.glob(f"{safe}.tw*")) + sorted(assets_dir.glob(f"{safe}.td*"))
        actual = next((c for c in candidates if c.exists()), None)
        if actual is None:
            row["download_error"] = "fetcher reported success but no file landed"
            results.append(row)
            continue
        row["file"] = str(actual)
        row["ours"] = parse_ours(actual)
        row["theirs"] = parse_theirs(actual, scripts)
        results.append(row)
        mark = "ok " if row["ours"].get("ok") and row["theirs"].get("ok") else "DIFF"
        LOG.info(
            "[%d/%d] %-46s %s ours=%s his=%s",
            index,
            len(todo),
            name[:46],
            mark,
            "ok" if row["ours"].get("ok") else "FAIL",
            "ok" if row["theirs"].get("ok") else "FAIL",
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
