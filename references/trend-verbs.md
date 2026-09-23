# Snapshots, metrics and trends — flags and usage

The verbs that build and read this stack's local history: `snapshot`, `trend`, `metrics`, `digest`,
`alerts`. Flags for the query verbs are in [scripts.md](scripts.md); the implementation notes are in
[internals.md](internals.md); scheduling any of these on a recurring cadence (Windows/macOS/Linux,
plus the heartbeat-log pattern) is in [scheduling.md](scheduling.md).

**`design/trends.md` is the design spec for this feature and is not the same document as this one** —
it records why the snapshot format is shaped as it is and what each phase decided. This file is the
usage reference. (The spec is internal and excluded from the shipped package.)

**Runtime:** every verb lives in `scripts/meridian.py` — Python 3, standard library only. One process
per verb, regardless of how many API calls the verb makes. State lives in `~/.meridian/`:
credentials (`config.json`, `stacks.json`), the 60/min rate-limit budget (`.ratelimit`), the top-N
rung cache (`topcache.<fqdn>.json`), cached field names (`fields.<fqdn>.json`) and cached aggregates
(`rescache.<fqdn>.json`). A Chromium browser is needed only for PDF reports.

## Trends: snapshots, metrics, entity deltas

`/CMDB/v2/system/metrics/data` is the only history the API offers, and it returns a 30-day **average of
totals** — no series, no per-day values, nothing for vulnerabilities, risk distribution or per-label
counts. There is no aggregation endpoint and no field projection either. So any trend beyond "assets vs
their 30-day average" is built from state this tool persists locally. **There is no backfill**: a trend
can only answer what was already being captured.

### Files

```text
~/.meridian/snapshots.<sanitised-fqdn>.jsonl   history, append-only, one JSON record per line
~/.meridian/metrics.<sanitised-fqdn>.json      the tracked metric definitions
~/.meridian/config.json                        also holds entity_salt (see PII posture)
```

Per stack, sanitised with the same expression the field and label caches use. `config.json` holds only
the *active* stack, so a shared history file would blend two customers' trend lines. JSONL rather than a
JSON array: appending never rewrites history, a corrupt line is skipped rather than losing the file, and
it avoids a read-modify-write. Every record carries `"schema": 1`; an unrecognised schema is **skipped
with a stated reason**, never coerced — a `try/except` falling back to "no history" would turn a
forward-incompatible file into a silent zero.

### Capture cost, measured — prefer a metric over a breakdown

| Captured item | API calls per snapshot |
|---|---|
| Totals + coverage (from a `digest` already computed) | **0** |
| One named metric | **1** |
| Breakdown by `Risk_Level` (3 groups) | ~12 |
| Breakdown by a 40-group field like `OS` | **~49** |
| `--entities top500` | ~5 (5 pages, ~12s) |

A breakdown costs one page-0 call, up to 8 stratified sample pages, and **one call per discovered
group**, out of a hard 60/min. So **anything you want to trend belongs in the metric list, not in a
second breakdown** — twenty metrics cost twenty calls; one high-cardinality breakdown costs forty-nine
and leaves no budget for the rest of the snapshot. A snapshot warns with the measured cost when a
breakdown's `distinctValuesSeen` is large, both after the fact and (from the previous snapshot's
measurement) before spending the calls. It warns rather than refuses: the operator asked for the field.

Retention is capped at **400 records** (~8 years of weeklies, ~13 months of dailies), pruned oldest
first, and **never silently** — a history that quietly loses its early end changes what a long trend
means. `snapshots list` reports count, oldest, newest and bytes.

### What `trend` refuses to compute, and why

Endpoints are chosen on the stack's own ingest date (`stackDate`), never the local clock. The refusals
are the feature:

| Flag | Meaning |
|---|---|
| `insufficientHistory` | fewer than two **distinct stack dates** — two snapshots on one date read the same daily ingest, so a 0% between them is the flattest possible wrong answer |
| `coverageChanged` | the failing/degraded connector set differs; computed, but never presented as a real movement. No metric→connector attribution is invented — the API doesn't expose it |
| `unverifiable` | connector health unreadable at an endpoint, so **nothing** is computed |
| `notTracked` | never captured. Never `0`, `0%`, "no change" or a flat line |
| `caveats` | a total, breakdown or ranking not comparable — including a ranking whose two endpoints used different `matchedAtThreshold`, which would compare different things |
| `cutoffMoved` | an entity scope is a **window**: "disappeared" means dropped below the cutoff, not left the inventory |

Output goes through `emit()`, so `--format csv` still prints the envelope — a spreadsheet column cannot
carry `coverageChanged`.

### The question-to-metric loop

An empty metric list makes every non-total trend permanently unanswerable, and there is no backfill. So
an unanswerable question writes the metric that makes it answerable: pass a definition alongside
`--metric` and `trend` validates it, returns today's value as a **baseline**, registers it, and reports
the registration — because it wrote to the user's config. Validation is the same as a hand-added metric,
so a question that couldn't be answered never leaves a broken metric behind. At the 20-metric cap it
refuses and lists what's tracked rather than evicting a series someone has been accumulating.

Metric definitions are validated at `add` time **and re-validated on every read** — a field can be
removed from a stack after a metric is saved. A metric that stops resolving records `ok: false` with the
reason and is reported unavailable; it is **never** recorded as `count: 0`, because a zero reads as good
news and nobody investigates it. A `smartLabel` metric is counted with `== true` for a Binary label and
**`exists` for every other type** — `== null` matches the records where the label is *absent* (measured:
32,597 of 34,270, against the 1,673 `exists` returns).

### Entity deltas and the PII posture

`--entities` captures per-entity scores so `trend` can answer *which* assets moved: `appeared`,
`disappeared`, `worsened`, `improved`. Two rules.

**Scope is bounded.** The full inventory is ~343 pages ≈ 13 minutes ≈ 6× the rate budget, so there is no
unbounded form. Default is `top500:<table>:<field>`; above 500 needs `--allow-large-scope` and is refused
with its measured cost stated; above `LIST_MAX_RECORDS` (5000) is refused regardless. A
`label:<SmartLabel>:<field>` scope takes the same default bound.

**Identity is hashed by default, and that is a policy line rather than a size trim.** Ids are
`sha256(entity_salt + identifier)[:16]`, which gives same-entity-across-time — all a delta needs — with
**no customer name on disk**. The salt is never written into a snapshot; only `saltId`, a fingerprint
saying which salt produced the hashes. A **mismatched `saltId` refuses to diff** rather than reporting
every entity as both appeared and disappeared.

The salt lives per stack in `stacks.json` and is mirrored into `config.json` like a token, so it survives
a `stacks switch` (which fully replaces that file) and so two customers' histories can't produce
identical hashes for identical names. **Losing it is not recoverable** — a new salt makes every earlier
entity record incomparable. It is written via a temp file + `os.replace`, since it is a read-modify-write
of the file holding the API token.

`--with-names` opts into storing raw identifiers. It warns **once** per stack, stamps a `privacyNote`
into the record, and `snapshots list` keeps reporting how many records hold identifiers. The file then
holds customer data and should be covered by the same `permissions.deny` rule that protects
`config.json`. To name movers without that, `trend --name-entities` re-queries live: names come from the
API at answer time and stay in memory. Only entities still in the scope can be named — a disappeared one
is by definition absent from the current result set — and the count that couldn't be named is stated.

### Alerts

Standing rules over the stored history. `alerts eval` reads snapshots only — it makes **no API calls**,
so it is cheap to run on every scheduled capture.

```bash
python scripts/meridian.py alerts add --name connector-regression --if coverage-regressed
python scripts/meridian.py alerts add --name kev-ceiling --if above --metric kev-exposed --value 1500
python scripts/meridian.py alerts list          # rules, and whether each can be evaluated
python scripts/meridian.py alerts eval          # verdicts; --window full, --since, --format json
python scripts/meridian.py alerts rm --name kev-ceiling
```

Conditions are `coverage-regressed` (a connector entered the failing set), `above` and `below` (a
tracked metric against a fixed threshold). Cap is 20 rules per stack.

**Rules and metrics are per stack and do not travel.** Both are stored per FQDN, and a rule is
re-validated on every eval against the **active** stack's metric registry — so a rule copied to a stack
where its metric was never defined is `unevaluable` forever rather than quietly wrong. Define the metric
with `metrics add` first.

#### Three verdicts, and an exit code that is a bit field

`firing` / `clear` / `unevaluable` — never just the first two, because an untestable condition is not a
passing one, and alerting is the layer people stop watching precisely because it is supposed to watch
for them. The exit code encodes that for a scheduler:

| Code | Meaning |
|---|---|
| 0 | every rule evaluated, nothing firing |
| 4 | at least one rule **firing** |
| 8 | at least one rule **unevaluable** — including a stack with **no rules at all** |
| 12 | both |

It is a bit field, not an ordered severity: collapsing it would force a choice about whether a firing
alert outranks a broken one, and either answer hides half the picture. An empty rule set exits 8 for
the same reason — a stack whose rules were lost looks exactly like a stack with nothing wrong.

A rule is unevaluable when the window holds fewer than two distinct stack dates, when connector health
was unreadable at either end (`unverifiable`), when the two endpoints identify connectors differently
(so no name can be matched across the boundary), or when a threshold rule's metric was not captured or
stopped resolving. A metric that did not resolve has an **unknown** count, never zero.

#### What a verdict carries

Every `coverage-regressed` row — firing and clear alike — carries the standing picture as well as the
change: `stillFailing`, `stillDegradedCount`/`stillDegraded`, and `degradedEntered`/`degradedLeft`.
That is deliberate. The rule watches for *movement*, so connectors failing for a fortnight produce a
`clear` verdict, and a bare "clear" would let "nothing entered the failing set since yesterday" be read
as "nothing is failing". `stillDegradedUnknown` means the snapshot recorded no degraded figures —
unknown, never 0.

`--include-degraded` makes the rule fire when a connector enters the **degraded** set too. It is
opt-in, and measured over 30 windows of real history it fired once, as a false alarm: 55 connectors
flapped ok→degraded inside a single anomalous snapshot. Every genuine degraded→failing move was already
caught without it, because such a connector enters the failing set whatever it was before. A flap also
fires on only one side — the recovery window is silent — so one bad snapshot buys one mass alert a day
late, pointing the wrong way.

Thresholds are the caller's judgement, and a rule that fires every day carries no information: one
`above` rule was measured firing 16 runs out of 16. Take the value from the current figure (`metrics`,
or `trend`) rather than a round number.

#### Window

`--window daily` (the default) compares the two most recent snapshots — "what changed since yesterday",
which is the alerting question. `--window full` compares oldest with newest, so a one-off regression
keeps firing forever and the rule stops describing now. `--since YYYY-MM-DD` overrides both. The output
states the window's real span: two snapshots are only "since yesterday" if one was taken each day, and
a laptop asleep over a weekend makes the same two records three days apart.

#### Delivery

`alerts notify` renders the current verdict to Slack, Teams or email. It changes no evaluation
semantics — the renderers only format what `alerts eval` already produced.

```bash
python scripts/meridian.py alerts notify --to slack,email     # default: all three that are configured
python scripts/meridian.py alerts notify --force              # send even if nothing changed
```

- **On-change only.** A rule's verdict has to differ from the last saved state (in
  `alertstate.<fqdn>.json`) for anything to send. The rendered message still lists **every** current
  rule; change only gates whether it goes out. Nothing is ever dropped from the message — suppression
  would be a way for this tool to go quiet, and a suppression bug is a silent failure.
- **Configuration is env-var only**, so nothing new is written to `~/.meridian/`:
  `MERIDIAN_ALERT_SLACK_WEBHOOK`, `MERIDIAN_ALERT_TEAMS_WEBHOOK`, and for email
  `MERIDIAN_ALERT_EMAIL_SMTP_HOST` + `MERIDIAN_ALERT_EMAIL_TO` (optionally `_SMTP_PORT`, `_FROM`,
  `_SMTP_USER`, `_SMTP_PASS`, `_STARTTLS`).
- **The Meridian token never reaches a third-party URL**: delivery does not go through the API
  transport at all — no Authorization header, no pooled connection.
- **Teams expects a Workflows webhook** (Microsoft retired the old connector webhooks), and the
  payload is the one its default template needs: a `message` with an Adaptive Card in `attachments`.
  That template accepts any other body with **HTTP 202 and posts nothing**, so a 202 is not proof of
  delivery — the card appearing in the channel is. The shape is Microsoft's documented one but still
  **unverified** against a live flow.

Running this on a schedule (and the heartbeat-log pattern for noticing a job that stopped) is in
[scheduling.md](scheduling.md).

### Output formats

`top`, `list` and `summary` take `--format json|csv` and `--out FILE`.

`emit()` always prints the JSON **envelope**, even in CSV mode — a spreadsheet cannot hold "37% of
records are unaccounted for" or "this ranking may not be the true top-N", and dropping those silently
would undo the point of measuring them. With `--out`, the CSV goes to the file and the envelope to
stdout; without it, the CSV goes to stdout and the envelope to **stderr**, so
`... --format csv > out.csv` still yields a clean file with the caveats on screen.

Two details that matter for a file someone opens in Excel: it is written **`utf-8-sig`**, because Excel
reads a BOM-less UTF-8 CSV as the local codepage and mangles non-ASCII asset names; and multi-value
fields are flattened to `a; b` rather than `str(list)`, which would put Python syntax in a cell someone
is about to sort.

### Scheduling a recurring report

The skill deliberately does **not** schedule anything itself — that belongs to the OS or to Claude
Code, and a daemon inside a CLI would be the wrong place for it. `digest` exists so the scheduled thing
is a single command:

```bash
python scripts/meridian.py digest > "$HOME/meridian-reports/digest.json" && python scripts/meridian.py report --input "$HOME/meridian-reports/digest.json" --out "$HOME/meridian-reports/Weekly-Posture.pdf" --title "Weekly Meridian Posture Digest"
```

Full per-OS setup (Windows Task Scheduler, macOS `launchd`, Linux `cron`/`systemd`) and the
silent-failure gotchas each one has — including the heartbeat-log pattern for noticing a job that
quietly stopped running — is in [scheduling.md](scheduling.md). Note the output carries customer PII,
so a scheduled job must write somewhere appropriate — see the data-handling rules.
