# `migrations/datasources/` — your published data source migrations

**This folder starts empty. It is where *your* work goes.**

One folder per Tableau **published data source** (`.tds`/`.tdsx`), producing a Power BI **semantic
model** — no report. Same shape as a workbook migration, minus the `.Report`:

```
migrations/datasources/<slug>/
├── source/<name>.tdsx            # gitignored - exported from Tableau Server/Cloud
├── migration-spec.json           # produced by scripts/parse_tableau.py (accepts .tds/.tdsx)
├── published-datasource.json     # declares WHICH published data source this satisfies
└── fabric/
    └── <Name>.SemanticModel/     # TMDL - the ONE model every consuming report binds to
```

## Why data sources are separate from `migrations/workbooks/`

A published data source is typically consumed by **many** workbooks. If its model lived inside one
consumer's `migrations/workbooks/<slug>/` folder it would look owned by whichever workbook happened to be
migrated first, and would be deleted or rebuilt with it while other reports still bind to it.
Splitting the trees by *what they produce* — semantic models here, reports in `migrations/workbooks/` — keeps
the model layer independent of every report that uses it, and mirrors the Fabric convention of
separating semantic models from reports.

## Migrate the model layer FIRST

For a whole Tableau estate, ask Tableau which data sources carry the most leverage:

```
python scripts/tableau_lineage.py --plan            # needs a Tableau PAT, see the script header
python scripts/tableau_lineage.py --plan --download migrations/datasources/_downloads
```

Then, per data source:

```
python scripts/parse_tableau.py migrations/datasources/<slug>/source/<name>.tdsx \
    -o migrations/datasources/<slug>/migration-spec.json
# ... build fabric/<Name>.SemanticModel ...
python scripts/published_datasource_registry.py --register "<site>/<name>" \
    --name "<Name>" --slug <slug>
```

Registering is what lets later workbook migrations **discover** the model and bind to it instead of
rebuilding a copy that will drift.

## Why the marker file exists

A standalone `.tds` carries an **empty** `<repository-location />`, so the dedup key
(`<site>/<name>`) cannot be recovered from the file itself — `published-datasource.json` records it.

## What is actually proven, and what isn't

Be aware of this before you rely on it at a customer. The detection and key rules were tested
against **real** public Tableau files, but one path could not be:

| Behaviour | Status | Evidence |
|---|---|---|
| Parsing a `.tds` / `.tdsx` at all | ✅ verified | 7 real public files; testing found a bug where `.tds` silently yielded **0 data sources** |
| Name precedence `derived-from` → `dbname` → `@id` | ✅ verified | a real Cloud workbook (`vimosh0812/ai-bi-assistant`) had a **stale `id='new'`** after a rename while everything else said `dandan003` — keying on `id` would split one shared source into two keys |
| An empty `<repository-location />` does **not** mean "published" | ✅ verified | Tableau's own `document-api-python` fixture `datasource_test.tds` |
| Percent-decoding the publish URL (`Sales%20Master` → `Sales Master`) | ✅ verified | regression test; the API returns the name plain, the URL encodes it |
| Full round trip: workbook flags a published DS → export its `.tds` → parse → **same** dedup key | ⚠️ **not verified** | no public `.tds` has a *populated* `repository-location`; that metadata only exists in server-downloaded files |
| The live Tableau REST / Metadata API lineage path (`tableau_lineage.py`) | ⚠️ **not verified** | needs a real Tableau Server/Cloud with a PAT |

The two ⚠️ rows are the ones to sanity-check on first contact with a real server: after registering,
run `--scan` and confirm the key derived from the **workbook** matches the key you registered from
the **data source**. If they differ, the dedup silently degrades to "not yet migrated" and you get a
duplicate model — so it is worth the one-minute check.

The committed test fixtures are **synthetic** (`contoso.com`) on purpose — third-party workbooks
aren't ours to redistribute — so they encode the rules above rather than being captured artifacts.
