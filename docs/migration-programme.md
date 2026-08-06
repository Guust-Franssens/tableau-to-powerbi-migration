# The migration programme — how we run a Tableau → Power BI engagement

> **Status:** plan of record for the *programme* layer (assessment → cutover → decommission).
> For the conversion layer see [`deterministic-tier-integration.md`](deterministic-tier-integration.md);
> for outstanding review actions see [`review-remediation-plan.md`](review-remediation-plan.md).
>
> **Evidence basis.** Two research passes (2026-08-06) over Microsoft's Power BI Migration Series,
> Tableau docs, SQLBI and practitioner sources, **plus** direct measurement against a live Tableau
> Cloud site (13 workbooks, 29 views, 8 published datasources, 12 projects, 7 groups). Where the two
> disagree, the measurement wins and is marked ⚡.

---

## 0. The boundary — what we own, what we consume

> **He owns everything *inside* the workbook. We own everything *around* it.**

| | inside the workbook | around the workbook |
|---|---|---|
| **what** | schema, calculated fields, layout, RLS-as-row-filters, the conversion itself | which workbooks, who uses them, who may see them, is it faithful, is it fast |
| **who** | the deterministic engine (`tableau-fabric-skills`) | this repo |

This is not a territorial claim, it is a **testable predicate**, and it already explains the
measured facts:

- Tableau *user filters* live in the workbook → he translates them to TMDL `role` blocks with
  `tablePermission` DAX. ✅ built (`tmdl_generate.py`).
- Tableau *object permissions* are site metadata, and land in Fabric via REST/Entra — outside his
  pipeline at **both** ends. Measured: `granteeCapabilities` 0 hits, `/permissions` 0,
  `contentPermissions` 0, `siteRole` 0 across his entire repo. → ours.
- Tableau's *rendered answer* (`/views/{id}/data`) is outside the workbook → ours, feeding his
  `fidelity_oracle` value tier, which today reads only the **Power BI** side.

**Corollary — when to file an issue instead of building:** if the output is an *input to his engine*
(e.g. embedded-vs-published detection), it is his. If the output is *a decision for a human*, it is
ours.

---

## 1. Phase model

| phase | exit gate |
|---|---|
| **0. Assess** | a signed scope: the migrate / consolidate / archive / retire list, with effort and named risks |
| **1. Foundation** | infrastructure DAG §2 complete **before any content is published** |
| **2. Pilot** | 2–3 representative workbooks at parity, and the estimate re-calibrated against actuals |
| **3. Waves** | per wave: build → fix → validate → business sign-off |
| **4. Cutover** | subscriptions, alerts and embedded URLs live; Tableau set read-only |
| **5. Decommission** | licences reclaimed, archive retained per policy |

**Programme policy, stated once and enforced:** *translate first, improve later.* Mixing migration
and redesign is the most commonly cited reason these programmes slip — the business cannot sign off
a parity test when the KPI definition changed underneath it.

---

## 2. The dependency DAG

### 2.1 Infrastructure, before any content

```
Entra groups synced → capacity sized → workspace topology → gateway cluster →
gateway data sources → shared semantic models → RLS roles tested →
deployment pipelines → reports bound to those models → apps → subscriptions/alerts/URLs
```

Two that are cheap now and near-impossible later: **capacity cannot be added retroactively without
workspace migration**, and **workspace topology must be designed against the exported Tableau
permission model** — restructuring after population is the single most disruptive rework in the
programme.

⚡ **Size capacity on refresh volume and concurrency, not user count.** Our own estate already shows
why: view-data latency ranged **0.9 s to 17.8 s** across workbooks, and the slow end correlates with
live Snowflake/Databricks connections, not with report size.

### 2.2 Content ordering — and the trap that hides it

A workbook bound to a **published datasource** must wait for that datasource. His engine states the
failure mode plainly: such a workbook *"rebuilds to an empty report."*

⚡ **The obvious detection method is wrong, in the dangerous direction.** Measured on our site:

| source | workbooks with a hard predecessor |
|---|---|
| Metadata API `upstreamDatasources` | **0 of 13** |
| REST `/workbooks/{id}/connections` → `type: sqlproxy` | **9 of 13** ✅ |

An assessment built on the Metadata API — the natural choice, one GraphQL call for the whole estate —
would report *"migrate in any order"*, produce nine empty reports, and pass a green structural
validator on every one. **`sqlproxy` is the ground-truth marker and it only exists in the REST
connections endpoint.**

⚡ **And the join key is a second trap.** The connection object looks ID-joinable:
`"datasource": {"id": "e6b65700…", "name": "Meridian Calc Gauntlet (Live Snowflake)"}` — but that id
is **not** the published datasource's site LUID (`6591c9ef…`). Joining by id silently yields nothing,
which again reads as *"no dependencies"* rather than an error. **Only the name joins**, and Tableau
permits duplicate names across projects, so it must be qualified by project.

> **Recurring hazard — name-keyed identity.** This is the third instance found in this codebase:
> the engine's estate-global `--approved-dax` map is name-keyed; reference PNGs are written by
> worksheet name; now the dependency graph. Treat *any* name-keyed join in the Tableau ecosystem as
> collision-prone until proven otherwise, and always record the LUID alongside.

### 2.3 Other sequencing items

- **Extract → Import**: refresh schedule and freshness SLA must exist before cutover.
- **Live → DirectQuery/Direct Lake**: validate under production query load, not in isolation.
- **Cross-database blend**: no equivalent — explicit re-engineering *before* the report can migrate.
- **RLS inheritance**: live-connected reports inherit model RLS and cannot override it, so the model's
  RLS must be complete before any dependent report goes live.
- **Sensitivity labels** must be configured tenant-wide before publishing, not retrofitted.

---

## 3. Assessment — the entry point (Phase 0)

Four dimensions. Output is a **decision**, not an artifact.

| dimension | signal | source | status |
|---|---|---|---|
| **Liveness** | `usage.totalViewCount`, `updatedAt`, **subscriptions**, **alerts**, favourites, revisions | REST | ❌ |
| **Complexity** | sheets, dashboards, calcs, LOD/table-calc, custom SQL, `hasUserReference` | Metadata API | ❌ |
| **Overlap** | does the data already exist in Fabric? | his `tableau-fabric-datasource-comparison` | ✅ his |
| **IAM** | project/workbook/**view** permissions, groups, `contentPermissions` | REST | ❌ |

**Liveness is the highest-value dimension, because the best outcome is migrating less.** Microsoft's
own customer case study reports roughly half of a large estate unaccessed in a year, and *"that
number could be cut in half again"* on value — practitioner sources independently put 30–50% as
unused or duplicate. Triage into **migrate / consolidate / archive / retire**.

Two measured cautions:

- ⚡ **Subscriptions and alerts beat view counts as a liveness signal** — a view count includes the
  person who opened it once by accident; a subscription is a standing choice. Both endpoints verified
  present, as are `/flows` (Tableau Prep ETL) and `/customviews`. A **custom view** is the strongest
  signal of the set: a user bothered to save a personalised filter state.
- ⚡ **Cloud REST gives lifetime counts only.** There is no windowed "last 90 days" without Admin
  Insights. Record it as *lifetime* rather than implying a window we do not have.

### 3.1 IAM is a workstream, not a field

Measured: **12 distinct capabilities in use** across our workbooks
(`Read, Write, Filter, ExportData, ExportImage, ExportXml, ViewUnderlyingData, ViewComments,
AddComment, ShareView, WebAuthoring, Connect`) against Power BI's four workspace roles.

| Tableau | Power BI | consequence |
|---|---|---|
| 12+ granular capabilities | 4 roles + Read/Reshare/Build | lossy collapse — a **decision** per item |
| `Deny`, and Deny wins | no Deny at all | must be resolved by hand |
| nested projects | flat workspaces | folders carry no permissions |
| `ViewUnderlyingData` separate from `Read` | `Build` is all-or-nothing | **"see the chart, not the numbers" is not expressible** |
| per-**view** grants | sharing is per-**report** | **forces a report split** |
| groups `domain=local` | Entra groups | **no counterpart — must be created** |

⚡ **`contentPermissions` decides the scan cost.** Ours: 11 `ManagedByOwner`, 1 `LockedToProject`.
Locked ⇒ read one object per project. ManagedByOwner ⇒ owners were free to diverge, so **every item
must be enumerated**. At a customer that is the difference between 20 calls and 20,000.

⚡ **All 7 groups here are `domain=local`.** They do not exist in Entra. Creating them is not a BI
task — it needs an identity owner and a ticket, and it is routinely the long pole. This is why
permission export belongs in **week 1**, not before go-live.

---

### 3.2 Migration strategy — a customer decision, not ours

The customer chooses **how much of the estate moves**. Two positions get argued:

- **A — Lift everything, tidy up in Power BI later.** Simple message ("it all moves"), no
  "who deleted my dashboard" politics, no triage effort.
- **B — Usage-led.** Move what carries ~99% of actual usage; leave the tail behind.

Three refinements make this decidable rather than philosophical.

### It is a dial, not a binary

A is simply the 100% end of the same dial. So do not present two options — present **the curve**, and
let the customer pick a point on it with the cost of each point visible:

| coverage of total usage | workbooks in scope | effort | left behind |
|---|---|---|---|
| 100% (A) | all | … | nothing |
| 99% | … | … | … |
| 95% | … | … | … |

BI usage is severely long-tailed, so the useful property is that the 99% point is usually a *small*
fraction of the content. **We measure that fraction from their own data instead of asserting it** —
the coverage curve is the most persuasive artifact the assessment produces, and it converts an
opinion-driven meeting into picking a threshold.

### Three destinations, not two

"Not migrated" is not one thing, and conflating them is what makes B feel risky:

| destination | meaning | cost |
|---|---|---|
| **Migrate** | rebuilt, validated, signed off | full |
| **Archive** | must stay *accessible* (retention/audit) but not *live* — static PDF/image + data extract | ~5% of migration |
| **Retire** | no use, no owner — delete after a notice period | ~0 |

Archive absorbs most of the anxiety about B: *"we still have it"* becomes true without rebuilding it.

### The asymmetry is the opposite of how it feels

A *feels* safer, but it is the **irreversible** option:

- Retiring is **reversible during parallel run** — Tableau is still there, read-only. If someone
  objects in month two, migrate it then, with a named owner and a real requirement.
- Migrating dead content is **not** free later. It permanently inflates capacity sizing and refresh
  contention, creates workspace sprawl, and appears in every future audit and access review. And
  "clean up later" is precisely the thing that never happens — the same inertia that stalls
  decommission.

Framing for the customer: **B defers a reversible decision; A commits an irreversible one.**

### Two traps that must be designed for

⚡ **Seasonality will retire your most important report.** Year-end close, the annual regulatory
filing, the board pack — near-zero view count, business-critical. Two mitigations, both required: a
**minimum 13-month observation window** so annual cycles appear at all, and the standing rule that
**usage proposes, the owner disposes** — nothing is retired on a metric alone.

⚡ **Criticality must propagate *up* the dependency graph.** A published datasource has few direct
views of its own; its importance is inherited from what binds to it. Measured on our site: the
datasources report **0 downstream** in the Metadata API while **9 workbooks bind to them via
`sqlproxy`** (§2.2). Triaging datasources on their own usage would retire the foundation of the
estate. **A node's criticality is the maximum over its dependents** — computable precisely because we
build the DAG from `sqlproxy` rather than trusting reported lineage.

### What this means for the tooling

The strategy is a **parameter, not a fork**: `--coverage-target 0.99`, with A being `1.0`. The
assessment emits the curve, the tier assignment and the DAG-propagated criticality; the coordinator
scopes waves to the chosen point. One code path, one dial.

⚠️ **Not demonstrable on the lab site yet** — created today, so every `totalViewCount` is 0. The
machinery is buildable and unit-testable now, but the curve needs a real estate with history.

---

## 4. Validation and sign-off

The practitioner standard is **KPI parity to the penny** for the same filter state, evidenced by a
per-workbook pack: comparison matrix, numeric reconciliation, **RLS test matrix**, functionality
checklist, refresh validation, named business-owner sign-off.

We are unusually well placed on the numeric half, and should be honest about the rest:

- ✅ `capture_tableau_oracle.py` gives **Tableau's own computed values**, LUID-keyed, with provenance.
- ⚠️ **It is a *view*-level oracle, not figure-level.** Measured: REST `/views` exposes **9 of
  Superstore's 27** worksheets — the other 18 are used inside dashboards and are unreachable. Per-figure
  validation needs **VizQL Data Service**, which queries the datasource with explicit fields and
  filters and therefore reaches them.
- ⚠️ **`/data` returns display-formatted text** (`"19.5%"`, `"$12"`), plus generated fields
  (`Latitude (generated)`) with no model counterpart. Normalisation is a comparison-time decision;
  capture stays raw and records format hints.
- ⚡ **Record the identity the capture ran as.** Images and data render **as the authenticated user**,
  so RLS applies. Without `captured_as`, a "fidelity mismatch" may just be two different row filters.

**What parity testing does not catch, and we should say so out loud:** a calculation that was already
wrong in Tableau stays wrong and passes; RLS never tested in Tableau passes on both sides;
subscriptions and alerts are usually outside UAT scope and surface only after cutover.

---

## 5. Gaps — honest inventory

Ranked by (likelihood × cost of late discovery), research ranking in brackets.

| # | gap | owner | notes |
|---|---|---|---|
| 1 | **IAM export + workspace topology design** [🔴1] | ours | zero coverage anywhere today; blocks Foundation |
| 2 | ~~**Dependency DAG from `sqlproxy`**~~ | **his — DELIVERED** | `estate_survey.py` (2.77.0, issue #98, filed and fixed same day). ✅ Verified live against our site: reports **9 of 13** — exact match with our REST ground truth — and resolves the *correct* published LUID rather than the decoy id. Emits `fetch_order` (datasources first, then workbooks). **We consume it; we do not rebuild it.** |
| 3 | **Liveness triage** [🔴3 partly] | ours | the "migrate less" lever; ~30–50% typically retirable |
| 4 | **Subscriptions + alerts inventory** [🔴3] | ours | endpoints verified; nothing built |
| 5 | **Capacity sizing input** [🔴2] | ours (advisory) | we already measure per-view latency — feed it, don't invent an F-SKU |
| 6 | **Embedded/hard-coded URL audit** [🔴4] | ours (advisory) | needs a SharePoint/Confluence crawl — outside Tableau's API |
| 7 | **Numeric complexity score** [🟠6] | **ours** | ⚡ his survey deliberately emits only a `complexity_understated` **boolean** — *"I deliberately did not invent a scoring model."* Correct call on his part, but it means the corrected score (folding the published datasource's calcs/LODs into its dependants) is **ours to compute** |
| 8 | **Tableau Prep flows** [🟠8] | ours | ⚡ never considered; `/sites/{id}/flows` **verified 200**. Flows are ETL — a separate dependency chain that must land before the extracts they produce |
| 8b | **Custom views** | ours | ⚡ `/sites/{id}/customviews` **verified 200**. Per-user saved filter states — both a migration item (≈ PBI personal bookmarks) and the *strongest* liveness signal available: making one is unambiguous deliberate use |
| 9 | **RLS test matrix per role** [🟠7] | ours | he *builds* roles; nobody *tests* them per-role |
| 10 | **Refresh schedule staggering** [🟡9] | ours | trivially avoidable stampede on go-live morning |
| 11 | **Figure-level oracle via VDS** | ours | needed for per-visual parity; 100 calls/hr/Creator limit |
| 12 | **Evidence pack + sign-off artifact** | ours | we produce findings, not a signable document |
| 13 | **Decommission support** | ours (light) | read-only period, archive, licence reclamation |

### 5.1 The reconciliation seam — both halves are now within reach

His `translation_reconcile` has always had two injection points and **neither has ever had a real
implementation attached** (his words on #96: *"no real executor has ever been attached… please do
prototype against it"*).

| socket | what it must do | who can fill it |
|---|---|---|
| `fabric_oracle(dax_query) -> result` | **execute DAX against the built Power BI model** | **us** — `probe_desktop_query.discover_port` + ADOMD already does exactly this; he ships `subprocess_oracle` / `persistent_oracle` adapters explicitly *"for a Desktop/XMLA executor whose startup is expensive"* |
| `tableau_oracle` / `tableau_values=` | **Tableau's own ground-truth number** | **us** — `capture_tableau_oracle.py`, built today |

⚠️ **Correction to an earlier assumption of ours:** the Tableau capture is **not** a `fabric_oracle`.
That socket executes DAX; ours supplies the ground-truth side. Wiring it to the wrong parameter would
have compared Tableau against Tableau.

Filling both closes a reconciliation loop that has never once run end-to-end.


**Not gaps — already covered:** conversion, RLS *translation*, structural validation, fidelity
structural/image tiers, estate fan-out, Fabric-overlap comparison.

---

## 6. Build order

1. **`assess_estate.py`** — liveness + IAM + complexity scoring in one read-only pass, **consuming
   his `estate_survey.py --json` as the dependency input** rather than recomputing it. This is the
   **entry point** and it unblocks gaps 1, 3, 4, 7.
2. **Desktop DAX executor adapter** — fills his `fabric_oracle(dax_query)` socket from our existing
   `probe_desktop_query` ADOMD path, and pairs with the Tableau oracle to close the reconciliation
   loop (§5.1).
3. **Provenance stamp** into the oracle manifest (`captured_as`, usage, lineage, blast radius).
4. **VDS figure-level oracle** (gap 11) — the last piece of true per-visual parity.
5. **Evidence pack generator** (gap 12) — turns our findings into something a business owner signs.

Everything in 1 and 3 is read-only REST/GraphQL against Tableau, needs no Fabric capacity, and
touches none of the engine's internals — so it can be built and shipped independently of the
conversion tier.

---

## 7. The scan — exactly what to call, in what order

Every endpoint below was **verified live** against Tableau Cloud (API 3.29) on 2026-08-06. Ordered by
cost, because the cheap passes decide whether the expensive ones are worth running at all.

### Pass 1 — inventory (cheap, 1 call each, whole site)

| what | call | gives |
|---|---|---|
| workbooks | `GET /sites/{s}/workbooks` *(paginate)* | name, project, owner, size, createdAt, **updatedAt**, tags |
| views + usage | `GET /sites/{s}/views?includeUsageStatistics=true` | `usage.totalViewCount`, contentUrl, viewUrlName |
| datasources | `GET /sites/{s}/datasources` | the publish targets |
| projects | `GET /sites/{s}/projects` | **`contentPermissions`** — decides Pass 3's cost |
| groups | `GET /sites/{s}/groups` | + `/groups/{id}/users` for membership |
| flows | `GET /sites/{s}/flows` | Tableau Prep ETL — its own dependency chain |

**Always paginate.** A site survey that stops at page 1 under-reports the estate.

### Pass 2 — structure (1 GraphQL call for the whole estate)

```graphql
{ workbooks { name projectName
    sheets { name } dashboards { name }
    embeddedDatasources { name hasUserReference
      fields { name __typename
        ... on ColumnField     { role dataType }
        ... on CalculatedField { role dataType formula } } } }
  publishedDatasources { name isCertified hasExtracts extractLastRefreshTime
    downstreamWorkbooks { name } upstreamTables { fullName } } }
```

Gives sheet/dashboard counts, **calculated-field formulas** (so LOD and table-calc detection is free),
`role` = DIMENSION/MEASURE, `isCertified`, **extract freshness**, and fully-qualified `upstreamTables`
— the join key for "does this already exist in Fabric".

⚠️ **Inline fragments are required.** `fields { role }` fails with `FieldUndefined` — `role` lives on
the concrete types, not the `Field` interface.

⚠️ **Do NOT use this for dependencies.** Measured: `upstreamDatasources` reported **0 of 13** where
REST showed **9**. Use Pass 3.

### Pass 3 — per-item (N calls; this is where cost lives)

| what | call | note |
|---|---|---|
| **dependencies** | `GET /workbooks/{id}/connections` → `type == "sqlproxy"` | ground truth. `datasource.name` is the join key — **the id is not the site LUID** |
| permissions | `GET /{workbooks\|datasources\|views}/{id}/permissions` | ⚡ **skip entirely when the project is `LockedToProject`** — read the project once instead. Ours: 11 `ManagedByOwner` + 1 locked |
| liveness extras | `/subscriptions`, `/dataAlerts`, `/customviews`, `/workbooks/{id}/revisions` | a **custom view** is the strongest deliberate-use signal |

Or just run the engine's `estate_survey.py --json` for the dependency half — verified to reproduce our
REST ground truth exactly (9/13) and it emits a ready `fetch_order`.

### Pass 4 — exports (expensive, session-fragile, ONLY for scoped-in content)

`/views/{id}/data` (numbers) and `/views/{id}/image?resolution=high` (2× linear / 4× pixels). Measured
**~6 s per view**, 262 s for 29 views. Run **after** triage, only on what is in scope.

⚠️ Sessions drop intermittently with `401002` (observed after 1–58 exports). Re-authenticate and
**record it**; a silently truncated capture is indistinguishable from a clean one.

⚠️ Only **published views** are reachable — 9 of Superstore's 27 worksheets. The rest live inside
dashboards; per-figure numbers need VizQL Data Service.

---

## 8. Where it lands — the store

**Raw JSON as evidence, SQLite as the query layer, both git-ignored.**

```
_assessment/<site>/<yyyy-mm-dd>/
  raw/            # verbatim API responses - the audit trail, never edited
  estate.db       # SQLite: the queryable model
  oracle/         # per-view CSV + PNG (only for scoped-in content)
  report.md       # what the customer sees
```

Raw responses are kept because **an assessment is evidence for a commercial decision** — "retire these
40 dashboards" must be defensible months later, and an API response is not reproducible once the
estate changes.

SQLite because every question that matters is a **join or an aggregate**, not a lookup:

| table | key columns |
|---|---|
| `workbook` | luid, name, project, owner, size, created_at, updated_at, sheets, dashboards, calcs, lods, table_calcs |
| `view` | luid, workbook_luid, name, content_url, total_view_count, updated_at |
| `datasource` | luid, name, project, is_certified, has_extracts, extract_last_refresh |
| `dependency` | workbook_luid → datasource_name *(name-joined, by necessity)* |
| `upstream_table` | datasource_luid, full_name *(the Fabric-overlap key)* |
| `permission` | object_type, object_luid, grantee_type, grantee_luid, capability, mode |
| `group_member` | group_luid, user_luid |
| `signal` | object_luid, kind (subscription/alert/custom_view/favourite), count |

The coverage curve — the artifact that makes the strategy decision decidable — is then one query:

```sql
SELECT name, views, SUM(views) OVER (ORDER BY views DESC) * 1.0
       / SUM(views) OVER () AS cumulative_share
FROM   (SELECT w.name, SUM(v.total_view_count) AS views
        FROM workbook w JOIN view v ON v.workbook_luid = w.luid GROUP BY w.name)
ORDER  BY views DESC;
```

Read the workbook count where `cumulative_share` first exceeds the target. That is the answer to
*"how much do we migrate?"*, computed from their data rather than asserted.

**Criticality must then propagate up the dependency graph** — a datasource inherits the **max** of its
dependants, or triage retires the foundation of the estate.

---

## 9. The Fabric target — how it deploys

### Mapping

| Tableau | Fabric / Power BI | note |
|---|---|---|
| published datasource | **semantic model** | migrate first; reports rebind to it |
| workbook | **report** | thin report over the shared model |
| project | **workspace** | ⚠️ projects nest, workspaces do not — a tree becomes N workspaces or folders, and **folders carry no permissions** |
| project permissions | workspace roles | 12 capabilities → 4 roles: a **decision** per item |
| local group | **Entra group** | must be created — identity owner, long lead time |
| user filter / entitlement join | **RLS role + DAX** | the engine already translates these |
| extract | Import + refresh schedule | replicate the freshness SLA |
| live connection | DirectQuery / Direct Lake | validate under production load |

### Order (the infrastructure DAG of §2.1, made concrete)

```
1  Entra groups created + synced         <- longest lead time, start week 1
2  capacity sized on REFRESH + concurrency, not headcount
3  workspaces provisioned from the permission export
4  gateway + data source credentials tested
5  semantic models published (one per published datasource)
6  RLS roles tested AS A REAL USER, not a service principal
7  deployment pipelines dev -> test -> prod
8  reports published, bound to (5)
9  apps + audiences
10 subscriptions, alerts, embedded URLs re-pointed
```

Steps 1–4 have **nothing to do with report conversion** and are the usual cause of a slipped go-live.

### Mechanics

- **Local-first**: the engine emits a PBIP (TMDL + PBIR) to disk. Validate offline, then deploy — never
  author in the portal when a local folder exists.
- **Deploy** with `fab import` (or git-sync on a git-connected workspace), one item per artifact.
- **Rebind** each report to its shared semantic model after the model lands.
- **Parameterise data sources** before bulk deployment, or dev→test→prod promotion breaks connection
  strings.
- **Stagger refresh schedules across waves** — identical windows cause a first-morning capacity
  stampede.
- **Sensitivity labels** must be configured tenant-wide *before* publishing, not retrofitted.

### Naming

`<domain>-<layer>` for workspaces (e.g. `sales-prod`), semantic model named for the **Tableau published
datasource** it replaces, report named for the **workbook**. Keeping the source names is what makes the
parity conversation possible — a business owner must be able to find their dashboard.

---

## 10. Keeping the engine current
The engine moves fast — 2.60.0 → 2.78.0 in two days, with issues filed and fixed same-day. Two
mechanics matter:

- **`preflight.ps1 -CheckUpstream`** compares the local engine clone against `origin/HEAD` and tells
  you to pull, because *"the deterministic engine went 2.60.0 → 2.72.0 unnoticed, and issues were
  nearly filed against a build that had already fixed them."* Re-verify any open issue against the
  new build before citing it.
- **A stale *installed plugin* is invisible.** `copilot plugin update` fails while a session holds the
  directory, but that lock only blocks **renaming** the plugin directory — files inside stay writable.
  So a content refresh is a straight in-place overwrite from the clone, no restart required. Verified
  2026-08-06: 273/273 files hash-identical afterwards, and the newly-shipped `estate_survey.py` ran
  correctly against a live site in the same session. Clear `__pycache__` afterwards.

