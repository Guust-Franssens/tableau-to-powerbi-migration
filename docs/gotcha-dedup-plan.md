# Compact migration cores — Refs #732 / #733

## Approved supersession and scope

✅ Owner approval `approve_compact_gotcha_core=true` and independent same-reviewer conformance
**APPROVE — amended artifact conforms** (`b339f945`, 2026-10-18) authorize the amended **N=10**
implementation, not runtime acceptance or publication. The
[approved issue plan](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/732#issuecomment-6248826479)
records the invariant, controls, ownership and non-goals. Base:
`d3ecae21d659af883da6be02ec2a9a1dfe1f3cc5`, tree `76366fa269f08a328c97744dcfd8dff4042d85dd`.

This full replacement supersedes [PR #759's D1 plan](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/pull/759),
not its historical evidence. The
[original census at the immutable base](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/blob/d3ecae21d659af883da6be02ec2a9a1dfe1f3cc5/docs/gotcha-dedup-plan.md)
retains its dated measurements, corrections and unresolved observations. D1's small pointer pass
was not authority for this stronger rewrite; neither its old status labels nor its estimates are
current implementation results.

The sole integration/pin owner works sequentially: **A5** (two skills, this index, build metadata,
`test_skills.py`) then **B5** (R/S personas' necessary citations/generated indexes, reference and
openability tests, sanctioned pins). #733's authored persona diet is separate; five shared paths
remain under serial integration ownership. No AI/refresh/live-source implementation, shared
convention, translation guide, cookbook, gate, fixture or workflow change is authorized here.

## Current owners and retained contract

This is a **host-repository-only, optional symptom index**, not mandatory default reading or a
portable plugin procedure. Missing history leaves current attribution `CANNOT_ESTABLISH`, not an
observed defect. Missing required host tooling blocks only its affected operation; never improvise.

| Current responsibility | Owner and reachability |
|---|---|
| Generic report procedures | Installed `powerbi-report-cli`: existing report → `authoring`; missing page → `design` then `authoring`; selected `references/<mode>.md` and its needed topics. Missing/retired skill, wrong mode or unavailable topic blocks that operation. |
| Generic model procedures | Installed `semantic-model-authoring` existing-model workflow and `references/tmdl-guidelines.md`; report-side field parameters use the report CLI's authoring topic. Keep the inline constructor guard until both resolve in a fresh semantic role. |
| Tableau/source and shipped report contract | Host `.github\skills\powerbi-report-gotchas\SKILL.md` §§3/8/10: exact IDs/captions, pivots, nesting/grain/scale, effective first-open defaults, post-aggregation LAST and independent source comparison. |
| Tableau/model and queue contract | Host `.github\skills\powerbi-semantic-model-gotchas\SKILL.md` §§1–4/8 plus `docs\tableau-dax-translation-guide.md`: roles/targets/parameters, LOD/table-calculation context, original requests and distinct denominators; selfcheck is an engine claim. |
| Custody/binding/declarations | Host `AGENTS.md`, `scripts\promote_unit.py`, `scripts\declare_generated_edit.py` and their tests. Repair only working/shipped bytes; shared promotion rewrites `byPath` and checks its target. Bundle edits require replay/declaration and `--tamper` exit 0; packages retain their separate contract. |
| Capture, reload, rows and persistence | Host `.github\skills\pbip-model-refresh\SKILL.md`, especially “Order matters,” “How persistence actually works” and “Bind to the right instance.” Held out of the playbook; resolve the host path or block the dependent handoff. Every commissioned import/live table needs current-model canaries and commissioned durability/cold-reopen proof. |
| Credentials and shipped-artifact live proof | Host `.github\skills\live-source-reachability\SKILL.md` alone; not shipped by the playbook. Native verdicts/retry rules prevail. An unchanged credential retry is not remediation. |
| AI policy | Shipped, locally owned `powerbi-ai-readiness` §0 is the sole stamping/batch/strict-seal policy. Generic upstream AI guidance does not replace this bounded exception. |

Missing, stale, inaccessible or ambiguous evidence → `CANNOT_ESTABLISH`/BLOCKED; observed mismatch →
REFUSE/finding. Neither becomes PASS. Credential owners keep their native vocabulary. Structural
validation, one arbitrary row, `PERSISTED`, mtime or a screenshot cannot establish fidelity, full
table coverage or durable cold reopen. Returned connection names still need independent current
contents/definition checks against the intended working model.

Pending [#730's measurement contract](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/730#issuecomment-6233480308),
the supported host capture/reload/identity and custody/persist/export rules outrank generic upstream
assumptions. ❌ No winner, universal MCP/reload ban or workaround retirement is established here.
Published schema definitions do not prove this host can reach their endpoints; unreachable/skipped
checks stay unverified. No “2.9 newest,” “ShapeMap unusable” or “PBIP has no data cache” claim survives
in the compact cores; no schema bump or “all old defects fixed” claim replaces them.

## Load diagnostics only for a matching symptom

Optional immutable history is evidence, **not a current procedure or required context load**.

| Symptom | Existing owner / bounded historical lead |
|---|---|
| Stale definitions, save conflict, empty cold reopen | Refresh owner's sections above; [#641](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/641) distinguishes observation from durability. [Pinned R §7](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/blob/d3ecae21d659af883da6be02ec2a9a1dfe1f3cc5/.github/skills/powerbi-report-gotchas/SKILL.md#L611-L676) and [S §7.2](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/blob/d3ecae21d659af883da6be02ec2a9a1dfe1f3cc5/.github/skills/powerbi-semantic-model-gotchas/SKILL.md#L1057-L1087): explicit stale UI Save measured 2026-08-21; Desktop version unrecorded, idle autosave not reproduced. Bridge 0.1.2 observations are not bridge 1.0.0 results; only measured #730 evidence can retire safeguards. |
| Wrong-model query / concurrent MCP target changes | [Pinned S lines 934–975](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/blob/d3ecae21d659af883da6be02ec2a9a1dfe1f3cc5/.github/skills/powerbi-semantic-model-gotchas/SKILL.md#L934-L975), measured 2026-08-08 / 2026-08-18, MCP version unrecorded. Returned connection plus independent contents/current-definition identity remains the rule, not a universal MCP prohibition. |
| Legacy `.xls`, locale/navigation, partial render | Load only matching original [S §§6–7](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/blob/d3ecae21d659af883da6be02ec2a9a1dfe1f3cc5/.github/skills/powerbi-semantic-model-gotchas/SKILL.md#L850-L1139) or pinned R §7 and the relevant existing repro/test. The subsection's dated environment is its ceiling; unrecorded versions stay unknown. Reproduce currently before attributing a present defect. |
| Automatic-mark or translated-calculation disagreement | Existing `tests\test_issue_424_chart_type_pin.py`, `fixtures\upstream-repros\README.md`, upstream #184 and local #425 are symptom leads, not current-emitter/runtime promises. Source shelves/images and authorized numeric results govern. |

## Release and acceptance record

✅ Public playbook metadata checked 2026-10-18: **0.3.0**. Stage **0.3.2** locally; the frozen,
unpublished 0.3.1 build remains evidence, not a prerequisite to publish/install. `SHIPPED_SKILLS`
is unchanged: AI readiness and the two compact migration cores. Refresh/live-source machinery,
translation/declaration/gate owners and this index require the host checkout, not just the plugin.

Approved LF-normalized replacement accounting (Unicode characters including final LF; physical
lines; conservative CRLF adds one character per LF):

| Default skill load | Pinned before LF chars / lines | Compact LF chars / lines | Conservative CRLF before → after |
|---|---:|---:|---:|
| Report | 71,124 / 872 | 5,722 / 48 | 71,996 → 5,770 |
| Semantic | 101,002 / 1,356 | 6,032 / 50 | 102,358 → 6,082 |
| Pair | **172,126 / 2,228** | **11,754 / 98** | **174,354 → 11,852** |

Source default-core reduction: **160,372 LF characters / 93.2%**, not tokens, cost, latency or
fidelity improvement. Tests/pointers/generated indexes have their own before/after accounting in
the implementation PR; do not credit deletions without their growth. Whole-role net context and
actual runtime savings remain unestablished: count mandatory upstream modes/topics/local owners
once per role from post-publication transcripts, not optional history as compulsory corpus.

❌ Publication/install/profile-sync consent is absent. Build/check/copy only into ignored output;
never update installed plugins or profiles while sessions are open. A later separately authorized
0.3.2 publication and closed-session sync must verify exact bytes and genuinely fresh-role owner
resolution before dependent #733 deletions. Static tests/builds do not prove content was consumed.
Live Desktop/data/cache durability, fresh-role resolution, #730 retirement and two-lane blind
acceptance remain unverified. Next: exact-head independent issue+diff-only review and CI; no
ready/merge/publication verdict is implied.
