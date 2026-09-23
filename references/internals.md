# Helper internals

Why the helpers are shaped the way they are: the HTTP transport, rate-limit pacing, concurrency, TLS
posture, the aggregate cache, JSON output, how `top` finds its threshold, `selfupdate`, and the
release/packaging maintenance scripts.

**Read this before editing `scripts/meridian.py`.** None of it is needed to *use* a verb — for flags
see [scripts.md](scripts.md), [trend-verbs.md](trend-verbs.md) and [reports.md](reports.md). It is
kept separate so a lookup for one verb's arguments does not pull in the whole implementation.

Most of these notes exist because the obvious alternative was measured and was worse, or was silently
wrong. Where a figure is quoted, it was measured on a live stack — and page sizes in particular are a
per-stack fact, so scale from a fresh measurement rather than reusing a number from here.

## Import cost

One process runs per verb, so module import is paid on **every** invocation. Measured with
`python -X importtime scripts/meridian.py`: 118.9 ms across 157 modules before this was addressed,
against a ~60 ms bare interpreter.

Four modules are deferred to their call sites — `concurrent.futures` (25.4 ms), `hashlib` (16.0 ms),
`difflib` (8.0 ms), `secrets` (4.0 ms). Most verbs touch none of them: `concurrent.futures` is used in
exactly one place (`parallel()`), and a cached `summary`/`connectors` read returns before reaching it;
`difflib` only formats a did-you-mean on an error path; `secrets` only mints an entity salt; `hashlib`
only hashes a cache key or an entity id. Total import time drops to **87.8 ms** and every verb's wall
clock by **25–40 ms (10–16%)**.

`test_lazy_imports` guards this in a subprocess, because the regression is invisible: adding
`import hashlib` at the top of the file breaks nothing and passes every other test while silently
giving the time back. It also calls each deferred module's code path, since a missed call-site import
raises `NameError` only there — a "these modules aren't loaded" check on its own would bless exactly
that bug.

**Three things deliberately not deferred — don't "finish the job":**

- **`http.client` / `ssl` / `socket` (~25 ms).** `_TRANSPORT_ERRS` is a module-level tuple built from
  `http.client.HTTPException`, so deferring these means restructuring the single HTTP chokepoint,
  whose reconnect, same-host-redirect and error-string behaviour each have a test and a comment
  saying not to disturb them. Not worth ~25 ms of an already-imperceptible startup.
- **`csv` (0.5 ms) and `datetime` (0.4 ms).** Below measurement noise; the extra import sites cost
  more in clarity than they return.
- **`shutil` (13–16 ms) cannot be deferred at all, and it is not imported by this file.** `argparse`
  imports it itself — 3.14's `HelpFormatter.__init__` reads the terminal width — roughly 40 times over
  during parser construction. It shows up as a top-level import in `importtime` output, which makes it
  look like this file's to fix. It isn't. Don't go hunting for it here.

A caution on method: wall-clock benchmarking by spawning subprocesses is noise-dominated at this
granularity. An early pass measured deferring `hashlib` as a *negative* saving. Use
`-X importtime`, which is deterministic, and treat subprocess timings as confirmation only.

## Self-update

The skill is distributed as a zip, so an install has no git remote and — before this — no way to know
it was stale: the version lives in the release tag and doesn't survive into an extracted tree. Two
pieces close that: `make-package.py` synthesizes a `VERSION.json` into every package (the one member
not from `git ls-files`), and `selfupdate` compares it against the latest public release.

- `selfupdate [--check] [--apply] [--force]` — **run once per session, alongside `connect`.** With no
  flags it reports one `state`; `--apply` installs a newer release over this one. `--force` ignores
  the day-long check cache and, with `--apply`, reinstalls the latest release even if this install
  already matches. Presentation rules are SKILL.md §0.5. **Exit code is always 0**, every state
  included — this runs at launch, and a non-zero exit from a housekeeping check reads as a broken
  skill.

| `state` | Meaning |
|---|---|
| `current` | Installed version matches the latest release |
| `outdated` | A newer release exists; `assetUrl` says what would be installed |
| `unknown` | **The check failed** — unreachable, proxied, or rate-limited. Never conflate with `current` |
| `disabled` | No update repo compiled in, or the user opted out |
| `dev` | A working tree or unstamped copy; `reasons` lists why it will never be overwritten |
| `ahead` | This build is newer than the release, so it is left alone (never a downgrade) |

Things not to change without reading why:

- **`UPDATE_REPO` is a compiled-in constant and must be set before the release that ships it.** An
  install can only self-update if the copy the user *already has* knows where to look — an empty
  constant means `disabled` and every user needs a second manual upgrade later. `MERIDIAN_UPDATE_REPO`
  overrides it for testing, validated to an `owner/name` pair: it picks a repo **on** github.com and
  cannot introduce a host. There is deliberately **no "update from this URL" variable** — that would
  be remote code execution for anything able to set an env var.
- **Four independent markers veto an apply, and any one is enough**: a symlinked install directory, a
  `.git` entry, a `CLAUDE.md` (excluded from every package, so its presence means a working tree), or
  a missing/uncomparable `VERSION.json`. `~/.claude/skills/meridiancs` is symlinked to a checkout on a
  maintainer's machine, so an updater that trusted its own path would delete the repo it was built
  from. The symlink test is `islink` **only** — it also compared `realpath` against `abspath` until
  that flagged every Windows install whose TEMP or profile path arrives in 8.3 short form
  (`C:/Users/FIRSTN~1/...`) as "a link to" its own resolved path. It failed safe and silently, which
  is the worst shape for a guard: the feature simply wouldn't work, and the reason read as deliberate.
- **A failed check is `unknown`, never `current`.** The cache stores that verdict too, but for one
  hour rather than a day (`UPDATE_RETRY_INTERVAL`), so a single outage can't hide a release for a
  whole day while the check reports success.
- **Nothing is fetched off github.com, and the Meridian token never goes near this path** — no
  `call()`, no Authorization header, no pooled connection, same discipline as `_post_webhook`. The
  `insecure_tls` opt-out explicitly does **not** apply: a self-signed appliance certificate is a
  reason to skip verification for that stack's API, not a licence to fetch executable code over an
  unverified connection.
- **The package is validated before a byte is extracted** — every member inside `meridiancs/`, no
  `..` or drive-qualified path (separators normalised *first*, or a backslash member walks straight
  past a forward-slash check on Windows), member-count and unpacked-size caps, the three required
  files present, and a `VERSION.json` matching the release tag. That last check is what stops a
  mislabelled release from installing and then reporting `current` forever, since the stamp is the
  only thing the next check reads.
- **The swap is staged and reversible**: download to a temp dir *beside* the install (same
  filesystem, so the move is a rename, not a copy), extract, run the staged copy's own `--help` as a
  smoke test, move the live tree aside, move the new one in, and only then delete the old — restoring
  it if the move-in fails. A half-updated skill is the worst outcome available here, and the smoke
  test is what turns "we replaced your skill with a broken zip" into a rejected package.
- **When the directory can't be renamed, its contents are swapped instead.** Windows refuses to
  rename a directory that is *any* process's working directory (WinError 32), and the likeliest such
  process is the assistant's own shell after `cd` into the skill. Every update on such an install used
  to fail with `applied: false`, and since §0.5 is silent on that, it failed every session and no
  release ever arrived. The fallback moves each entry to a fresh `.meridiancs-previous-*` directory
  beside the install, moves the new entries in, and moves everything back on any failure. If even
  that restore can't finish, the error names the backup directory and it is **never deleted**. The
  result's `swap` says which path ran (`directory` or `contents`). A process parked in a
  *subdirectory* (`scripts/`) still blocks that entry and rolls the update back.
- **After an apply, the scripts on disk are newer than the SKILL.md already in context.** The result
  carries a `note` saying the new instructions take effect next session; SKILL.md §0.5 requires that
  be surfaced. Without it, an update reads as behaviour changing for no reason.
- Opt-out is `MERIDIAN_NO_AUTOUPDATE=1` or `"autoupdate": false` in `config.json`. Read straight off
  disk rather than via `load_config()`, which `die()`s when no stack is configured — the check has to
  work before a user has connected to anything.

## How `top` picks its threshold

There is no server-side sort, so a correct top-N means reading **every** record above some threshold.
`top` finds that threshold by walking a ladder (`100000, 10000, 1000, 300, 100, 30, 10, 3, 1, 0`)
downward until enough records match, then sorts client-side. Two refinements keep the call count down:

- **Per-stack rung cache** (`~/.meridian/topcache.<fqdn>.json`, `{"<table>.<field>": rung}`) records
  the rung that worked, so later queries for the same field start there instead of probing
  `100000`/`10000` against a stack whose scores top out in the hundreds. A stale cache is never
  wrong — too high and it descends as before, too low and the refinement below tightens it. Delete
  the file to reset.
- **Speculative descent on a cold field.** With no cached rung the descent can walk most of the ladder,
  one full round trip per rung, so rungs are probed **three at a time** and the highest one that fills
  wins. The extra probes in the winning batch are the price of not waiting for them serially. With a
  *warm* cache the remembered rung usually matches on the first probe, so speculating would burn two
  calls every time for nothing — `batch = 1 if start > 0 else 3`. Measured on a cold field: 3 round
  trips instead of 8; the same field warm costs 1 probe.
- **Threshold refinement** past `TOP_REFINE_MIN` (500 records): the chosen rung can be far looser than
  needed, and every extra 100 records costs a call. `top` probes **three evenly-spaced points at once**
  in the gap between the chosen rung and the rung above it. That narrows the interval to a quarter,
  where three *sequential* bisections would reach an eighth — same call budget, one round trip instead
  of three, and the only thing the tighter bound buys is a page or two fewer later. Below
  `TOP_REFINE_MIN` it's skipped entirely, since refining can only save `pages - 1` fetches and on small
  tails that *loses* calls. Measured: `--top 250` went 8.9s/12 calls → 4.4–5.1s/9 calls, with the
  slightly looser threshold showing up as a 4-page tail instead of 3 (fetched concurrently anyway).

Beyond `TOP_MAX_PAGES` (20 pages / 2000 records) the result sets `truncated: true` and says so —
that is the one case where the answer may not be the true top-N.

> **Legacy BOM tolerance:** `config.json` and `topcache.<fqdn>.json` are read as `utf-8-sig`. Earlier
> versions of the skill shipped PowerShell helpers that wrote these files with `Set-Content
> -Encoding utf8`, which in PowerShell 5.1 emits a **BOM**; an upgraded install can still have a
> BOM'd file on disk. Reading it as plain `utf-8` throws inside a `try/except` that falls back
> silently — no error, just a cold cache or an unresolved credential. Keep the `utf-8-sig`.

## Aggregate result cache

The API has no aggregation endpoint and no field projection, so every aggregate is paid for by
downloading records. Measured on a live stack:

| | API calls | data pulled | wall | output |
|---|---:|---:|---:|---:|
| `connectors` (brief) | 5 | ~99 MB | 5.9s | 21 KB |
| `summary --by Risk_Level` | 11 | ~47 MB | 6.6s | 85 tokens |
| `summary --by sourcetype` | 33 | ~47 MB | 7.7s | 462 tokens |

33 calls does not fit the hard 60/min budget, so that last one was also measured spending **92.6s
inside the pacer**. All three describe a daily-cadence fact, so `summarize_connectors()` and
`summarize_by()` cache per stack in `rescache.<fqdn>.json`. Warm, measured end to end:

```text
connect --with-connectors   5369 ms -> 468 ms
summary --by Risk_Level     6636 ms -> 231 ms
summary --by sourcetype     7697 ms -> 242 ms
```

231 ms is bare interpreter start, so a cached read is effectively free.

`RESCACHE_TTL` is 900s for query aggregates and `RESCACHE_CONNECTOR_TTL` 3600s for the coverage block —
two values because the access patterns differ. A breakdown is re-asked inside one session (a
breakdown, then a follow-up on one of its groups), so minutes suffice. The coverage block is fetched
once per session by the mandatory preflight, so a sub-hour TTL would never hit the case that actually
costs: a new session started shortly after the last.

`--refresh` on `connect`/`connectors`/`summary`/`digest` bypasses; `MERIDIAN_NO_CACHE=1` disables
read and write entirely; `refresh-fields` drops the file.

**Five rules, each of which exists because breaking it is silent:**

- **Aggregates only, never record rows.** `list`/`top`/`profile` payloads are not cached — they carry
  the real customer PII (names, departments, leaked-credential counts, per-CVE detail) and CLAUDE.md's
  rule is that raw responses are not persisted. Written via `_private_write`, so 0600 and atomic: the
  connector warning messages alone contain customer hostnames.
- **A snapshot is measured, never served.** `take_snapshot` *and* `cmd_digest --snapshot` pass
  `refresh=True`. A history row is what `trend` later compares, so a cached breakdown written under
  today's date invents a flat segment out of a value never measured that day — per `design/trends.md`
  the most convincing wrong answer this tool can produce. `digest --snapshot` is the scheduled path, so
  it is the one that would have accumulated the damage unnoticed. Both are guarded, and both guards
  were verified to fail against the unfixed code.
- **A partial connector fetch is never cached.** If either endpoint errored, storing the result would
  let one transient 403 report a connector-less stack for a full hour — the trap `_LABELS_PROVISIONAL`
  already exists for, where an unreadable half is indistinguishable from an empty one.
- **A breakdown with no completeness verdict is not cached** (`complete` absent = the coverage check
  could not run). Caching it pins an unanswered question in place for the TTL; re-running may answer it.
- **Every served entry is stamped** with `fromCache`/`cacheAgeSeconds`, and a negative age (a clock
  moved backwards) is treated as a miss rather than a hit whose staleness cannot be stated. SKILL.md
  requires surfacing the age when the answer is time-sensitive.

`MERIDIAN_NO_CACHE=1` is set for the whole offline suite. The cache short-circuits *before* `call()` is
reached, so a fixture-driven test on a machine with a real cache file was served the operator's live
stack instead of its fixture — the suite silently stopped being deterministic, which is the one
property CI depends on. Only `test_result_cache` turns it back on, around a temp `CFG_DIR`.

## JSON output

Every verb prints through `jout()`, which emits **compact** JSON (`separators=(",", ":")`). The
consumer is the model that invoked the verb, and indentation carries nothing a JSON parser or a model
reads — it was measured at **36% of output**: the session preflight's own payload was 75,288 chars
indented against 48,104 compact, i.e. ~7,500 tokens of pure whitespace on the one call SKILL.md makes
mandatory. `MERIDIAN_PRETTY_JSON=1` restores `indent=2` for reading output by hand.

Two things this does **not** change:

- **Files keep `json.dump(..., indent=2)`** — config, the per-stack caches and snapshot history are
  read and diffed by people far more often than they are re-parsed, and they cost no context.
- **CSV mode is untouched.** `emit()` still prints the JSON envelope alongside the rows (to stderr
  when the CSV goes to stdout), because a spreadsheet cannot carry `truncated` or `unaccountedRecords`.

Add new output through `jout()` rather than a bare `print(json.dumps(...))`, or that verb quietly
becomes the expensive one again.

## Rate limiting

`meridian.py` self-paces every request through `pace()`, a file-based token bucket
(`~/.meridian/.ratelimit`) that keeps all callers under the API's hard 60/min — so route HTTP through
the script rather than calling `curl` or `Invoke-WebRequest` directly. `pace()` holds a
`threading.Lock` because it's a read-modify-write on a file and the concurrent verbs (`parallel()`)
would otherwise race each other.

**Don't add per-call `time.sleep` on top of it.** The pacer is the single throttle and only sleeps
when the 60/min bucket is actually full; extra fixed sleeps in the multi-call verbs (`summary`,
`list`, `top`, `check`) are pure added latency and were removed for that reason. The `time.sleep(2)`
inside the request-retry paths is **backoff, not pacing** — leave it alone.

## Concurrency

Independent calls go through `parallel()` (stdlib threads, 8 workers max). Anything that doesn't need
a previous answer should not wait for one:

| Verb | What runs together |
|---|---|
| `profile` | linked-asset query + change log; exact-key + fuzzy name lookup |
| `connectors` | the two connector endpoints |
| `connect --with-connectors` | validation + both connector endpoints |
| `summary --by` | discovery pages 1..N; then every per-value count |
| `summary --metrics` | the metrics and license GETs |
| `top` | every page of the qualifying tail |
| `list` | every page needed to fill `--limit` |
| `check` | all four capability probes |

A 100-record page is ~1.1MB of 312-field records and costs ~2.3s against a remote stack, so serial
paging dominated everything.

> **Page size is a per-stack fact, not a constant — don't treat the ~1.1MB above as a ceiling.** The
> figures in this file and in CLAUDE.md were measured on a stack of ~34k assets / ~10k users. Measured
> on a different one (26.7k assets / **66k** users): the same 100-record request returns **5.87 MB**
> across **264** populated fields — 5.3x larger, ~61.5 KB per record. Record width tracks how many
> connectors populate how many fields, so a heavily-integrated stack is wider. Anything reasoning about
> bytes should scale from a live measurement rather than quoting a number from here: on that stack a
> 20-page `top` tail moves ~117 MB rather than ~22 MB, and `list --all` at `LIST_MAX_RECORDS` is
> ~294 MB rather than ~55 MB. The *call*-count reasoning behind `LIST_MAX_RECORDS` is unaffected — that
> is a 60/min budget fact, and pages cost one call each whatever their size. Measured before → after: `summary --by Risk_Level` 17.7s → 6.6s,
`check` 2.6s → 1.5s. `top`'s pages now overlap, which leaves its **serial threshold probes** as that
verb's remaining cost (~6s of a 8.9s run) — the bisection steps genuinely depend on each other, but
the fixed-ladder descent could be probed speculatively.

Two constraints worth knowing:

- **`pace()` still applies.** Going 8-wide consumes the 60/min budget faster; it doesn't exceed it. A
  wide fan-out on a large operation will hit the bucket and sleep, so it gets faster, not unbounded.
- **Issuing two *processes* concurrently is slower, not faster** — 4.5s against 3.2s measured, from
  TLS handshake and interpreter-start contention. Parallelize calls inside one process.

`parallel()` returns exceptions as values rather than raising, so each caller decides: `profile`
treats a failed change log as optional, while `top` and `list` re-raise, because a silently missing
page shortens a ranking and that is how a top-N goes quietly wrong.

## Transport

Requests go over `http.client` on a **pooled keep-alive connection**, not `urllib.urlopen`. The pool
is keyed on `(fqdn, tls-posture)`, so a `stacks switch` or an `insecure_tls` change can never hand back
a connection to the wrong host or with the wrong verification. A *pool* rather than thread-locals is
what captures the win: `parallel()` builds a new `ThreadPoolExecutor` per batch, so thread-local
connections would be discarded on every fan-out.

- **Stale sockets are absorbed, not retried.** A pooled connection can be closed by the server between
  calls, surfacing as a transport error on the *next* request. `_round_trip` reconnects once for that
  before giving up; it isn't the caller's `retries` budget.
- **Redirects are followed same-host only, max 3 hops.** `urlopen` followed them transparently and
  `http.client` doesn't, so a trailing-slash redirect would otherwise read as a mystery failure. An
  off-host redirect is **refused** — these headers carry the API token and it must not be replayed to
  another host. Tested.
- **The error string shape is load-bearing**: `classify_connect_error()` parses `HTTP <code>` out of
  it, so keep `"HTTP %s: %s"`.

Measured effect, on top of the concurrency work: `check` 1.48s → 1.05s, `summary --by` 6.6s → 5.8s,
`--metrics` 1.12s → 0.74s. Part is the ~79ms handshake saved per reused connection, part is
`http.client` being lighter than `urllib.request`'s opener-and-handler machinery.

## TLS

Certificate verification is **on by default**, via a module-level cached `SSLContext` (building one
per call re-read the system trust store; the handshake itself measures ~79ms). Verification was
previously disabled outright to match the retired PowerShell helpers' `-SkipCertificateCheck`
posture — this client sends a bearer token and receives customer PII, so that default is not
defensible now the reason is gone.

A stack with a self-signed or internally-issued certificate opts out with `MERIDIAN_INSECURE_TLS=1`
or `"insecure_tls": true` in `config.json`. Both `connect` and `check` report `tlsVerified`, and
`connect` adds a `tlsWarning` when verification is off, so an opt-out saved once cannot quietly
become the permanent posture. A rejected certificate is **not** retried — it is a trust problem, not
a blip — and the error names both opt-out mechanisms.

## Maintenance tools (not verbs)

These are release/packaging scripts, not something a user's question ever routes to — moved here
because this file's audience ("editing a helper") is already the audience for these, not the ordinary
verb-flag lookups `scripts.md` serves.

- `scripts/make-impact.py` — regenerates `Cyderes-Meridian-Skill-Enhancements.pdf`, the branded
  change/impact summary (what shipped and why it matters commercially). Refresh it when a batch of
  work lands. Its figures are **measured, not estimated** — re-measure before changing any of them.
  Stakeholder-facing, so the same no-customer-data rule applies.
- `scripts/make-guide.py` — regenerates the distributed
  `Cyderes-Meridian-Skill-Guide.pdf`: a branded overview plus the 7-step install walkthrough handed
  to new users. Run `python scripts/make-guide.py` after changing the install flow or the skill's
  capabilities, and commit the resulting PDF. It reuses the same brand helpers and headless-Chrome
  print path as `report`, so it needs a Chromium browser. **Keep it free of customer data** — it
  ships in the repo, so use the `<person>` placeholder rather than a real name from any stack
  (`*.pdf` is gitignored precisely because report PDFs are not safe to commit; this one file is
  carved out by name).
- `scripts/make-brief.py` — regenerates `Cyderes-MeridianCS-Brief.pdf`, the **customer- and
  executive-facing** brief: what the skill does, the business value per seat, and what asking it
  actually looks like. Regenerate whenever a customer-visible capability lands. It ships in the
  package and goes outside Cyderes, so its sample prompts use the `<person>` placeholder and its
  claims stay the measured ones from the impact doc — the hand-made original had no generator, which
  is how it fell behind the skill it describes.
- **All three doc generators take an optional output path, and it must end in `.pdf`.** They
  overwrite the target without asking -- headless Chrome's `--print-to-pdf` clobbers whatever is
  there and still exits 0 -- so `make-brief.py SKILL.md` used to replace SKILL.md with a 134KB PDF,
  silently. `docout.resolve_out` now refuses a non-`.pdf` target, a directory, and a missing parent
  directory, exiting 2 with a specific message. Overwriting an existing `.pdf` is still allowed:
  that is the regenerate case. Snyk reports this argument as Path Traversal (18 LOW findings) -- it
  is not; the operator names their own output file. The overwrite was the real bug.
- `scripts/make-package.py` — rebuilds `meridiancs.v<X.Y.Z>.skill.zip`, the distributable package.
  `--version` is required (nothing in the tree stores one, and the zip is built before the release tag
  exists, so `git describe` would name the *previous* release); it self-derives only from a tag on a
  clean HEAD. The output name must end `.skill.zip` or the script exits, since `.gitignore` matches that
  pattern and a package outside it becomes committable. **Rerun it
  after any change to the skill's behaviour** and re-upload wherever the zip is published; it is
  gitignored (a build artifact, not source), so nothing in the repo reminds you it went stale — which
  is exactly how it once shipped two PRs behind. Contents come from `git ls-files` plus exactly one synthesized member, `VERSION.json` (see
  Self-update above -- it is what lets an install know whether it is stale), so an untracked file
  can't be packaged by accident and generated report PDFs (untracked by policy)
  can't ride along in a file handed to someone else. `EXCLUDE`/`EXCLUDE_DIRS` drop the files that
  maintain the skill rather than run it (`CLAUDE.md`, `evals/`, the three generators). Refuses a
  dirty tree unless given `--allow-dirty`, and writes fixed member order plus fixed timestamps so an
  unchanged tree rebuilds byte-identically.
