# Windows path limits, and what they cost a shipped bundle

> **Headline, measured 2026-08-29/30.** Power BI Desktop refuses an over-long path using its **own
> managed guard**, so no OS or git setting can rescue it. Our **52-asset estate bundle already breaks
> two consumers today, on this machine**: Desktop refuses **183 paths**, and `git add -A` on the
> affected units stages **0 of 179 files and exits 128**. Issue
> [#235](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/235) left "is Desktop
> long-path aware?" as "the highest-value unknown currently open on the toolkit". This page is the
> answer, the experiments, and the arithmetic.

Run the check:

```
python scripts/check_path_ceiling.py <bundle> [--json report.json] [--min-root-budget 40]
```

Exit `0` clean · `1` findings · `2` usage · `3` could not evaluate.

`--json` is a **machine-readable contract**: the artifact is written **before** anything is printed,
so a console that cannot encode a path — a Windows cp1252 terminal meeting a filename with a
combining character — degrades the *display* (`errors="backslashreplace"`) instead of destroying the
*output*. Before that ordering was fixed, such a run exited 1 with **no file written at all**, and
exit 1 is also the "findings" code, so a consumer could not tell a crash from a real finding.

---

## 1. Three consumers, three different answers, one machine

This is why *"it works here"* was never evidence. All measured on the same host, with
`LongPathsEnabled = 1` and `core.longpaths` unset:

| consumer | tolerates a 287-char path? | fixable by config? | evidence |
|---|---|---|---|
| **Python 3.6+** (our generator) | **yes** | — | writes the whole estate bundle in silence |
| **Power BI Desktop** | **NO** | **no — nothing helps** | end-to-end A/B: opens at file 259 / dir 247, refused at 260 / 248 |
| **git** (`core.longpaths` unset) | **NO** | yes — `core.longpaths=true` | 0 of 179 files staged, `fatal`, exit 128 |

### Desktop's `EnsureNotLong` is real — but it is **not** what refuses 260/248

⚠️ **An earlier revision of this page got this wrong**, and the correction is worth keeping because
it is issue #235 in miniature: reading intent off an error message instead of measuring the thing.

`Microsoft.PowerBI.Packaging.dll` 2.157.828.0 was loaded and the method invoked directly:

```
PBIProjectUtils.EnsureNotLong(string path, bool isFolder)

FILE 258 ALLOWED   259 ALLOWED   260 ALLOWED   261 THREW PathTooLongException
DIR  246 ALLOWED   247 ALLOWED   248 ALLOWED   249 THREW PathTooLongException
```

**It compares with `>`, so 260 and 248 are ALLOWED.** Its message —

> The specified path, file name, or both are too long. The fully qualified file name must be less
> than **260** characters, and the directory name must be less than **248** characters.

— describes *intent*, not the comparison it performs. Two consequences:

* The end-to-end refusal observed at **file 260 / dir 248** (§5) did **not** come from this guard.
  **⚠️ Inferred, not measured:** the most consistent explanation is the .NET/Win32 `MAX_PATH` limit
  applying because `PBIDesktop.exe` carries no `longPathAware` manifest entry (§3) — which is the
  *original* framing, before `EnsureNotLong` was over-read into it.
* `EnsureNotLong` is nonetheless real and does fire: the crash report that first named it came from a
  **268**-character path, and 268 > 260.

So Desktop has **at least two independent length guards**, and the effective limit is the stricter of
them. `FILE_CEILING = 259` / `DIR_CEILING = 247` are therefore **one character tighter** than the
assembly's own inclusive limits — deliberately, because the gate follows the *observed* end-to-end
boundary rather than any single implementation's comparison.

### Why `LongPathsEnabled` still cannot rescue it

```
HKLM\SYSTEM\CurrentControlSet\Control\FileSystem  ->  LongPathsEnabled = 1   (Windows default: 0)
git config core.longpaths                         ->  unset                  (git default: false)
```

Every Desktop measurement here ran with `LongPathsEnabled = 1` and Desktop refused anyway. So the
severity is **not** *"customers on stock Windows are at risk."* It is **every consumer on every
machine, including ours.** The registry setting only ever governed whether our *generator* could
**write** these paths — Python declares `longPathAware`, so here it can. That asymmetry *is* the
defect: **we produce artifacts we cannot open.**

**One being set and the other not is exactly how this stayed invisible**, which is why
`check_path_ceiling.py` prints both — and states plainly that neither makes the artifact portable.

> ⚠️ The earlier reasoning in #234 — *"282 is an archive budget, not a filesystem limit; we run at 269
> on disk today without trouble"* — was wrong twice over. "Without trouble" was not a property of the
> artifact, and it was not even a property of this machine.

### Lengths are counted in UTF-16 code units, not code points

.NET's `String.Length` counts **UTF-16 code units**; Python's `len()` counts **code points**. Every
non-BMP character (emoji, astral planes) is **1 for Python and 2 for Desktop**, so a code-point
measurement lets an over-long path through:

```
a real path:  python len() = 259     UTF-16 units = 261     -> Desktop refuses
```

`check_path_ceiling.py` measures `len(s.encode("utf-16-le")) // 2` for both absolute lengths and
tails. A name UTF-16 cannot represent at all — a lone surrogate, which is what `os.walk` returns for
an undecodable POSIX filename under `surrogateescape` — is reported **`unknown`**, never clean.

---

## 2. The estate bundle — the realistic shape

`_runs/estate-2.339.0-20260829`, engine `2.339.0` (`canonical: true`, `source: plugin`), 45 workbooks
+ 7 datasources. Root on this machine: **90 characters**.

| metric | files only | files + directories |
|---|---:|---:|
| measured | 2,548 | 4,851 |
| longest path | **287** | 287 |
| over 260 | **80** | 153 |
| over 282 | **66** | 66 |
| over **Desktop's** ceilings (file > 259, dir > 247) | 82 | **183** |
| longest **relative** path | **196** (tail 197 incl. separator) | |

The two counts reconcile exactly: **82 files over 259 + 101 directories over 247 = 183.** A
file-only measurement reports 80; the extra 103 are the directory rule, which §4 shows is the one
that fails *silently*.

### 3 of 51 units are already over the ceiling as they sit

| unit | paths over | longest tail | root budget |
|---|---:|---:|---:|
| `Section 15 - Tableau Sales _ Customer Dashboards` | 48 | 197 | **62** |
| `Section 12 - Row Level Calculations (Functions)` | 32 | 195 | **64** |
| `Section 12 - Aggregate Calculations` | 2 | 171 | 88 |

### The dominant driver is structural, not a long-name accident

```
pbip\<NAME>\<NAME>.Report\definition\pages\<page-id>\visuals\<visual-id>\visual.json
```

**`<NAME>` is duplicated.** That is a property of the bundle layout, so it scales with *every* unit —
a 48-character unit name spends **97 characters** on those two segments before any content. Our own
directory prefix is ~12 characters, so no convention we control can rescue a long unit name. The
doubled segment is upstream (`Yarbrdab000/tableau-fabric-skills`) and is by far the largest lever.

> For contrast, the earlier 2-unit `_runs/coldrun-2.339.0-20260829/bundle` measures 114 paths,
> longest 251, root budget 106 — clean, and unrepresentative. It is a footnote, not the headline.

---

## 3. Experiment A — the embedded application manifest (static, machine-independent)

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

Against Power BI Desktop **2.157.828.0** (Store package, `.../bin/`):

| binary | role | `longPathAware` |
|---|---|---|
| `PBIDesktop.exe` | the application | **absent** (manifest id 1, 1929 bytes: `dependency` + `compatibility` only) |
| `msmdsrv.exe` | Analysis Services engine | **absent** |
| `Microsoft.Mashup.Container.NetFX45.exe` | M engine | **absent** |
| `python.exe` | **control (positive)** | present |
| `explorer.exe` | **control (negative)** | absent |

### Why the static result alone is NOT conclusive

A program can bypass `MAX_PATH` by prefixing `\\?\` itself:

| binary | manifest says | actually read a 262-char path? |
|---|---|---|
| `node.exe` | absent | **yes** — libuv prefixes internally |
| `pwsh.exe` | absent | **yes** — .NET Core prefixes in `PathInternal` |
| `cmd.exe`, `powershell.exe`, `notepad.exe` | present | yes |

That is why #235's earlier probe found *every* consumer succeeding at 460 characters. So the manifest
was treated as a **hypothesis** — and it is now the *leading* explanation for the observed 260
refusal, since §1 shows `EnsureNotLong` allows 260. §4/§5 are the end-to-end proof that something
refuses it.

---

## 4. Experiment B — git, and the silent data loss

`core.longpaths` is unset in **every** scope on this machine (system, global, local) — git's default
is `false`. A faithful copy of the three offending estate units at the same 90-character root
(179 files, exactly 80 over 260):

| `core.longpaths` | result |
|---|---|
| **unset (default)** | 74 × `warning: could not open directory ...: Filename too long`, then `error: unable to index file`, `fatal: adding files failed` — **exit 128, 0 of 179 staged** |
| **`true`** (control) | 0 warnings, **exit 0, 179 of 179 staged** |

Length is the sole variable; the config is the sole difference.

### ⚠️ The failure is not always loud — and this is the finding that justifies the directory rule

The two modes differ by **which** path is overlong:

| overlong thing | git's behaviour |
|---|---|
| a **file**, parent directory readable | `error: unable to index file` → **FATAL**, exit 128 |
| a **directory** | `warning: could not open directory` → **SKIPPED**, contents never seen |

In the second case git never sees the files inside, so nothing reports them missing. Measured on a
synthetic tree with one 265-character directory:

```
git add -A            -> exit 0      (1 of 2 files staged)
git commit            -> exit 0
git ls-tree -r HEAD   -> README.txt          <- the file inside the long directory is GONE
git status --porcelain-> exit 0, prints NOTHING
```

**A green, silent, content-missing commit.** Its only trace is a `warning:` on stderr that any script
redirecting stderr throws away. That is the strongest independent argument for gating on the
**directory** ceiling and not only on file paths — and it is a second consumer confirming the
directory rule that Desktop only *stated*.

---

## 5. Experiment C — the live Desktop A/B, pinned to the exact boundary

Three **byte-identical** copies of `examples/shipping-kpis/fabric` (27 files) differing only in root
length, each judged by **two independent instruments** — a window title is a *loading state* before
it is a verdict; the Desktop Bridge is not:

| copy | deepest file | deepest dir | `.pbip` entry | title @ t+200 s | `powerbi-desktop status --pid` |
|---|---:|---:|---:|---|---|
| control | 200 | 188 | 119 | `ShippingKPIs` | (not probed) |
| **F259** | **259** | **247** | 178 | `ShippingKPIs` | **`ready` / `connected`**, pages enumerated |
| **S260** | **260** | **248** | 179 | `Untitled - Power BI Desktop` | **`error`** — *"Host is not ready to accept operations"* |

Every entry-point `.pbip` was far below any limit, so the failure is attributable to a deep child and
nothing else. **One character decides it.**

| constant | value | meaning |
|---|---:|---|
| `FILE_CEILING` | **259** | longest legal full file path — Desktop refuses **260** |
| `DIR_CEILING` | **247** | longest legal directory path — Desktop refuses **248** |

> ⚠️ S260 crosses both boundaries at once (file 260 *and* dir 248), so that pair cannot say *which*
> guard fired. F259 opening proves both 259 and 247 are legal, which is what a gate needs.

### Reproducing it

```powershell
# 1. copy examples/shipping-kpis/fabric to a root of length (target - 99); its deepest tail is 99.
#    Python is long-path aware, so it can WRITE a copy Desktop cannot read.

# 2. launch, naming the PID
$p = Start-Process -FilePath $env:PBI_DESKTOP_PATH -ArgumentList "`"<path>\ShippingKPIs.pbip`"" -PassThru
$p.Id

# 3. wait >= 90 s, then judge with the BRIDGE, not the title
npx --yes @microsoft/powerbi-desktop-bridge-cli status --pid <literal pid> --wait-seconds 60

# 4. clean up with a LITERAL pid
Stop-Process -Id <literal pid> -Force
```

> ⚠️ Deleting a >259-char fixture needs the `\\?\` prefix, and a git repo inside it needs a chmod
> handler because git objects are read-only:
> `shutil.rmtree("\\\\?\\" + str(root), onexc=lambda f, p, e: (os.chmod(p, stat.S_IWRITE), f(p)))`

### One correction to the crash report's framing

A crash report from this experiment showed Desktop producing a **Frown dialog**, suggesting an
unattributable crash. **That is not what two independent runs produced.** In both, Desktop showed a
modal **"Issues were found"** dialog that *names the offending file* and states the rule — an
attributable error. No Frown window was present on any process afterwards, and no crash artifact
appeared under the Store app's local cache. The likely origin is a `Stop-Process -Force` on an
instance holding that modal, or telemetry raised for the handled `FilePathTooLongError`.

This **lowers that one severity claim** — Desktop does tell you which file is at fault — and lowers
nothing else. Note the contrast with git, which in the directory case tells you **nothing**.

---

## 6. Two questions, and why the gate is what it is

The absolute count ("183 over ceiling") is a fact about *where the bundle sits*. The customer will put
it somewhere else. So the check answers **both** questions:

| question | measure | status |
|---|---|---|
| *Can this be used **where it is**?* | absolute path lengths vs 259 / 247 | **blocking (exit 1)** |
| *Can this be **shipped**?* | the **minimum** remaining budget across every path, each judged against **its own** ceiling | advisory + opt-in gate |

**Why absolute stays blocking:** it is not hypothetical. At its current location the estate bundle
breaks git *today* (exit 128, or a silent partial commit) and Desktop refuses it. "Where it sits" is a
real place where real tools run — this repo's own build root is 90 characters.

⚠️ **The root budget is a minimum across both ceilings, not `259 − longest_tail`.** A short filename
makes the stricter **directory** rule decisive, and that is the ordinary PBIR shape rather than a
contrived one — a blank page directory holds only `page.json`:

```
dir  tail 40  ->  247 - 40 = 207     <- the real budget
file tail 50  ->  259 - 50 = 209     <- what a file-only calculation would report
```

At a 208-character install root that page directory becomes 248 and breaches `DIR_CEILING`, so a
file-only budget would pass a bundle that cannot open. The check reports the **binding path** on every
run so the constraint is attributable, not just a number. (On the estate bundle the binding path is a
`visuals\<id>` **directory** at tail 185 — `247 − 185 = 62` — which happens to equal `259 − 197`
because `\visual.json` is exactly 12 characters and the two ceilings also differ by 12.)

**Why the portable number is now always printed:** a bundle at a *short* root can pass the absolute
check and still be unshippable. Note the arithmetic — a clean tree always has
`root_budget ≥ root_length`, so this blind spot only opens on short-rooted builds (a CI runner,
`C:\b\`, a container), which is exactly where an automated run happens. Any bundle whose root budget
falls below **40** now prints `TIGHT ROOT BUDGET`.

That 40 is derived, not invented: `C:\Users\<name>\Documents\` is already ~28 characters before the
customer creates a single folder. `--min-root-budget N` turns it into a hard gate; it stays opt-in
because the right value depends on where the customer unpacks.

**282 is not encoded.** It remains an unreproduced archive anecdote
(`docs/deterministic-tier-integration.md:414`) — though the estate bundle now has **66 files** past it.
Pass `--ceiling 282` to test that budget.

---

## 7. What the check deliberately does NOT do

* **It never asks the OS whether a path can be opened.** The verdict is computed arithmetically from
  path strings, so neither the registry, nor git's config, nor the host OS can soften it. Linux CI
  produces the same numbers. Both settings are read and printed *as context*, and labelled as not
  making the artifact portable.
* **A path it cannot measure is `unknown`, never passing.**
* **An empty target is `no_paths`, not `ok`.**

---

## 8. What is still unverified

* **Which guard actually refuses 260/248.** `EnsureNotLong` allows both (§1, measured against the
  assembly), so the observed refusal comes from something else — most plausibly the .NET/Win32
  `MAX_PATH` limit applying because `PBIDesktop.exe` is not `longPathAware`. **That attribution is
  inferred, not measured.** The *boundary* is measured end-to-end; only its cause is not.
* **Neither ceiling is independently established.** The PBIR layout cannot produce a tree whose
  deepest directory breaks 247 while every file stays within 259, so the pair was pinned together.
  247 is conservative: `EnsureNotLong` tolerates directories up to 248, and git up to 260.
* **The classic per-machine installer.** Everything was measured against **2.157.828.0, the MSIX /
  Microsoft Store package** (`...\Power BI Desktop Store App\...`). The managed code is shared, so a
  difference would be surprising — but surprising is not measured, and MSIX is already flagged
  elsewhere in our docs as unresolved.
* **Whether Desktop fails identically with `LongPathsEnabled = 0`.** It cannot be *better* — every
  measurement here was taken with the opt-in ON and Desktop refused anyway — but it was not measured.
* **Whether the archive/ship step has a lower budget of its own.** 282 remains one anecdote.
* **The warning count in the git repro.** This run counted 74 `Filename too long` warnings; an
  independent reviewer counted 73 on the same shape. Immaterial to the verdict, but not reconciled.

---

## 9. Proposed follow-up (not done in this pass)

`scripts/preflight.ps1` has **zero** long-path checks. It should report `LongPathsEnabled`,
`git config core.longpaths`, **and** the longest generated path — the first two as context explaining
why we can write and commit the bundle, the third as the thing that decides whether anyone can open
it. That file is out of scope here; see the pull request for #235.


