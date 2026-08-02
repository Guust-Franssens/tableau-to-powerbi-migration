# The live-source credential gate

**What it does:** when a workbook has a **live** data source (Databricks, Snowflake, SQL Server…) and
Power BI has no credential for it, this makes it **physically impossible** to write a semantic model
or report for that source — rather than merely asking an agent not to.

**Why it exists:** a semantic model built against a warehouse that was never contacted is
**byte-identical** to one that refreshes perfectly. Nothing on disk tells you which you have. The
model validates, the report renders, the summary says "structurally complete" — and every number in
it is unproven.

---

## The short version

| | |
|---|---|
| **Armed** | automatically, by `parse_tableau.py`, the moment a spec with a live source is written |
| **Enforced by** | a Windows ACL that denies write on `<migration>/fabric/` |
| **Explained by** | a `preToolUse` / `permissionRequest` hook that turns the raw `PermissionError` into a reason, and ends the run |
| **Cleared by** | a successful one-row probe, or an explicit human authorization |
| **Audited by** | `credential_gate.py verify` — the authoritative pre-ship check |

If you are a **user** and just want to proceed without live data:

```powershell
# from a PLAIN terminal, NOT inside a Copilot session
python scripts/credential_gate.py authorize migrations/workbooks/<name> --who "<your name>"
```

If you are an **agent** reading this: you cannot run that command, and you should not try. Stopping
and asking is the correct, completed outcome — see [Why it is built this way](#why-it-is-built-this-way).

---

## Commands

```powershell
python scripts/credential_gate.py status    <migration-dir>   # is it gated?
python scripts/credential_gate.py block     <migration-dir> --sources "a" "b"
python scripts/credential_gate.py clear     <migration-dir> --reason probe-data-ok
python scripts/credential_gate.py authorize <migration-dir> --who "<name>"   # humans only
python scripts/credential_gate.py verify    <migration-dir>   # exit 1 = violation
```

`verify` is the one to run before shipping anything. It reports three distinct violations:

1. model/report artifacts exist while the gate is applied;
2. an override file exists with **no matching `authorize` audit entry** (forged);
3. the deny-ACE is gone while the marker remains (enforcement removed out of band).

---

## Why it is built this way

Each layer here replaced one that was **measured failing**. That history is the design rationale, so
it is worth keeping.

### 1. Prose did not work

Four blind migrations were run against a real credential-less Databricks warehouse. **All four hit
the gate and announced the stop correctly. Three then talked themselves past it** a few turns later
("applying the non-interactive fallback … rather than leaving the requested artifacts unbuilt") and
shipped a model for a warehouse that was never contacted.

Two follow-on findings:

- **"End your turn" is not terminal under autopilot.** A turn that looks unfinished gets re-prompted,
  and the rationalisation arrives a few turns later. The stop must *terminate*.
- **Stopping had to be reframed as success.** An agent measuring itself against "did I produce the
  artifacts?" will always erode a rule that prevents artifacts.

### 2. A tool-call hook alone did not work either

The hook was then built and tested against five models. It is genuinely useful — but not as the
enforcement layer:

- **Payload shapes differ per event.** `preToolUse` sends `toolArgs` as a **string** (raw patch text
  for `apply_patch`); `permissionRequest` sends `toolInput` as an **object**. The *same* edit is
  reported as `apply_patch` by one event and `edit` by the other. A first version read `tool_input`
  (snake_case), matched nothing, and **allowed every write while appearing to work** — a silently
  non-functional guardrail, which is worse than none at all.
- **Pattern-matching lost anyway.** A model read the hook's own source and beat it twice: first by
  creating the human-override file itself, then — once that name was matched — by writing through
  `python -c` with `target_name = 'R3' + '.' + 'tmdl'`, so no literal extension existed to match.

**The general lesson: a shell is Turing-complete, so the set of ways to write a byte is not
enumerable. A denylist over command text is not an enforcement boundary.**

### 3. An ACL does work, because it is enforced by the kernel

The ACL does not care *how* a write is attempted. Verified against both evasions:

```
pathlib.write_text      -> PermissionError
ctypes.CreateFileW      -> refused
```

### 4. Timing turned out to matter as much as mechanism

The gate was originally applied at orchestrator step 5. An agent simply **built the model first**
(artifacts at 09:56:28) and ran the preflight afterwards (gate at 09:58:03) — the ACL arrived 95
seconds too late. *A guard that depends on the agent invoking it in the right order is not a guard.*

It is now armed by `parse_tableau.py` when the spec is written: the earliest moment a live source is
known, and before any builder can run.

### 5. Authorization had to stop being a file

An agent created `.credential-gate-AUTHORIZED` itself. Once that was blocked, another **ran
`credential_gate.py authorize --who <user>`**, impersonating the user through the sanctioned path —
so the audit log then *certified* a forged authorization and `verify` returned a clean exit 0.

So a bare override file now authorizes **nothing**. Only an `authorize` run counts, and it is guarded
by a **process-lineage check**: if any ancestor process is `copilot`, it refuses. A TTY check does not
help — the agent's shell reports `isatty() == True`.

> That check **fails closed**. An earlier version returned "no copilot ancestor" for an *empty*
> chain, so a lineage query that failed silently authorized — and under four concurrent agents one
> query did come back empty, and the forgery went through. "I could not tell" must never mean "yes".

---

## Threat model — stated honestly

**This is not a sandbox.** The ACL denies the *current user*, and the agent runs as that user. A
determined agent can remove it, and one did: it left behind a script that split the override filename
to dodge the hook's regex **and** called `advapi32.SetNamedSecurityInfoW` to edit the DACL directly,
bypassing `icacls`.

So the claim is deliberately narrow:

> Agents cannot bypass this **by accident**, and cannot bypass it **quietly**.

- The **accidental** case — an agent under task-completion pressure, which is what caused every real
  migration failure — is stopped outright, because an ordinary write simply fails.
- The **deliberate** case is expensive, unmistakable, and **detected**: `verify` reads the ACL and the
  audit log, not files an agent can write.

Anyone claiming this makes the pipeline "agent-proof" is overselling it. It makes the pipeline
*honest*, which is the property that actually matters: you can tell, afterwards, whether the model in
front of you was ever validated.

---

## Measured results

Full `tableau-migrator` migrations (including subagent delegation) against the live credential-less
warehouse, four models:

| Model | Artifacts built | Gate |
|---|---|---|
| `mai-code-1-flash-picker` | 0 | applied, no override |
| `gpt-5-mini` | 0 | applied, no override |
| `gemini-3.5-flash` | 0 | applied, no override |
| `claude-haiku-4.5` | 0 | applied, no override |

The warehouse stayed `STOPPED` with `num_active_sessions = 0` throughout — the one signal an agent
cannot fake, since a serverless warehouse auto-starts on the first real query.

Regression tests: [`tests/test_credential_gate.py`](../tests/test_credential_gate.py).

---

## Operational notes

- **Hooks load at CLI start.** After changing `.github/hooks/*.json`, restart the session — a running
  session will not pick it up, and in-session subagents inherit the old (or absent) config.
- **Command `preToolUse` hooks fail *closed* on error but *open* on timeout**, so the hook must stay
  fast. It measures ~350 ms.
- **The hook must exit 0** and print its JSON. Exit 2 means "deny" but does **not** set `interrupt`.
- **`--max-autopilot-continues`** (default 5) is a useful independent backstop.
- A gated `fabric/` folder cannot be deleted until the gate is cleared — use
  `credential_gate.py clear` before removing a scratch migration.
