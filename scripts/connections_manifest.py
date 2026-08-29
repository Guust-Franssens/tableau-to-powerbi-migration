"""
purpose: tell the customer WHICH data sources they must connect after a migration, and what breaks
         until they do - as a customer-infrastructure deliverable that must live in a git-ignored
         output directory.
usage:   python scripts/connections_manifest.py --bundle <dir>
         python scripts/connections_manifest.py --bundle <dir> --out <ignored-dir> --format json

Why this exists
---------------
Credentials do not travel with a migrated item. Every model backed by a live system needs its
connection re-established in the target workspace, and today the customer learns which ones by
importing everything, hitting a refresh failure, opening the model, reading the connection string,
and repeating. For an estate of dozens that is a day of round trips through the portal.

The information is already in our hands at parse time; it was simply never presented as a list. This
assembles it:

  * ``migration_bundle.load_bundle``  - the data sources, from either tier's contract
  * ``preflight_source_credentials.classify_source`` - live vs flat-file, fail-safe by design
  * the engine's handover slices - which workbook binds to which source (the blast radius)

Three refusals, each from a way this question is normally answered wrongly:

1. **It refuses unignored in-repo output.** The manifest intentionally names real customer servers
   and databases, so writing to `ses-prep/` or any other unignored checkout path is a hard stop.
2. **It never emits a secret.** Host, database and account name are configuration; passwords, keys
   and tokens are not. The manifest is meant to be safe to email, and a test proves no
   credential-shaped value reaches it.
3. **It never calls an extract "connected".** A model built from a materialised ``.hyper`` has no
   upstream connection at all - it is a SNAPSHOT, frozen at extract time, that will never refresh.
   Customers consistently read those as broken connections and go looking for a credential that does
   not exist. They are listed separately, and labelled.

Ordering is by blast radius, not alphabetically: a published data source feeding twelve workbooks is
a different task from one feeding a single archived report, and the dependency graph that tells them
apart is already computed.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from migration_bundle import load_bundle  # noqa: E402  # pylint: disable=wrong-import-position
from preflight_source_credentials import classify_source  # noqa: E402  # pylint: disable=wrong-import-position

LOG = logging.getLogger("connections_manifest")
DEFAULT_OUT = REPO_ROOT / "_connections_manifest"
OUTPUT_ARTIFACTS = ("connections.md", "connections.json")


class OutputPathNotIgnoredError(RuntimeError):
    """The output is in a checkout, but git cannot prove the manifest artifacts are ignored."""


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str] | None:
    """Run git in `cwd`. Returns None when git itself could not be run at all."""
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _existing_ancestor(path: Path) -> Path | None:
    """Nearest existing directory at or above `path` - git needs a real directory to run in."""
    return next((p for p in (path, *path.parents) if p.is_dir()), None)


def unignored_output_paths(out: Path, anchor: Path | None = None) -> list[Path]:
    """Which manifest artifacts under `out` git would offer to commit.

    Empty list means safe to write. This probes the files inside the output directory, not the
    directory itself: directory-only ignore patterns do not match a not-yet-created directory, and
    adding a trailing slash makes `git check-ignore` lie on some Windows git builds.

    The caller chooses which path form and anchor to probe. `main` checks both the lexical path git
    would see from the current checkout and the resolved path it writes to, because `~` expansion can
    otherwise make the guard and writer reason about different places.
    """
    out = out.absolute()
    anchor = anchor or _existing_ancestor(out)
    if anchor is None:  # pragma: no cover - a drive/filesystem root always exists
        return []

    inside = _git(["rev-parse", "--is-inside-work-tree"], anchor)
    if inside is None:
        if any((parent / ".git").exists() for parent in (anchor, *anchor.parents)):
            raise OutputPathNotIgnoredError(
                f"cannot run git, but {out} is inside a .git checkout, so it cannot be proven ignored"
            )
        return []
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return []

    unignored: list[Path] = []
    for artifact in OUTPUT_ARTIFACTS:
        target = out / artifact
        probe = _git(["check-ignore", "-q", "--", str(target)], anchor)
        if probe is None or probe.returncode not in (0, 1):
            detail = "git could not be run" if probe is None else (probe.stderr.strip() or f"exit {probe.returncode}")
            raise OutputPathNotIgnoredError(f"could not ask git whether {target} is ignored: {detail}")
        if probe.returncode == 1:
            unignored.append(target)
    return unignored


def refuse_unignored_output(out: Path, anchor: Path | None = None) -> bool:
    """True when the run must stop before writing customer infrastructure names."""
    try:
        unignored = unignored_output_paths(out, anchor=anchor)
    except OutputPathNotIgnoredError as exc:
        message = str(exc)
    else:
        if not unignored:
            return False
        message = (
            f"git does not ignore {', '.join(str(p) for p in unignored)}. This manifest names real "
            "customer servers and databases, and this repo is PUBLIC, so a `git add -A` would stage "
            "customer infrastructure metadata (issue #322). Fix: use the ignored default "
            f"`--out {DEFAULT_OUT.name}`, point --out outside the checkout, or add a .gitignore rule."
        )
    LOG.error("REFUSING to write connections manifest into %s: %s", out, message)
    return True


def _current_worktree_root() -> Path | None:
    """Current checkout root, if the operator invoked the script from inside one."""
    cwd = Path.cwd().absolute()
    root = _git(["rev-parse", "--show-toplevel"], cwd)
    if root is None or root.returncode != 0:
        return None
    return Path(root.stdout.strip()).absolute()


def _is_relative_to(path: Path, parent: Path) -> bool:
    """Compatibility wrapper for lexical containment checks."""
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _output_checks(out: Path) -> list[tuple[Path, Path | None]]:
    """Path forms and anchors that must agree before customer infrastructure names are written."""
    lexical = out.absolute()
    resolved = out.expanduser().resolve()
    checks: list[tuple[Path, Path | None]] = [(lexical, None), (resolved, None)]
    current_root = _current_worktree_root()
    if current_root is not None and _is_relative_to(lexical, current_root):
        checks.append((lexical, current_root))
    return list(dict.fromkeys(checks))


# Config the customer's platform team needs in order to create a connection. Deliberately an
# ALLOW-list: anything not named here is dropped rather than passed through, so a future connection
# field carrying a token cannot reach the manifest by default. Fail-closed, like connection_target's
# class handling.
#
# `database` is the CANONICAL name in both contracts (`parse_tableau.py:239` maps Tableau's `dbname`
# attribute onto it, and `migration_bundle._engine_connection` does the same). An earlier version
# allow-listed `dbname` and so emitted ONLY the class for every source on a real 27-source bundle --
# a manifest that looked right and carried nothing. `dbname` stays accepted as an input alias.
SAFE_CONNECTION_FIELDS = (
    "class",
    "server",
    "database",
    "dbname",
    "warehouse",
    "schema",
    "service",
    "port",
    "auth_method",
)

# Anything whose KEY looks like a credential is dropped even if it appears in the list above. Belt
# and braces: the allow-list is the control, this is the alarm.
SECRET_KEY_PATTERN = re.compile(r"password|secret|token|pwd|credential|apikey|api_key|sas|key$", re.IGNORECASE)

# A secret does not have to arrive under a credential-shaped KEY. It can ride inside an allowed
# VALUE: URL userinfo (`//user:pass@host`), a credential query parameter (`?token=...`), or an
# ODBC/JDBC property string (`Driver=X;PWD=...`) pasted into a server or database field. Filtering
# keys alone let all three through into a document meant to be emailed (found in review of #100).
_URL_USERINFO = re.compile(r"(?:(?<=//)|(?<![\w@./-]))[^/@\s:]+:[^/@\s]+@")
_CREDENTIAL_ASSIGNMENT = re.compile(
    # The keyword may be the WHOLE key (`token=`, `PWD=`), not just a suffix of it. An earlier
    # version required at least one leading character, so `?token=...` and `;PWD=...` -- the two
    # commonest real forms -- slipped through while `user_password=` was caught.
    r"([\w.\s-]*(?:password|secret|token|pwd|credential|apikey|api[_-]?key|sas|key))\s*=\s*"
    r"(\"[^\"]*\"|'[^']*'|[^;&\s]+)",
    re.IGNORECASE,
)


def _sanitize_scalar(value: Any) -> str:
    """Sanitize one non-container value."""
    cleaned = _URL_USERINFO.sub("[REDACTED]@", str(value))
    return _CREDENTIAL_ASSIGNMENT.sub(lambda m: f"{m.group(1)}=[REDACTED]", cleaned)


def _sanitize_structured(value: Any) -> Any:
    """Recursively sanitize containers without exposing Python reprs."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            safe_key = _sanitize_scalar(key)
            out[safe_key] = "[REDACTED]" if SECRET_KEY_PATTERN.search(str(key)) else _sanitize_structured(item)
        return out
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_structured(item) for item in value]
    if value is None:
        return ""
    return _sanitize_scalar(value)


def sanitize(value: Any) -> str:
    """Strip credential material out of a value that is about to be published.

    Applied to EVERY emitted string -- connection values, data-source names, classification reasons
    and workbook names -- not only to connection fields, because a secret pasted into a source name
    reaches the same document by a different route. Accepts any type and coerces: a real bundle
    supplies `None` reasons, and a sanitizer that raises is a sanitizer people route around.
    """
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(_sanitize_structured(value), sort_keys=True, default=str)
    return _sanitize_scalar(value)


SNAPSHOT = "snapshot (extract - no upstream connection)"
NEEDS_CREDENTIAL = "needs a credential"
REVIEW = "needs review"

# Tableau's own proxy for a PUBLISHED data source: the workbook talks to Tableau Server, and the real
# upstream (Snowflake, Databricks, ...) is defined server-side in the datasource itself. Telling a
# platform engineer to "connect to sqlproxy" is meaningless, so say what it actually is and where the
# answer lives. This is the same shape as the under-reporting `parse_tableau.py` warns about for
# sqlproxy-backed workbooks.
PUBLISHED_PROXY_CLASSES = frozenset({"sqlproxy"})

# Tableau's OWN extract engines. A leg with one of these is the extract itself, not an upstream: the
# "server" is a path to a `.hyper`/`.tde` inside the package. `connection_target` treats them as live
# (correctly - it allow-lists file classes and everything else is live, so an unknown class fails
# safe), but for a CUSTOMER-facing document neither answer is honest. "Connect to
# hyper @ Data/Extracts/xyz.hyper" is meaningless, and "no credential needed" is the fail-open this
# codebase has been burned by. The truthful answer is that the original upstream is not recorded in
# the workbook and a human must decide - which is what the review status is for.
EXTRACT_ENGINE_CLASSES = frozenset({"hyper", "dataengine", "tde"})


def safe_connection(connection: dict[str, Any]) -> dict[str, str]:
    """Project a connection down to the fields a platform engineer needs, and nothing else.

    Values are sanitized as well as keys: an allow-listed field is not a promise that its CONTENT is
    safe. `dbname` is folded onto the canonical `database`.
    """
    out: dict[str, str] = {}
    for field in SAFE_CONNECTION_FIELDS:
        value = connection.get(field)
        if value in (None, ""):
            continue
        if SECRET_KEY_PATTERN.search(field):
            continue
        out["database" if field == "dbname" else field] = sanitize(value)
    return out


def legs(connection: dict[str, Any]) -> list[dict[str, str]]:
    """The per-system legs of a federated connection, each projected safely.

    A Tableau federated source spans several systems, and the real targets live in
    ``connection["connections"]``. Reading only the top level reported ``federated`` and nothing
    else, which tells a platform team to connect to a word rather than to a database.
    """
    raw = connection.get("connections")
    if not isinstance(raw, list):
        return []
    out = []
    for leg in raw:
        if isinstance(leg, dict):
            projected = safe_connection(leg)
            if projected:
                out.append(projected)
    return out


def _leg_identity(connection: dict[str, Any]) -> dict[str, str]:
    """Stable, non-secret fields that identify a connection leg across contracts."""
    return {
        target: sanitize(value)
        for source, target in (
            ("class", "class"),
            ("connection_class", "class"),
            ("server", "server"),
            ("database", "database"),
            ("dbname", "database"),
            ("warehouse", "warehouse"),
            ("schema", "schema"),
            ("service", "service"),
            ("port", "port"),
            ("auth_method", "auth_method"),
        )
        if (value := connection.get(source)) not in (None, "")
    }


def _connection_identity(connection: dict[str, Any]) -> str:
    """Identity used only to match handover bindings back to full manifest rows."""
    raw_legs = connection.get("connections")
    if isinstance(raw_legs, list) and raw_legs:
        normalized = [_leg_identity(leg) for leg in raw_legs if isinstance(leg, dict)]
    else:
        normalized = [_leg_identity(connection)]
    return json.dumps(normalized, sort_keys=True)


def _handover_connection(source: dict[str, Any]) -> dict[str, Any]:
    """Translate a handover datasource summary into the same shape used by bundle sources."""
    if isinstance(source.get("connection"), dict):
        return source["connection"]
    if isinstance(source.get("connections"), list):
        return {"connections": source["connections"]}
    return source


def _handover_source_name(source: dict[str, Any]) -> str:
    """Name fields differ between parser and engine handover contracts."""
    return str(source.get("name") or source.get("caption") or source.get("label") or "")


def blast_radius(bundle_dir: Path) -> dict[str, Any]:
    """Index workbooks by datasource name and, when available, by datasource identity.

    Returns an empty map for a bundle that has no handover (a single-workbook parser spec), which is
    reported as unknown rather than as zero - a source with no known consumers is not the same as a
    source with none, and conflating them would silently deprioritise it.
    """
    by_name: dict[str, set[str]] = defaultdict(set)
    by_identity: dict[tuple[str, str], set[str]] = defaultdict(set)
    handover = bundle_dir / "handover"
    if not handover.is_dir():
        return {"has_handover": False, "by_name": {}, "by_identity": {}}
    for slice_path in sorted(handover.glob("*.json")):
        try:
            workbook = json.loads(slice_path.read_text(encoding="utf-8")).get("workbook", {})
        except (json.JSONDecodeError, OSError):  # a malformed slice must not abort the manifest
            LOG.warning("could not read %s; its workbook is missing from the blast radius", slice_path.name)
            continue
        name = workbook.get("name") or slice_path.stem
        for source in workbook.get("consolidated_datasources") or workbook.get("embedded_datasources") or []:
            key = _handover_source_name(source) if isinstance(source, dict) else str(source)
            if key:
                by_name[key].add(name)
            if isinstance(source, dict) and key:
                identity = _connection_identity(_handover_connection(source))
                if identity != "[{}]":
                    by_identity[(key, identity)].add(name)
        bound = workbook.get("bound_datasource")
        if bound:
            by_name[bound].add(name)
    return {
        "has_handover": True,
        "by_name": {k: sorted(v) for k, v in by_name.items()},
        "by_identity": {k: sorted(v) for k, v in by_identity.items()},
    }


def classify(connection: dict[str, Any]) -> tuple[str, str]:
    """Verdict + reason for one source, resolving the two cases a top-level class alone gets wrong.

    Delegates to ``classify_source`` for policy and only handles composition:

    * **A federated source spans several systems.** If ANY leg is live, the source needs a
      credential -- judging the whole thing by its top-level class can call it a snapshot while a
      live leg sits underneath, the fail-OPEN direction this codebase has been burned by before.
    * **Every leg is one of Tableau's own extract engines.** There is no upstream recorded anywhere
      in the workbook, so we can neither name a system to connect nor promise none is needed.
    """
    verdict, reason = classify_source(connection)
    reason = reason or ""
    live_legs = [leg for leg in (connection.get("connections") or []) if isinstance(leg, dict)]

    if verdict != "needs-credential" and live_legs:
        leg_verdicts = [classify_source(leg)[0] for leg in live_legs]
        if "needs-credential" in leg_verdicts:
            return "needs-credential", "at least one leg of this federated source is a LIVE system; " + reason
        if "review" in leg_verdicts:
            verdict = "review"

    leg_classes = {(leg.get("class") or "").lower() for leg in live_legs} or {(connection.get("class") or "").lower()}
    if leg_classes and leg_classes <= EXTRACT_ENGINE_CLASSES:
        return "review", (
            "this is a Tableau extract (" + ", ".join(sorted(leg_classes)) + "); the workbook does not record "
            "what it was extracted FROM. Decide whether to connect it to that upstream or keep it as a snapshot."
        )
    return verdict, reason


def _source_consumers(
    raw_name: str,
    identity: str,
    radius: dict[str, Any],
    source_identities_by_name: dict[str, set[str]],
) -> tuple[list[str], bool]:
    """Return consumers plus whether that assignment is specific enough to trust."""
    identity_consumers = radius["by_identity"].get((raw_name, identity))
    if identity_consumers is not None:
        return [sanitize(c) for c in identity_consumers], True
    if radius["has_handover"] and len(source_identities_by_name[raw_name]) == 1:
        return [sanitize(c) for c in radius["by_name"].get(raw_name, [])], True
    return [], False


def _manifest_entry(
    source: dict[str, Any],
    radius: dict[str, Any],
    source_identities_by_name: dict[str, set[str]],
) -> tuple[tuple[str, str], dict[str, Any]]:
    """Build one manifest entry before duplicate full-connections are merged."""
    connection = source.get("connection") or {}
    verdict, reason = classify(connection)
    raw_name = source.get("name") or ""
    name = sanitize(raw_name or "(unnamed)")
    identity = _connection_identity(connection)
    consumers, entry_blast_radius_known = _source_consumers(raw_name, identity, radius, source_identities_by_name)
    key = (name, json.dumps(connection, sort_keys=True, default=str))
    return key, {
        "name": name,
        "status": {"needs-credential": NEEDS_CREDENTIAL, "no-creds": SNAPSHOT}.get(verdict, REVIEW),
        "connection": safe_connection(connection),
        "legs": legs(connection),
        "published_datasource": (connection.get("class") or "").lower() in PUBLISHED_PROXY_CLASSES,
        "why": sanitize(reason),
        "used_by": consumers,
        "used_by_count": len(consumers) if entry_blast_radius_known else None,
        "blast_radius_known": entry_blast_radius_known,
    }


def _merge_duplicate_entry(target: dict[str, Any], duplicate: dict[str, Any]) -> None:
    """Merge duplicate rows produced by the same full connection."""
    merged = sorted(set(target["used_by"]) | set(duplicate["used_by"]))
    merged_known = target["blast_radius_known"] and duplicate["blast_radius_known"]
    target["used_by"] = merged
    target["blast_radius_known"] = merged_known
    target["used_by_count"] = len(merged) if merged_known else None


def build(bundle_path: Path) -> dict[str, Any]:
    """Assemble the manifest. Pure data in, pure data out - no I/O beyond reading the bundle."""
    bundle = load_bundle(bundle_path)
    radius = blast_radius(bundle.path if bundle.path.is_dir() else bundle.path.parent)
    source_identities_by_name: dict[str, set[str]] = defaultdict(set)
    for source in bundle.data_sources:
        source_identities_by_name[source.get("name") or ""].add(_connection_identity(source.get("connection") or {}))

    entries: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for source in bundle.data_sources:
        # Dedupe on the FULL connection, not the projected one: a field dropped for safety is also
        # invisible to a key built from the projection, which would merge two genuinely different
        # systems into one job and hide the second.
        key, entry = _manifest_entry(source, radius, source_identities_by_name)
        if key in seen:
            _merge_duplicate_entry(seen[key], entry)
            continue
        seen[key] = entry
        entries.append(entry)

    # Highest blast radius first, then by name so the order is stable between runs.
    entries.sort(key=lambda e: (-(e["used_by_count"] or 0), e["name"]))
    return {
        "bundle": sanitize(str(bundle.path)),
        "kind": bundle.kind,
        "total": len(entries),
        "needs_credential": sum(1 for e in entries if e["status"] == NEEDS_CREDENTIAL),
        "snapshots": sum(1 for e in entries if e["status"] == SNAPSHOT),
        "needs_review": sum(1 for e in entries if e["status"] == REVIEW),
        "blast_radius_known": radius["has_handover"] and all(e["blast_radius_known"] for e in entries),
        "connections": entries,
    }


def _connection_summary(entry: dict[str, Any]) -> str:
    """What to connect to, naming every leg of a federated source rather than the word 'federated'.

    A published-datasource proxy is named for what it is instead: `sqlproxy` is Tableau's own front
    end, and its real upstream is defined server-side, so printing the class would tell a platform
    engineer to connect to nothing.
    """
    if entry.get("published_datasource"):
        return "**published data source** - upstream defined in Tableau, not in the workbook"
    if entry.get("legs"):
        return "<br>".join(_one_target(leg) for leg in entry["legs"])
    return _one_target(entry["connection"])


def _one_target(connection: dict[str, str]) -> str:
    """`class @ server / database` for one system, without inventing missing parts."""
    klass = connection.get("class", "unknown")
    where = connection.get("server") or ""
    what = connection.get("database") or connection.get("warehouse") or ""
    tail = " / ".join(p for p in (where, what) if p)
    return f"`{klass}`" + (f" @ {tail}" if tail else "")


def render(manifest: dict[str, Any]) -> str:
    """Render the human half. Written for a platform engineer who has never heard of Tableau."""
    lines = [
        "# Data source connections required after migration",
        "",
        "Migrated semantic models arrive **without credentials** - connections do not travel between",
        "tenants or workspaces. This lists every data source in the migration, what it connects to,",
        "and which reports stay broken until it is connected.",
        "",
        f"- **{manifest['needs_credential']}** source(s) need a connection before their reports work",
        f"- **{manifest['snapshots']}** source(s) are **snapshots** - extracted data with no upstream to connect",
        f"- **{manifest['needs_review']}** source(s) need a look (we could not classify them confidently)",
        "",
    ]
    if not manifest["blast_radius_known"]:
        lines += [
            "> **Impact column unavailable.** This bundle carries no per-workbook handover, so we",
            "> cannot say which reports depend on which source. Ordering below is alphabetical, not",
            "> by impact.",
            "",
        ]

    needs = [e for e in manifest["connections"] if e["status"] == NEEDS_CREDENTIAL]
    if needs:
        lines += [
            "## Connect these",
            "",
            "Ordered by impact: the number of reports that stay broken until it is connected.",
            "",
            "| Data source | Connect to | Reports affected | Which reports |",
            "|---|---|---:|---|",
        ]
        for e in needs:
            who = ", ".join(e["used_by"][:4]) + ("…" if len(e["used_by"]) > 4 else "")
            count = e["used_by_count"] if e["blast_radius_known"] else "?"
            lines.append(f"| **{e['name']}** | {_connection_summary(e)} | {count} | {who or '—'} |")
        lines.append("")

    snapshots = [e for e in manifest["connections"] if e["status"] == SNAPSHOT]
    if snapshots:
        lines += [
            "## Snapshots - nothing to connect",
            "",
            "These were **extracts** in Tableau: a frozen copy of data, not a live connection. The",
            "migrated model holds that same copy. It will not refresh, and there is no credential to",
            "supply. If you need it live, that is a separate exercise - the upstream system has to be",
            "identified and connected for the first time.",
            "",
            "| Data source | Was |",
            "|---|---|",
        ]
        lines += [f"| {e['name']} | {_connection_summary(e)} |" for e in snapshots]
        lines.append("")

    review = [e for e in manifest["connections"] if e["status"] == REVIEW]
    if review:
        lines += [
            "## Needs a look",
            "",
            "We could not classify these with confidence. Treat them as needing a connection until",
            "confirmed otherwise - the failure we are avoiding is a source silently treated as",
            "requiring nothing.",
            "",
            "| Data source | What we saw |",
            "|---|---|",
        ]
        lines += [f"| {e['name']} | {e['why']} |" for e in review]
        lines.append("")

    lines += [
        "---",
        "",
        "*This document contains connection targets (server, database) and never credentials.*",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle", required=True, type=Path, help="estate bundle dir or migration-spec.json")
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"write connections.md / connections.json here; must be git-ignored (default: {DEFAULT_OUT})",
    )
    parser.add_argument("--format", choices=("md", "json", "both"), default="both", help="what to emit")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    manifest = build(args.bundle)

    output_checks = _output_checks(args.out)
    if any(refuse_unignored_output(out, anchor=anchor) for out, anchor in output_checks):
        return 2
    args.out = args.out.expanduser().resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    if args.format in ("md", "both"):
        (args.out / "connections.md").write_text(render(manifest), encoding="utf-8")
    if args.format in ("json", "both"):
        (args.out / "connections.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    LOG.info(
        "%d source(s): %d need a connection, %d snapshot(s), %d to review -> %s",
        manifest["total"],
        manifest["needs_credential"],
        manifest["snapshots"],
        manifest["needs_review"],
        args.out,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
