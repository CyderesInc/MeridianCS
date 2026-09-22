# Meridian recipe library — ready-to-run answers for common questions

Map a user's question to a command. Every command is `python scripts/meridian.py <verb> ...` (run from
the skill directory, or pass an absolute path) and returns JSON to format per SKILL.md §4. Field names
are case-sensitive and vary by stack — run `meridian.py refresh-fields --search <term>` first if
unsure a field exists.

`--where` takes one `"<FieldName> <operator> <Type> <value>"` clause and is **repeatable**; repeated
clauses are ANDed. Numeric values are unquoted, and `==`/`in` are case-sensitive (see
query-syntax.md).

## Verb cheat-sheet

- **ask** — raw call: `meridian.py api <path> [-X POST --body-file q.json]`
- **rank** — top-N by a numeric field: `meridian.py top --table <t> --field <f> --top <n> [--where ...]`
- **profile / investigate** — one entity + blast radius: `meridian.py profile --name <name> [--type asset]`
- **compare** — two entities: `meridian.py compare --name1 <a> --name2 <b> [--type asset]`
- **list** — all matching a filter: `meridian.py list --table <t> --where "<clause>" [--all | --limit N] [--count-only] [--select <cols>]`
- **summary** — group-by / posture: `meridian.py summary --table <t> --by <field>` or `--metrics`
- **check** — token capability preflight: `meridian.py check`
- **refresh-fields** — cache this stack's fields: `meridian.py refresh-fields [--search <term>]`
- **labels** — the customer's own SmartLabels: `meridian.py labels [--search "<business term>"]`
- **digest** — whole periodic posture review in one call: `meridian.py digest [--top N]`
- **report** — Cyderes-branded PDF from any verb's JSON: `meridian.py report --input <json> --out <file.pdf> --title "..."`

## Picking the cheapest shape of the answer

Every row-returning verb (`top`, `list`, `summary`) takes `--select` and `--format csv --out`, and
which one you reach for decides whether an answer costs a few hundred characters or a few hundred
thousand. Measured on the demo stack, the same 1,198 KEV-carrying assets:

| the question | the shape | cost |
|---|---|---|
| "how many X?" | `--count-only` | 101 chars |
| "show me the worst ones" | `top --top 5 --select` | 547 chars |
| "the first page of them" | `--limit 10 --select` | 995 chars |
| "list them all" (readable) | `--all --select Asset_Name,Count_KEV` | 94,294 chars |
| "list them all" (bare) | `--all` | **288,137 chars** |
| a set to work from | `--all --format csv --out kev.csv` | **347 chars** + 75KB on disk |

- **Always pass `--select`** with just the columns the answer uses. Without it every row carries
  the default six fields whether the answer needs them or not.
- **Past a few hundred rows, use `--format csv --out <file>`.** The JSON envelope still prints —
  `totalRecords`, `truncated`, the completeness flags — so report the count, name the file, and read
  back only the slice the question needs. `--all` at the 5,000-row ceiling is roughly 229,000 tokens
  of raw rows: the ceiling is a rate-limit fact *and* a context fact.
- A narrower `--where` beats a wider one plus filtering afterwards — the API charges the same per
  page either way, and the narrow one never enters the answer.

## Posture & inventory

- "How many assets/users? overall posture" → `meridian.py summary --metrics`
- "Break assets down by risk level" → `meridian.py summary --table asset --by Risk_Level`
- "Assets by OS / by cloud / by type" → `--by OS` · `--by sourcetype` · `--by Lucidum_Asset_Type`
- "Users by department" → `meridian.py summary --table user --by Owner_Department`
- "High-risk users by department" → `meridian.py summary --table user --by Owner_Department --where "Risk_Level == String 3-high"`

## Risk ranking

- "Top 10 riskiest assets / users" → `meridian.py top --table asset --field Risk_Score --top 10`
- "Riskiest assets in Legal" → `meridian.py top --table user --field Risk_Score --top 10 --where "Owner_Department == String Legal"`
- "Most-vulnerable assets by KEV count" → `meridian.py top --table asset --field Count_KEV --top 10`

## Vulnerability / exposure

- "Assets with known-exploited vulns (KEVs)" → `meridian.py list --table asset --where "Count_KEV >= Integer 1" --count-only` (drop `--count-only` for rows; add `--all` for every match rather than the first 50)
- "Public assets with KEVs" → `meridian.py list --table asset --where "Is_Public == Binary 1" --where "Count_KEV >= Integer 1"`
- "Unencrypted assets holding confidential data" → `meridian.py list --table asset --where "Is_Encrypted == Binary 0" --where "Data_Classification == String Confidential"`
- "End-of-life OS inventory" → `meridian.py list --table asset --where "OS match String Windows XP"` (repeat for `Windows Server 2012`, `Ubuntu 16.04`, etc., or `--by OS` on `summary` to see all)

## Agent / tool coverage ("what's missing CrowdStrike?")

**Don't reach for `Missing_Sources` — it exists in metadata on both tables but is entirely
unpopulated** (see [field-map.md](field-map.md) response quirks). **One query, never a
subtraction.** Meridian holds one asset node per data source (joined by `SAME_AS`,
which `/CMDB/v2/data/cmdb` does not traverse), so `count(all) − count(has_agent)` counts covered
machines' other representations as gaps. Anchor to a deduplicated population instead and filter inside
it — see [field-map.md](field-map.md) response quirks for the measured overstatements.

- **Step 1, confirm the exact `sourcetype` values** — they are unintuitive and a value matching nothing
  returns 0 records, which reads as "no EDR anywhere":

  ```bash
  python scripts/meridian.py summary --table asset --by sourcetype
  ```

  On the demo stack the EDR sources are `crowdstrike_host` and `sentinelone_agent` — **not**
  `crowdstrike` / `sentinelone`.
- **Step 2, count the gap inside one deduplicated population:**

  ```bash
  python scripts/meridian.py list --table asset \
    --where "Online_Compute_SmartLabel == Binary 1" \
    --where "sourcetype not in List crowdstrike_host,sentinelone_agent" --count-only
  ```

  Measured on the demo stack: **9,000** of 15,000 active compute assets, and the complement
  (`sourcetype in …`) returns **6,000** — the two partition the population exactly, so the pair is a
  free self-check. The naive subtraction over the whole asset table (34,000 − 8,800) claims **25,200**,
  a 2.8× overstatement that looks entirely plausible.
- Drop `--count-only` for rows, `--all` for every match. Swap the label for whatever
  `labels --search compute` resolves on the stack in front of you — `Online_Compute_SmartLabel` is
  demo-derived, not universal.
- Same shape for MDM/patch/vuln-scanner coverage: replace the `sourcetype` list (`tenable_scan` on
  the demo stack; whatever step 1 reports elsewhere) and keep the anchor clause.
- **If the customer's console disagrees, the console is right** — it traverses `SAME_AS`. Say so,
  correct the figure, and reconcile by grouping the surplus records by `sourcetype` combination.

## Identity / hygiene

- "Users with MFA disabled" → `meridian.py list --table user --where "Count_No_MFA >= Integer 1"`
- "Users with leaked credentials" → `meridian.py list --table user --where "Threat_List match List Leaked Password"`
- "Non-compliant users (SailPoint)" → `meridian.py list --table user --where "Count_Non_Compliance >= Integer 1"`
- "Investigate <person>" / "their risks and recommendations" → `meridian.py profile --name "<name>"` (accepts partial/display names). The response carries `findings` and `recommendations` already — present those; don't re-derive them and don't render a report just to read them.

## Certificates & time-based

**Never use the windowed operators** (`within past`, `within future`, `within`, …). They don't work —
half return HTTP 400, half return every record while silently ignoring the window. `meridian.py`
refuses them. Use a relative value, which it resolves to an absolute timestamp client-side; see
query-syntax.md for the measurements.

- "Certs expiring in the next 90 days" →

  ```bash
  python scripts/meridian.py list --table asset --where "Lucidum_Asset_Type match String Certificate" --where "Expired_Datetime >= Datetime today" --where "Expired_Datetime <= Datetime +90d"
  ```

  (a lower bound opens at 00:00:00, an upper bound closes at 23:59:59, so the range is inclusive)
- "Certs already expired" → `--where "Expired_Datetime < Datetime today"`
- "Assets first seen in the last 7 days" → `--where "First_Discovered_Datetime >= Datetime -7d"`
  (**not** `First_Time_Seen` — this file claimed that name for a while and no such field exists on the
  demo stack's 570; `refresh-fields --search Discovered` is how to confirm it on any given stack)
- "Assets not discovered in the last 30 days" → `--where "Last_Discovered_Datetime < Datetime -30d"`

## Trend / change

The API keeps no history beyond a 30-day average of totals, so trends come from snapshots this tool
stores. **There is no backfill** — a trend can only answer what was already being captured.

- "Set up a weekly snapshot" → schedule this one command (Task Scheduler on Windows, `launchd` on
  macOS, `cron`/`systemd` on Linux, or Claude Code's own scheduling — the skill provides the command,
  not the timer; per-OS setup and gotchas in `references/scheduling.md`):

  ```bash
  python scripts/meridian.py digest --snapshot > digest.json
  ```

  It appends to the history at **zero extra API calls**, and `digest.json` still pipes to `report`.
- "How has our asset count / risk mix changed since June?" →

  ```bash
  python scripts/meridian.py trend --since 2026-06-01
  ```

  **State `coverageChanged` / `insufficientHistory` / `unverifiable` before quoting any percentage**
  (SKILL.md). A count that fell because its connector broke is not an improvement.
- "Trend our **crown jewels** exposure" → their own vocabulary, so track it as a SmartLabel metric:

  ```bash
  python scripts/meridian.py metrics add --name crown-jewels --label "Crown jewels" --smart-label "crown jewels"
  python scripts/meridian.py trend --metric crown-jewels
  ```

  One API call per snapshot. If it isn't tracked yet, `trend` says so, gives today's value as a
  baseline, and registers it in one step:

  ```bash
  python scripts/meridian.py trend --metric crown-jewels --derive-smart-label "crown jewels" --derive-label "Crown jewels"
  ```

  That writes to the user's config — **say so in the answer.**
- "What got worse since Friday?" → needs snapshots taken with an entity scope:

  ```bash
  python scripts/meridian.py snapshot --entities top500:asset:Risk_Score     # on a cadence
  python scripts/meridian.py trend --since 2026-08-01 --name-entities
  ```

  Ids are salted hashes on disk; `--name-entities` resolves them live. `disappeared` means dropped
  below the top-500 cutoff, **not** removed from the inventory — say which.
- "What are we tracking?" / "start tracking X" → `meridian.py metrics list` / `metrics add`
  (cap 20, one call each). Prefer a metric over a breakdown: `Risk_Level` costs ~12 calls, `OS` ~49.
- "How much history do we have?" → `meridian.py snapshots list` (count, oldest, newest, bytes).
  Trim with `snapshots prune --keep N`; it reports exactly what it dropped.
- "Asset & user counts for a past date" → `meridian.py api 'CMDB/v2/system/metrics/data?date=2026-06-01'`
  — the one genuinely historical endpoint, and only for totals.
- "What changed on <asset>/<user>" → `meridian.py api 'CMDB/v2/data/cmdb/asset/change?id=<name>'`

> Omit the leading slash on `api` endpoints — Git Bash on Windows rewrites a leading `/` into a
> Windows path. Only `api` takes a raw endpoint.

## Reports (branded PDF)

- "Give me a PDF of the top 10 riskiest assets" →

  ```bash
  python scripts/meridian.py top --table asset --field Risk_Score --top 10 --select "Asset_Name,Risk_Score,Risk_Level,IP_Address,OS,Count_KEV" > top.json
  python scripts/meridian.py report --input top.json --out "Cyderes-Top10-Assets.pdf" --title "Top 10 Riskiest Assets" --date <today>
  ```

- Works for `list` and `summary` output too — pipe the JSON to `report`. Add `--html` for HTML instead of PDF.

## The customer's own vocabulary

- "Which **crown jewels / PCI-scope / tier-1** assets are exposed?" → `meridian.py labels --search "crown jewels"` **first**, then query the `field` it returns. The label's `purpose` text is the customer's own definition — use it to frame the answer.
- "What labels do we have?" → `meridian.py labels` (add `--table user`). Anything with `queryable: false` has a definition but no live field.
- "We just created a label and it isn't showing" / a search misses a label the user is sure exists → `meridian.py labels --refresh` (the cache doesn't expire on its own).
- Output carries `fieldMetadataUnavailable` → the label lookup failed, so **report it as unavailable**; do not report the term as not being one of their labels.

## Recurring reporting & exports

- "Send me a weekly posture report" →

  ```bash
  python scripts/meridian.py digest > digest.json
  python scripts/meridian.py report --input digest.json --out "Weekly-Posture.pdf" --title "Weekly Meridian Posture Digest"
  ```

  Drive it from Task Scheduler (Windows), `launchd` (macOS), `cron`/`systemd` (Linux), or Claude Code
  scheduling — the skill provides the one command, not the timer; see `references/scheduling.md` for
  per-OS setup and the silent-failure gotchas each one has.
- "Give me that in Excel" → add `--format csv --out <file>.csv` to `top` / `list` / `summary`. The JSON envelope still prints — pass on its `truncated` / `complete` / `unaccountedRecords` rather than letting the file imply the set is whole.
- "Export everything matching X" → `meridian.py list --table asset --where "<clause>" --all --format csv --out x.csv`

## Operations

- "Which connectors are failing?" / "what data do we actually have?" → `meridian.py connectors` — enabled connectors, connection-test status, and records ingested per source; see SKILL.md §1.5. Don't query `/CMDB/v2/connector/profile` directly; its response can carry sensitive connector configuration
- "When does data ingestion next run?" → `meridian.py api 'CMDB/v2/system/metrics/data-ingestion/next'` — **needs elevated permissions**; verified to return 403 on a normal User Generated token, so expect it to fail and fall back to `connectors` for last-ingest times
- "Can my token do X?" → `meridian.py check`

> Building a new recipe: pick the verb (list for sets, summary for counts, top for ranking,
> profile for one entity), then express the filter as `"<FieldName> <operator> <Type> <value>"`.
> Remember numeric values are unquoted and `==`/`in` are case-sensitive (see query-syntax.md).
