# Reading the `connectors` verb

Read this before presenting `meridian.py connectors` output (SKILL.md §1.5 sends you here). The
launch summary from `connect --with-connectors` needs none of it: that one arrives already rendered,
by these same rules, and is shown as it stands.

Standalone (a mid-session "connector status?", "which connectors are failing?", or after a stack switch):

```bash
python scripts/meridian.py connectors
```

It merges the two endpoints that each tell half the story — `/CMDB/v2/connector/profile` (what's
**configured and enabled**, and whether each service's last connection test passed) and
`/CMDB/v2/system/metrics/connector` (what actually **ingested**, with record counts) — into:

| Field | Read it as |
|---|---|
| `summary.connectorsEnabled` / `healthy` / `degraded` / `failing` / `idle` | the headline tally (one count per connector **profile**, not per service) |
| `summary.ingestingSources` / `recordsLastRun` / `lastIngestUtc` | how much data the last run of each source brought in, and when the newest run was |
| `connectors[]` | one row per enabled connector: `servicesEnabled` / `servicesPassing` / `servicesFailing` (connection test), plus `lastIngest` (`status`, `records`, `utc`) when a run matches. Past the top `connectorsDetailed` its `lastIngest` keeps `records` only — enough for the rollup line, which is all §1.5 asks of it |
| `delivering[]` | **only on `connect --with-connectors --coverage-rows` (`shape: "preflight"`)**: the connectors that carry no failure and no detail row, as `connector` / `profile` / `records` / `warned`. They are exactly the ones the rollup line names — the two tables' rows all stay in `connectors[]` — so read the two lists together as the full population, and **never as two categories**: a connector here is delivering data, not a lesser class of one. `deliveringRolledUp` counts them. `connectors` (the verb) and `connect --coverage-full` return every row in full |
| `connectors[].health` | `ok` (tests pass, last run clean) · `degraded` (some services failing, or the run warned/errored) · `failing` (no enabled service passes its test) · `idle` (green but no run on record — only ever set when `fetched.ingestion` is `"ok"`, so it always means "hasn't ingested", never "couldn't tell") |
| `warningGroups[]` | the actual causes, cleaned of log-line framing and Python call frames, **one entry per distinct message across the whole stack**: `{severity: fail\|warn, message, connectors[], connectorCount}`, worst first then biggest group first. This is what answers "what's the warning" — the rolled-up `status` ("Warning"/"Success") never says why. One cause routinely spans many connectors (measured: 22 sharing a single AWS permission error), so read `connectorCount` as the finding — "22 connectors, one cause" is the answer, not 22 separate problems |
| `warningIds` | **only in the preflight shape**: on a `connectors[]`/`delivering[]` row or a `failures[]` entry, the `id`s of its `warningGroups[]` causes. There each group carries `id` instead of a `connectors[]` list, and a failure with `warningIds` has no `message` of its own — quote the group's. Resolve ids to messages; never show an id to the user |
| `warningGroupsTruncated` / `warningsUndetailed` | causes not quoted, and how many connector-instances they cover. Say a count was elided; never treat an unquoted cause as no cause |
| `connectors[].warned` | `fail` \| `warn` — present on **every** connector whose last run wasn't clean, whether or not its cause survived the `warningGroups` cap. This, not the absence of a message, is how you tell a clean run from an elided one |
| `connectors[].ingestProfileInferred` | the run was matched by service name, not profile name — attribute it loosely ("last run for this service") |
| `failures[]` | hard failures only (connection-test fail, or an ingestion run whose health is `fail`), grouped by identical message, so one bad credential is one row with `serviceCount` services |
| `otherSources[]` | sources with data in the stack but no matching enabled connector (`configured: false` = no profile at all). Their records are real but nothing is refreshing them |

**Lead with what's delivering data, call out real problems separately, and always say what a warning
actually is.** Three rules, all for one reason: a reader trusts what's in front of them, so an
inaccurate or unexplained signal reads as fact.

1. **A `degraded` connector that's ingesting fine looks identical to a broken one in the tally.**
   `degraded` fires for *any* non-`ok` last-run status, including a harmless `Warning` — measured, a
   stack with 100% of connectors actively ingesting reported "0 healthy", every connector degraded. **Use one dot
   for "is this connector giving me data" — 🟢 — for every connector with no entry in `failures[]`,
   whether its JSON `health` says `ok` or `degraded`.** Reserve 🟠/🔴 for connectors with a
   `failures[]` entry or `failing` outright. Recompute from `failures[]` each time; never approximate
   it from the tally.
2. **Never present "Warning" as the explanation for anything — it's a status, not a cause.** Find the
   connector's group — by its `warningIds`, or in `warningGroups[].connectors` when a group lists its
   members — and quote that group's cleaned `message` next to it, even when it's 🟢. "Wiz — Warning"
   tells the user nothing they can act on; "Wiz — _no local data template and save options, or
   invalid json file format_" at least tells them what actually happened, even for a 🟢 row. When one group covers many connectors, say that once — "12 connectors, all the same AWS
   `AccessDeniedException` on `ListAccounts`" — rather than repeating the sentence per row.
3. **Lead with the delivering-data count and its warnings, not the failing count.** A headline like
   "0 healthy, 55 degraded, 3 failing" reads as a stack in trouble even when nearly all of it is
   ingesting fine. Put the 🟢 picture — sized from the split above, not the raw tally — first, with its
   warning messages, then the real problems:

> ✅ **Connected to Meridian** — `company.meridian-cyderes.com` is tracking **34,201 assets** and **10,118 users**, data as of **2026-07-27 10:12 UTC** (latest Meridian rebuild).
>
> **Feeding that inventory:** **42 of 51 connectors are actively delivering data** — 93,127 records
> across 51 sources, most recently 27 Jul 14:58 UTC. **9 need attention** (below).
>
> | | Connector | Profile | Records | Last ingest | Warning |
> |---|---|---|---:|---|---|
> | 🟢 | **Amazon Web Services (AWS)** | Lucidum Prod | 6,800 | Success, 27 Jul | — |
> | 🟢 | **SailPoint IdentityNow** | Partner Tenant | 9,552 | Warning, 27 Jul | Rate limited on the group-membership endpoint, retried |
> | 🟢 | **Okta** | Prod | 5,607 | Warning, 27 Jul | Cannot resolve manager reference for 12 deprovisioned users |
>
> _+ 6 more delivering data, no warnings on their last run: Azure, OCI, Datadog, GitHub, Snyk, Freshservice._
>
> ⚠️ **9 need attention:** Microsoft Intune and Defender fail authentication (0/2 services passing
> each) — no fresh device data from either. SentinelOne and both GCP profiles have credentials that now
> fail, so their existing records are **stale**. SharePoint, Slack and DocuSign are similarly
> unauthenticated.
>
> _Also in the inventory with no active profile: Orca (14,000 records), AD (10,311), Okta (5,607), VMware, Infoblox, Code42, Tenable + 5 more._

Rules for it:

- **Two lists, opposite sort order.** The delivering-data table is biggest-first (it shows the value
  the stack is already getting), capped at ~6-8 rows plus one rollup line ("+ N more delivering data,
  no warnings: …", or naming the warning-carriers if it fits). The needs-attention list is worst-first
  — `failing` (no data at all) ahead of one still landing records — capped the same way with a rollup
  for the rest. Name the connectors in both; a bare count is not actionable.
- **A blank/`—` Warning cell means the run was actually clean, not that the message was omitted.** Only
  a connector **without** `warned` gets one; don't invent a generic "minor issue" filler for a 🟢 row
  that has none. A row that carries `warned` but whose cause fell outside `warningGroups` (no
  `warningIds`) is *not* clean — say it warned and that the detail was elided (`warningsUndetailed`
  counts them), or re-run with a higher `--max-warnings`. Reading an elided cause as a clean run is the one mistake this shape
  can cause that the old one couldn't.
- **Never let a 🟢 connector show up in the needs-attention list, and never let a 🟠/🔴 one hide in the
  delivering-data table.** The split is `failures[]` membership, recomputed every time — not the raw
  `health` tally, and not a guess from the last-run `status` string alone.
- **The tally (`healthy`/`degraded`/`failing`/`idle`) stays in the JSON and in trend comparisons**
  (`design/trends.md`'s `failingNames`/`degradedNames` need the tri-state as-is) — these rules govern
  how you *narrate* the summary, not what `connectors` returns. Just don't quote the raw tally as the
  headline in chat.
- Roll `otherSources` into a single muted line, largest first — never a second full table.
- **A `fromCache: true` coverage block is up to an hour old.** Fine for framing an inventory answer;
  say the age (or re-run `connectors --refresh`) when the user asks about a connector's state *right
  now*, or just fixed one and wants to see it recover.
- **Say it once per session.** Repeat only on request, after a `stacks switch`, or if the user asks a
  question that a failing connector would have answered (see below).
- **Never dump the raw JSON** and never call `/CMDB/v2/connector/profile` directly — that payload
  carries hosts, proxies and an encrypted password. The verb extracts only the safe fields (§5).
- If `fetched.profiles` or `fetched.ingestion` holds an error instead of `"ok"`, report the half you
  got plus one line on the other ("connector detail needs a full User Generated token — 403"). A
  scoped token still answers questions; don't turn this into a blocker.
- **Tie it back when it matters.** If a question lands on a failing connector — "which assets are
  missing CrowdStrike?" with CrowdStrike degraded, "who has no MFA?" with the identity connector
  failing — say so alongside the answer instead of presenting the number as complete.
