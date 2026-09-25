# Bundled scripts — why each verb behaves as it does

**For exact arguments, run `python scripts/meridian.py <verb> --help`.** Every verb and flag
documents itself there, which is a few hundred tokens against several thousand for this file, and
cannot drift from the code the way a second copy would. SKILL.md carries the one-line
**question → verb** routing map.

This file holds what `--help` cannot: the measurements and the reasoning behind each verb — why a
ceiling sits where it does, what a flag costs, which shapes were tried and rejected, and which
defaults exist because the alternative produced a confident wrong answer.

Split by what you are doing, so a lookup for one verb's flags does not pull in the whole feature set:

| For | Read |
|---|---|
| query verbs — `connect`, `asof`, `connectors`, `top`, `list`, `summary`, `profile`, `compare`, `check`, `labels`, `stacks`, `api` | this file |
| `snapshot`, `trend`, `metrics`, `digest`, `alerts` | [trend-verbs.md](trend-verbs.md) |
| scheduling any of the above on a recurring cadence, per OS | [scheduling.md](scheduling.md) |
| `report` — branded PDFs, blast-radius graphs, trend charts | [reports.md](reports.md) |
| editing the helpers — transport, pacing, concurrency, TLS, caching, `top`'s ladder, `selfupdate` — or the release/packaging maintenance scripts | [internals.md](internals.md) |

**Runtime:** every verb lives in `scripts/meridian.py` — Python 3, standard library only. One process
per verb, regardless of how many API calls the verb makes. State lives in `~/.meridian/`:
credentials (`config.json`, `stacks.json`), the 60/min rate-limit budget (`.ratelimit`), the top-N
rung cache (`topcache.<fqdn>.json`), cached field names (`fields.<fqdn>.json`) and cached aggregates
(`rescache.<fqdn>.json`). A Chromium browser is needed only for PDF reports.

## Verbs

**Every verb that answers from the LDG carries `dataCurrency`**: which LDG rebuild the answer describes.
The LDG changes only when a merger run *completes*, so the stamp is the last completed, non-failed
`Lucidum Asset Merger` / `Lucidum User Merger` run (`end_time`), never the query time and never the
latest ingest. On the local stack at the time of measuring, the mergers finished at 10:12 UTC while a
source ingested at 15:31, and none of that 15:31 data was in any answer yet. Shape:
`{"class": "current", "ldgRebuiltUtc": {"asset": ..., "user": ...}, "queriedUtc": ...}`, or
`{"class": "unknown", "reason": ...}`. The rules that make it trustworthy:

- **Unknown is never current.** An unreadable stamp (scoped token, mergers not in the newest pages, a
  renamed merger) says so with a reason. It never falls back to "now".
- **A failed merge is skipped** (it left the LDG unchanged). The stamp is the previous good run, and
  `lastRebuildFailed` names the failed one. The backwards search is cached per failed run, so it
  costs up to 25 calls once, not on every question.
- **A rebuild mid-query is not an answer.** `top`, `list --all`/`--limit >100`, `summary --by` (when
  computed) and `digest` re-read the stamp afterwards. If it moved, the class is `unknown` with
  `rebuildDuringQuery: true`.
- **Mixed payloads label their past-state parts** under `dataCurrency.sections`: the 30-day averages
  in `summary --metrics`/`digest`, and a user profile's change-log-derived `stability`.
- `mergersSplit: true` means the asset and user stamps come from different pipeline runs, so an
  answer spanning both has two as-of times.

Cost: one call, overlapped with the verb's own, so roughly no wall clock; the multi-call verbs above
pay a second one afterwards. `api` is a raw passthrough and is not stamped. Treat its output as
currency-unknown unless paired with `asof`.

- `asof` — **when the LDG was last rebuilt, and nothing else.** One call (a descending-sorted page of
  the run metrics). It is the check before reusing Meridian data already in the conversation: if the
  stamp has moved since those rows were fetched, they describe an LDG that no longer exists. `message`
  is already in the display form: `Data as of 2026-09-24 10:12 UTC (latest Meridian rebuild)`.
- `connect` — **onboarding preflight; run this first every session.** Resolves
  credentials, validates in one cheap call, and returns a single `state` (`connected` /
  `not_configured` / `auth_error` / `forbidden` / `unreachable` / `http_error`) with the fqdn,
  token source + last 4, and asset/user counts when connected. Drives the §1 message templates so
  connection handling is deterministic instead of improvised. Read-only; token stays redacted.
  `--with-connectors` appends the `connectors` block — use it on the **first** connect of a
  session so the greeting can say what data the answers cover. Best-effort: if the connector
  endpoints are blocked, `connectors.unavailable` explains why and the state is still `connected`.
- `connectors` —
  **data-coverage summary: which connectors are enabled, whether they're succeeding, and how much
  data each last brought in.** Merges
  `/CMDB/v2/connector/profile` (configured + enabled + connection-test status) with
  `/CMDB/v2/system/metrics/connector?size=2000` (actual ingestion runs and record counts) into
  `summary` / `connectors[]` / `warningGroups[]` / `failures[]` / `otherSources[]`. Two GETs, both
  paced and issued concurrently. Field-by-field reading guide and the presentation rules are in
  SKILL.md §1.5.
  - **The default shape is `brief` (`"shape": "brief"`), and it is the default because this is the one
    call SKILL.md makes mandatory on every session.** Measured on a live 58-connector stack, the old
    shape was 75,288 chars (~20,900 tokens) against 21,862 (~6,100) now — a 71% cut on the payload
    that lands before the user's actual question is touched. `--full` restores per-row
    `lastIngest.notes[]` when you need to see exactly which service carried which message.
  - What brief changes, and the reasoning: 50 of those 58 connectors were ingesting *and* warning, so
    per-row notes repeated 143 note instances drawn from **67 distinct messages** — one appeared on 22
    connectors, and 8,892 of 18,023 message chars were byte-identical repeats. So notes move off the
    row into stack-wide `warningGroups[]`, and only the failing rows plus the `--max-detail` biggest
    ingesters keep `lastIngest`'s `status`/`utc`.
  - **Three invariants brief must not break**, all covered by `test_connector_brief_shape`:
    no connector row is ever dropped (`_snapshot_coverage` asserts `len(failingNames) == failing`, so
    a missing row makes a trend report coverage movement that never happened); a row whose cause lost
    the cap still carries `warned`, because a row with no warning marker has to mean the run was
    clean; and failing rows do **not** spend the `--max-detail` budget — they sort first, so charging
    them to the same counter left the delivering table with 2 rows instead of 10. That last one
    shipped in the first draft and the test was verified to fail against it.
  - **`connect --with-connectors` narrows it once more, to `"shape": "preflight"`.** Brief cut the
    payload but not the row count, and §1.5 renders ~6-8 delivering rows plus a rollup line and ~6-8
    needing attention — so a 58-connector stack handed the model 58 full rows to print about 18 of
    them, 8,377 of the block's 12,933 chars describing connectors that reach the answer only as
    "+ 40 more delivering data: …". Rows in `failures[]`, rows `failing` outright, and the
    detail-window rows the delivering table draws are all kept **in full**; the rest move to
    `delivering[]` as `connector`/`profile`/`records`/`warned`. Measured live: 22,174 → 18,574 chars.
    So it aligns the payload with what §1.5 already prints instead of changing what it prints —
    which is why §1.5 needed no rule change, and what `test_preflight_coverage` guards. `--coverage-full`
    restores every row, as does the `connectors` verb. It is applied at `cmd_connect`'s output
    boundary only: `digest`/`snapshot` call `summarize_connectors()` in-process and never see it, so
    `_snapshot_coverage`'s `len(failingNames) == failing` is untouched.
  - **The preflight also states each warning cause once.** After the rollup,
    `warningGroups[].connectors` still repeated names every row already carries (84 mentions of 51
    idents, 3,857 chars), and 7 of 10 `failures[]` messages were byte-identical to a group's, since a
    failed ingestion run is both. So each group gets an `id` and loses its member list, and rows and
    those failures carry `warningIds` instead. Measured live: 18,810 → 15,693 chars (−16.6%), and
    the original groups and failure messages rebuild exactly from the ids. Two guards: it only
    applies when the result is smaller (a reference costs ~22 chars, so short idents like
    `Okta (Prod)` would grow the block), and a group naming a connector no row carries leaves the
    whole block verbatim rather than lose that membership. This one did need a §1.5 table row
    (`warningIds`). The digest is why it lives here and not in brief: its renderer reads
    `failures[].message`.
  - The runs endpoint is paginated at 20/page (~31 pages); `size=2000` returns the lot in **one**
    call — don't loop pages. It also accepts `sort=_time%2Cdesc`, and a smaller sorted window is
    ~0.4s faster — **don't**: the window spans only the last few days on a stack that ingests daily,
    so a source that last ran a month ago would silently drop out of the summary and report as
    `idle`, which is supposed to mean "hasn't ingested", not "couldn't tell".
  - The two endpoint fetches run concurrently, and `connect --with-connectors` overlaps them with the
    validation call, so the whole launch preflight costs one round trip's latency (~1.6s) rather than
    three. Issuing the preflight and the first query as two *processes* at once is slower, not
    faster — measured 4.5s against 3.2s sequential, from SSL and interpreter-start contention.
  - `profile` on a run comes back either as a name or as the **whole profile object**, config
    included; only `profile_name` is read out. Same reason the profile payload is field-filtered:
    it carries hosts, service accounts, proxies and an encrypted password, so raw responses from
    either endpoint must never reach the transcript.
  - Runs are matched to a connector on service **and** profile name, falling back to service alone
    (a run is often filed under `Default profile` while the configured profile has a real name). The
    fallback sets `ingestProfileInferred` so the answer can hedge instead of overclaiming.
- `api` — raw API caller; reads credentials
  from config/env, auto-selects the action token for `/data/ldg`, gives targeted hints on
  401/403/DNS errors. The endpoint is **positional** and should omit the leading slash under Git Bash
  (see the Git Bash note in SKILL.md §2); `_normalize_endpoint` restores it.
- `trend`'s JSON carries a per-date `series` block (dates, totals, metrics, breakdown groups) for
  charting — `report --input <trend.json>` renders it as branded line charts. Absence in a series is
  `null`, never 0, and the chart draws it as a break in the line; refusal payloads
  (`insufficientHistory`, `unverifiable`) carry no series and render as a page saying why.
  `--format csv` drops the series from the printed envelope so its caveats stay readable.
- `stacks` — **manage multiple stacks and switch the active one.**
  Keeps every stack's credentials in `~/.meridian/stacks.json` and mirrors the active one into
  `config.json`, so switching never overwrites the other stacks. `--token -` reads the token from
  stdin and omitting `--token` falls back to `MERIDIAN_API_TOKEN` — both keep it out of shell
  history and process listings. `--insecure-tls` records a self-signed stack's posture per stack,
  carried across switches (reverting is `rm` + re-add). Re-running `add` on an existing name
  updates it in place, preserving keys it wasn't given (entity salt, action token, TLS posture).
  `switch` also validates via the connect preflight and returns the same `state`. `list` redacts
  tokens to the last 4.
- `top` — **top-N by a numeric field**
  (riskiest/most-vulnerable/etc.) with automatic threshold-narrowing and client-side sort. Use for
  any "top/highest/riskiest" question instead of hand-writing threshold queries. Reports
  `truncated: true` when the qualifying tail exceeds 2000 records —
  past that the ranking is **not** a guaranteed global top-N (no server-side sort means an unread
  page could hold a higher value), so narrow with `--where`. Threshold mechanics: [internals.md](internals.md#how-top-picks-its-threshold).
- `profile` — **full risk/blast-radius profile for one named
  user or asset** in a single call. Use this for any "tell me about / investigate / what's the risk
  of \<name\>" question. For a user it returns identity linkage (every source account/email/status/MFA),
  threats split into DLP-behavioral vs leaked-credential, non-compliance, all linked assets with
  per-asset risk/KEV/encryption and which *other* users share them (blast radius), and a stability
  check that flags identity-dedup oscillation. For an asset: risk block, KEV list, vuln counts,
  per-CVE severity/exploitability detail, owner, public-IP exposure, and associated users.
  - **The asset branch reads `Vuln_List` out of the record it already fetched**, so CVSS, EPSS
    and fixability cost no extra call. Without it the rules could only see KEV counts, and a host
    whose risk is entirely CVSS/EPSS-driven rendered as "no critical exposure signals" -- measured
    on a live host carrying a CVSS 10.0, 21 unfixable CVEs and 31 in the top 10% of EPSS, which
    produced exactly one finding.
  - **The per-CVE `detail` array is OFF by default; `--vuln-detail` opts in.** Measured at
    v2.14.0 it was 8,563 of an asset profile's 12,569 characters -- 68% of the payload, ~2,100
    tokens -- and **nothing read it**: not `_derive_insights`, not `_profile_html`, not the
    `--ascii` view, not `compare`. Roughly 5,000 of those characters were 25 copies of a
    200-char CVE description. The rules consume `maxCvss`, the counts and the named CVE lists,
    all of which are always present, so a default profile went 12,569 -> 2,983 characters with
    byte-identical findings (asserted). Use `--vuln-detail` to enumerate specific CVEs, not to
    get the findings.
  - **`detailTotal` is emitted either way**, so "no detail shown" never reads as "no CVEs", and
    a `detailNote` names the flag. With `--vuln-detail`, `detail` is capped at
    `PROFILE_VULN_DETAIL_MAX` (25) ranked worst-first and `detailTruncated` says so; the named
    CVE lists are capped at `PROFILE_VULN_CVES_MAX` (12) with `notFixableCount` /
    `highEpssCount` carrying the true figures. The findings quote the counts rather than the
    capped lists -- a cap that silently shrank a finding would be the truncated-result-set bug
    in a second place.
  - **`maxCvss: null` with `scoredCount: 0` means no CVE reported a score**, not a score of 0;
    the finding says "no CVSS reported" instead of omitting the figure, so absence never reads
    as low severity. Same reason `_to_float` exists beside `_to_int`.
  - **`publicIps` is derived from `IP_Address` via `_is_private_ip`**, because `Is_Public` is
    unpopulated on some stacks. Anything it can't positively identify as a routable IPv4 literal
    (IPv6, a hostname, a malformed value) counts as private, so it under-claims exposure rather
    than inventing it. The exposure finding fires only when a public address coincides with an
    actual high/critical/KEV vulnerability.
  - **It also returns `findings` and `recommendations`** — rule-derived analyst reasoning (the same
    text the PDF prints), ordered most-severe first. Present those rather than re-deriving them;
    they used to be reachable only by rendering a report, so an investigation question paid ~4s of
    headless Chrome for text the JSON could have carried. Generated by `_derive_insights()`, which
    both the JSON and HTML branches now call.
  - Three round trips, not four: the linked-asset query and the change log are fetched together
    (`parallel()`), and a human-looking name issues the exact-key and fuzzy lookups at the same time
    instead of one after the other. The change log is optional — an error there leaves
    `stability.oscillating` false rather than failing the profile.

  ```bash
  python scripts/meridian.py profile --name AEXAMPLE                # exact Owner_Name
  python scripts/meridian.py profile --name "Firstname L"           # human/partial name works too
  python scripts/meridian.py profile --name I-0EXAMPLE0000000 --type asset
  ```

  `--name` accepts either the exact key (`Owner_Name`/`Asset_Name`) **or a human/partial name** —
  it tries the exact key first, then a substring match on display name (users) or host/FQDN
  (assets). So you can pass what the user actually said ("Firstname L", a surname fragment) without looking up
  the key first. Returns structured JSON; format it per SKILL.md §4. If several entities match, it
  returns a **risk-ranked candidate list** (ownerName, displayName, department, risk, level) so you
  can show the options and re-run with the exact key — no extra query needed to disambiguate.
  - **Terminal blast-radius view:** `--ascii` renders the profile as a text **blast-radius graph**
    (emoji severity dots + block-bar risk sizing) instead of JSON — the terminal counterpart to the
    SVG graph in the PDF, for when you can't show a picture. Bar length encodes the risk score
    (matching the SVG's node size), scaled so the highest-risk node fills the bar. Works for both
    user and asset profiles. Use the branded PDF (`report`) when the user wants something to share;
    use `--ascii` for a fast inline answer in a bare terminal.
- `list` — **filtered set**: "show me all X."
  - **`--all` returns every match**, up to `LIST_MAX_RECORDS` (5000). The old hard cap was 300, which
    made "show me all X" unanswerable the moment a filter matched more: 1,201 KEV assets returned 300
    with no way to reach the rest. Measured: `--all` on that set is 1,201 rows in 14 calls / ~6.8s.
  - The ceiling is a rate-limit fact, not a preference — a page is 100 records, ~1.1MB and one call, so
    50 pages is most of the hard 60/min budget. Past it the response reports the ceiling and asks you
    to narrow, rather than stalling behind the pacer for minutes and looking like a hang. Output
    carries `apiCalls` so the cost is visible.
  - **It is also a context fact, and that is the larger one.** The rows land in the answer, so the
    shape of the call decides whether it costs hundreds of characters or hundreds of thousands.
    Measured on the demo stack over the same 1,198 KEV-carrying assets:

    | call | stdout | ≈ tokens |
    |---|---|---|
    | `--all` | 288,137 chars | ~72,000 |
    | `--all --select Asset_Name,Count_KEV` | 94,294 chars | ~23,600 |
    | `--all --format csv --out kev.csv` | 347 chars | **~87** |

    A bare `--all` at the 5,000-row ceiling is ~229,000 tokens — a whole context window on most
    budgets, spent on rows the caller was asked to summarise. `--select` is a third off for free;
    `--out` is three orders of magnitude, because the envelope (with `totalRecords`, `truncated` and
    the completeness flags) still prints while the rows go to disk. Default to `--select` on every
    row-returning verb, and to `--out` past a few hundred rows.
- `summary` — **group-by / posture**: `--by <field>`
  returns exact per-value counts; `--metrics` returns stack totals + license (two GETs, concurrent).
  - **Which values exist is sampled; each count is exact.** There is no aggregation endpoint, so
    discovery reads `SUMMARY_SAMPLE_PAGES` (8) pages and collects the distinct values it sees. Every
    breakdown therefore states its own reach, and **no path returns a silently partial answer**.
  - **The sample is stratified, not the first N pages.** Records come back clustered by source rather
    than shuffled — measured, page 0 held 3 distinct `sourcetype` values, page 1 held 4 — so
    consecutive pages keep re-seeing the same values while whole categories sit further in. Page 0 is
    read first (its `totalRecords` sizes the sample and serves as the denominator), then the remaining
    pages are spread across the full range. At identical cost this found a 14,000-record source the
    first-8-pages sample missed entirely: `--by sourcetype` went from 21 groups with **12,680 records
    (37%) unplaced and unflagged** to 23 groups with 174 (0.5%) unplaced and stated.
  - **Single-valued fields** are checked by arithmetic, at no extra call: the counts partition the
    records, so their sum is compared against the total, giving `complete`, `accountedRecords` and
    (when short) `unaccountedRecords`. The comparison is `== total`, not `>=` — an over-count means
    records are landing in several groups, i.e. the field is really multi-valued, so it reports
    `overcountedRecords` and names stale metadata as the cause.
  - **`List` fields** can't use that arithmetic, since one record legitimately lands in several groups.
    Coverage is measured directly instead: one extra OR query (every discovered value as objects in a
    single inner array) counts the records matching *any* of them, and the shortfall against the total
    is exact. Reported as `coveredRecords` / `unaccountedRecords` / `complete`.
  - **High cardinality returns partial data rather than nothing.** Past `SUMMARY_MAX_GROUPS` (40) it
    counts the largest values the sample saw, sets `groupsCapped` and `distinctValuesSeen`, is never
    `complete`, and the coverage check states how many records the dropped values hold. `--by OS`
    previously returned zero groups; it now returns 40 with exact counts and "2,532 of 28,480 (8.9%)
    unplaced".
  - Trade-off worth knowing: stratifying is a large win on clustered fields and a small loss on ones
    whose values happen to cluster early — `--by Lucidum_Asset_Type` moved from 54 unplaced to 191.
    Both are *reported exactly*, so neither is a silent answer; the 37% case was.
- `compare` — **two entities side by side.**
- `hr` — **which HR systems feed this stack, and how many user records carry their data.** The
  answer to "do we have HR data?", and the first call for any HR, manager or employee question.
  - It recognises ADP, BambooHR, Dayforce, HiBob, Sage People, UKG and Workday by catalog
    `bridge_name`, falling back to display name. A fixed list, because nothing in the API marks a
    connector as HR: the catalog files all of them under *Identity Access Management* with Okta and
    Entra, and a description regex matched non-HR tools. iCIMS is left out on purpose: applicant
    tracking holds candidates, not employees.
  - A connector's enabled services are the `sourcetype` values its records carry, so each is counted
    exactly with its own query. `userRecords` is one OR query across them, so a person in two HR
    systems counts once. Not `summary --by sourcetype`, which samples and misses a small source.
  - `state` is `has_data`, `configured_no_data`, `configured_disabled`, `none_configured` or
    `unknown`. Only `none_configured` means there is no HR system, and only because the profiles were
    read. An unreadable endpoint or a failed count is `unknown`, never a zero.
  - `where` is the `--where` clause that scopes `list`/`top`/`summary` to HR records. `hrFields` lists
    the user fields that come from HR: the source's `alias_<sourcetype>_*` copies and SmartLabels
    named after the system. The core `Owner_Manager`/`Owner_Department` fields are merged across every
    identity source.
  - Each system row carries the connector's `health` and `lastIngest` from the same scrubbed summary
    `connectors` uses. Credential values from the profile read are discarded before anything else runs.
- `check` — **token capability preflight**: which API areas the token can reach + rate-limit
  headroom. Run this if you hit a 403 or before a big investigation.
- `refresh-fields` — re-read this stack's real fields into
  `~/.meridian/fields.<fqdn>.json` (both metadata calls concurrent); `--search <term>` finds a field by
  name. **No longer required before querying** — `load_field_map()` fetches and caches metadata on
  first need. Run it to *refresh* a stale cache, which is what an `overcountedRecords` breakdown is
  telling you to do. It also clears the SmartLabel cache (`labelsCacheCleared`), because label→table
  resolution is derived from this metadata.

### `--where` clauses

`--where "<Field> <operator> <Type> [value]"`, repeatable and ANDed, on `top` / `list` / `summary` and in
a `metrics add` definition. Validated locally before any request: the shape, the operator, the type, and
the field name against this stack's metadata. A typo therefore surfaces as an error rather than as
`totalRecords: 0`, which is indistinguishable from a real zero.

**Multi-word operators work** — `split_clause` matches longest-first against `CLAUSE_OPERATORS`, so the
operator table decides where the type slot starts:

```bash
--where "OS not match String Windows"          # the DSL's only negation -- there is no NOT wrapper
--where "sourcetype not in List aws_ecs"
```

Because matching is against the table and not by position, a value containing operator words is safe:
`sourcetype in List not match` parses as `in` with the value `not match`.

**⛔ The windowed Datetime operators are refused.** `within`, `within past`, `within future` and their
negations do not work on this API — half return HTTP 400 and half return HTTP 200 **having ignored the
window**: `Expired_Datetime within future Datetime <N>, days` gave the same 52 records for N = 1, 30, 90
and 3650, and `not within Datetime 30, days` gave **0** where the truth was **60**. The verbs refuse all
six and name the absolute form that asks the same question. See
[query-syntax.md](query-syntax.md) for the full measurements.

**Relative Datetime values are resolved client-side**, which is the supported way to ask a time-window
question — full value table, the open/close bound rule, and the measured bare-date pitfall are in
[query-syntax.md](query-syntax.md#relative-values-in---where-resolved-client-side); don't re-derive
them here.

```bash
# certificates expiring in the next 90 days
list --table asset --where "Expired_Datetime >= Datetime today" --where "Expired_Datetime <= Datetime +90d"
# assets not discovered in the last 30 days
list --table asset --where "Last_Discovered_Datetime < Datetime -30d"
```

In a **metric**, store the relative value, not a resolved date — `metric_query()` re-resolves on every
snapshot, so `+90d` tracks a moving window. A frozen absolute date would make the series decline toward
zero as the window receded, which looks like real improvement.

Numeric values go out as **unquoted JSON numbers** (`Float`/`Integer` are coerced); a quoted number
returns 0 records silently. `==`, `!=`, `in`, `not in` are **case-sensitive** — prefer `match` when the
user's phrasing is fuzzy.

### Field metadata is trusted, not guessed

`load_field_map(table)` returns `{fieldName: dataType}`, reading the per-stack cache and fetching it if
absent. Two things depend on it, and both used to fail silently:

- **`field_type()`** decides `summary --by`'s operator (`match` for `List`, `==` otherwise). It used to
  answer `"String"` for everything when the cache was missing — which it was until someone ran
  `refresh-fields` by hand. A genuinely multi-valued field then got `==`, and the breakdown counted
  records in several groups at once: measured 38,943 accounted against a 34,229 total.
- **`check_fields()`** validates every field name `top`/`list`/`summary` are handed, *before* any
  request. A typo used to return `totalRecords: 0` — indistinguishable from a real zero, and a
  confidently wrong answer. It now names the closest match, and calls out a case-only miss separately
  because the API is case-sensitive and that is the usual mistake.

If metadata is unreachable (a scoped token 403s), both degrade quietly: types fall back to `String` and
validation is skipped rather than inventing a failure. Tested in both directions.

**A miss against the cached map re-reads the metadata once before refusing.** The cache never expired,
and enabling a connector is exactly what adds fields (its `alias_<sourcetype>_*` copies, the
customer's SmartLabels for it), so a newly connected source's fields were refused as "doesn't exist".
The re-read happens once per table per process, so a run of typos costs one call; a changed field set
also clears the SmartLabel and result caches, as `refresh-fields` does.

- `labels` — **the stack's own SmartLabels**, resolved to
  queryable fields. Each carries the customer's `llmBusinessValue` description, so this is the bridge from
  their vocabulary to a query. `--search` resolves a business term (exact → substring → fuzzy) and returns
  the full purpose text. Cached per stack in `~/.meridian/labels.<fqdn>.json`.
  - **A bare listing omits the purpose text entirely** (and says so via `purposeOmitted`), because
    that call's question is "what vocabulary exists here", which the name and field answer. Earlier
    revisions truncated the blurbs to 110 characters instead, which barely helped: measured, 151
    truncated blurbs were 16,947 of the payload's 42,895 characters — 40%, ~4,200 tokens. Dropping
    them took a full listing to 22,795. Listings of 12 or fewer keep the detail, since the cap
    exists to stop an answer being swamped, not to withhold anything.
  - **The table is resolved from field metadata, not from `field_collection`.** Those collection names
    are stack-specific (one demo stack calls them `AWS_CMDB_Output` and `User_Combine`), so trusting
    them wouldn't generalise. A label whose field no longer exists is returned with
    `queryable: false` and never offered as a match.
  - SmartLabel metadata declares its own type names. Only two differ from the DSL: `Str` → `String`
    and `Boolean` → `Binary` (`SMARTLABEL_TYPES`). Everything else passes through.
  - **`--refresh` refetches instead of reading the cache.** Labels are the customer's own vocabulary and
    they keep defining new ones, so unlike field metadata this cache going stale is the expected case.
    Nothing invalidated it, so a label defined after the first `labels` run stayed invisible
    indefinitely. `refresh-fields` now clears it too (reporting `labelsCacheCleared`), since the table
    resolution derives from exactly that metadata. Use `--refresh` when the user is sure a label exists
    and the search misses — the no-match note says so.
  - **A provisional result is never cached.** Since tables resolve against field metadata, a 403 on
    *that* leaves every label with no table and `queryable: false` — indistinguishable from "they never
    defined it". Persisting that let one blip hide every SmartLabel permanently and answer "the term may
    simply not be one of them" about a label sitting right there. Such a result is now flagged
    `fieldMetadataUnavailable` and kept out of the cache file.
- `digest` — **one-command periodic posture review**:
  inventory totals against their 30-day averages, connector coverage, a breakdown, and the riskiest
  users and assets. Composed from `stack_metrics()`, `summarize_connectors()`, `summarize_by()` and
  `top_n()` — the same implementations the individual verbs use, so every completeness caveat and health
  verdict arrives unchanged rather than being approximated. ~20 calls, ~6.7s measured.
  - A section that fails is listed in `sectionsUnavailable` with its error and **omitted rather than
    faked**. It runs unattended, so a scheduled report that quietly loses a section is how a broken
    permission goes unnoticed; SKILL.md requires the failure be surfaced.
  - `report --input digest.json` renders it as a branded multi-section PDF (`_digest_html`).
  - `--snapshot` also appends the result to this stack's local trend history, at **zero extra API
    calls** — it reduces the payload it already computed. This is the recommended way to accumulate
    history. The snapshot report is folded into the same JSON document under `snapshot`, so
    `digest --snapshot | report` still parses.
- `snapshot` — take a snapshot without wanting a digest. Same building blocks and same defaults.
  See **Trends** below.
- `trend` — compare two snapshots. See **Trends** below; its refusals are the point of the verb.
- `metrics` — the named counts captured on every snapshot, one API call each.
- `snapshots` — inspect or trim the history file.
- `report` — branded PDF; several input files render one document with several subjects
  (profiles only). See below.

Release/packaging maintenance scripts (`make-impact.py`, `make-guide.py`, `make-brief.py`,
`make-package.py`) are not verbs a user's question ever routes to — see
[internals.md](internals.md#maintenance-tools-not-verbs).
