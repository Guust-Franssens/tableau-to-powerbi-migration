"""Mutation proof for the #472 download watchdog, the sweep tally and the orphan trace.

usage: python tests/mutate_harvest_watchdog.py

⚠️ Three things are required before a mutation is called KILLED, because "the tests went red" is
not evidence on its own. This repo has a measured case of 22/22 mutations scored "caught" that were
all false positives -- an import error exits non-zero, and a naive scorer reads that as a detection.
So every mutant must:

1. **compile** (`py_compile`). A mutant that does not import never executed the mutated path, and
   scoring it as caught is a fabricated verdict. My own first campaign hit exactly this: a
   `trust-an-unmoved-probe` mutation with the wrong indentation was reported SURVIVED, and an
   earlier scorer would have reported it CAUGHT;
2. fail its **anchor**, named as a pytest NODE ID rather than a file. Mutations run under `-x`, so a
   whole-file target credits a mutation to whichever test fails first -- which may have nothing to
   do with the behaviour under test;
3. leave its **control** node passing. A mutation that reddens everything has not demonstrated that
   the anchor observes *this* behaviour; it has demonstrated that the module is broken.

Two discriminating controls run alongside the real mutations, because a campaign that reports
everything KILLED is indistinguishable from one that is not working:

* `control-cosmetic` changes prose no assertion depends on. It MUST **SURVIVE**.
* `control-absent-anchor` names a test that does not exist. It MUST be reported **INVALID** rather
  than caught, even though pytest exits non-zero for it.
"""

from __future__ import annotations

import py_compile
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = "scripts/harvest_estate_assets.py"
TESTS = "tests/test_harvest_download_watchdog.py"


def node(name: str) -> str:
    return f"{TESTS}::{name}"


# (label, find, replace, anchor node id, control node id, what it breaks)
MUTATIONS: list[tuple[str, str, str, str, str, str]] = [
    # ---------------------------------------------------------------- the watchdog decision
    (
        "stall-never-fires",
        "            if since_progress > stall_timeout:",
        "            if since_progress > stall_timeout * 1000:",
        node("test_a_stalled_download_is_killed_once_progress_stops"),
        node("test_a_short_healthy_run_returns_its_output_and_no_verdict"),
        "a stalled download is never killed",
    ),
    (
        "ceiling-kills-a-progressing-download",
        "        stall_armed = progress_observed and signal_live and blind_since is None\n"
        "        blind_for = now - (blind_since if blind_since is not None else now)",
        "        stall_armed = False\n        blind_for = elapsed",
        node("test_a_progressing_download_is_not_killed_by_the_wall_clock_ceiling"),
        node("test_a_short_healthy_run_returns_its_output_and_no_verdict"),
        "the pre-#472 behaviour restored verbatim: a plain wall clock over a transfer we can see moving",
    ),
    (
        "trust-an-unmoved-probe",
        "            elif last_value is not None and value != last_value:\n"
        "                progress_observed = True\n"
        "                last_change = now\n"
        "                blind_since = None",
        "            elif last_value is not None:\n"
        "                progress_observed = True\n"
        "                last_change = now\n"
        "                blind_since = None",
        node("test_progress_must_be_OBSERVED_before_a_flatline_counts_as_a_stall"),
        node("test_a_short_healthy_run_returns_its_output_and_no_verdict"),
        "a probe that never moved arms the stall deadline (the uv-trampoline trap)",
    ),
    (
        "no-subtree-walk",
        "        return [pid, *windows_descendants(pid)]",
        "        return [pid]",
        node("test_the_real_probe_sums_a_process_SUBTREE_not_just_the_pid_handed_to_popen"),
        node("test_a_short_healthy_run_returns_its_output_and_no_verdict"),
        "the probe samples only Popen.pid, i.e. the uv trampoline",
    ),
    (
        "no-descendant-kill",
        '    descendants = windows_descendants(proc.pid) if sys.platform == "win32" else process_tree(proc.pid)[1:]',
        "    descendants = []",
        node("test_terminate_tree_kills_the_descendant_too"),
        node("test_a_short_healthy_run_returns_its_output_and_no_verdict"),
        "terminate_tree orphans the real interpreter, socket and session included",
    ),
    # ---------------------------------------------------------------- blind review round 2
    (
        "stall-default-back-under-the-fetchers-read-timeout",
        "DEFAULT_STALL_TIMEOUT = ENGINE_READ_TIMEOUT_SECONDS + 120.0",
        "DEFAULT_STALL_TIMEOUT = 120.0",
        node("test_a_bursty_but_healthy_transfer_survives_a_gap_the_fetcher_would_tolerate"),
        node("test_a_stalled_download_is_killed_once_progress_stops"),
        "a bursty transfer urllib tolerates is killed and reported as hung",
    ),
    (
        "a-lost-probe-counts-as-a-flatline",
        "            signal_live = False\n"
        "            last_value = None\n"
        "            if blind_since is None:\n"
        "                blind_since = now",
        "            signal_live = True",
        node("test_losing_the_probe_after_movement_does_not_kill_a_healthy_download"),
        node("test_a_stalled_download_is_killed_once_progress_stops"),
        "an unreadable probe is read as 'no bytes moved' and kills a healthy download",
    ),
    (
        "partial-reading-passed-off-as-a-total",
        "            if not handle:\n                return None",
        "            if not handle:\n                continue",
        node("test_a_partial_subtree_reading_is_reported_as_UNAVAILABLE_not_as_a_smaller_sum"),
        node("test_a_stalled_download_is_killed_once_progress_stops"),
        "the readable trampoline's constant is reported as the subtree total",
    ),
    (
        "blind-ceiling-measured-from-process-start",
        "        elif timeout and blind_for > timeout:",
        "        elif timeout and elapsed > timeout:",
        node("test_the_wall_clock_restarts_from_the_MOMENT_the_signal_was_lost"),
        node("test_a_stalled_download_is_killed_once_progress_stops"),
        "a download that progressed for ages and then went blind is killed on the spot",
    ),
    (
        "losing-the-signal-is-not-announced",
        "            if progress_observed and not announced_signal_lost:",
        "            if False and progress_observed and not announced_signal_lost:",
        node("test_losing_the_signal_is_announced_once"),
        node("test_a_stalled_download_is_killed_once_progress_stops"),
        "the run goes blind silently",
    ),
    (
        "no-cli-warning-for-a-stall-timeout-under-300s",
        "    if 0 < args.download_stall_timeout < ENGINE_READ_TIMEOUT_SECONDS:",
        "    if False and 0 < args.download_stall_timeout < ENGINE_READ_TIMEOUT_SECONDS:",
        node("test_the_cli_warns_when_the_stall_deadline_undercuts_the_fetcher"),
        node("test_a_stalled_download_is_killed_once_progress_stops"),
        "an operator undercuts the fetcher's own read timeout and is told nothing",
    ),
    # ---------------------------------------------------------------- the failure text
    (
        "silent-heartbeat",
        "        if now - last_beat >= heartbeat:",
        "        if False and now - last_beat >= heartbeat:",
        node("test_a_long_run_reports_elapsed_time_rather_than_looking_like_work"),
        node("test_a_short_healthy_run_returns_its_output_and_no_verdict"),
        "a long download reports no elapsed time and looks like work",
    ),
    (
        "no-over-ceiling-warning",
        "            if timeout and elapsed > timeout and not announced_over_ceiling:",
        "            if False and timeout and elapsed > timeout and not announced_over_ceiling:",
        node("test_a_download_progressing_past_the_ceiling_is_announced_loudly"),
        node("test_a_short_healthy_run_returns_its_output_and_no_verdict"),
        "we sail past our own --download-timeout without saying so",
    ),
    (
        "stall-message-loses-the-flag",
        'f"transfer, not a slow one. Raise --download-stall-timeout above "',
        'f"transfer, not a slow one. Try again later, above "',
        node("test_the_stall_message_says_what_was_observed_and_which_flag_to_turn"),
        node("test_a_short_healthy_run_returns_its_output_and_no_verdict"),
        "the stall message names no flag to turn",
    ),
    (
        "ceiling-message-loses-elapsed",
        'f"timeout after {timeout:g}s without a usable download-progress signal (elapsed "\n'
        '                f"{elapsed:.1f}s, {blindness}); a slow-but-healthy transfer cannot be told from a "',
        'f"timeout after {timeout:g}s. A slow-but-healthy transfer cannot be told from a "',
        node("test_the_ceiling_message_distinguishes_itself_from_a_stall"),
        node("test_a_short_healthy_run_returns_its_output_and_no_verdict"),
        "the ceiling message reports the ceiling but not what was observed",
    ),
    # ---------------------------------------------------------------- the tally
    (
        "never-downloaded-is-invisible",
        '    return [r for r in results if "ours" not in r or "theirs" not in r]',
        "    return []",
        node("test_a_never_downloaded_asset_is_named_in_the_report"),
        node("test_a_clean_sweep_still_says_none"),
        "a row that never downloaded lands in no bucket while inflating the denominator",
    ),
    (
        "shape-keeps-the-digits",
        '    return re.sub(r"\\d+(?:\\.\\d+)?", "N", str(message)).strip()[:90] or "(no detail)"',
        '    return str(message).strip()[:90] or "(no detail)"',
        node("test_download_failures_are_grouped_by_shape_not_by_elapsed_seconds"),
        node("test_a_clean_sweep_still_says_none"),
        "two timeouts differing only in elapsed seconds are counted as two findings",
    ),
    (
        "parser-buckets-count-a-failed-download",
        '    ours_fail = [r for r in parsed if r.get("ours", {}).get("ok") is False]',
        '    ours_fail = [r for r in parsed if r.get("ours", {}).get("ok") is not True] + missing',
        node("test_the_parser_buckets_ignore_rows_that_never_reached_a_parser"),
        node("test_a_clean_sweep_still_says_none"),
        "a download failure is reported as a parser verdict",
    ),
    (
        "no-operator-report",
        '    if missing:\n        LOG.warning(\n            "%d of %d asset(s) NEVER DOWNLOADED',
        '    if missing:\n        LOG.debug(\n            "%d of %d asset(s) NEVER DOWNLOADED',
        node("test_the_failed_downloads_are_reported_to_the_operator_at_the_end"),
        node("test_a_clean_sweep_still_says_none"),
        "failed downloads are never surfaced where an operator reads",
    ),
    (
        "timeouts-not-passed-through",
        "                    timeout=args.download_timeout,\n"
        "                    stall_timeout=args.download_stall_timeout,",
        "                    timeout=DEFAULT_DOWNLOAD_TIMEOUT,\n"
        "                    stall_timeout=DEFAULT_STALL_TIMEOUT,",
        node("test_main_forwards_the_operator_s_timeouts_to_every_download"),
        node("test_a_clean_sweep_still_says_none"),
        "the operator's flags are parsed and then ignored",
    ),
    # ---------------------------------------------------------------- the orphan trace
    (
        "orphans-never-computed",
        "    orphans = orphaned_dependents(results, edges)",
        "    orphans = []",
        node("test_main_end_to_end_names_the_orphaned_workbook"),
        node("test_a_clean_sweep_still_says_none"),
        "a failed datasource stops naming the workbooks it orphans",
    ),
    (
        "orphans-flag-every-landed-workbook",
        "        if datasource_luid in failed_datasources and workbook_luid in landed_workbooks:",
        "        if workbook_luid in landed_workbooks:",
        node("test_nothing_is_orphaned_when_the_datasource_landed"),
        node("test_a_clean_sweep_still_says_none"),
        "workbooks bound to a datasource that landed fine are flagged too",
    ),
    (
        "orphans-include-a-workbook-that-itself-failed",
        '        for r in results\n        if id(r) not in missing_ids and r.get("kind") == "workbook"',
        '        for r in results\n        if r.get("kind") == "workbook"',
        node("test_a_workbook_that_ITSELF_failed_is_not_listed_as_an_orphan"),
        node("test_a_clean_sweep_still_says_none"),
        "a workbook that itself never landed is reported as an orphan of something else",
    ),
    (
        "orphan-edges-lose-the-name-fallback",
        "                OR (\n"
        "                    COALESCE(dependency.datasource_luid, '') = ''\n"
        "                    AND LOWER(TRIM(dependency.datasource_name)) = LOWER(TRIM(datasource.name))\n"
        "                )\n"
        "            ORDER BY datasource.name, workbook.name",
        "            ORDER BY datasource.name, workbook.name",
        node("test_an_edge_resolved_only_by_NAME_still_finds_the_dependent"),
        node("test_a_clean_sweep_still_says_none"),
        "an edge the survey could not resolve to a LUID is dropped silently",
    ),
    (
        "orphans-not-written-to-the-report",
        '    if orphans:\n        lines.append("## Do not convert yet',
        '    if False and orphans:\n        lines.append("## Do not convert yet',
        node("test_the_orphans_reach_the_report_and_the_operator"),
        node("test_a_clean_sweep_still_says_none"),
        "the orphan finding never reaches parse-sweep.md",
    ),
    # ---------------------------------------------------------------- discriminating controls
    (
        "control-cosmetic",
        "purpose: download every workbook and published datasource on a Tableau site",
        "purpose: download each workbook and published datasource on a Tableau site",
        node("test_a_short_healthy_run_returns_its_output_and_no_verdict"),
        node("test_a_clean_sweep_still_says_none"),
        "MUST SURVIVE: prose no assertion depends on",
    ),
    (
        "control-absent-anchor",
        "    return [pid, *windows_descendants(pid)]",
        "    return [pid]",
        node("test_this_test_does_not_exist_and_never_did"),
        node("test_a_clean_sweep_still_says_none"),
        "MUST BE INVALID: pytest exits non-zero for an unknown node id, which is not a detection",
    ),
]

EXPECT_SURVIVE = {"control-cosmetic"}
EXPECT_INVALID = {"control-absent-anchor"}


def pytest_node(node_id: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", node_id, "-q", "--no-header", "-p", "no:cacheprovider", "--color=no"],
        cwd=ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def ran_a_test(proc: subprocess.CompletedProcess[str]) -> bool:
    """True when pytest actually selected and ran something.

    Exit code 4 (usage error) and 5 (nothing collected) both mean no verdict exists, and scoring
    either as a detection is how a campaign reports kills it never earned.
    """
    summary = (proc.stdout or "").strip().splitlines()
    return bool(summary) and any(("passed" in line or "failed" in line) for line in summary[-3:])


def verdict_for(label: str, anchor: str, control: str, compiles: bool) -> tuple[str, str]:
    if not compiles:
        return "INVALID", "the mutant does not compile, so the mutated path never executed"
    anchor_run = pytest_node(anchor)
    if not ran_a_test(anchor_run):
        return "INVALID", f"the anchor selected no test ({(anchor_run.stdout or '').strip().splitlines()[-1:]})"
    if anchor_run.returncode == 0:
        return "SURVIVED", "the anchor still passes"
    control_run = pytest_node(control)
    if control_run.returncode != 0:
        return "OVERBROAD", "the control broke too, so the anchor proves nothing specific"
    return "KILLED", f"anchor red, control green ({label})"


def main() -> int:
    path = ROOT / TARGET
    original = path.read_text(encoding="utf-8")
    scratch = ROOT / "_mutant_compile_check.py"
    rows: list[tuple[str, str, str, str]] = []
    try:
        for label, find, replace, anchor, control, breaks in MUTATIONS:
            if original.count(find) != 1:
                rows.append((label, "UNAPPLIED", f"anchor text appears {original.count(find)}x", breaks))
                print(f"{'UNAPPLIED':<10} {label}")
                continue
            mutant = original.replace(find, replace)
            path.write_text(mutant, encoding="utf-8")
            scratch.write_text(mutant, encoding="utf-8")
            try:
                py_compile.compile(str(scratch), doraise=True, cfile=str(scratch.with_suffix(".pyc")))
                compiles = True
            except py_compile.PyCompileError:
                compiles = False
            try:
                verdict, detail = verdict_for(label, anchor, control, compiles)
            finally:
                path.write_text(original, encoding="utf-8")
            rows.append((label, verdict, detail, breaks))
            print(f"{verdict:<10} {label:<52} {detail[:60]}")
    finally:
        path.write_text(original, encoding="utf-8")
        scratch.unlink(missing_ok=True)
        scratch.with_suffix(".pyc").unlink(missing_ok=True)

    print("\n| mutation | breaks | verdict |")
    print("|---|---|---|")
    for label, verdict, _, breaks in rows:
        print(f"| `{label}` | {breaks} | **{verdict}** |")

    wrong = [
        label
        for label, verdict, _, _ in rows
        if verdict != ("SURVIVED" if label in EXPECT_SURVIVE else "INVALID" if label in EXPECT_INVALID else "KILLED")
    ]
    killed = sum(1 for label, verdict, _, _ in rows if verdict == "KILLED")
    print(f"\n{killed} killed; {len(rows) - killed} control(s)/other. Unexpected verdicts: {wrong or 'none'}")
    return 1 if wrong else 0


if __name__ == "__main__":
    raise SystemExit(main())
