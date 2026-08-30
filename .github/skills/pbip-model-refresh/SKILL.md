---
name: pbip-model-refresh
description: Refresh a local PBIP/TMDL semantic model open in Power BI Desktop, and persist the result to .pbi/cache.abf (headlessly via AMO Server.ImageSave, UI-Automation fallback). Persisting is the DEFAULT - opt OUT with `--no-save` for read-only or validate-then-deploy work; saving aligns database.tmdl's declared compatibilityLevel so the cache stays loadable, and the cache write is staged and swapped atomically. Use after finishing TMDL edits, before handing a model to report authoring, or whenever Desktop reopens a migrated model empty. For DAX-only edits, pass `--calculate-only` / `--measures-only` to recalculate formulas without re-reading source rows. Source-tool agnostic - the model is already Power BI, so this applies equally to Tableau, Qlik and Cognos migrations.
---

# Refresh a local PBIP model, and make the data survive a close

**Windows only.** Talks to the local Analysis Services instance that Power BI Desktop hosts, over
ADOMD.NET/AMO (pythonnet), plus the Desktop Bridge CLI for process to file mapping.

Requirements, in full: Windows with Power BI Desktop, Python >= 3.11 with `pythonnet`, the ADOMD.NET
and AMO client libraries under the NuGet global-packages cache (`$env:NUGET_PACKAGES` when set,
otherwise `~/.nuget/packages`), and `npx` for `@microsoft/powerbi-desktop-bridge-cli`. No other
dependency, and nothing repo-specific.

The ADOMD.NET client is a *separate* nuget package from TOM/AMO, so a machine can have one and not the
other. If `probe_desktop_query.py` prints `AdomdClient.dll not found in the nuget cache` (and
`preflight.ps1` WARNs `ADOMD.NET client (Desktop probe/refresh)`), restore it — forcing a supported TFM
so `dotnet add` cannot silently no-op on a net10 default:
`dotnet new console -o $env:TEMP\adomd --framework net8.0; dotnet add $env:TEMP\adomd package Microsoft.AnalysisServices.AdomdClient.NetCore.retail.amd64 --version 19.84.1`

## Available scripts

- [**`scripts/refresh_pbip_model.py`**](scripts/refresh_pbip_model.py) - refresh the open model,
  refuse a wrong-instance bind, and persist `cache.abf` (AMO `ImageSave`, UI-Automation fallback).
- [**`scripts/probe_desktop_query.py`**](scripts/probe_desktop_query.py) - read-only preflight: port
  discovery, ADOMD loading, table listing, and the one-row DAX probe. `refresh_pbip_model.py`
  imports from this file and from nothing else.
- [**`scripts/probe_desktop_credential.ps1`**](scripts/probe_desktop_credential.ps1) - the arbiter a
  refresh TIMEOUT names: a UI-Automation check for a data-source sign-in modal, so "slow" and
  "blocked" are told apart by evidence, not guessed. Ships **inside** the bundle, so the instruction
  the script prints at runtime resolves to a file that is actually here. **Only `CREDENTIAL_MISSING`
  (exit 1) is a hard stop**; everything it cannot positively identify as a credential prompt lands in
  the exit-3 "could not probe" band (`REFRESH_IN_PROGRESS` / `DIALOG_UNRECOGNIZED` /
  `DIALOG_UNREADABLE` / `UNKNOWN`) rather than escalating to a human — see the verdict table below.
- [**`tests/`**](tests) - the regression suite for both, runnable from this folder
  (`pytest tests`). It is what makes the portability claim below checkable rather than aspirational.

## The problem this solves

A migrated model hands over as *TMDL plus a promise*. Two things go wrong:

1. **Refreshing is hand-rolled every time.** The only working path against a local PBIP is TMSL over
   the child `msmdsrv` port: discover the port, load ADOMD.NET, resolve the catalog GUID, send
   `refresh`. Re-deriving that per migration is slow and easy to get subtly wrong.
2. **An XMLA refresh is not persisted.** It populates the *in-memory* model only. Desktop keeps a
   PBIP's data in `<Name>.SemanticModel/.pbi/cache.abf`, and if nothing writes that file the next
   person opens an empty model and hits the credential prompt all over again.

## Run it

```
python scripts/refresh_pbip_model.py [--pid <pbidesktop-pid>] [--canaries "A" "B"] [--tables "A" "B"]
                                     [--calculate-only|--measures-only] [--no-save] [--verify-only]
                                     [--ui-save]
```

**`--calculate-only` / `--measures-only` is an opt-in DAX-only shortcut, not the default.** It sends
TMSL refresh type `calculate`, which recalculates formulas, relationships and hierarchies without
re-reading source rows. Use it only when the caller knows the pending edit was measure/DAX-only; after
an M, partition, relationship or data-shape change, `calculate` can leave stale data wearing a fresh
verdict, so the default remains the safe full refresh. This recipe is credited to SES field use
(Sandeep Munagala, 2026-08-21), where the proven sequence was live measure edit -> Calculate ->
ExportToTmdlFolder -> cache persist across ~6 workbooks in one day.

**Persisting is the DEFAULT** — that is this script's stated purpose, *"so the next agent (and the
next Desktop open) sees real data instead of an empty model"*. Pass **`--no-save`** for read-only
work (the validator is read-only **by contract**) or for validate-then-deploy, where the project must
stay byte-identical.

> ⚠️ **Saving ALIGNS `database.tmdl`, and that alignment is what makes it work.**
> **Root-caused 2026-08-07.** The earlier finding (3-vs-3 on 2026-08-04: a persisted `cache.abf`
> made the PBIP open as **"Untitled - Power BI Desktop"** with the bridge reporting
> `Host is not ready to accept operations`) was **real but mis-attributed**. Persisting is not
> inherently destructive; persisting a cache whose compatibility level **disagrees with the
> project's** is.
>
> **The mechanism.** Desktop silently runs the database at a HIGHER level than the project declares —
> measured live `Database.CompatibilityLevel: 1606` against a `database.tmdl` of `1604`, on a server
> whose default is `1700`. `ImageSave` serialises the **live** database, so without alignment the
> cache is a 1606 image in a 1604 project. The reopen then hits Desktop's own **"Tabular databases do
> not support CompatibilityLevel downgrade"**, which surfaces with **no visible message** — only the
> `Untitled` window and the bridge error.
>
> **`--save` therefore aligns, always, with no flag.** That is exactly what Desktop's own Save does: a
> UI Save was measured updating `database.tmdl` `1604 -> 1606` and writing `cache.abf` **at the same
> timestamp**. An earlier version put the alignment behind `--align-compat` and refused otherwise;
> that was removed as **ceremony rather than safety** — the only possible response to the refusal is
> to re-run with the flag, so an agent simply learns to always pass it. Verified end to end: after
> aligning, a cold reopen gives `PREFLIGHT: DATA_OK` with **no refresh**, and the write touches
> **2 files** where a UI Save touches **79**.
>
> ⚠️ **Saving EDITS THE DEPLOYABLE ARTIFACT**, which is what `--no-save` is for. `database.tmdl` ships
> with the model and its declared level goes up. A **read-only** consumer — auditing, validating,
> grading fidelity — must pass `--no-save`, or it mutates the very artifact it is judging.
>
> ⚠️ **A refusal must never fall through to the UI-save fallback.** Measured while fixing this: an
> early version *returned* a refusal, `main()` read it as "ImageSave unavailable" and drove Desktop's
> UI instead — which wrote `cache.abf` anyway and rewrote **74 of 89 files**, performing precisely the
> write that had just been refused. Anything that declines to write must stop the pipeline, not hand
> off to a fallback that writes.
>
> ⚠️ **`MainWindowTitle` is a LOADING STATE before it is a verdict — do not read it early.** Without a
> cache the title still reads `Untitled - Power BI Desktop` at t+25 s and only becomes the report name
> by ~t+55 s. Reading at 25 s produced a false "it is broken" on a bundle that was merely still
> opening. **Wait >= 90 s**, and prefer the bridge error over the title.
>
> ⚠️ **Never write TMDL with a BOM.** Desktop's project reader hard-rejects one —
> `UTF8EncodingThrowOnBOM.CheckBom` -> *"Only text with UTF8 encoding without BOM is supported"* — and
> the file does not open. This bit us during the investigation itself: a probe wrote `database.tmdl`
> with `[System.Text.UTF8Encoding]::new($true)` and contaminated a whole test arm. In Python use
> `encoding="utf-8"`; in PowerShell use `-Encoding utf8NoBOM` and never the `Out-File`/`Set-Content`
> defaults. **This is a separate failure from the compatibility mismatch** — same symptom, different
> cause, and only the Desktop crash report ("Frown") distinguishes them. Ask for one rather than
> inferring from the window title.

> ⚠️ **A refresh TIMEOUT is not evidence of a credential modal.** This script used to assert that,
> and it was wrong: measured 2026-08-04 it fired on 2 of 5 Desktop instances opened on the *same*
> bundle that refreshed cleanly on the other 3 (38.8 s on a good run, >87 s on a slow one, against a
> 90 s ceiling — a ~3 s margin). It now reports `REFRESH: TIMEOUT` with the cause **UNKNOWN** and
> names the arbiter (`scripts/probe_desktop_credential.ps1`, which **ships in this bundle** — the
> script prints its absolute path at runtime so the instruction always points at a file that is here).
>
> **The ceiling is 300 s and is deliberately NOT agent-tunable — there is no flag.** A knob here is
> an attractive nuisance: an agent that hits a timeout reaches for a bigger number, and the one case
> where waiting longer never helps is the credential wait, which this ceiling cannot interrupt
> anyway. If a model legitimately needs longer, refresh less with `--tables`. Never emit a
> "this needs a human" stop from an unverified timeout - that phrasing names the one blocker an
> agent must not retry, so a false positive turns a slow refresh into a permanent dead end.
>
> ⚠️ **Bridge errors that look technical can be a blocking Desktop dialog.** The pair
> `powerbi-desktop status` -> **"Host is not ready to accept operations"** and
> `powerbi-desktop screenshot` -> **"Print metadata is not available"** was measured on an otherwise
> healthy Desktop/bridge when a data-source dialog was already open. Some connector dialogs expose no
> readable text, so the fast check reports either `CREDENTIAL_MISSING` (signature text matched) or one
> of the `DIALOG_*` / `REFRESH_IN_PROGRESS` verdicts (a dialog is up whose content did not positively
> read as harmless). In both cases a human must look at Desktop before the run proceeds. Check this
> first before suspecting the bridge or filing an upstream defect.

> ⚠️ **A big window is NOT evidence of a credential wall — the arbiter no longer says it is
> (issue #367).** `probe_desktop_credential.ps1` used to decide "blocking" from geometry alone: any
> visible non-main window >= 100x100 was returned as a blocking dialog and reported
> `VERDICT: BLOCKED_BY_DIALOG`, **exit 1 — the same hard-stop band as a real credential wall**. A Power
> BI Refresh progress dialog satisfies that trivially. Field report, 2026-08-28: running three
> concurrent refreshes, the probe returned a blocking verdict for one unit; screenshotting the window
> showed Power BI's own Refresh progress dialog, stalled behind a Snowflake warehouse cold start. A
> false credential wall is expensive precisely *because* the toolkit treats a real one as a hard stop
> that no retry may override.
>
> It now classifies each candidate window from **what it says**, and only `CREDENTIAL_MISSING` reaches
> exit 1:
>
> | verdict | exit | what was actually observed |
> |---|---|---|
> | `CREDENTIAL_MISSING` | 1 | text matched `credential_modal_signature.regex`. The hard stop. |
> | `CREDENTIAL_PRESENT` | 0 | a refresh was invoked and ran to the deadline with nothing unclassifiable up. Still not the gate of record for a serverless source — confirm with the one-row data probe. |
> | `REFRESH_IN_PROGRESS` | 3 | a dialog whose **whole content** positively reads as refresh progress was already up **at t=0**: another refresh owns this instance. Wait for it or cancel the stale one; do not stack a second refresh. |
> | `DIALOG_NEEDS_HUMAN` | 3 | a **known** human-blocking prompt that is not a credential prompt — the **native-database-query approval modal** above all. Not exit 1, because the remedy is an approval, not a sign-in. Never suppressed, and it outranks any progress text in the same window. |
> | `DIALOG_UNRECOGNIZED` | 3 | a dialog is up whose text matched **no** signature, **or** which shows progress text *alongside* prose that is not progress status. We read it, and it is **not** a credential prompt — but we cannot account for all of it. |
> | `DIALOG_UNREADABLE` | 3 | a dialog is up that could not be **shown to be harmless**: no text at all, or only a reassuring **caption**, or benign-looking content read from an **incomplete** harvest (truncated, timed out, or a pattern that threw). Deliberately distinct from `DIALOG_UNRECOGNIZED`: *absent is not empty*, and "we could not establish it" is a weaker state of knowledge than "we read it and it did not match". |
> | `UNKNOWN` | 3 | no window for the pid, a minimized owner, or no Refresh control was ever invoked. |
>
> **Which way it errs, and why.** A false *positive* here terminates at exit 1 and escalates to a
> human, so nothing downstream ever runs. A false *negative* lands on `CREDENTIAL_PRESENT`, which the
> repo already treats as untrustworthy on its own — `docs/data-source-credentials.md` records three
> false `CREDENTIAL_PRESENT` results against a serverless warehouse, which is why the one-row data
> probe is the gate of record. One direction has a backstop; the other does not. So this arbiter errs
> **away from declaring a hard stop** — but never into silence: an unclassifiable dialog is *latched*
> during the poll loop and reported at the deadline, so it can neither halt a healthy run nor be
> erased by a quiet timeout.
>
> Two supporting mechanisms:
> - **Modality is used ONE WAY.** `IsWindowEnabled(GetWindow(hwnd, GW_OWNER))` returning true proves a
>   window blocks nothing, and exonerates it. The converse is not used: Power BI's refresh dialog also
>   disables its owner, so a disabled owner would convict the innocent. No owner at all reports `null`
>   — the test did not apply, which is not the same as passing it.
> - **`scripts/benign_dialog_signature.regex`** is the progress vocabulary ("Evaluating", "N rows
>   loaded", "Waiting for other queries"). ⚠️ It is **inferred** from Power BI's refresh UI, not
>   captured from a live dialog, and it is deliberately not load-bearing: a miss only downgrades
>   `REFRESH_IN_PROGRESS` to `DIALOG_UNRECOGNIZED` — both exit 3, neither a credential wall. Keep its
>   alternatives narrow and anchored; a broad pattern here is the one way this file could hide a real
>   modal, and `test_the_benign_signature_can_never_shadow_a_credential_prompt` gates exactly that. If
>   you ever have a real progress dialog on screen, capture its exact text and tighten this file.

> ⚠️ **Win32 child-HWND text does NOT see inside a WPF dialog — and the burden of proof runs ONE WAY.**
> WPF renders its entire visual tree into **one** HWND, so `EnumChildWindows` harvests nothing and only
> the window **caption** survives. Two blind-review rounds on 2026-08-29 found two different silent
> false negatives here, and the second was worse than the first:
>
> | attempt | rule | how it broke |
> |---|---|---|
> | 1 | benign by **caption** | a WPF modal captioned `Refresh` whose content read `Enter your credentials` → suppressed → **`CREDENTIAL_PRESENT`, exit 0** |
> | 2 | benign by caption **+ "we harvested some text"** | a `Cancel` button satisfied that. Beaten three ways: `TextPattern`-only content, content past the element cap, and a split defeated by an interposed element |
>
> Round 2's root cause is the lesson: `ContentRead` was a **proxy** for *"we read the credential-bearing
> content"*, and **no proxy can carry that weight**. A silent false negative on a hard stop is strictly
> worse than the loud false positive #367 removed — and note the old size-only detector caught all of
> this *by accident*, precisely because it never trusted text it had not read.
>
> **The rule now: a dialog is suppressed only when we POSITIVELY READ benign CONTENT** — never because
> we read *something* and the caption looked reassuring. Measured, same owned WPF modal:
>
> | build | result |
> |---|---|
> | before | `refresh invoked: True` → *no credential modal within 12s* → **`CREDENTIAL_PRESENT`, exit 0** |
> | now | `credential modal detected: 'Enter your credentials'` → **`CREDENTIAL_MISSING`, exit 1** |
>
> Four mechanisms, each doing one job:
>
> 1. **UIA harvest** — `Get-AutomationHarvest` reads `Name`, `ValuePattern` **and `TextPattern`** for
>    every candidate's descendants. `TextPattern` is not optional: a read-only `RichTextBox` with an
>    **empty `Name`** exposes its text only there. ⚠️ `LegacyIAccessiblePattern` is **not available** —
>    the type does not exist in the managed `System.Windows.Automation` API (verified; it is UIA-COM
>    only), so MSAA-bridged-only content is unreadable from here. That gap is survivable *only* because
>    completeness is never assumed.
> 2. **`HarvestComplete`, and it must be a real Boolean.** Truncated by the element cap, timed out, or
>    any pattern read that threw → not complete → benign-looking content is reported
>    `DIALOG_UNREADABLE`, not suppressed. `-eq $true` is **coercive**: integer `1` and the string
>    `"true"` both passed it and both cleared a window in review. The test is
>    `($v -is [bool]) -and $v`.
> 3. **A killable child process per harvest.** `FindAll` is a synchronous cross-process COM call —
>    `try/catch` cannot interrupt it and neither can a background thread. Measured against a modal
>    sleeping 120 s on its own UI thread: **6.2 s bounded vs 70.5 s in-process**, same verdict both
>    times. The defect was purely the budget, so only a *time* assertion can see it.
> 4. **The prose join skips interactive elements.** WPF splits a sentence across elements, and an
>    interposed `Cancel` button between `Enter your` and `credentials` defeated a naive whole-window
>    join.
>
> **The join stays asymmetric, on purpose.** The **credential** signature reads individual elements
> *and* the prose join (maximum recall for the detector that convicts). The **benign** signature reads
> individual **content** elements only — never the caption, never a join. Joining can manufacture a
> phrase — two adjacent table names reading `Account` `Key` join to the signature `Account Key` — and
> the direction matters: on the credential path that is a **loud** false stop a human resolves by
> looking at the screen; on the benign path it would be a **silent** false clear.
>
> **The net effect is deliberately conservative.** Every uncertainty — unread content, a truncated
> harvest, a wedged provider, an unknown field type, prose nobody can explain — lands in the exit-3
> band, which is loud and recoverable. A mostly-conservative arbiter with a narrow proven-benign path is
> a better artifact than a clever one with a residual silent clear.

> ⚠️ **One benign element must not account for a whole window — and the native-query modal is why.**
> Round 3 of review found that the **first** content element matching the progress signature classified
> the entire window, so `Evaluating` sitting beside
> *"Permission is required to run this native database query"* was suppressed → **exit 0**. That prompt
> is documented as a live hazard three sections below, and
> `tests/test_credential_modal_detection.py` already treated it as blocking — the bundle would have
> contradicted itself. Three changes, in order of how much they carry:
>
> 1. **`scripts/blocking_prompt_signature.regex`** — known human-blocking prompts that are *not*
>    credential prompts (native-query approval, `Authentication required`). Matched **before** benign
>    and **before** the enabled-owner exoneration, and reported as **`DIALOG_NEEDS_HUMAN`, exit 3** —
>    a human must act, but the remedy is an approval, not a sign-in, so it must not enter the band
>    whose documented meaning is *"sign in once"*.
> 2. **The whole content is scanned before benign is concluded.** Progress text plus prose that is not
>    progress status is `mixed-content` → `DIALOG_UNRECOGNIZED`. The backstop for prompts in *neither*
>    signature is a word count: a content element of **5+ words** that is not itself recognised status
>    is prose written for a human, and it vetoes suppression. Short data labels (`Orders`,
>    `1,204 rows loaded`, `Cancel`) do not — otherwise the benign path is unreachable and the probe
>    could never return `CREDENTIAL_PRESENT` while Desktop shows its own refresh dialog.
> 3. **The benign expressions are whole-element status patterns**, not substrings. `\bLoading data\b`
>    matched inside *"Loading data requires authentication"*. Every alternative is now anchored, so a
>    status word buried in a sentence is not a status.
>
> ⚠️ **And validate the harvest child's payload, not just its JSON.** The parent computed
> `(-not $p.Truncated) -and (-not $p.PatternsIncomplete)`. A missing property is `$null`, and
> `-not $null` is `$true`, so a well-formed-but-schema-incomplete payload became `HarvestComplete`
> **`$true`** — a *real Boolean*, which then sailed straight through the strict
> `Test-HarvestComplete` guard because the coercion had already happened upstream of it. The child must
> now exit 0, and both flags must **exist** and be actual Booleans. Items are still merged when they
> parse (unread text only lowers credential recall), but `Complete` stays false.
>
> **Notice the shape all three review rounds share: MISSING EVIDENCE READ AS GOOD EVIDENCE.** A caption
> standing in for content; "we read something" standing in for "we read the thing that matters"; the
> first benign element standing in for the whole window; a missing JSON property standing in for a
> completed harvest. If you extend this probe — or write any other detector whose output can stop a
> pipeline — that is the failure to look for first.
>
> ✅ **The Python fast check now classifies too (issue #376).** `_credential_modal` had the same
> size-only defect on a **more dangerous** path: `inspect_credential_modal` returned the first visible
> non-main window >= 100x100 as a `blocking_dialog`, and `refresh_pbip_model.py` /
> `probe_desktop_query.py` — **the gate of record** — printed `BLOCKED_BY_DIALOG` at **exit 1**, which
> `probe_live_source` maps to `NO_CREDENTIAL` ("you may NOT build; a human must sign in; terminate the
> run"). It now reads the window and speaks the **same vocabulary as the arbiter** — `CREDENTIAL_MISSING`
> (exit 1, the only hard stop), `REFRESH_IN_PROGRESS` / `DIALOG_NEEDS_HUMAN` / `DIALOG_UNRECOGNIZED` /
> `DIALOG_UNREADABLE` (all **exit 3**). `BLOCKED_BY_DIALOG` is **retired** from the Python path.
>
> Measured before/after on the same synthesised windows, all 702x355 and non-main so size cannot
> separate them:
>
> | window | before | after |
> |---|---|---|
> | `Refresh` + `Evaluating...` | `BLOCKED_BY_DIALOG`, **exit 1** | `REFRESH_IN_PROGRESS`, exit 3 (and **ignored** while our own refresh is in flight) |
> | `Please specify how to connect` | `CREDENTIAL_MISSING`, exit 1 | unchanged |
> | native-query approval | `BLOCKED_BY_DIALOG`, **exit 1** | `DIALOG_NEEDS_HUMAN`, exit 3 |
> | no text at all | `BLOCKED_BY_DIALOG`, exit 1 | `DIALOG_UNREADABLE`, exit 3 |
> | `Save changes?` | `BLOCKED_BY_DIALOG`, exit 1 | `DIALOG_UNRECOGNIZED`, exit 3 |
> | credential text in an **80x60** window | **no finding at all** | `CREDENTIAL_MISSING`, exit 1 |
>
> That last row is the half nobody had noticed: the 100x100 filter gated the **hard stop** as well as
> the classification, because `match_credential_modal` was fed only the size-filtered candidates. A
> credential prompt in a smaller window produced **no finding at all** — a silent false negative on the
> one verdict that matters most. It now scans **every window, any class, any size**, exactly as
> `Test-CredentialModal` always has.
>
> **Three deliberate divergences from the arbiter**, each because Win32 child-HWND text is strictly less
> evidence than a UIA harvest. Do not "fix" them by copying the arbiter:
>
> | arbiter mechanism | Python | why |
> |---|---|---|
> | prose **join** before the credential match | **not ported** | the join needs a control-type signal to skip an interposed `Cancel`; Win32 has none, so the only join available is the naive whole-window one the arbiter discarded — and a join can *manufacture* a phrase (`Account` + `Key` → `Account Key`). That error lands on the one verdict #376 says to err away from. Recall lost this way routes to `unrecognized`/`unreadable` (exit 3, loud), and the arbiter is the escalation path. |
> | enabled-owner **exoneration** | **not ported** | it is a *suppression* path needing owner/enabled state this module does not harvest, and unverifiable here without a live Desktop. Omitting it costs one more exit 3. |
> | `benign-unverified` (truncated harvest) | **not needed** | a text read that throws fails the whole enumeration (`Win32EnumerationError` → `unknown_reason`), so a partial read never reaches the classifier. |
>
> **One asymmetry, and it is deliberate:** a proven-benign progress dialog is reported at **t=0**
> (`REFRESH_IN_PROGRESS` — it is somebody else's refresh; do not stack a second on it) and **ignored**
> once our own operation is in flight. Nothing else is ever ignored. In the bounded refresh wait a
> dialog finding is **latched** rather than raised, so it cannot abort a refresh that may still finish;
> in `probe_desktop_query`'s poll it is **acted on**, because that loop has no deadline of its own and
> a latched dialog behind a wedged query would wait for ever.
>
> **Downstream, honestly:** `probe_live_source` now recognises the dialog tokens **structurally**
> (`scripts/_verdict_lines.py`'s `DIALOG_VERDICT_RE` → `classify_child_verdict`) and maps them to
> **`ERROR`** — *"the probe itself could not run"*. `rc != 0`, the gate stays armed, and nothing claims
> a credential wall we never observed.
>
> ⚠️ **That structural step is load-bearing, not tidiness.** Blind review of PR #400 measured what
> happened without it: the parent fell through to `CREDENTIAL_MARKERS`, an **unanchored scan of the
> whole transcript**, so `DIALOG_NEEDS_HUMAN` quoting its own evidence excerpt `Authentication
> required` — an alternative that genuinely lives in `blocking_prompt_signature.regex` — was relabelled
> `NO_CREDENTIAL`. The parent contradicted the child on a single word and fired *"a human must sign in;
> terminate the run"*. Adding the tokens to the **credential-stop** family instead would have been the
> other wrong answer: it asserts the very wall the child says it did not see.

> ⚠️ **Blind review of PR #400 found the same defect class INSIDE the fix, four more times. Read this
> before touching the classifier — three of the four were "we could not establish it" quietly becoming
> "clean" again.**
>
> | # | what collapsed into the clean bucket | fix |
> |---|---|---|
> | 5 | **A length heuristic stood in for evidence.** `MIN_PROMPT_WORDS = 5` excused every unmatched element under five words, so `Refresh` + `Evaluating...` + **`Please enter your password`** classified `benign` and was **suppressed entirely** in flight. `Password:` (two words) too. Neither matches `credential_modal_signature.regex`, so the prepass did not rescue them. **In that shape the fix was worse than the bug** — the repo's rule is that a credential modal is never worked around. | The amnesty is **gone**. Any content element that is not recognised progress status vetoes dismissal, unless it is in the **enumerated** `benign_chrome_signature.regex` (`Cancel`/`OK`/`Close`) — a positive claim about specific strings that carry no prompt, not a claim about their size. |
> | 3 | **A geometry threshold stood in for harmlessness — the original defect, surviving inside its own fix.** `dialog_candidates` still rejected everything under 100x100, and the all-window credential prepass only rescued *known-signature* text. Measured: a visible **80x60** unreadable owned window beside a normal main window returned `modal=None, dialog=None, unknown_reason=None` — byte-identical to a healthy Desktop. | **No size test at all.** Every visible non-main window is classified. Two exclusions remain and both are positive claims: **zero-area** windows (they rasterise nothing, so they show a human nothing) and the named `HELPER_WINDOW_CLASSES`. |
> | 4 | **A report title fabricated a hard stop.** The all-window prepass read the Desktop **main** window, caption and children, so a report legitimately named `Account Key` / `Personal Access Token` / `Databricks Client Credentials` produced `CREDENTIAL_MISSING` at exit 1. | The **identified main window** is excluded from the prepass — and only it. Every real dialog is still scanned at every size and in every class, so an unusual modal class stays detectable. ⚠️ Helper windows are excluded from *classification* but **not** from the prepass: Power BI's AAD sign-in renders in the `Internet Explorer_Hidden` web view, so credential text really can appear there. |
> | 1 | **Production bypassed the semantics the tests were checking.** `_join_refresh_worker` overrode `join_with_credential_poll`'s in-flight default with the t=0 `_credential_state`, the progress-monitor branch called the t=0 **raise** helper, and the final timeout check repeated it — so a proven-benign progress dialog **stopped the refresh it belonged to**. Measured in both branches: `DialogFoundError(REFRESH_IN_PROGRESS)` on the first poll, with the detector receiving **no `in_flight` argument at all**. The existing test asserted the *helper's* default identity, which production overrode: a test passing for the wrong reason. | Both wait branches and the final check use `_in_flight_credential_state`, and the progress branch latches through the shared `raise_latched_verdict` instead of raising at t=0 semantics. Its latches used to be computed and **discarded**, so it always degraded to a bare `TimeoutError`. The test now asserts on **production's call site** — the argument the detector actually receives. |
>
> **The through-line, and the rule that comes out of it:** a fix that adds a *suppression* path has to
> make its positive claim actually positive. "Short", "big enough", "we read something", "the caption
> looked fine" are all the same mistake wearing different clothes.

> ⚠️ **The PowerShell arbiter still has finding 5's hole — measured, filed as #406, deliberately NOT
> fixed here.** `probe_desktop_credential.ps1` keeps `$MinPromptWords = 5`, and driving its shipped
> classifiers through the test harness shows the identical result: `Refresh` + `Evaluating` +
> `Please enter your password` → `benign` → `REFRESH_IN_PROGRESS`, and **suppressed to `$null`** under
> `-RefreshInFlight`; `Password:` likewise. It was left alone on purpose, following the precedent that
> created issue #376 itself: #367's author found this defect in the Python half, declared it out of
> scope, and filed it rather than silently widening the diff. Removing the amnesty there also reverses
> a reviewed decision — `test_short_data_labels_beside_progress_text_do_not_block_suppression` exists
> to keep `CREDENTIAL_PRESENT` reachable while Desktop shows its own refresh dialog — so it needs its
> own issue and its own review, not a drive-by.
>
> **Reachability, stated rather than hidden.** Removing the amnesty costs the Python detector its
> `benign` path whenever a dialog exposes a table name as child text (`Orders` is not progress status
> and not chrome, so it vetoes). That is the sanctioned trade: `benign` is used only to avoid aborting
> **our own** in-flight operation, so losing it costs extra **exit 3**s, never a silent clear.
> ⚠️ Unobserved in this corpus whether Power BI's real refresh dialog exposes table names as child
> HWNDs — no Desktop was available. If it does, expect `DIALOG_UNRECOGNIZED` where you hoped for
> `REFRESH_IN_PROGRESS`; that is loud and recoverable, and it is the direction this bundle errs in.

> ⚠️ **Do not wait blindly for `NO_BRIDGE` / `not_connected` — bound the bridge wait, then prove the
> executable by PID.** Field report, 2026-08-18, two machines: a box with two Desktop versions
> installed silently launched the **older** one by default, and the Desktop Bridge stayed at
> `NO_BRIDGE` for 10+ minutes with no error. `PBI_DESKTOP_PATH` does **not** fix an already-running
> wrong instance; it is read at launch, not polled. Kill that Desktop by PID, set `PBI_DESKTOP_PATH`
> first, then relaunch.
>
> Use the CLI's bounded wait instead of a human-length cold-start guess:
>
> ```
> powerbi-desktop status --pid <pid> --wait-seconds 90
> ```
>
> Measured in this doc pass with
> `npx --yes @microsoft/powerbi-desktop-bridge-cli status --help`: `--wait-seconds <seconds>` is
> documented by the CLI itself as **"Wait for bridge readiness before returning not_connected"**
> (default `0`). Ninety seconds matches this repo's ~2-minute cap for an unresponsive external
> system. If the bounded wait still returns `not_connected`, treat the likely cause as "wrong Desktop
> build for this machine's bridge", not "keep waiting".
>
> The bridge CLI does **not** expose the Desktop exe path or version. Verified output for a connected
> instance has exactly these fields: `pid`, `bridgeStatus`, `currentFilePath`, `hasUnsavedChanges`,
> `reportDir`, `pages`. So the discovery primitive is OS-level: take the `pid` from `status`, then
> run `Get-CimInstance Win32_Process -Filter "ProcessId=<pid>" | Select-Object CommandLine`.
>
> ⚠️ **MSIX/Store Desktop remains unresolved, not supported or unsupported.** A separate field report
> found that the documented known-good `2.157.627.0` was not installed on a third machine; only a
> classic per-machine build and a Store/MSIX build were present. Do not generalize from that. Open
> [#178](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/178) Defect 2
> reports `refresh_pbip_model.py` refusing an MSIX-launched Desktop with `WRONG_MODEL` because
> pid→model-folder resolution could not follow the Store launch path, while
> `probe_desktop_query._child_port` resolved the same pid fine. That measures a narrow
> identity-resolution gap, not Store bridge compatibility.

> ⚠️ **A `Value.NativeQuery` partition can block an unattended refresh on its own approval modal —
> and this is now a live concern, because migrated custom-SQL sources emit exactly that shape.**
> Power BI Desktop gates native database queries behind *"Permission is required to run this native
> database query"*, controlled by **Options → Global → Security → "Require user approval for new
> native database queries"**
> ([Microsoft Learn](https://learn.microsoft.com/en-us/power-query/native-database-query)).
> Researched 2026-08-19; the parts that decide how you handle it:
>
> | fact | consequence |
> |---|---|
> | The prompt is a **Desktop / mashup-engine** construct. The Service, a gateway refresh and XMLA-triggered refresh never show it. | If the deliverable refreshes in the Service, there is no problem to solve. It is *our* Desktop-bridge pipeline that is exposed. |
> | Approval is keyed to the **exact query text**, not the data source or the file. | A migration that regenerates M re-arms the prompt every time the SQL changes by so much as whitespace. |
> | Approval state is **user- and machine-scoped** and is **not** stored in the PBIP/PBIX. | It does not travel with the artifact. A fresh build agent, or the customer's analyst opening the deliverable, starts unapproved. |
> | `[EnableFolding=true]` does **not** suppress it — the Learn folding page shows the prompt appearing *with* that option set. | Do not expect the emitted options record to help. |
> | `Sql.Database(server, db, [Query="..."])` triggers the **same** prompt. | Switching connector shape is not an escape. |
> | No option in `Value.NativeQuery`'s options record suppresses it. | The only supported off switch is the global setting. |
>
> ⚠️ **The registry recipe you will find online (`DisableNativeDbQueryPrompt = 1` under
> `HKCU\SOFTWARE\Microsoft\Microsoft Power BI Desktop`) is community folklore, not Microsoft-documented
> — and it does NOT apply to an MSIX/Store Desktop.** Measured here 2026-08-19 on Desktop
> 2.157.828.0 installed from the Store: all three candidate keys (`HKCU\SOFTWARE\Microsoft\...`,
> `HKLM\SOFTWARE\Microsoft\...`, `HKCU\SOFTWARE\Policies\Microsoft\...`) are **absent**, because MSIX
> virtualises registry writes into
> `%LOCALAPPDATA%\Packages\Microsoft.MicrosoftPowerBIDesktop_8wekyb3d8bbwe\Settings\settings.dat`.
> So on a Store install, pushing that key does nothing at all and gives you a false sense of having
> fixed it. Verify by observation on **your** install: `reg export` the key, toggle the option in the
> Options UI, export again, diff. On MSIX, set it through the UI once per agent profile.
>
> **If the probe or a refresh stalls on a custom-SQL source, check for this modal before concluding
> anything about credentials.** ✅ **Both detectors now classify it by name.**
> `scripts/blocking_prompt_signature.regex` recognises `native database quer(y|ies)` /
> `requires your approval`, and both `probe_desktop_credential.ps1` (issue #367) and the Python
> `_credential_modal.classify_dialog` (issue #376) report **`DIALOG_NEEDS_HUMAN`, exit 3** — checked
> *before* the progress signature, so a refresh dialog in the same window cannot suppress it. Round 3
> of #367's review found exactly that suppression and it exited 0; see the verdict table above.
> Until #376 the Python path reported this as `BLOCKED_BY_DIALOG` at exit 1, i.e. as a sign-in wall,
> which is the wrong remedy: this needs an **approval**, not an account.

Read-only preflight (proves credentials + source reachability without changing anything):

```
python scripts/probe_desktop_query.py [--pid <pbidesktop-pid>] [--canaries "A" "B"]
```

Use a model with a persisted `.pbi/cache.abf` for Desktop/bridge smoke tests. A PBIP whose live SQL
Server source does not exist in the current tenant will always open behind a credential dialog and is
a bad unattended smoke-test fixture, no matter how well the bridge itself is working.

**Name one canary table per distinct live source** with `--canaries`. A model-level `DATA_OK` is only
emitted when *every* named canary returns rows; with **no** table named, only the first queryable
table is probed and the verdict is **downgraded** to `TABLE_OK '<table>'` — a static parameter/CSV
table can return rows while a live source never loaded, so one arbitrary table can never certify the
whole model. This mirrors the `powerbi-semantic-model-gotchas` rule: *prove a REAL read per live
source*.

⚠️ **`--canaries` verifies; `--tables` narrows the refresh. They are different knobs, on purpose.**
They used to be one, and that built a trap: the only documented way to earn `DATA_OK` was to name
tables in `--tables`, which simultaneously refreshed **only** those tables — certifying a *model*-level
verdict over a *partially* refreshed model, i.e. the "next agent gets an empty model" failure this
bundle exists to prevent, wearing a green badge. So `refresh_pbip_model.py` now emits
`TABLES_OK '<a>', '<b>'` — never `DATA_OK` — whenever `--tables` narrowed the refresh, and
`--canaries` verifies without narrowing anything. `--tables` alone still supplies the canary set (old
callers keep a probe), it just can no longer certify the whole model. `probe_desktop_query.py` never
refreshes, so there `--tables`/`--table` remain plain aliases for `--canaries`.

Both print a machine-readable last line: `REFRESH: DATA_OK + PERSISTED` / `TABLES_OK '<a>', '<b>'` /
`TABLE_OK '<table>'` / `NO_DATA` / `NOT_PERSISTED` / `WRONG_MODEL` / `ERROR <msg>`, and
`PREFLIGHT: DATA_OK` / `TABLE_OK '<table>'` / `NO_DATA` / `ERROR`. Exit 0 is the good outcome — but it
covers a model verdict (`DATA_OK`), a scoped verdict (`TABLES_OK`) **and** a single-table verdict
(`TABLE_OK`), so a gate that needs model-level certainty must require the literal `DATA_OK` — which
means running a **whole-model** refresh with explicit `--canaries`, not merely exit 0.

## Order matters, do not refresh before you finish editing

Desktop **discards `cache.abf` when the model definition is newer than the cache**. Verified: a model
whose `definition/*.tmdl` were touched after the cache was written opened `NO_DATA` despite a 113 KB
cache sitting right there. So:

> make **all** TMDL edits, **reopen Desktop so it loads them**, then refresh, then save.

That middle step is not optional, and it is the easiest one to drop: `powerbi-desktop reload` does
**not** re-read edited TMDL (measured below). Refresh without reopening and you refresh the *old*
in-memory definition, then persist a cache that does not match what is on disk — which opens
`NO_DATA` and looks like the cache-invalidation problem rather than the operator error it is.

Anything that rewrites TMDL afterwards invalidates it, including the host repo's own sanitize step
(here, `set_data_folder.py --sanitize`, which must run before committing). If you sanitize last, you
have thrown the cache away; re-run this script after.

**A cache newer than the definition can still be invalid — you cannot win this by ordering.** Measured
2026-08-01 on `logistics-live-dbx`: sanitize rewrote `expressions.tmdl` at 12:24:08, `ImageSave` wrote
`cache.abf` at 12:24:23 (15 s *newer*, 57.5 KB) — and a cold reopen still came back `NO_DATA`.
Re-localizing, reopening, refreshing and saving again produced a cache that **did** survive a
`Stop-Process -Force` + reopen (`PREFLIGHT: DATA_OK`, no refresh). So "refresh last, after sanitize"
does not rescue the cache: Desktop keys the cache to the definition it was built from.

⚠️ **Scope this measurement honestly.** It changed `expressions.tmdl` — a *data-affecting* file.
Whether a culture-only edit (AI instructions), a description-only `ExportToTmdlFolder` or a
byte-identical rewrite also invalidates the cache is **UNMEASURED**; do not cite this paragraph for
those. What it does establish — and all the warning below needs — is that a favourable mtime proves
nothing.

> ⚠️ **Do NOT gate on `cache.abf` mtime ≥ newest definition-file mtime.** Proposed independently on
> 2026-08-19 by a field team hitting the post-ship-edit case above, and it is a **false-green
> generator**: the measurement in the paragraph above is precisely a cache that was 15 s *newer* than
> the definition and still opened `NO_DATA`. A timestamp comparison passes exactly the build it needs
> to catch. The concern behind it is real — a post-ship polish edit (a caption, a reference line, a
> textbox resize) silently invalidates an already-verified refresh — but the only sound gate is
> behavioural: **after any TMDL rewrite, re-run refresh + persist, then prove it with a cold reopen**
> (`Stop-Process -Id <literal pid> -Force`, reopen, `PREFLIGHT: DATA_OK` with no refresh). Compare
> behaviour, never timestamps.

> **Hand-off rule:** leave the model **localized + refreshed**. Persisting is the default and safe —
> the compatibility alignment above keeps the cache loadable — but a persisted cache only *survives*
> a hand-off when nothing rewrites the TMDL afterwards, and the committer still has to run the
> sanitize step, which invalidates it. So either persist and tell whoever commits to re-run this
> script after sanitizing, or hand off refreshed-but-not-persisted with `--no-save` and have them
> refresh once more post-sanitize. ⚠️ **Revised 2026-08-04, corrected 2026-08-07:** an earlier note
> here blamed *persisting* for making the PBIP unopenable and told you to persist only with a flag;
> root-caused, that was a compatibility-level mismatch `image_save` now aligns away, not persistence
> itself, so persisting is the safe default again.

**`powerbi-desktop reload` does NOT re-read edited TMDL.** Measured 2026-08-01: after editing two
measures on disk, `reload` returned `{"success": true}` and `INFO.MEASURES()` still showed the **old**
expressions — the reload refreshes the report, not the model definition. Only closing Desktop
(`Stop-Process -Id <literal pid> -Force`) and reopening the `.pbip` picks up a model change. This is
easy to miss precisely because the reload reports success, so **verify a model edit landed** with
`EVALUATE SELECTCOLUMNS(INFO.MEASURES(), "Name", [Name], "Expr", [Expression])` before trusting any
number you read back.

⚠️ **A stale Desktop session can also SAVE the old model back over same-day file edits.** Customer
report: Hemang Patel (SES), 2026-08-21, found a verified fix silently reverted overnight, with every
file in the affected workbook's `.Report` and `.SemanticModel` folders sharing one timestamp burst.
Measured by us the same day: while Desktop held the pre-edit in-memory model, a disk-only sentinel
measure was added to TMDL. `powerbi-desktop reload` returned success and **did not overwrite** the
file; `refresh_pbip_model.py --pid` **refused safely** with `REFRESH: WRONG_MODEL`, exit 2; Desktop's
own UI **Save** overwrote the file silently, removed the sentinel, and reverted the TMDL hash exactly
to the pre-edit value.

**Forensic signature:** many files in one workbook folder sharing a single modified time to the
second. Hemang's customer evidence showed ~30+ files rewritten together; our fixture-scale
reproduction rewrote all 18 files under `.Report` + `.SemanticModel` in one burst (15 at
`2026-08-21 12:10:52`, 3 at `12:10:53`). That signature is how to diagnose this after the fact when
no Desktop process remains to inspect.

**Boundary of the measurement:** we reproduced an immediate, explicit Desktop UI Save. We did **not**
reproduce an overnight/idle autosave, and we did **not** evaluate the "Apply external changes" prompt.
Do not cite this as proof of either. The operational rule is still binding: **if you edited files on
disk, do not Save from a Desktop instance that was already open before the edit; close and reopen it
first.** The guarded script path is safer than the human Save path precisely because it fails closed
on `WRONG_MODEL` instead of writing stale state.

## How persistence actually works

| Path | What it is | Status |
|---|---|---|
| **AMO `Server.ImageSave(databaseId, Stream)`** | Writes `cache.abf` directly from the engine. No UI, works headless; staged to a per-run private file and swapped in atomically, so a failed/partial write (or a concurrent second run) can't destroy an existing good cache. | **Default (opt out with `--no-save`)** |
| **UI Automation (`InvokePattern` on the "Save" element)** | Drives Desktop's own Save through the Windows *accessibility* tree. | Fallback / `--ui-save` |

⚠️ **Read the `--no-save` warning above before using either.** Both write the same `cache.abf`. A
present cache was once believed to make the PBIP unopenable; root-caused 2026-08-07, that was a
compatibility-level **mismatch**, which `image_save` now prevents by aligning `database.tmdl` (and
rolling that alignment back if the write fails). With the hazard gone, persisting is the DEFAULT and
matches this script's stated purpose; `--no-save` opts out for read-only or validate-then-deploy work.

**Why ImageSave exists at all**, because the obvious answer says it should not: the guidance is that
writing model state back to `.pbip`/`cache.abf` "is a separate Save action that only the Desktop UI
performs". That is true of the MCP/Bridge surface but **not of the engine**. A TMSL `backup` is
refused only because Desktop runs Analysis Services in **Diskless mode** ("Backup/Restore ... not
supported"), and that same configuration sets `EnableDisklessTMImageSave=1`. AMO exposes the matching
call, and it works. Proven end to end with the control that rules out a silent re-refresh: delete
`cache.abf`, refresh in memory, `ImageSave`, **kill Desktop with `-Force`** (no save prompt),
**rename the source data folder away**, reopen, `DATA_OK` with real rows. With the source absent, that
data can only have come from the cache.

Two traps:

- The AMO client raises **"The server sent an unrecognizable response"** *while writing the file
  correctly*. Judge success by the FILE, never by the absence of an exception — but "the file" means a
  **complete** ABF: only that specific benign response is tolerated, and the staged file is swapped in
  only after its **block chain** walks cleanly to EOF. A `cache.abf` is an Analysis Services backup,
  **not** a Compound File / OLE2 container — an earlier round asserted CFBF and its check therefore
  rejected 100% of real caches, silently disabling persist-by-default on every run. Measured across 13
  real caches (17 KB → 60 MB, written by Desktop *and* by `ImageSave`): a 100-byte UTF-16LE
  `"This backup was created using XPress9 compression."` preamble, a 2-byte pad, then blocks of
  `uint32 uncompressed, uint32 lengthFromTheMagic, 4-byte magic 2A D7 86 4E, payload`, where the next
  header sits at `offset + 8 + length` and the chain ends **exactly** at EOF. A truncated write leaves a
  block claiming bytes past EOF; a write that stopped on a chunk boundary leaves a full-2 MiB *final*
  block (every measured final block is smaller), so both are rejected instead of replacing a good cache.
  The check **fails closed** on any uncertainty and **prints the reason** it refused — a predicate that
  can only say "no" is how the CFBF mistake survived a whole review round. Any other exception propagates and
  the provisional compatibility-level alignment is rolled back; if that rollback cannot itself be
  completed the run **stops fatally** rather than falling through to the UI Save, because
  `database.tmdl` would otherwise ship declaring a level no cache was ever written at.
- `database_operations ExportToTmdlFolder` persists model *definition* changes but carries no rows.
  It cannot substitute for this.

UIA is a **workaround, not a contract**: an accessibility surface is not an automation API. It depends
on an element literally named "Save", so it breaks on ribbon changes and non-English installs, cannot
run headless, and a modal dialog swallows the invoke silently. It is not SendKeys, because
`SetForegroundWindow` is refused in this context, so a Ctrl+S keystroke lands on whatever window has
focus while the model stays dirty. Its result is confirmed against the bridge's own
`hasUnsavedChanges` flag. If a real `save` verb ever ships, delete it.

## Bind to the right instance, or you will corrupt a sibling

The destination is resolved from the **pid**; the data comes from whatever instance answers on the
**port**. If those disagree (one bad `--port`, or a port lookup that widens to "any `msmdsrv` on the
machine") this writes *another migration's model into your own correct-looking `cache.abf`*, and every
downstream signal still agrees: the file exists, is non-empty, is newly written, and the row count
queries that same wrong port.

So, before refreshing, querying or saving anything, two guards run. First, **if you pass `--port` it
must EQUAL the port derived from `--pid`** — a mismatch aborts, so `--port` can never bypass PID-based
discovery to point the read (and the `ImageSave` write) at another instance. Second, `same_model()`
compares the connected model against the TMDL that owns the destination cache and requires an **exact
match of tables, columns AND measures** — table names alone are too coarse (a bound sibling with the
same table names but different columns or measures would pass), so the fingerprint descends into each
table's columns and the model's measures, filtering only the auto-generated noise that never appears in
TMDL (RowNumber system columns, `LocalDateTable_*` / `DateTableTemplate_*` auto-date tables). A
mismatch — or an identity it cannot establish at all (no model folder resolved, or no TMDL to
fingerprint) — aborts with `WRONG_MODEL`, **failing closed** rather than assuming it is fine. A superset
schema (all your tables plus more) is a mismatch, not a "confirmed". File metadata fundamentally cannot
tell you whose rows are in a blob, only the model's own contents can. When a project folder holds
several `.SemanticModel` directories the destination is resolved from the `.pbip` → report →
`definition.pbir` `byPath` **binding first**; only if no binding resolves does a single same-named
sibling act as a last-resort fallback — the binding is authoritative and the name heuristic can never
short-circuit it. In a parallel batch **always pass `--pid`** (`powerbi-desktop status` maps pid to open
file); with several instances open the scripts refuse to guess.

**Sweep for orphans before you build, not just siblings while you build.** Measured 2026-08-01: a
Desktop instance was already running on the *exact `.pbip` path* about to be generated, left over from
an earlier, since-deleted attempt. The tables-only fingerprint then in force would not have caught it —
its TMDL tables matched — though the columns+measures fingerprint now would, because `INFO.MEASURES()`
showed measures the new build never defines. But do not lean on that: an orphan whose schema is
byte-for-byte your model (the common re-open case) is **identical** to `same_model()`, one Save away
from overwriting the files you are generating. So at the start of a build, list `Get-Process
PBIDesktop`, read each one's command line
(`Get-CimInstance Win32_Process -Filter "ProcessId=<pid>"`), and force-close any instance already bound
to your own `.pbip` before writing to it. Identify by `MainWindowTitle` + command line, never by "the
one instance that is running".

## Reusing this in another migration repo

Nothing here is Tableau-specific, the input is already a Power BI model. **Copy this folder.**
`SKILL.md`, the two Python scripts it runs, the `probe_desktop_credential.ps1` arbiter they name at
runtime, and the tests that gate them are one unit: the scripts import only each other, name the
arbiter by their own bundled path, and `tests/conftest.py` resolves them at `../scripts`, so the
suite — and every runtime instruction the scripts print — runs from wherever the folder lands. Drop
it in the target repo's skill location (`.github/skills/`) or promote it to a global one such as
`~/.copilot/skills/`, then prove the copy on the spot:

```
pytest tests
```

The one repo-bound fixture degrades honestly: the TMDL-fingerprint test that reads this repo's
`examples/*/fabric/*.SemanticModel` corpus **skips with a reason** when there is no such tree, rather
than failing or silently collecting nothing.

In the source repo that claim is a CI gate, not a sentence:
`tests/test_skills.py::test_a_bundled_skill_passes_its_own_tests_after_being_copied_out_of_this_repo`
copies this folder to a temp directory and runs `pytest tests` there with the repo root out of
`sys.path`. A new `import <something-repo-local>` fails there, before it can fail in your repo.

Two things do **not** travel, by design:

- `scripts/probe_desktop_query.py` and `scripts/refresh_pbip_model.py` at the *source repo's* root
  are forwarding shims for callers that predate the move. Do not copy them.
- `refresh_pbip_model.py` warns about this repo's `set_data_folder.py --sanitize`. Read that as
  "any post-processing that rewrites TMDL", and substitute your own.

No `allowed-tools` in the frontmatter, deliberately. Pre-approving `shell` would save one
confirmation per run, and GitHub's warning is the reason it is not worth it: pre-approving `shell` or
`bash` *"removes the confirmation step for running terminal commands and can allow attacker-controlled
skills or prompt injections to execute arbitrary commands in your environment"*
([docs](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/customize-cloud-agent/add-skills)).
These scripts spawn PowerShell, load .NET assemblies and write a binary cache file, so they are
exactly the shape that warning is about. The field is also marked experimental in the spec, with
support varying by runtime. Revisit only with a reason.
