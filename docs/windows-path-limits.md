# Windows path limits, and what they cost a shipped bundle

> **Headline, measured 2026-08-29: Power BI Desktop refuses a path it considers too long using its
> OWN managed guard, so `LongPathsEnabled` cannot rescue it — on any machine, including ours.** A
> bundle whose deepest file reaches **260** characters, or whose deepest directory reaches **248**,
> cannot be opened. Issue
> [#235](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/235) left this open as
> "the highest-value unknown currently open on the toolkit". This page is the answer and the
> experiment.

Run the check:

```
python scripts/check_path_ceiling.py <bundle> [--json report.json] [--min-root-budget 60]
```

Exit `0` clean · `1` findings · `2` usage · `3` could not evaluate.

---

## 1. The mechanism — and why the registry setting is a red herring

Desktop's failure is **not** an OS error it inherited from the filesystem. It is a length comparison
in Desktop's own managed code, executed before any file is touched. A failing open names it directly:

```
Microsoft.PowerBI.Packaging.Project.PBIProjectUtils.EnsureNotLong(String path, Boolean isFolder)
  at Microsoft.PowerBI.Client.Windows.Services.DiskProjectFilesReader.<GetAsync>d__2.MoveNext()
```

surfaced as `FilePathTooLongError`, wrapped in `Error Reading StorageSection: ReportDocument`:

> The specified path, file name, or both are too long. The fully qualified file name must be less
> than **260** characters, and the directory name must be less than **248** characters.

Windows offers an opt-in that lifts `MAX_PATH`:

```
HKLM\SYSTEM\CurrentControlSet\Control\FileSystem  ->  LongPathsEnabled = 1     (default is 0)
```

**It does not help Desktop, and cannot.** `EnsureNotLong` never asks the OS. Every measurement below
was taken on a machine with `LongPathsEnabled = 1`, and Desktop refused anyway.

That is materially worse than this issue's original framing. It is **not** "customers on stock
Windows are at risk" — it is **every consumer on every machine, including ours**, regardless of
registry configuration. The registry setting only ever governed whether our *generator* could
**write** these paths: Python 3.6+ declares `longPathAware`, so here it can. That asymmetry is the
whole defect — we can produce artifacts we can never open.

> ⚠️ The earlier reasoning in #234 — *"282 is an archive budget, not a filesystem limit; we run at 269
> on disk today without trouble"* — was wrong twice over. "Without trouble" was not a property of the
> artifact, and it was not even a property of this machine.

| machine | `LongPathsEnabled` | files | over the ceiling | longest |
|---|---:|---:|---:|---:|
| our build machine (issue #235, `_bundle-*`) | **1** (non-default) | — | 93 | 269 |
| customer SES machine A | 0 | 1,408 | 0 | 258 |
| customer SES machine B | 0 | 4,458 | 0 | 248 |
| `_runs/coldrun-2.339.0-20260829/bundle` | 1 | 73 | 0 | 251 |
| **`_runs/estate-2.339.0-20260829` (52-unit run)** | 1 | **2,548** | **183** | **287** |

---

## 2. Experiment A — the embedded application manifest (static, machine-independent)

A binary's long-path awareness lives in its `RT_MANIFEST` resource, baked into the shipped `.exe`:

```python
import ctypes
from ctypes import wintypes as w

k = ctypes.WinDLL("kernel32", use_last_error=True)
k.LoadLibraryExW.restype = ctypes.c_void_p
k.LoadLibraryExW.argtypes = [w.LPCWSTR, w.HANDLE, w.DWORD]
k.FindResourceW.restype = ctypes.c_void_p
k.FindResourceW.argtypes = [ctypes.c_void_p] * 3
k.SizeofResource.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
k.LoadResource.restype = ctypes.c_void_p
k.LoadResource.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
k.LockResource.restype = ctypes.c_void_p
k.LockResource.argtypes = [ctypes.c_void_p]

h = k.LoadLibraryExW(exe_path, None, 0x2 | 0x20)   # AS_DATAFILE | AS_IMAGE_RESOURCE
res = k.FindResourceW(ctypes.c_void_p(h), ctypes.c_void_p(1), ctypes.c_void_p(24))  # id 1, RT_MANIFEST
size = k.SizeofResource(ctypes.c_void_p(h), ctypes.c_void_p(res))
ptr = k.LockResource(ctypes.c_void_p(k.LoadResource(ctypes.c_void_p(h), ctypes.c_void_p(res))))
print("longPathAware" in ctypes.string_at(ptr, size).decode("utf-8", "replace"))
```

Results against Power BI Desktop **2.157.828.0** (Store package, `.../bin/`):

| binary | role | `longPathAware` |
|---|---|---|
| `PBIDesktop.exe` | the application | **absent** (manifest id 1, 1929 bytes: `dependency` + `compatibility` only, no `windowsSettings`) |
| `msmdsrv.exe` | Analysis Services engine that loads the model | **absent** |
| `Microsoft.Mashup.Container.NetFX45.exe` | M engine that reads source files | **absent** |
| `python.exe` | **control (positive)** | present |
| `explorer.exe` | **control (negative)** | absent |

### The static result alone is NOT conclusive — and knowing why matters

A program without the manifest declaration can still handle long paths by prefixing `\\?\` itself:

| binary | manifest says | actually read a 262-char path? |
|---|---|---|
| `node.exe` | `longPathAware` absent | **yes** — libuv prefixes `\\?\` internally |
| `pwsh.exe` | `longPathAware` absent | **yes** — .NET Core prefixes in `PathInternal` |
| `cmd.exe`, `powershell.exe`, `notepad.exe` | present | yes |

That is why #235's earlier probe found *every* consumer succeeding at 460 characters and concluded
"the masking is total": several of those tools were never depending on the registry at all. So the
manifest was treated as a **hypothesis**; `EnsureNotLong` (§1) is the real mechanism, and the A/B
below is the proof.

---

## 3. Experiment B — the live A/B, pinned to the exact boundary

Three **byte-identical** copies of `examples/shipping-kpis/fabric` (27 files), differing only in the
length of their root, opened on this machine with `LongPathsEnabled = 1`. Each was judged by **two
independent instruments** — the window title, and the Desktop Bridge, because a title is a *loading
state* before it is a verdict:

| copy | deepest file | deepest dir | `.pbip` entry | window title @ t+200 s | `powerbi-desktop status --pid` |
|---|---:|---:|---:|---|---|
| control | 200 | 188 | 119 | `ShippingKPIs` | (not probed) |
| **F259** | **259** | **247** | 178 | `ShippingKPIs` | **`ready` / `connected`**, pages enumerated |
| **S260** | **260** | **248** | 179 | `Untitled - Power BI Desktop` | **`error`** — *"Host is not ready to accept operations"* |

Every entry-point `.pbip` path was far below any limit, so the failure is attributable to a deep
child file and nothing else. S260 raised a modal **"Issues were found"** dialog naming the exact
`visual.json`.

**One character decides it.** `F259` opens; `F259` + 1 does not. So:

| constant | value | meaning |
|---|---:|---|
| `FILE_CEILING` | **259** | longest legal full file path — Desktop refuses **260** |
| `DIR_CEILING` | **247** | longest legal directory path — Desktop refuses **248** |

> ⚠️ **The S260 fixture crosses both boundaries at once** (file 260 *and* directory 248), so that one
> pair cannot say *which* guard fired. `F259` opening proves both 259 and 247 are legal, which is what
> a gate needs. Separating the two would require a tree whose deepest directory breaks 247 while every
> file stays within 259 — and the PBIR layout cannot produce one (see §7).

### Reproducing it

```powershell
# 1. copy examples/shipping-kpis/fabric to a root of length (target - 99); its deepest tail is 99.
#    Python is long-path aware, so it can WRITE the long copy even though Desktop cannot read it.

# 2. launch, naming the PID
$p = Start-Process -FilePath $env:PBI_DESKTOP_PATH -ArgumentList "`"<path>\ShippingKPIs.pbip`"" -PassThru
$p.Id

# 3. wait >= 90 s, then judge with the BRIDGE, not the title
npx --yes @microsoft/powerbi-desktop-bridge-cli status --pid <literal pid> --wait-seconds 60

# 4. clean up with a LITERAL pid
Stop-Process -Id <literal pid> -Force
```

> ⚠️ Deleting a >259-char fixture needs the `\\?\` prefix
> (`shutil.rmtree("\\\\?\\" + str(root))` from Python); Explorer and many shells cannot remove it.

### One correction to the crash report's framing

A crash report from this experiment showed Desktop producing a **Frown dialog**, suggesting the
failure is an unattributable crash rather than a graceful error. **That is not what two independent
runs of this A/B produced.** In both, Desktop showed a modal **"Issues were found"** dialog that
*names the offending file* and states the rule — an attributable error, not a silent crash. No Frown
window was present on any process afterwards, and no crash artifact appeared under the Store app's
local cache. The most likely origin of the Frown is a `Stop-Process -Force` on an instance holding
that modal dialog, or telemetry raised for the handled `FilePathTooLongError`.

This is worth stating precisely because it *lowers* one part of the severity: a customer hitting the
limit does get told which file is at fault. It does not lower any other part — the bundle still
cannot be opened.

---

## 4. The failing shape

The path that fails is always the deepest routine structure PBIR produces:

```
<unit>\<unit>.Report\definition\pages\<page-id>\visuals\<visual-id>\visual.json
```

`<unit>` appears **twice**. On the 52-unit estate run, a 48-character workbook name spends **97
characters** on those two segments alone, and produced the worst path in the estate at **287**.

The directory rule earns its place without adding false-alarm surface: `\visual.json` is exactly 12
characters and `260 − 248` is also exactly 12, so for that file the two rules bite at the same point.
The directory rule is only *stricter* for shorter names (`page.json`, `.platform`) — precisely what a
file-only check would miss.

**282 is not encoded.** It remains an unreproduced archive anecdote (`docs/deterministic-tier-integration.md:414`).
Pass `--ceiling 282` if you want to test that budget.

---

## 5. The portable number: tail length and root budget

"183 files over the ceiling" is a fact about where a bundle happens to sit on *one* disk. The customer
will put it somewhere else. The number that survives relocation is the longest **tail** — the path
relative to the bundle root — and the budget it leaves:

```
root_budget = 259 - longest_tail
```

That is the longest install root the bundle tolerates. A customer unpacking to
`C:\Users\<name>\Documents\migrations\` has already spent ~40 characters.

### `_runs/estate-2.339.0-20260829` — 52-unit run, measured 2026-08-29

```
measured   : 4851 paths (2548 files, 2303 directories)
longest    : 287 chars   (28 over the ceiling)
OVER CEILING: 183 paths
near ceiling (> 240): 279 paths                                        EXIT=1
```

3 of 51 units in `pbip/` are already over the ceiling **as they sit**:

| unit | paths over | longest tail | root budget |
|---|---:|---:|---:|
| `Section 15 - Tableau Sales _ Customer Dashboards` | 48 | 197 | **62** |
| `Section 12 - Row Level Calculations (Functions)` | 32 | 195 | **64** |
| `Section 12 - Aggregate Calculations` | 2 | 171 | 88 |
| `Seed - 93 - Interactivity Gauntlet` | 0 | 169 | 90 |
| `Section 12 - Table Calculations` | 0 | 163 | 96 |

A root budget of 62 means the *entire* path to the bundle — drive, user folder, everything — must fit
in 62 characters or the unit cannot be opened. That is not a hypothetical.

### `_runs/coldrun-2.339.0-20260829/bundle` — small run, for contrast

```
measured    : 114 paths (73 files, 41 directories)
longest     : 251 chars   ->  clean, 8 characters of headroom
longest tail: 153 chars   ->  root budget 106                          EXIT=0
```

`--min-root-budget N` turns the budget into a gate. It is opt-in because the reasonable value depends
on where the customer unpacks.

---

## 6. What the check deliberately does NOT do

* **It never asks the OS whether a path can be opened.** The verdict is computed arithmetically from
  path strings, so there is no code path by which the host's registry setting — or the host's
  operating system — can soften it. Linux CI produces the same numbers for the same tree. The registry
  value *is* read and printed, and labelled as **not affecting Desktop**, because #235 exists entirely
  because nobody printed it.
* **A path it cannot measure is `unknown`, never passing.** Unreadable directories are counted, named,
  and force a non-zero exit.
* **An empty target is `no_paths`, not `ok`.** Nothing to measure is not the same as nothing wrong.

---

## 7. What is still unverified

* **Which of the two guards fires.** The PBIR layout cannot produce a tree whose deepest directory
  breaks 247 while every file stays within 259, so the ceilings were pinned together, not separated.
* **The classic per-machine installer.** Everything here was measured against **2.157.828.0, the
  MSIX / Microsoft Store package** (`...\Power BI Desktop Store App\...`). `EnsureNotLong` is managed
  code shared by both, so a difference would be surprising — but surprising is not measured, and MSIX
  is already flagged elsewhere in our docs as an unresolved area.
* **Whether the archive/ship step has a lower budget of its own.** 282 remains one anecdote.
* **Whether Desktop fails identically with `LongPathsEnabled = 0`.** It cannot be *better* there —
  the guard is managed code that never consults the registry — but it was not measured.
* **The exact `EnsureNotLong` comparison operator.** The boundary was established behaviourally
  (259 opens, 260 refuses); the source was not read.

---

## 8. Proposed follow-up (not done in this pass)

`scripts/preflight.ps1` has **zero** long-path checks. It should report the host's `LongPathsEnabled`
**and** the longest generated path — the first as context that explains why we could write the
bundle, the second as the thing that actually decides whether anyone can open it. That file is out of
scope here; see the pull request for #235.

