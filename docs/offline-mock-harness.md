# Offline end-to-end mock harness

Runs the whole Tableau → Power BI/Fabric pipeline **with no Tableau server, no Fabric tenant and no
credentials**, in about eight seconds. Its job is to make a deploy regression-testable and to let the
migration be rehearsed before it is run against a customer's tenant.

```
python -m pytest tests/test_mock_fabric.py tests/test_mock_tableau.py tests/test_e2e_offline.py -q
```

| file | what it is |
|---|---|
| [`tests/mocks/fabric.py`](../tests/mocks/fabric.py) | in-process Fabric REST fake, substituted at `deploy_estate._request` |
| [`tests/mocks/tableau.py`](../tests/mocks/tableau.py) | Tableau REST + GraphQL Metadata fake, served over a loopback socket |
| [`tests/mocks/estate.py`](../tests/mocks/estate.py) | a synthetic estate built from this repo's real fixture workbooks, plus a stand-in for the deterministic engine |
| [`tests/test_mock_fabric.py`](../tests/test_mock_fabric.py) | 51 fidelity self-tests for the Fabric fake |
| [`tests/test_mock_tableau.py`](../tests/test_mock_tableau.py) | 21 fidelity self-tests for the Tableau fake |
| [`tests/test_e2e_offline.py`](../tests/test_e2e_offline.py) | 18 joined end-to-end tests (marked `slow`) |
| [`tests/mutation_harness.py`](../tests/mutation_harness.py) | 46 deliberate mutations that prove those tests can fail — **22** attack this offline-mock suite (tabled below); the other **24** attack the skill-plugin sync (`skillsync-` / `skillsource-`, issue #410), which shares this harness rather than growing a second scorer |

---

## The rule this harness is built on

**A mock that is kinder than the real service is worse than no mock at all**, because it manufactures
confidence. So:

* Behaviour that was **measured** against a real tenant or a real site is reproduced exactly, and the
  test that pins it says so.
* Behaviour that was **not** measured is implemented as the **strictest plausible** variant and listed
  in [ASSUMED, NOT MEASURED](#assumed-not-measured) below. Over-rejecting costs a false alarm;
  under-rejecting ships a bug to a customer.
* Where being strict would **invent** a failure the real service does not have, the mock stays
  permissive and says why (see item-display-name rules below). That is the one exception, and it is
  as important as the others: a mock that rejects `Sales v1.2` wastes a day chasing a bug that does
  not exist.
* Every ASSUMED error **code name** is a plausible invention. **Never assert on an assumed code** -
  assert on the status and the effect. The tests here do exactly that.

## No network, by construction

* The Fabric fake is **in-process**. Nothing is opened, not even a socket.
* The Tableau fake binds `127.0.0.1:0` (an OS-assigned loopback port). A loopback socket is not a
  network call - no packet leaves the machine - and it is necessary because
  `harvest_estate_assets.py` and the engine's `estate_survey.py` run in **subprocesses** that no
  monkeypatch can reach. `TableauSite.call(path)` exposes the same router with no socket at all, for
  the engine's injected-caller seam.
* `test_the_mock_never_points_at_a_real_host` asserts the generated environment contains a loopback
  URL and no `online.tableau.com`.
* `run_estate.py`'s provenance stamping is the one step that *could* contact a real site. It is safe
  here for two reasons that are both worth knowing: `resolve_env` gives the **process environment
  precedence over `.env`**, and the whole call is best-effort inside a broad `except`. In the E2E it
  reports `0 confirmed against the site` and carries on.

---

## MEASURED — reproduced, with evidence

### Fabric

| # | Behaviour | Evidence | Pinned by |
|---|---|---|---|
| 1 | `POST /items` returns `201 {"id": ...}`, or `202` + `Location`/`x-ms-operation-id` for an LRO | measured against a real tenant | `test_a_long_running_create_must_be_polled_to_completion` |
| 2 | **A repeated `Report`/`SemanticModel` displayName is ACCEPTED** - Fabric creates a duplicate | measured; this is why duplicates went undetected on a real estate | `test_a_duplicate_display_name_is_accepted_because_fabric_accepts_it` |
| 3 | `GET /items` → `{"value": [...]}` with `id`, `displayName`, `type`, `description` | measured | `test_a_listing_row_carries_id_display_name_type_and_description` |
| 4 | **`folderId` is OMITTED ENTIRELY at the workspace root** - not present-and-null | measured | `test_folder_id_is_omitted_at_the_root_not_returned_as_null` |
| 5 | Paging via `continuationToken` + `continuationUri`; 68 items came back in **one** page with no token | measured | `test_paging_emits_a_continuation_token_and_uri`, `test_list_all_in_the_deployer_follows_the_continuation_token` |
| 6 | `updateDefinition` replaces the definition and does **not** touch `description` or `folderId` | measured | `test_update_definition_leaves_description_and_folder_untouched` |
| 7 | `PATCH {"description": ...}` updates the description | measured | `test_patch_updates_the_description_and_move_replaces_the_item` |
| 8 | `POST /move {"targetFolderId": ...}`; an **empty body means the workspace root** | measured | `test_move_with_an_empty_body_means_the_workspace_root` |
| 9 | Folder names are **REJECTED, not coerced**, with `InvalidFolderDisplayName`: `& / \ : ? * " \| < # %` and **`.` anywhere** (not just trailing), plus a leading/trailing space | measured | `test_a_folder_name_with_a_rejected_character_is_refused_not_coerced`, `test_a_folder_name_with_surrounding_space_is_refused` |
| 10 | Folder names **accept** `- _ + ( )`, interior spaces and non-ASCII (`Ventes françaises` was accepted) | measured | `test_the_accepted_folder_characters_really_are_accepted` |
| 11 | Nesting deeper than **10** levels → `FolderDepthOutOfRange` | measured | `test_folder_nesting_deeper_than_ten_is_refused` |
| 12 | `GET /folders` rows carry `id`, `displayName`, `parentFolderId` | measured | `test_a_folder_listing_row_carries_id_display_name_and_parent` |
| 13 | A workspace holds at most **1000** items | measured | `test_the_thousand_item_workspace_limit_is_enforced`, `test_the_default_workspace_limit_is_the_measured_one_thousand` |
| 14 | `401` with `{"errorCode": "TokenExpired"}`, distinct from a plain invalid token | measured (a 66-item deploy outlived its token) | `test_token_expiry_answers_a_named_error_code_and_the_deployer_renews_once`, `test_an_invalid_token_is_not_reported_as_expired` |
| 15 | `429` + `Retry-After` in **both** the seconds form and the RFC 9110 HTTP-date form | measured; the date form raised `ValueError` in `float()` and killed a deploy mid-estate | `test_retry_after_is_served_in_both_measured_forms`, `test_a_throttled_create_is_retried_after_the_http_date_delay` |
| 16 | `ItemDisplayNameNotAvailableYet` / `ItemDisplayNameAlreadyInUse` are emitted as named codes | measured | `create_item` in `deploy_estate.py` branches on them; the mock emits them from `_create_item` |
| 17 | The `202` body is **empty** - the id is only reachable through the operation | measured | `test_a_long_running_create_must_be_polled_to_completion` |
| 18 | A **Failed** operation is indistinguishable from a successful one unless `/operations/{id}` is polled | measured: a probe reported both items deployed; one had failed and the workspace held one item | `test_a_failed_operation_is_indistinguishable_from_success_unless_polled` |
| 19 | PBIR schema **2.0.0** `byConnection` sets `additionalProperties: false` and permits **only** `connectionString`; the five-field 1.0.0 shape is rejected | measured against the published schema, and against the service | `test_a_byconnection_block_with_extra_properties_is_refused` |
| 20 | An empty report answers *"Content provider provided invalid package content stream"* | measured: 2 of 33 reports on a real estate | `test_a_report_with_no_pages_is_refused` |

**Injectable failure modes** (each one broke a real run): a transient `500` or a `429` on a
**listing**; a `Timeout` where our poll gives up but the operation succeeds server-side
(`stall_operations`); a token that expires partway through a long run (`expire_tokens`); and a
client-side network failure that surfaces as `HTTP 0` (`drop_network`), which the deployer treats as
*"we never reached the host"* rather than as a service verdict.

### Tableau

| Behaviour | Evidence | Pinned by |
|---|---|---|
| A lost session answers `401` whose body contains the literal `401002` | `assess_estate.Site.get` re-authenticates **only** on that substring - read the source | `test_a_lost_session_answers_the_literal_code_the_client_looks_for`, `test_the_real_assess_client_recovers_from_a_dropped_session` |
| Pagination numbers are **strings** (`"pageNumber"`, `"pageSize"`, `"totalAvailable"`) | Tableau REST API | `test_pagination_numbers_are_strings_because_tableau_emits_strings` |
| The download header is the non-standard `Content-Disposition: name="X.twbx"` with **no** `filename=` | the engine's `fetch_tds.derive_filename` carries a fallback for exactly this | `test_the_download_header_uses_tableaus_non_standard_name_form` |
| The `usage` block is absent unless `includeUsageStatistics=true` | Tableau REST API; it is the input to the tiering decision | `test_usage_statistics_are_absent_unless_requested` |
| Content download returns real `.twbx`/`.tdsx` **bytes** | the fixtures are this repo's own `tests/fixtures/*.twb` | `test_content_download_returns_a_real_packaged_workbook`, `test_a_downloaded_workbook_parses_with_the_real_parser` |
| **`estate_survey.py` blocks forever on a hidden `getpass` prompt when `TABLEAU_PAT_VALUE` is unset** | measured; it burned 13 minutes of a real session. Confirmed in `credential_resolver.resolve_secret`: the prompt fires when `allow_prompt` **and** `sys.stdin.isatty()` | `test_running_an_engine_script_without_the_pat_variable_fails_loudly` |

The GraphQL structure answer is derived from the **served workbook bytes** with `xml.etree`, on
purpose **independent of `parse_tableau`**: if the mock's expectations were produced by the parser
under test, a parser bug would be invisible because both sides would move together.

---

## ASSUMED, NOT MEASURED

Everything below is the **strictest plausible** behaviour, chosen because under-rejecting is the
dangerous direction. If the real service turns out to be more forgiving, this mock over-rejects -
which shows up as a false alarm in a test, not as a broken report in a customer tenant.

| # | Assumption | Why this choice | Risk if wrong |
|---|---|---|---|
| A1 | **Every error-code name for an assumed rejection is invented**, e.g. `FolderDisplayNameAlreadyInUse`, `InvalidConnectionInformation`, `WorkspaceItemLimitExceeded` | The *status* and the *effect* were reasoned about; the string was not | None, provided nothing asserts on the string. The tests here assert status/effect only. |
| A2 | `>` is also rejected in a folder name | `<` was measured as rejected; a matched pair is the plausible rule | Over-rejects a name Fabric might accept |
| A3 | Two sibling folders cannot share a display name | The alternative - two identical siblings - is unusable | Over-rejects |
| A4 | `parentFolderId` is omitted (not null) for a root folder | Mirrors the **measured** `folderId` omission on items | A client depending on the key would be under-tested |
| A5 | A report bound **`byPath`** is refused outright | The service has no filesystem to resolve a relative path against. Strict because this is the single most common rebinding bug | Over-rejects; and the E2E depends on it to catch a lost rebind |
| A6 | A `connectionString` with no `semanticModelId=<guid>` is refused | It names no model, so it cannot bind | Over-rejects |
| A7 | A `semanticModelId` that does not resolve to a `SemanticModel` **in this workspace** is refused | This is what makes *"bound to the wrong/deleted model"* a test failure instead of a plausible success | Over-rejects a cross-workspace binding, which this pipeline never emits |
| A8 | Unknown `type`, non-base64 `payload`, non-`InlineBase64` `payloadType`, missing `definition.pbism`/`definition.pbir` are all refused | A typo must not silently create something | Over-rejects |
| A9 | A non-existent `folderId` on create is refused | The alternative is an item filed nowhere | Over-rejects |
| A10 | An empty item display name is refused | An unnamed item is not addressable | Over-rejects |
| A11 | `PATCH` with unknown keys is refused | Silent no-ops hide bugs | Over-rejects |
| A12 | A **stalled** operation materialises its item **immediately** | The harsher, testable variant. A client that cannot see the item has no way to avoid the duplicate, so the invisible variant would only assert a known hazard | Understates how long a real duplicate window is |
| A13 | Exactly **one** page size is used for both items and folders | Simplification | Paging is exercised deliberately anyway |
| A14 | `single_row_as_object` (Tableau returning a lone row as an object, not a 1-element list) | **Off by default.** `assess_estate.paged` already handles it, but the behaviour was not observed here | If real, a client without that handling would be under-tested. Turn the switch on to test it. |
| A15 | Sign-in requires the PAT **name and secret** to match | Strict: the half-right credential is the commonest mistake in this pipeline | Over-rejects |

### Deliberately NOT made strict

* **Item display names are not held to the folder character rules.** The rules in row 9 above were
  measured on **folders**. Item names routinely contain dots (`Sales v1.2`). Importing the folder
  rules would invent a failure - the one kind of infidelity that costs a day chasing a bug that does
  not exist. Pinned by `test_an_item_display_name_is_not_held_to_the_folder_character_rules`.

---

## Not reproduced — left out on purpose

| Left out | Why |
|---|---|
| **The real deterministic engine's PBIP output.** `tests/mocks/estate.py` ships a *stand-in* that emits the output **contract** (`report.json` with a `definition_of_done`, a semantic model for every workbook, and a schema-valid `.Report` with a `byPath` reference when the workbook has convertible pages), parsing each workbook with this repo's real parser. | The engine is a ~200-file plugin that is not installed in CI, and claiming to reproduce its conversion fidelity would be fiction. What is tested is that `run_estate.py` - the real one, unmodified - accepts the bundle and that `deploy_estate.py` can deploy it. |
| **Azure AD / `az account get-access-token`.** `FabricService.token_for()` builds a real `deploy_estate.Token` whose `_mint` is bound to the fake. | Token *acquisition* is Azure's, not Fabric's. Token *expiry* mid-run is reproduced, because that is the part that broke a deploy. |
| **Workspace/capacity administration, item deletion, Git integration, dataset refresh.** | `deploy_estate.py` never calls them. Serving an endpoint nothing exercises is fiction with extra steps. |
| **Tableau flows, extract refresh schedules, custom-view content.** | Present as empty collections so a client that asks gets a well-formed answer; no behaviour is claimed. |
| **Rate-limit *policy*** (when Fabric decides to throttle). | Only the *response shape* is reproduced, and it is injected explicitly. Guessing a policy would produce timing-dependent tests. |

---

## Where the seam is, and why it is `_request` and not `call`

The task named `call(method, url, tok, body=None)`. The harness substitutes **`_request`** instead,
one layer lower. The reason is that `call` is not a transport function - it carries policy:

```python
def call(method, url, tok, body=None):
    status, headers, parsed = _request(method, url, str(tok), body)
    if status == 401 and isinstance(tok, Token) and "TokenExpired" in json.dumps(parsed):
        status, headers, parsed = _request(method, url, tok.refresh(), body)
    return status, headers, parsed
```

Patching `call` would delete the one-shot token renewal from every test that uses the mock - the
exact behaviour that a 66-item deploy needed. `FabricService.call` still exists and delegates to
`.request`, for tests that want the convenience.

Two consequences worth knowing:

* `call`'s renewal is guarded by `isinstance(tok, Token)`, so a duck-typed token **silently skips
  it**. Use `service.token_for(deploy_estate)`, which builds a genuine `Token` with `_mint` bound to
  the fake.
* `install()` also neutralises `time.sleep`, so a `Retry-After` wait and a 3-second operation poll
  cost nothing while the deployer's own back-off arithmetic still runs.

---

## Mutation testing — proof the tests can fail

A test that passes regardless of the code is worthless, so the suite was attacked with 22 deliberate
mutations (`tests/mutation_harness.py` — run it with
`python tests/mutation_harness.py`). Each patches the real deployer or the mock in a subprocess and
re-runs the suite. **All 22 were caught; there were no survivors.**

> The harness is **shared**, not offline-mock-specific: it also carries 24 mutations against the
> skill-plugin sync (`skillsync-` and `skillsource-`, routed to `tests/test_sync_installed_skills.py`
> and `tests/test_skill_plugin_source.py`), each restoring one defect issue #410's review reproduced
> by exit code. They are not tabled here — the tests they attack document them — but they run in the
> same pass, and a second scorer is deliberately never written: the verdict comes from pytest's own
> lifecycle records, and two attempts at a hand-rolled scorer had to be retracted.
>
> ⚠️ **Exact-node failure is not the bar; failing on the RIGHT assertion is.** Round-3 review found
> two of those mutations red for the wrong reason — one flipped the preliminary `--check` to drift
> before the deleting code ran, the other tripped post-copy verification — so neither ever reached
> the property it claimed to defend. Both are now scoped to apply time, and both die on the named
> foreign-survival assertion. When a mutation goes red, read WHICH assertion failed.

| mutation | caught by |
|---|---|
| `no-dedup-landing-claim` — `Landing.claim` always says "not here" | `test_re_running_the_same_deploy_creates_nothing_new` |
| `bind-report-to-wrong-model` — `rebind` uses a null GUID | `test_the_whole_chain_lands_the_expected_workspace` |
| `reports-before-models` — deploy order inverted | `test_the_whole_chain_lands_the_expected_workspace` |
| `flatten-the-folder-tree` — `project_parents` returns `{}` | `test_the_whole_chain_lands_the_expected_workspace` |
| `drop-the-provenance-stamp` — `stamp_for` returns `""` | `test_every_deployed_item_carries_a_provenance_stamp` |
| `listing-failure-means-empty` — `Landing.read` falls back to empty | `test_a_listing_that_fails_AFTER_preflight_still_refuses_to_guess` |
| `no-empty-report-skip` — `report_is_empty` always False | `test_the_whole_chain_lands_the_expected_workspace` |
| `skip-the-folder-sanitiser` — `folder_display_name` is identity | `test_the_whole_chain_lands_the_expected_workspace` |
| `mock-rejects-duplicate-names` — mock becomes kinder than Fabric | `test_a_duplicate_display_name_is_accepted_because_fabric_accepts_it` |
| `mock-omits-nothing` — root `folderId` returned as `null` | `test_a_duplicate_display_name_is_accepted_because_fabric_accepts_it` |
| `mock-accepts-bypath-reports` | `test_a_report_bound_by_path_is_refused` |
| `mock-coerces-bad-folder-names` | `test_a_folder_name_with_a_rejected_character_is_refused_not_coerced[Q1.2026]` |
| `mock-ignores-the-folder-depth-limit` | `test_folder_nesting_deeper_than_ten_is_refused` |
| `mock-never-throttles` | `test_retry_after_is_served_in_both_measured_forms[3]` |
| `mock-updatedefinition-also-moves-and-restamps` | `test_update_definition_leaves_description_and_folder_untouched` |
| `mock-forgets-the-item-limit` | `test_the_default_workspace_limit_is_the_measured_one_thousand` |
| `tableau-mock-accepts-any-pat-secret` | `test_sign_in_requires_both_halves_of_the_pat` |
| `tableau-mock-returns-empty-graphql-instead-of-errors` | `test_an_unsupported_graphql_query_is_an_error_not_an_empty_result` |
| `tableau-mock-always-sends-usage-statistics` | `test_usage_statistics_are_absent_unless_requested` |
| `tableau-mock-uses-the-standard-content-disposition` | `test_the_download_header_uses_tableaus_non_standard_name_form` |
| `tableau-mock-emits-integer-pagination` | `test_pagination_numbers_are_strings_because_tableau_emits_strings` |
| `engine-stand-in-emits-no-pages` | `test_the_whole_chain_lands_the_expected_workspace` |

Two mutations **survived the first pass**, and closing them is the most useful thing the exercise
produced:

1. **`listing-failure-means-empty` survived.** The workspace is listed **twice** - once by
   `preflight`, once by `Landing.read` - and only the second decides whether an item already exists.
   The original test failed the listing globally, so preflight refused first and the mutation was
   never reached. Fixed by adding `test_a_listing_that_fails_AFTER_preflight_still_refuses_to_guess`,
   which fails **precisely the second** listing.
2. **`mock-forgets-the-item-limit` survived.** The limit test passed `item_limit=3` explicitly, so
   nothing asserted the **default** was the measured 1000. Fixed by
   `test_the_default_workspace_limit_is_the_measured_one_thousand`, which also asserts the mock and
   the deployer agree on the number.

A third finding was in the harness itself and is worth recording: the first mutation run reported
**22/22 caught**, and every one of those was a **false positive** - the injected plugin was not on
`sys.path`, so pytest exited non-zero on an `ImportError` before running a single test. The harness
now fails loudly if the mutation did not apply. *A mutation run that catches everything on the first
try deserves suspicion, not celebration.*

### The same false-green returns whenever a mutation goes STALE

⚠️ A mutation harness is **not self-validating**. It patches symbols *by name*, so any rename or
deletion silently converts a real check into one of two useless outcomes:

| what the stale mutation does | how it scores | what it actually proves |
|---|---|---|
| patches a symbol that no longer exists as a plain attribute set | **SURVIVED** against its own anchor | nothing — the mutation was a no-op |
| references a deleted symbol and raises | **CAUGHT** against its own *control* | nothing — a `NameError`/`AttributeError`, not a detection |

Measured on `tests/mutation_reference_readiness.py` (issue #421) when the test suite was split to
match a module split: an anchor-resolution guard — resolve each anchor's file across the suites, and
**raise when a name is found in zero suites or in more than one** — flagged four entries on its first
run, and **all four were stale rather than wrong**. One patched a method since renamed; one
referenced a deleted constant; one used a module alias it never imported; and one compared against a
constant that had moved.

⚠️ **That last one exposed a real defect in the SHIPPED code, not the test.** Splitting a module had
left **two constants of the same name with different values** — `AMBIGUOUS = "__ambiguous__"` in the
old module and `AMBIGUOUS = "ambiguous"` in the new one — with the dead copy shadowing the live one's
meaning for any caller that imported the wrong module. Nothing in the type checker, the linter or the
test suite saw it; the mutation harness did, because a mutation written against one spelling stopped
biting.

**Generalisable:** when you split a module, enumerate the constants that existed in both halves
afterwards. A duplicate that agrees today is a landmine, and a duplicate that disagrees is already a
bug. Grep for the name, do not reason about whether you moved it.

---

## What the E2E asserts

`test_the_whole_chain_lands_the_expected_workspace` runs
**survey/assess → harvest → parse/convert → deploy** and then reads the resulting workspace:

1. **Exact item set** — 3 models, 2 reports. The third report is legitimately empty
   (`federated_multi_connection.twb` has no worksheets) and is **skipped**, not deployed empty. That
   is the MEASURED case `report_is_empty` exists for (2 of 33 reports on a real estate).
2. **Zero duplicates** — and the mock would have *accepted* duplicates, because Fabric does. This is
   a real check, not a tautology.
3. **Models before reports**, asserted from creation order.
4. **`byConnection` binding to the GUID the service returned for that report's own model** — not
   `byPath`, not another model's id.
5. **The Tableau project tree mirrored as folders**: `Finance/Q1-2026` and `R-D`, sanitised from
   `Finance/Q1.2026` and `R&D` because Fabric rejects `.` and `&` in a folder name — with the
   **nesting** preserved, which is what proves the tree survived rather than flattening.

The rest of the file is the "it has to catch things" half: idempotent re-run, a transient listing
failure at both layers, a throttled create with an HTTP-date `Retry-After`, an interrupted run that
resumes, a resume with the journal **deleted**, a customer's same-named item that must **not** be
overwritten, a token that expires mid-run, a dry run that creates nothing, and both item-limit gates.

## Running it

```powershell
# everything
python -m pytest tests/test_mock_fabric.py tests/test_mock_tableau.py tests/test_e2e_offline.py -q

# just the fast fidelity suites (no socket, no subprocess)
python -m pytest -q -m "not slow"

# the joined end-to-end only
python -m pytest tests/test_e2e_offline.py -q
```

The whole thing is ~8 seconds. `tests/test_e2e_offline.py` is marked `slow` (registered in
`pyproject.toml`) because it binds a loopback socket and spawns a subprocess; nothing else does.
