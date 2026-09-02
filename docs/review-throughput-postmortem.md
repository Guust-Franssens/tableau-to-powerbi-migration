# Review-throughput post-mortem, 2026-09-01/02

## Scope and counting rules

Snapshot: **2026-09-02 09:23:11Z**. Population: PRs
**#399, #405, #407, #408, #414, #422, #427, #428, #430, #431, #433, #437**,
plus master commits `c34adb5 b39aaae dfebc1d 58f83aa f3374d8 00b516f 1f85200
f23617b cdb1865`.

Evidence came from PR bodies/comments, full branch commit messages, PR file/commit metadata, issue
timestamps, and those master commits. I froze the Phase-1/2 conclusions before reading the
`AGENTS.md` review-spiral section.

Counting:

- A **round** is a numbered blind-review cycle evidenced in branch history/comments; an explicitly
  clean final cycle counts. Post-merge rounds are marked separately.
- A **finding** is one top-level reviewer-attributed actionable item. Multiple examples of one item
  count once; explicitly self-found follow-ons do not.
- **P** changes product/runtime/consumer-contract behaviour. **Q** changes proof machinery:
  tests, fixtures, mutation/anchor/census/static gates, lint, or CI.
- `S/T/D` means newly created files under scripts / tests-or-fixtures / docs. Final PR diff is used,
  not gross churn across commits.
- `repeat` counts R2+ findings whose coded defect class also occurred in round N-1 of another PR.
  The 13 classes are: false-clean, wrong-object, boundary-hole, mutable-input, security-sink,
  semantic-classifier, delivery-error, claim-drift, integration-currency, fixture-vacuity,
  mutation-attribution, proof-coverage, and lint-CI.
- For open PRs, `end` is the snapshot. Issue-to-end therefore is not issue-to-merge.

## Measured population

| PR | state / rounds | issue→end; PR→end | commits | final diff / files | new S/T/D | findings P/Q | repeat |
|---|---:|---:|---:|---:|---:|---:|---:|
| #399 | M / 10 | 10d23h39; 52h56 | 14 | +4214/-26 / 11 | 4/1/1 | ≥23/0 | 15/23 |
| #405 | M / 9 | 1d20h34; 43h20 | 22 | +5702/-201 / 17 | 3/3/0 | 21/6 | 13/21 |
| #407 | M / 7* | 7d19h52; 20h46 | 8 | +2035/-25 / 6 | 0/2/0 | ≥1/9 | 7/10 |
| #408 | M / 7 build + 2 descope | 14d15h58; 46h42 | 19 | +45/-3 / 1 | 0/0/0 | 22/3 | 7/20 |
| #414 | O / 6 | 2d10h12; 40h10 | 26 | +4228/-208 / 11 | 0/1/0 | 14/4 | 10/14 |
| #422 | M / 10 + clean | 2d07h21; 8h31 | 17 | +3214/-4 / 10 | 2/3/0 | 11/5 | 7/13 |
| #427 | M / 3 + investigation + clean | 12h22; 10h56 | 6 | +1348/-0 / 14 | 0/10/0 | 0/6 | 1/3 |
| #428 | M / 5 + 1 post | 15h11; 11h42 | 11 | +5551/-4 / 14 | 4/5/1 | 19/3† | 14/14 |
| #430 | O / 1 | 14h11; 13h29 | 5 | +525/-0 / 4 | 0/3/0 | 1/1 | 0/0 |
| #431 | O / 5 | 16h27; 13h24 | 13 | +5453/-507 / 17 | 2/4/0 | 9/3 | 4/7 |
| #433 | M / 6 + 1 post | 9h41; 9h38 | 17 | +4942/-232 / 10 | 1/5/0 | 18/5† | 17/19 |
| #437 | draft / 0 | —; 3h37 | 1 | +0/-0 / 0 | 0/0/0 | 0/0 | — |

`*` #407's branch history reaches round 7; its squash message says “four rounds.” I used the branch
evidence and report the contradiction. `†` includes the post-merge round; the diff column remains the
merged PR diff. #428's R6 landed as `cdb1865`; #433's R7 was still only on its branch at the snapshot.

Under the branch-label rule above, the eight merged PRs had **62 pre-merge rounds: mean 7.75,
median 8; all eight needed at least five**.
Median issue→merge was **49h58**, but median PR-open→merge was **16h14**: old backlog age materially
inflates “issue to merge.”

## What each round found

Notation is `P/Q`; these are compact descriptions of the counted top-level findings.

- **#399:** R1 list unavailable; R2 7/0 tamper-vs-harvest and unassessable inputs; R3 4/0 delegation
  seam; R4 2/0 query parsing/report truth; R5 1/0 re-derived status; R6 1/0 two snapshots; R7 3/0
  ABA/stale tallies/unscoped race; R8 2/0 classifier re-read/stale rows; R9 2/0 unmodelled reads;
  R10 1/0 missing caveat.
- **#405:** R1 5/1 version/format/indeterminate assumptions, token leak, scorer; R2 4/0 API/format/render
  legs; R3 5/1 redaction/status/rank/copy/count plus stale adapter; R4 1/0 transform before redact;
  R5 1/1 artifact sink and blind taint gate; R6 2/1 path/key leaks and gate bypass; R7 1/0 reason
  phrase; R8 1/1 split leak and waiver overlap; R9 1/1 duplicate HTTP client and known-gap proof.
- **#407:** R1 list unavailable; R2 1/1 oracle overclaim and vacuous prose test; R3 0/1 synonym
  denylist; R4 0/1 rendering recogniser; R5 0/2 marker boundary/whitespace; R6 0/3 raw pin,
  blank-line and invalid-control gaps; R7 0/1 `splitlines()` self-oracle.
- **#408:** R1 5/0 proxy classifiers; R2 5/1 parser/projection plus mutation vacuity; R3 2/0 first
  match; R4 clean; R5 3/0 equivalent DAX disagreed; R6 4/0 grammar plus stale prose; R7 2/2
  false-clean, void proof, false premise, red gates; descope R1 1/0 inaccurate surviving record;
  descope R2 clean.
- **#414:** R1 3/1 mutable ref/destination/default plus inert preflight test; R2 3/0 ownership/default/
  retirement; R3 3/1 unsafe marker/stale ownership/commit plus wrong-reason mutations; R4 2/1 aliases,
  missing marker, harness collision; R5 2/0 disagreeing consumers/traceback; R6 1/1 sibling junction
  and stale mutation.
- **#422:** R1 3/0 partial answer/leak/incomplete scope; R2 3/0 exceptions/fail-open/hidden sheets;
  R3 2/0 census and label; R4 1/0 envelope; R5 1/0 duplicated transport; R6 0/1 differential proof;
  R7 1/0 cp1252 delivery; R8 0/1 console scanner; R9 0/1 failed-node set; R10 0/2 unreachable
  verdict/dual declarations; R11 clean.
- **#427:** R1 0/3 non-discriminating fixture matrix, weak anchor, pylint; R2 0/1 fixture premise
  evaporated; R3 0/2 token-not-semantics and CI skips; R4 bounded flake investigation found no cause;
  R5 clean.
- **#428:** R1 7/1 eight fail-open evidence paths; R2 5/1 integrity/ceiling/provenance/pages/names plus
  anchor map; R3 2/0 identity and exclusivity; R4 2/1 unenforced “cannot,” physical identity, lint
  quarantine; R5 2/0 path fallback and serialised ambiguous values; post-merge R6 1/0 exact and
  normalised tables collapsed.
- **#430:** R1 1/1 geometry overclaim and non-zero pylint.
- **#431:** R1 4/1 erased intent, salvage/retry bounds, tie claim, anchor; R2 2/1 wall-clock bound,
  batch identity, moving base; R3 1/1 request lifecycle and substring keyword proof; R4 1/0
  proxy/TLS phase; R5 1/0 address iteration.
- **#433:** R1 3/1 foreign warning/partial spec/placeholder plus uncommitted campaign; R2 3/0 diagnosis
  treated as approval, kind collision, rename ambiguity; R3 3/1 raw joins/path/false measurement plus
  shared-file pin; R4 3/1 signatures/source/denominator plus diff instrument; R5 2/0 global identity;
  R6 3/0 oracle/exemption/workbook binding; post-merge R7 1/2 discarded `view_type`, vacuous census,
  stale base.
- **#437:** no implementation or review.

## Quantitative causal account

1. **The reviews were mostly finding real product defects.** The recoverable count is **184:
   139 P / 45 Q**. Even from R3 onward it is **75 P / 31 Q**. The product did not stop being the
   subject: **71% of late findings were still product findings**, commonly security, data loss, or a
   false clean verdict.
2. **Proof work amplified the cost.** The PRs added **37,257 lines**: **23,935 tests/fixtures
   (64.2%)**, 11,833 scripts, 1,475 docs, 14 other. They created **55 files: 16 scripts, 37
   tests/fixtures, 2 docs**. Q share rises from **18% in R1-2** to **29% in R3+** and **33% in R5+**.
3. **The repeatable mechanism was an unbounded claim fixed one site at a time.** Of 144 findings in
   R2+, **95 (66%)** had the same class in round N-1 of another PR. Recurrence was 32/32 for
   wrong-object evidence, 19/22 for false-clean, 13/17 for proof coverage, and 7/9 for boundary-hole.
   Examples: #405 moved through credential sinks/transformations; #399 through filesystem reads;
   #431 through connection phases; #433 through identity-flattening joins; #407 through successive
   prose/pin recognisers.
4. **Size was not the cause.** Among merged PRs, additions vs pre-merge rounds were Pearson
   **-0.060** / Spearman **-0.048**; changed files were **-0.236 / -0.280**. #422 had 11 rounds at +3214/10 files,
   while larger #428 and #431 had five pre-merge/current rounds. #408's final +45/-3 also shows why
   final size can hide seven discarded build rounds.
5. **Contention amplified but did not cause the spiral.** Core shared-file count vs rounds was only
   Pearson **0.295** / Spearman **0.232**. #399 reached ten rounds with no shared core file; #407 was
   mostly isolated; #427 overlapped only on `docs/INDEX.md`. Contention did produce real extra work
   in #405/#422/#431 and the shared `object_identity.py` work in #428/#433.
6. **Operator routing was not a necessary bottleneck.** #422 completed 11 rounds in 8h31 with no
   inter-commit gap above 58 minutes. #433 completed six pre-merge rounds in 9h38 with no gap above
   1h58. Those are continuous review/fix loops, not a queue. GitHub does not expose enough data to
   split each interval between reviewer runtime, fixer runtime, and operator hand-off.

**Verdict:** the review bar was usually justified; the avoidable cost was allowing a universal
claim and its bespoke proof framework to expand inside one PR without an early surface inventory or
stop/split/descope rule. The defect classes were already known elsewhere, but 66% were rediscovered
one serial round later.

## Discriminating cases

| Hypothesis | Discriminating case | Result |
|---|---|---|
| “They were simply large” | #422 vs larger #428/#431; merged correlations above | ruled out |
| “Contended files caused it” | #399: 10 rounds, zero shared core files | not necessary; amplifier only |
| “Proof code itself causes it” | #430: proof-only, 525 additions, one round | not sufficient |
| “Only product complexity causes it” | #407: 9/10 findings were proof; #427: 6/6 | not sufficient |
| “Operator routing consumed the hours” | #422 and #433 high-cadence timelines | not necessary |
| “Reviewers were merely fussy” | 139/184 findings changed product contracts | contradicted |

## Testing the existing `AGENTS.md` analysis

| Existing claim | Verdict | Evidence |
|---|---|---|
| Baseline numbers are accurate | **Contradicted** | Under the stated branch-label rule, all eight merged PRs had ≥5 pre-merge rounds, not six. #428 was +5551/-4 with 4 new scripts and 5 new test files, not 9,929 insertions and 9 test modules. #431 has five labelled rounds, not seven. |
| Every R3+ finding fits the three questions | **Partly supported** | A generous mapping fits 105/106 late findings: 30 reachability/proof, 29 whole-surface, 46 evidence/verdict; #433's post-merge stale-base finding does not. The buckets are broad enough to be descriptive, not a proven cause. |
| Each fix became a new mechanism; product stopped being the subject | **Partly supported / contradicted** | Mechanism-on-mechanism is real in #407/#422/#427 and parts of #428/#433, but 75/106 R3+ findings were product findings. #399/#405/#408/#431 remained product-heavy. |
| **(a)** Author answers the three questions in the PR body | **Partly supported; amend** | The questions cover most late findings, but an answer after authoring is late and too abstract. Put an executable surface/oracle/stop contract in the brief before coding; copy results to the PR body. |
| **(b)** Hard artifact count (`no new script`, `≤1` test module) | **Contradicted as a causal lever** | Among merged PRs, new test-file count vs pre-merge rounds was Spearman **-0.695**; test LOC was **-0.060**. #422 had 5 new files/11 rounds; #408 0/9; #427 10/5. Use a risk/time budget and a complexity trigger, not a universal file cap. |
| **(c)** Cap at two rounds; residualise by fail-open/fail-closed | **Partly supported; replace the cap** | After R2 there were 106 findings, including 44 false-clean/wrong-object/security findings that normally block merge. Direction-based routing survives; an absolute two-round cap either ships known blockers or ceases to be a cap. Freeze scope after R2 and force simplify/delete/split/descope on a new class. |
| **(d)** Time-box proof machinery separately | **Supported** | Tests/fixtures were 64.2% of additions; Q rose to 33% by R5+; #427 spent a bounded campaign on a non-reproducing failure. Proof deserves its own budget and often its own PR. |

**Which survived intact:** only **(d)**. **(a)** survives after being moved earlier and made
executable. The direction half of **(c)** survives, but the absolute round cap does not. **(b)** does
not survive the measurements.

## Replacement brief instruction

Paste this into the implementation brief **before coding**:

> ### Review contract
> 1. **Invariant and direction:** State the exact pass/refuse/cannot-establish contract. Name the
>    fail-open consequence, fail-closed consequence, and which one blocks merge.
> 2. **Closed surface:** Enumerate every current consumer, phase, transformation, identity-loss join,
>    and mutable read that can affect the invariant (`N = ___`). Name residuals explicitly. If review
>    finds a new class or an unlisted surface after round 1, do not add another local guard: simplify,
>    delete, split, or descope the mechanism.
> 3. **Independent oracle:** For each verdict, name evidence not produced by the code under test and
>    one positive plus one negative control. A proof must fail on its intended assertion; a non-zero
>    process alone is not a kill.
> 4. **Budgets:** Product budget: ___ files / ___ hours. Proof budget: ___ files / ___ hours. Exceeding
>    either requires splitting the PR. Missing proof may remain in this PR only when it could hide a
>    fail-open, security, or data-loss defect.
> 5. **Round route:** Round 1 reviews the invariant and enumerated surface. Round 2 checks regressions
>    and whether the class is closed. After round 2, freeze scope: a new defect in the same class may
>    be fixed; a new class or new proof mechanism forces simplify/delete/split/descope. Fail-open,
>    security, and data-loss findings block; fail-closed/diagnostic/proof residuals become issues.
> 6. **Integration:** Name shared/contended files and the base SHA. Bring the branch current once
>    before final review, then prove the reviewed tree's SHA rather than relying on a three-dot diff.

Traceability: line 2 addresses the 66% cross-PR recurrence; line 3 the 45 Q findings; line 4 the
64.2% proof footprint; line 5 preserves the useful direction rule without pretending two rounds
erase 44 late blockers; line 6 addresses the measured contention/currency findings without blaming
them for the whole spiral.

## What could not be determined

- The separate blind-review reports were not stored on GitHub. #399 R1 and #407 R1 finding lists
  cannot be reconstructed; 184 findings is therefore a lower bound.
- #407's squash message says four rounds while its branch reaches R7. I could not determine which
  counting convention the squash author intended.
- Commit and comment timestamps cannot separate reviewer runtime, fixer runtime, and operator
  routing. They can rule out long routing gaps in some cases, not allocate every minute.
- Open PRs #414, #430, #431 and draft #437 had no final merge outcome at the snapshot.
- “Same kind” requires a taxonomy. The 13-class coding is stated above so the 66% can be challenged;
  it is not a natural constant.
- No PR in this population used the replacement contract prospectively, so its throughput benefit
  remains a testable hypothesis rather than a measured result.
