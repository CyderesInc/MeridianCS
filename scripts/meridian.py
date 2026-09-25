#!/usr/bin/env python3
"""
Meridian (Lucidum) API v2 CLI for Mac/Linux/Windows - the skill's only runtime.
Standard library only (no pip installs).

Config resolution:
  1. env MERIDIAN_FQDN / MERIDIAN_API_TOKEN / MERIDIAN_ACTION_TOKEN
  2. ~/.meridian/config.json  {"fqdn":..., "api_token":..., "action_token":...}

Verbs:
  connect        onboarding preflight: meridian.py connect [--with-connectors]  (config + validate)
  connectors     data coverage:       meridian.py connectors  (enabled / succeeding / ingesting)
  api            raw call:            meridian.py api /CMDB/v2/system/metrics/data
                                      meridian.py api /CMDB/v2/data/cmdb -X POST --body-file q.json
  refresh-fields cache stack fields:  meridian.py refresh-fields [--search risk]
  top            top-N by number:     meridian.py top --table user --field Risk_Score --top 5 [--where "F op T v"]
  list           filtered set:        meridian.py list --table asset --where "OS match String Windows XP" [--count-only]
  summary        group-by / posture:  meridian.py summary --table asset --by Risk_Level   |   summary --metrics
  profile        one entity:          meridian.py profile --name "Firstname L" [--type asset]
  compare        two entities:        meridian.py compare --name1 AEXAMPLE --name2 BEXAMPLE
  check          token capability:    meridian.py check
  selfupdate     is this skill current? meridian.py selfupdate [--apply]  (public release; run at launch)
  labels         the stack's own SmartLabels, and what the customer says each one means [--refresh]
  digest         one-command periodic posture review; pipe to `report` for the branded PDF
                 meridian.py digest --snapshot   also appends to local history, at no extra API cost
  snapshot       append today's counts to ~/.meridian/snapshots.<fqdn>.jsonl (the API keeps no history)
                 [--entities top500:asset:Risk_Score] adds per-entity scores, ids salted-hashed by default
  snapshots      inspect/prune history:  meridian.py snapshots list | prune [--keep 400]
  trend          compare two snapshots:  meridian.py trend [--since 2026-07-01] [--by Risk_Level]
  metrics        named counts captured on every snapshot (one API call each), for trending:
                 meridian.py metrics list | add --name kev --label "KEV exposed" --where "..." | rm kev
  stacks         multiple stacks:     meridian.py stacks list | add --name prod --fqdn F --token T | switch prod | rm prod

`top`, `list` and `summary` also take --format csv [--out FILE]; the JSON envelope still prints, so the
completeness caveats survive into whatever reads the CSV.

Numeric (Float/Integer) values in --where are sent as real JSON numbers automatically.
Output is JSON on stdout.
"""
import argparse, csv, datetime, http.client, json, os
import socket, ssl, sys, threading, time
# Only urllib.parse: urllib.request/.error were leftovers from the pre-pool urlopen transport, and
# urllib.request alone drags in email/base64/etc -- ~20-40ms of startup paid by every verb.
import urllib.parse, re
# Deferred to their call sites, same discipline as urllib.request/smtplib/shutil. Measured with
# `python -X importtime` on this file: concurrent.futures 25.4ms, hashlib 16.0ms, difflib 8.0ms,
# secrets 4.0ms -- ~53ms of every verb's startup, for modules that most verbs never touch.
# `concurrent.futures` is used in exactly one place (parallel()); a cached `summary`/`connectors`
# read returns before reaching it. `difflib` only formats a did-you-mean on an error path, `secrets`
# only mints an entity salt, and `hashlib` only hashes a cache key or an entity id.
#
# NOT deferred, deliberately:
#   * `http.client`/`ssl`/`socket` (~25ms) -- `_TRANSPORT_ERRS` is a module-level tuple built from
#     `http.client.HTTPException`, so deferring these means restructuring the one HTTP chokepoint,
#     whose reconnect, same-host-redirect and error-string behaviour all have tests and comments
#     saying not to disturb them. Not worth ~25ms of an already-imperceptible startup.
#   * `csv` (0.5ms) and `datetime` (0.4ms) -- below noise; the extra import sites cost more in
#     clarity than they return.
#   * `shutil` -- already lazy here, but `argparse` imports it itself (3.14's HelpFormatter reads the
#     terminal width), 40 times over during parser construction. Its 16.4ms is unavoidable while
#     this CLI uses argparse; don't go looking for it in this file.

# Windows consoles default to cp1252, which can't encode the severity dots the `--ascii` views and
# `top`/`summary` labels use - printing one raised a charmap error instead of a graph. JSON output was
# unaffected (json.dumps escapes non-ASCII), which is why this hid for so long.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa - a redirected or exotic stream isn't worth failing over
        pass

HOME = os.path.expanduser("~")
CFG_DIR = os.path.join(HOME, ".meridian")
CFG_PATH = os.path.join(CFG_DIR, "config.json")
STACKS_PATH = os.path.join(CFG_DIR, "stacks.json")  # registry of named stacks (multi-stack support)
RL_PATH = os.path.join(CFG_DIR, ".ratelimit")
RL_LIMIT = 55
# Ceiling on how long pace() will wait for a slot before proceeding with a warning. A legitimately
# full budget clears within one 60s window, so anything past ~2 of them is a broken stamps file, not
# back-pressure -- and an unbounded wait is a silent hang rather than a slow command.
PACE_MAX_WAIT = 125.0
LOCK_MAX_WAIT = 5.0   # then run unlocked; see _FileLock


def jout(payload, stderr=False):
    """The single stdout JSON writer. Compact by default -- measured, indentation was 36% of output.

    Every verb's result is read by the model that invoked it, not by a human scrolling a terminal, and
    indentation carries no information a JSON parser or a model uses. On the session preflight
    (`connect --with-connectors`) the same payload measured 75,288 chars indented against 48,104
    compact -- 27,184 chars, ~7,500 tokens, of pure whitespace on the one call SKILL.md makes
    mandatory. Set MERIDIAN_PRETTY_JSON=1 to get the indented form back when reading output by hand.

    Files keep their `json.dump(..., indent=2)`: config, caches and snapshot history are read by
    people (and diffed) far more often than they are re-parsed, and they cost no context.
    """
    if os.environ.get("MERIDIAN_PRETTY_JSON") == "1":
        text = json.dumps(payload, indent=2)
    else:
        text = json.dumps(payload, separators=(",", ":"))
    print(text, file=sys.stderr if stderr else sys.stdout)


_CFG_CACHE = None


def load_config():
    """Resolve credentials once per process. `mirror_active_to_config` clears the cache, since a
    stack switch rewrites config.json mid-process and the validation that follows must see it."""
    global _CFG_CACHE
    if _CFG_CACHE is not None:
        return _CFG_CACHE
    fqdn = os.environ.get("MERIDIAN_FQDN")
    api_token = os.environ.get("MERIDIAN_API_TOKEN")
    action_token = os.environ.get("MERIDIAN_ACTION_TOKEN")
    if os.path.exists(CFG_PATH):
        with open(CFG_PATH, encoding="utf-8-sig") as f:  # BOM: written by the retired PS helpers pre-v2.2
            c = json.load(f)
        fqdn = fqdn or c.get("fqdn")
        api_token = api_token or c.get("api_token")
        action_token = action_token or c.get("action_token")
    if not fqdn:
        die("No Meridian FQDN configured (env MERIDIAN_FQDN or ~/.meridian/config.json).")
    fqdn = re.sub(r"^https?://", "", fqdn).rstrip("/")
    _CFG_CACHE = (fqdn, api_token, action_token)
    return _CFG_CACHE


# --- Multi-stack registry -------------------------------------------------------------------
# All named stacks live in ~/.meridian/stacks.json:
#   {"active": "prod", "stacks": {"prod": {"fqdn":..,"api_token":..,"action_token":..}, "demo": {..}}}
# config.json stays the single "active" credential set every helper already reads; switching a
# stack just mirrors that stack into config.json, so the other stacks are never overwritten.

def _clean_fqdn(fqdn):
    return re.sub(r"^https?://", "", fqdn or "").rstrip("/")


def _stack_name_from_fqdn(fqdn):
    """Derive a sensible default stack name from an FQDN (first label), e.g. company.lucidum.cloud -> company."""
    base = _clean_fqdn(fqdn).split(".")[0]
    return re.sub(r"[^A-Za-z0-9_-]", "", base) or "default"


def load_stacks():
    """Return the stack registry {active, stacks}. If stacks.json doesn't exist yet, migrate the
    legacy single config.json into a named entry (in memory; persisted on the first save)."""
    if os.path.exists(STACKS_PATH):
        try:
            with open(STACKS_PATH, encoding="utf-8-sig") as f:
                reg = json.load(f)
        except Exception:
            reg = {}
        if not isinstance(reg, dict):
            reg = {}
        reg.setdefault("stacks", {})
        reg.setdefault("active", None)
        return reg
    reg = {"active": None, "stacks": {}}
    if os.path.exists(CFG_PATH):
        try:
            with open(CFG_PATH, encoding="utf-8-sig") as f:
                c = json.load(f)
        except Exception:
            c = {}
        if c.get("fqdn") and c.get("api_token"):
            name = _stack_name_from_fqdn(c.get("fqdn"))
            entry = {"fqdn": _clean_fqdn(c["fqdn"]), "api_token": c.get("api_token"),
                     "action_token": c.get("action_token")}
            # The migration must carry everything the mirror carries, or the first `stacks add` on
            # an upgraded install strips it from the registry and the next switch strips it from
            # config.json. For entity_salt that loss is unrecoverable -- every earlier entity
            # snapshot would read as appeared-and-disappeared under a regenerated salt.
            if c.get("entity_salt"):
                entry["entity_salt"] = c.get("entity_salt")
            if c.get("insecure_tls"):
                entry["insecure_tls"] = True
            reg["stacks"][name] = entry
            reg["active"] = name
    return reg


_CFG_DIR_CHECKED = False


def _ensure_cfg_dir():
    """Create ~/.meridian owner-only, and tighten it if it already exists looser than that.

    makedirs(mode=0o700) only applies at creation, so a directory made by anything else -- an
    editor, a file tool writing config.json by hand, an old umask -- stays 0755 forever, and every
    cache written into it with a plain open() (fields, labels, metrics, topcache, .ratelimit) is
    then readable by every local user. Hardening the directory once covers all of them at the one
    choke point, rather than relying on each writer to remember. POSIX only: on Windows chmod just
    toggles read-only and the profile directory's ACLs are the protection.
    """
    global _CFG_DIR_CHECKED
    os.makedirs(CFG_DIR, mode=0o700, exist_ok=True)
    if _CFG_DIR_CHECKED or os.name == "nt":
        return
    _CFG_DIR_CHECKED = True
    try:
        if os.path.islink(CFG_DIR):
            return   # someone else's layout; don't chmod through a link we didn't create
        if os.stat(CFG_DIR).st_mode & 0o077:
            os.chmod(CFG_DIR, 0o700)
    except OSError:
        pass


def _private_write(path, payload):
    """Write a token-bearing JSON file the only acceptable way: owner-only, and atomically.

    A plain open(path, "w") has two failure modes this file cannot afford. Under the default POSIX
    umask it lands 0644, so every local user can read every saved stack's bearer token -- and a
    temp-file rewrite with default perms silently UNDOES a chmod 600 the operator applied by hand.
    And truncate-then-write means a crash or full disk mid-dump costs the credentials themselves.
    So: perms are set on the temp file at creation (no world-readable window, however brief) and
    os.replace makes the swap atomic. On Windows os.open's mode only toggles read-only -- the
    profile directory's ACLs are the protection there -- so this is effectively POSIX hardening.
    """
    _ensure_cfg_dir()
    # PID in the temp name, not a fixed ".tmp": a scheduled `snapshot --entities` calling
    # entity_salt() concurrently with an interactive `stacks switch` had both processes truncating
    # and replacing the SAME temp path, and interleaved writes of different lengths land as invalid
    # JSON -- which load_config swallows, so the user just sees "not configured" and re-adds a stack.
    # PID *and* thread id: entity_salt() and mirror_active_to_config can both run inside one
    # process's parallel() fan-out, so a PID-only name collides with itself and O_EXCL fails.
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    # O_NOFOLLOW where it exists (not on Windows): this path is fully attacker-controlled if
    # ~/.meridian was ever created under a lax umask, since makedirs(mode=0700) only applies at
    # creation and an upgraded install keeps the old mode.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(tmp, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            # The atomic-swap promise is only worth anything if the DATA is durable before the
            # rename. Without this, a power loss can persist the rename ahead of the contents and
            # leave a zero-length config.json -- precisely the credential loss this claims to prevent.
            f.flush()
            os.fsync(f.fileno())
        # Windows raises PermissionError (WinError 5) when the destination is momentarily held --
        # another writer replacing it, or a reader with it open. Measured: two threads rewriting
        # config.json 15x each hit it. The swap stays atomic (the file is never half-written), but
        # without a retry one write just fails, and every caller here swallows exceptions -- so a
        # rotated token or a fresh salt would silently not land. POSIX rename has no such window.
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if os.name != "nt" or attempt == 19:
                    raise
                time.sleep(0.02)
    except Exception:
        # Never leave a partial token blob lying around under a name nothing will clean up.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_stacks(reg):
    # The registry holds EVERY stack's token, and it used to get a plain truncate-then-write --
    # while config.json two functions away was protected for exactly this reason.
    _private_write(STACKS_PATH, reg)


def mirror_active_to_config(reg):
    """Write the active stack's credentials into config.json so every existing helper picks them up.

    Only the keys this registry OWNS are replaced. config.json is a documented hand-editable file --
    `call()`'s own TLS error tells the operator to put `"insecure_tls": true` in it -- and this
    function fully rewrites it, so a from-scratch payload silently discarded anything set by hand.
    That got worse when `stacks add` on the active stack started re-mirroring: rotating a token
    would drop a hand-set insecure_tls, and the next call would fail cert verification with no clue
    why. A hand-set posture is also promoted INTO the registry entry, so it survives the next switch
    rather than living only until something else writes the file.
    """
    global _CFG_CACHE
    name = reg.get("active")
    if not name or name not in reg.get("stacks", {}):
        return
    s = reg["stacks"][name]
    prior = {}
    try:
        with open(CFG_PATH, encoding="utf-8-sig") as f:
            prior = json.load(f) or {}
    except Exception:
        prior = {}
    same_stack = _clean_fqdn(prior.get("fqdn") or "") == _clean_fqdn(s.get("fqdn") or "")
    if same_stack and prior.get("insecure_tls") and not s.get("insecure_tls"):
        # Hand-set on this stack: keep it, and persist it into the registry so it survives the next
        # switch instead of living only until something else rewrites config.json. Saved here
        # because callers save the registry BEFORE mirroring, so an in-memory flag would be lost.
        s["insecure_tls"] = True
        try:
            save_stacks(reg)
        except Exception:
            pass   # promotion is a convenience; the mirror below still honours the posture
    # Keys the registry doesn't model, carried through rather than dropped -- but only when
    # config.json is still pointing at THIS stack. Mirroring a different stack must not inherit the
    # previous one's settings.
    payload = {k: v for k, v in prior.items() if same_stack} if same_stack else {}
    payload.update({"fqdn": s.get("fqdn"), "api_token": s.get("api_token")})
    payload.pop("action_token", None)
    if s.get("action_token"):
        payload["action_token"] = s.get("action_token")
    if s.get("entity_salt"):
        # Carried across a switch, per stack. This rewrite is a full replacement, so leaving the salt out
        # would silently regenerate it on the next entity snapshot -- and a new salt makes every earlier
        # entity record incomparable, which reports the whole population as appeared-and-disappeared.
        payload["entity_salt"] = s.get("entity_salt")
    # TLS posture is per stack too (one self-signed lab stack among verified ones). The mirror used
    # to drop it, so any switch-away-and-back reverted the lab stack to strict verification and its
    # next call failed -- fail-closed, but a standing setting shouldn't evaporate. Explicitly
    # removed when the stack doesn't carry it, so switching to a verified stack can't inherit it.
    payload.pop("insecure_tls", None)
    if s.get("insecure_tls"):
        payload["insecure_tls"] = True
    _private_write(CFG_PATH, payload)
    _CFG_CACHE = None  # the active credentials just changed; force a re-read


# --- `top` threshold ladder + per-stack rung cache ------------------------------------------
# Risk_Score and friends are unbounded and vary wildly per stack, so `top` finds a threshold by
# walking this ladder down until enough records match. Remembering the rung that worked skips the
# dead probes at the top of the ladder on every later query for the same field.

# Half-decade rungs all the way down. 3000 and 30000 were missing, and on a stack whose records run
# to megabytes the gap was the cost: the 1000 rung's tail page ran past 100MB where the next
# half-decade rung matched a third as many records. A rung is stored by value, not index, so a cache
# written against the old ladder still resolves.
TOP_LADDER = [100000, 30000, 10000, 3000, 1000, 300, 100, 30, 10, 3, 1, 0]
TOP_LOOSE_FACTOR = 3  # a warm hit this many times over `top` probes one rung up; see cmd_top
TOP_MAX_PAGES = 20   # hard ceiling on records read for one top-N (100 per page)
TOP_REFINE_MIN = 500  # only tighten the threshold past this many records; see cmd_top for the math

# Pages of records `summary --by` reads to discover which distinct values exist. The API has no
# aggregation endpoint and no field projection (tested: `fields`/`selectFields`/`columns` are all
# silently ignored, so every page is ~1.1MB of 312-field records), so discovery has to sample. This
# was 5 serial pages (~11.8s); the pages now go out together, so a wider sample costs no extra wall
# time. cmd_summary reports exactly how many records a breakdown failed to account for, so an
# insufficient sample is visible rather than silent.
SUMMARY_SAMPLE_PAGES = 8
# Most groups `summary --by` will count exactly. Each one is an API call, so this is a rate-limit
# bound. Past it the biggest values the sample saw are counted and the coverage check states how many
# records the rest hold -- it used to return no groups at all, which made "break assets down by OS"
# (51 values) unanswerable.
SUMMARY_MAX_GROUPS = 40

# Ceiling on records one `list` call will read. A page is 100 records, ~1.1MB and one API call, so 50
# pages is most of the hard 60/min budget -- past this, paging on would stall behind the rate limiter
# for minutes and look like a hang. `list` reports the ceiling and says to narrow instead.
LIST_MAX_RECORDS = 5000

# `/CMDB/v2/system/metrics/connector?size=2000` was assumed to return the whole run history in one
# page ("the default page is 20 of ~30 pages"), but that's a fact about page *size*, not history
# *depth* -- a busy stack (35 AWS services x 20+ profiles, run daily) exceeded 2000 rows within 3
# days and needed 4 pages for 7,425 total. The API returns this endpoint unsorted, so an under-fetch
# doesn't drop the oldest rows, it drops an arbitrary slice -- on the stack that surfaced this, page 0
# alone happened to hold only the 3 *oldest* days, so every connector's "last ingest" read up to 5
# days stale while a persistent nightly failure looked like an old, resolved one. Cap pagination
# rather than fetch unboundedly: 20 pages (40,000 rows) is still a fraction of the 60/min budget.
CONNECTOR_RUNS_MAX_PAGES = 20


def _top_cache_path():
    # One cache file per stack, keyed on a filename-sanitized fqdn.
    fqdn, _, _ = load_config()
    return os.path.join(CFG_DIR, "topcache.%s.json" % re.sub(r"[^A-Za-z0-9._-]", "_", fqdn))


def _top_rung_start(table, field):
    """Ladder index to begin probing at, from the last successful run. 0 (the top) if unknown."""
    try:
        # utf-8-sig: an install upgraded from <=v2.1 may have a cache written by the retired PS
        # helpers, whose `Set-Content -Encoding utf8` emitted a BOM under PowerShell 5.1. Reading
        # it as plain utf-8 throws, which would silently start cold instead of warm.
        with open(_top_cache_path(), encoding="utf-8-sig") as f:
            rung = json.load(f).get("%s.%s" % (table, field))
        return TOP_LADDER.index(rung) if rung in TOP_LADDER else 0
    except Exception:  # missing, unreadable, or written by an older version - just start at the top
        return 0


def _top_rung_save(table, field, rung):
    """Remember the rung that worked. Best-effort: a cache write must never fail a query."""
    p = _top_cache_path()
    try:
        data = {}
        if os.path.exists(p):
            with open(p, encoding="utf-8-sig") as f:  # may carry a legacy BOM; see _top_rung_start
                data = json.load(f)
        if data.get("%s.%s" % (table, field)) == rung:
            return
        data["%s.%s" % (table, field)] = rung
        _ensure_cfg_dir()
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


# --- aggregate result cache ---------------------------------------------------------------------
#
# The API has no aggregation endpoint and no field projection, so every aggregate is paid for by
# downloading records. Measured on a live stack: one 100-record page of /data/cmdb is 5.87 MB (264
# fields, ~61.5 KB per record), and the connector run history is 24.8 MB per page over 4 pages. That
# makes the cheap-looking verbs expensive:
#
#   connectors (brief)        5 calls   ~99 MB   5.9s  ->  21 KB of output
#   summary --by Risk_Level  11 calls   ~47 MB   6.1s  ->  85 tokens of output
#   summary --by sourcetype  33 calls   ~47 MB          ->  and 92.6s of pacer wait, because 33 calls
#                                                           against a hard 60/min budget cannot fit
#
# None of those inputs change more than once a day: the stack ingests on a daily cadence. So the
# aggregate is cached per stack and re-read within a TTL.
#
# TWO RULES, both load-bearing:
#
# 1. **Aggregates only -- never record rows.** Counts, breakdowns and connector health are cached;
#    `list`, `top` and `profile` payloads are not. Those carry the real customer PII (names,
#    departments, leaked-credential counts, per-CVE detail) and CLAUDE.md's rule is that raw responses
#    do not get persisted. The cache is also 20x smaller for it.
# 2. **Every served entry is stamped, and nothing pretends to be fresher than it is.** A cached
#    payload carries `cachedAt`/`cacheAgeSeconds`, and SKILL.md requires surfacing the age when the
#    answer is time-sensitive. A silently-stale breakdown presented as current is the same failure
#    class as a number quoted from a dead connector: nothing about it looks partial.
#
# Two TTLs rather than one, because the two access patterns differ. Query aggregates are re-asked
# inside a single session (a breakdown, then a follow-up on one of its groups), so minutes is enough.
# The connector block is fetched once per session by the mandatory preflight, so a TTL under an hour
# would never hit on the case that actually costs -- a new session started shortly after the last.
RESCACHE_TTL = 900             # 15 min: query aggregates (summary --by, bare counts)
RESCACHE_CONNECTOR_TTL = 3600  # 1 hour: the connector/coverage block
RESCACHE_MAX_ENTRIES = 200     # keep the file small; oldest-first eviction
RESCACHE_SCHEMA = 1


def _rescache_path():
    fqdn, _, _ = load_config()
    return os.path.join(CFG_DIR, "rescache.%s.json" % re.sub(r"[^A-Za-z0-9._-]", "_", fqdn))


def _rescache_key(kind, **parts):
    """A stable key for a cacheable aggregate. Sorted, so argument order cannot split the cache."""
    payload = json.dumps([kind, sorted((k, parts[k]) for k in parts)], sort_keys=True, default=str)
    import hashlib
    return "%s:%s" % (kind, hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20])


def _rescache_read():
    try:
        # utf-8-sig for the same reason as topcache/config: an install upgraded from <=v2.1 can have
        # BOM'd files on disk, and reading one as plain utf-8 throws inside a silent except.
        with open(_rescache_path(), encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if data.get("schema") == RESCACHE_SCHEMA else {}
    except Exception:
        return {}


def rescache_disabled():
    """MERIDIAN_NO_CACHE=1 turns the aggregate cache off entirely, read and write.

    Exists for two reasons. An operator who wants every answer measured (a compliance snapshot, a
    hand-verified figure) needs one switch rather than a --refresh per verb. And the offline test suite
    needs it: the cache short-circuits *before* `call()` is reached, so a fixture-driven test on a
    machine with a real cache file was served the operator's live stack instead of its fixture -- the
    suite silently stopped being deterministic, which is the one property CI depends on.
    """
    return os.environ.get("MERIDIAN_NO_CACHE") == "1"


def rescache_get(kind, ttl, **parts):
    """A cached aggregate plus its age in seconds, or (None, None). Never raises."""
    if rescache_disabled():
        return None, None
    try:
        rec = (_rescache_read().get("entries") or {}).get(_rescache_key(kind, **parts))
        if not isinstance(rec, dict):
            return None, None
        age = time.time() - (rec.get("at") or 0)
        # A negative age means a clock moved backwards; treat it as a miss rather than serve an entry
        # whose staleness cannot be stated, since the whole contract here is an honest stamp.
        if 0 <= age < ttl:
            return rec.get("value"), int(age)
    except Exception:
        pass
    return None, None


def rescache_put(kind, value, scope=None, **parts):
    """Store an aggregate. Best-effort: a cache write must never fail a query.

    `scope` is an optional second key naming what the entry answers WITHOUT the parts that version it
    (the rebuild stamp, for summary --by), so rescache_scope_live() can tell a guaranteed miss before
    the version is known."""
    if rescache_disabled():
        return
    try:
        data = _rescache_read() or {}
        entries = data.setdefault("entries", {})
        rec = {"at": time.time(), "kind": kind, "value": value}
        if scope is not None:
            rec["scope"] = scope
        entries[_rescache_key(kind, **parts)] = rec
        if len(entries) > RESCACHE_MAX_ENTRIES:
            for k in sorted(entries, key=lambda k: entries[k].get("at") or 0)[:len(entries) - RESCACHE_MAX_ENTRIES]:
                entries.pop(k, None)
        data["schema"], data["fqdn"] = RESCACHE_SCHEMA, load_config()[0]
        # Same atomicity as the token file: this is a read-modify-write, and concurrent verbs
        # (parallel(), or a scheduled digest overlapping an interactive question) would otherwise
        # interleave into invalid JSON -- which _rescache_read swallows, silently emptying the cache.
        _private_write(_rescache_path(), data)
    except Exception:
        pass


def rescache_scope_live(kind, ttl, scope):
    """Whether a live `kind` entry could answer `scope`, under any version. Never raises.

    False only when it provably cannot: no live entry of that kind carries this scope. An entry with
    no scope at all (written before scopes existed) might be the one, so it counts as a possible hit,
    and so does an unreadable cache file -- a wrong False would skip the cache for an answer it held."""
    if rescache_disabled():
        return False
    try:
        now = time.time()
        for rec in (_rescache_read().get("entries") or {}).values():
            if isinstance(rec, dict) and rec.get("kind") == kind and 0 <= now - (rec.get("at") or 0) < ttl:
                if rec.get("scope") in (None, scope):
                    return True
        return False
    except Exception:
        return True


def drop_rescache():
    """Forget every cached aggregate for the active stack. True if a file was removed."""
    try:
        os.remove(_rescache_path())
        return True
    except OSError:
        return False


def _stamp_cache(payload, age):
    """Mark a payload as served from cache. Absent keys mean freshly computed, never 'age unknown'."""
    if isinstance(payload, dict) and age is not None:
        payload = dict(payload, fromCache=True, cacheAgeSeconds=age)
    return payload


# --- LDG rebuild stamp: which LDG an answer describes ------------------------------------------
# Full design: design/data-currency.md. The LDG changes only when a merger run COMPLETES (confirmed
# by the platform owner), so "current" means "from the latest completed merger run", not "fetched
# recently". Two calls an hour apart return the same data if no merger ran between them; two calls a
# minute apart disagree if one finished in between. Ingest time is not rebuild time either -- a source
# can ingest hours after the last merge, and none of that is in the LDG yet.
#
# The mergers are ML-ENGINE rows on the run-metrics endpoint. That endpoint ignores `platform=` and
# `bridge_name=` filters (measured: still 324 mixed pages), but honours a descending sort, so the
# newest page nearly always holds both. This is NOT the per-source sort the connector gotcha in
# CLAUDE.md warns against: that one drops sources that last ran a month ago, whereas here only the
# newest row of two known services is wanted, which a descending sort serves first.
#
# It also honours a two-key sort, `platform` ascending then `_time` descending, which puts every
# ML-ENGINE row together, newest first. Rows carry large `event_messages`, so a 20-row page of that is
# a small fraction of the 200-row time-sorted page (CLAUDE.md has the measurement), and the stamp is
# read once or twice by nearly every verb. That order is not documented, so the sorted
# read is trusted only while it VERIFIES itself (platforms never decrease; within a platform, `_time`
# never increases) and only for a positive answer, a good run for every merger. Anything else -- an
# error, a row out of order, a table with no good run in view -- falls back to the time-sorted read
# below, which is unchanged and still decides every unknown. A fallback costs one small call; trusting
# a page whose order was not checked could stamp an answer with a superseded rebuild, which is the one
# mistake this block exists to prevent.
#
# The names are exact and stable by platform contract; only the timestamp changes between runs. A
# rename must fail LOUDLY (unknown, naming what was found), never be absorbed by a looser match --
# `--live` asserts the names so a platform rename fails the suite before it reaches a release.
LDG_MERGERS = (("asset", "Lucidum Asset Merger"), ("user", "Lucidum User Merger"))
LDG_REBUILD_PAGE_SIZE = 200
LDG_REBUILD_MAX_PAGES = 3           # no merger row at all within this many pages -> unknown
# After a FAILED newest run (a failed merge leaves the LDG unchanged), page back to the previous good
# one. The stop condition is finding it; this cap is only a rate-limit backstop (one call per page
# against the 60/min budget), not a cadence estimate -- stacks rebuild anywhere from daily to every
# 4 hours, and how far back the last good run sits depends on that and on ingest volume.
LDG_REBUILD_FAILED_MAX_PAGES = 25
# That search's answer cannot change while the failed run is still the newest one (it looks strictly
# backwards from a fixed point), so it is cached keyed on the failed run itself. The key goes stale by
# construction -- a new merger run is a new newest run -- so this TTL only bounds file growth.
LDG_FAILED_CACHE_TTL = 90 * 86400
LDG_SORTED_PAGE_SIZE = 20           # a daily-merging stack's whole ML-ENGINE group fits in one page
LDG_SORTED_MAX_PAGES = 10           # 200 merger rows: 100 merges, weeks of history at a 4-hour cadence
LDG_MERGER_PLATFORM = "ML-ENGINE"


def _merger_end(row):
    """(epoch seconds, 'YYYY-MM-DDTHH:MM:SSZ') when a merger run finished, or (None, None).

    `end_time` is the merge's own completion time. `_utc` is when the run RECORD was written; it agrees
    to the second today, so it is the fallback for a row without `end_time`, never the first choice."""
    t = row.get("end_time")
    try:
        t = float(t) if t not in (None, "") else None
    except (TypeError, ValueError):
        t = None
    if t is None:
        mt = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(?:Z|\+00:00)$", str(row.get("_utc") or ""))
        if not mt:
            return None, None
        t = datetime.datetime.strptime(mt.group(1), "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=datetime.timezone.utc).timestamp()
    if t > 1e11:   # milliseconds, not seconds
        t /= 1000.0
    return t, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _config_resolvable():
    """Whether load_config() would find a stack AND a token, asked without its die(). Same resolution
    order as load_config (env, then config.json), the way diagnose_connection resolves it."""
    if _CFG_CACHE is not None:
        return bool(_CFG_CACHE[0] and _CFG_CACHE[1])
    fqdn, tok = os.environ.get("MERIDIAN_FQDN"), os.environ.get("MERIDIAN_API_TOKEN")
    if (not fqdn or not tok) and os.path.exists(CFG_PATH):
        try:
            with open(CFG_PATH, encoding="utf-8-sig") as f:  # BOM: see load_config
                c = json.load(f)
        except Exception:  # noqa - unreadable config is "not configured" here, never a crash
            c = {}
        fqdn, tok = fqdn or c.get("fqdn"), tok or c.get("api_token")
    return bool(fqdn and tok)


def _merger_run(utc, row):
    return {"rebuiltUtc": utc, "status": row.get("status"), "dagRunId": row.get("dag_run_id")}


def ldg_rebuild():
    """When each LDG table was last rebuilt: {"asset": {...}, "user": {...}, ...}. Never raises.

    Each table is either {"rebuiltUtc", "status", "dagRunId"[, "lastRebuildFailed"]} or
    {"unknown": reason}. Unknown is never replaced by the query time: stamping an answer with "now"
    when the rebuild can't be read would make a stale answer look fresh, which is the one thing this
    exists to prevent. A failed or status-less newest run is skipped (it left the LDG unchanged) and
    reported as `lastRebuildFailed` on the run that DID rebuild it."""
    if not _config_resolvable():
        # load_config()/call() would die() -- a SystemExit, which parallel() does not contain, printed
        # beside whatever the verb itself reports -- and `connect` on an unconfigured install must stay
        # a clean, no-network `not_configured` answer.
        return {t: {"unknown": "no stack is configured"} for t, _ in LDG_MERGERS}
    s = _ldg_scan(True)
    fallback = s.pop("fallback", None)
    if fallback:
        spent = s["api_calls"]
        s = _ldg_scan(False)
        s["api_calls"] += spent
        s["fallback"] = fallback
    return _ldg_stamp(s)


def _ldg_order_key(row):
    """(platform, _time) as the platform sort orders them, or None for a row that can't be placed.
    A missing platform sorts last (the search backend's default for an ascending sort)."""
    if not isinstance(row, dict):
        return None
    plat = row.get("platform")
    if plat is not None and not isinstance(plat, str):
        return None
    try:
        t = float(row.get("_time"))
    except (TypeError, ValueError):
        t = None
    return (plat, t)


def _ldg_scan(platform_sorted):
    """Page the run metrics for the newest and newest-good run of each merger. Returns the scan state;
    with `platform_sorted`, a state carrying "fallback": <reason> means the sorted read was not used.

    The sorted read is used only for a POSITIVE answer -- a good run for every merger -- on rows whose
    order checked out: platforms never decrease, within each platform `_time` never increases, and at
    least one same-platform pair was seen to strictly DEcrease. That last one is evidence the time sort
    was applied at all: a server serving oldest first could otherwise hand back one old merge's two
    rows followed by one row each of other platforms, pass both order checks, and have the oldest merge
    stamped as current. Every other outcome (a renamed merger, a failed newest run with no good one in
    view, no merger row at all) goes to the time-sorted read, which reaches those unknowns exactly as
    it always did; they are rare, and a view that is newest-first but not platform-grouped can end the
    ML-ENGINE group early and must not turn that partial view into a definitive "no"."""
    names = {n: t for t, n in LDG_MERGERS}
    newest = {t: None for t in names.values()}   # (epoch, utc, row): newest run, any status
    good = {t: None for t in names.values()}     # (epoch, run dict): newest run that did not fail
    from_cache, checked, other = set(), set(), set()
    read = api_calls = page = 0
    err = None
    last = None               # previous row's order key, carried across pages
    ml_seen = group_done = descended = False

    def bail(reason):
        return {"fallback": reason, "api_calls": api_calls}

    while True:
        if platform_sorted:
            url = ("/CMDB/v2/system/metrics/connector?size=%d&page=%d&sort=platform%%2Casc&sort=_time%%2Cdesc"
                   % (LDG_SORTED_PAGE_SIZE, page))
        else:
            url = ("/CMDB/v2/system/metrics/connector?size=%d&page=%d&sort=_time%%2Cdesc"
                   % (LDG_REBUILD_PAGE_SIZE, page))
        try:
            r = call("GET", url, retries=0)
        except Exception as e:  # noqa - reported as unknown with the reason, never raised
            if platform_sorted:
                return bail("the platform-sorted read failed (%s)" % str(e)[:200])
            err = str(e)
            break
        api_calls += 1
        content = r.get("content") if isinstance(r, dict) else None
        if not isinstance(content, list):
            if platform_sorted:
                return bail("the platform-sorted read returned an unexpected shape")
            err = "unexpected response shape from /CMDB/v2/system/metrics/connector"
            break
        read += len(content)
        if platform_sorted:
            for row in content:
                key = _ldg_order_key(row)
                if key is None:
                    return bail("a row on the platform-sorted read could not be placed in the sort order")
                if key[0] == LDG_MERGER_PLATFORM and key[1] is None:
                    return bail("a merger run has no _time to check the sort against")
                if last is not None:
                    a, b = last[0], key[0]
                    if a is None and b is not None or (a is not None and b is not None and b < a):
                        return bail("platforms came back out of order (%r after %r)" % (b, a))
                    if a == b and None not in (key[1], last[1]):
                        if key[1] > last[1]:
                            return bail("%r runs came back out of time order" % b)
                        descended = descended or key[1] < last[1]
                if key[0] == LDG_MERGER_PLATFORM:
                    ml_seen = True
                elif ml_seen:
                    group_done = True     # past the whole ML-ENGINE group
                last = key
        for row in content:
            if not isinstance(row, dict) or row.get("platform") != LDG_MERGER_PLATFORM:
                continue
            t = names.get(row.get("bridge_name"))
            if t is None:
                other.add(str(row.get("bridge_name")))
                continue
            end, utc = _merger_end(row)
            if end is None:
                continue
            if newest[t] is None or end > newest[t][0]:
                newest[t] = (end, utc, row)
            if t in from_cache or _run_health(row.get("status")) in ("fail", "unknown"):
                continue
            if good[t] is None or end > good[t][0]:
                good[t] = (end, _merger_run(utc, row))
        for t in names.values():
            if good[t] is None and newest[t] is not None and t not in checked:
                checked.add(t)
                hit, _ = rescache_get("ldg_failed", LDG_FAILED_CACHE_TTL, table=t,
                                      dag=newest[t][2].get("dag_run_id"), end=newest[t][1])
                if isinstance(hit, dict) and hit.get("rebuiltUtc"):
                    good[t] = (None, hit)
                    from_cache.add(t)
        page += 1
        need = [t for t in names.values() if good[t] is None]
        total_pages = r.get("totalPages")
        end = not content or (isinstance(total_pages, int) and page >= total_pages)
        if platform_sorted:
            if not need:
                if not descended:
                    return bail("the platform-sorted read showed no sign its time order was applied")
                break
            if group_done or end:
                return bail("the platform-sorted read held no good run for %s" % ", ".join(need) if ml_seen
                            else "no merger run in the platform-sorted read")
            if page >= (LDG_SORTED_MAX_PAGES if ml_seen else LDG_REBUILD_MAX_PAGES):
                return bail("the platform-sorted read found no good run for %s in %d pages"
                            % (", ".join(need), page))
            continue
        if not need or end:
            break
        if page >= max(LDG_REBUILD_FAILED_MAX_PAGES if newest[t] else LDG_REBUILD_MAX_PAGES for t in need):
            break
    return {"newest": newest, "good": good, "from_cache": from_cache, "other": other, "read": read,
            "api_calls": api_calls, "err": err, "sorted": platform_sorted}


def _ldg_stamp(s):
    """ldg_rebuild()'s result from a finished scan."""
    newest, good, from_cache, other = s["newest"], s["good"], s["from_cache"], s["other"]
    read, err = s["read"], s["err"]
    out = {}
    for t, name in LDG_MERGERS:
        if good[t] is not None:
            run = dict(good[t][1])
            n = newest[t]
            if n is not None and (t in from_cache or n[0] > good[t][0]):
                run["lastRebuildFailed"] = {"utc": n[1], "status": n[2].get("status"),
                                            "dagRunId": n[2].get("dag_run_id")}
                if t not in from_cache:
                    rescache_put("ldg_failed", _merger_run(run["rebuiltUtc"], {
                        "status": run["status"], "dag_run_id": run["dagRunId"]}),
                        table=t, dag=n[2].get("dag_run_id"), end=n[1])
            out[t] = run
        elif err:
            out[t] = {"unknown": "could not read Meridian's merger runs (%s)" % err[:200]}
        elif newest[t] is not None:
            n = newest[t]
            out[t] = {"unknown": "the newest %r run failed (status %r, finished %s) and no earlier "
                                 "successful run is in the newest %d runs"
                                 % (name, n[2].get("status"), n[1], read)}
        elif other:
            out[t] = {"unknown": "no %r run in the newest %d runs, but ML-ENGINE runs named %s were "
                                 "found -- the merger may have been renamed, which needs a skill update"
                                 % (name, read, ", ".join(repr(o) for o in sorted(other)))}
        else:
            out[t] = {"unknown": "no %r run in the newest %d runs" % (name, read)}
    out["runsRead"], out["apiCalls"] = read, s["api_calls"]
    # Which read produced this stamp. `sortFallback` says why the cheap one wasn't trusted, so a stack
    # that silently stopped honouring the sort shows up as a reason rather than as a slower command.
    out["runsSort"] = "platform" if s["sorted"] else "time"
    if s.get("fallback"):
        out["sortFallback"] = s["fallback"]
    if other:
        out["unexpectedMergers"] = sorted(other)
    return out


def currency_label_utc(iso):
    """'2026-09-24T10:12:12Z' -> '2026-09-24 10:12 UTC', the display form SKILL.md's labels use:
    absolute (a transcript is re-read later, so never "2 hours ago"), ISO date, 24-hour time, and UTC
    spelled out, since the skill cannot know the reader's time zone. Anything else passes through."""
    mt = re.match(r"^(\d{4}-\d\d-\d\d)T(\d\d:\d\d)", str(iso or ""))
    return "%s %s UTC" % mt.groups() if mt else str(iso)


def _table_kind(table):
    return "user" if str(table or "").startswith("user") else "asset"


def data_currency(stamp, tables, after=None):
    """The `dataCurrency` block for an answer built from `tables`, given the rebuild stamp read with
    it -- and, for a multi-call query, the stamp re-read after it (`after`).

    class is "current" only when every table's rebuild is known and, if re-read, unchanged. A rebuild
    that completed mid-query means the figures may mix two LDGs, so that answer is `unknown` with
    `rebuildDuringQuery`, not current; so is one whose re-read failed, because a mid-query rebuild
    can then not be ruled out."""
    out = {"class": None, "queriedUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    stamp = stamp if isinstance(stamp, dict) else {}
    tables = [t for t in dict.fromkeys(tables) if t in dict(LDG_MERGERS)]
    per = {t: stamp.get(t) if isinstance(stamp.get(t), dict) else {} for t in tables}
    missing = [(t, per[t].get("unknown") or "no rebuild stamp") for t in tables if not per[t].get("rebuiltUtc")]
    if stamp.get("unexpectedMergers"):
        out["unexpectedMergers"] = stamp["unexpectedMergers"]
    if missing:
        out.update({"class": "unknown", "reason": "; ".join("%s: %s" % m for m in missing)})
        return out
    if after is not None:
        after = after if isinstance(after, dict) else {}
        moved, unread = [], []
        for t in tables:
            a = after.get(t) if isinstance(after.get(t), dict) else {}
            if not a.get("rebuiltUtc"):
                unread.append("%s: %s" % (t, a.get("unknown") or "no rebuild stamp"))
            elif a["rebuiltUtc"] != per[t]["rebuiltUtc"]:
                moved.append("%s %s -> %s" % (t, per[t]["rebuiltUtc"], a["rebuiltUtc"]))
        if moved:
            out.update({"class": "unknown", "rebuildDuringQuery": True,
                        "reason": "Meridian rebuilt the LDG while this query ran (%s), so its figures may "
                                  "mix two rebuilds. Re-run it." % ", ".join(moved)})
            return out
        if unread:
            out.update({"class": "unknown",
                        "reason": "could not re-read the rebuild stamp after the query (%s), so a rebuild "
                                  "during it can't be ruled out" % "; ".join(unread)})
            return out
    out["class"] = "current"
    out["ldgRebuiltUtc"] = {t: per[t]["rebuiltUtc"] for t in tables}
    failed = {t: per[t]["lastRebuildFailed"] for t in tables if per[t].get("lastRebuildFailed")}
    if failed:
        out["lastRebuildFailed"] = failed
    if len({per[t].get("dagRunId") for t in tables}) > 1:
        # Legitimate (one merger failed, the other didn't), but then an answer spanning both tables
        # has two as-of times, and must say both rather than merge them into one.
        out["mergersSplit"] = True
    return out


def attach_currency(payload, currency, sections=None):
    """Put `dataCurrency` on a verb's dict result. `sections` names parts of a MIXED payload that
    describe the past (a change log, a 30-day average): one top-level class on a payload that is part
    history is exactly the mislabel this exists to prevent."""
    if isinstance(payload, dict):
        payload = dict(payload)
        block = dict(currency)
        if sections:
            block["sections"] = sections
        payload["dataCurrency"] = block
    return payload


def _stamp_or_unknown(stamp):
    """A stamp read that came back from parallel() as an exception, as the unknown it means."""
    if isinstance(stamp, Exception):
        return {t: {"unknown": "could not read Meridian's merger runs (%s)" % str(stamp)[:200]}
                for t, _ in LDG_MERGERS}
    return stamp if isinstance(stamp, dict) else {}


def with_currency(tables, fn, straddle=False, sections=None):
    """Run `fn()` (a verb's returning half) beside a rebuild-stamp read, and stamp its result.

    The stamp read overlaps the verb's own calls, so it costs one call of budget and roughly no wall
    clock. `straddle=True` re-reads it afterwards for verbs that make many calls over tens of seconds
    (`top`, `list --all`), where a rebuild can land mid-query."""
    before, out = parallel([ldg_rebuild, fn])
    if isinstance(out, Exception):
        raise out
    before = _stamp_or_unknown(before)
    after = ldg_rebuild() if straddle and isinstance(out, dict) and not out.get("error") else None
    return attach_currency(out, data_currency(before, tables, after), sections)


def die(msg, code=2):
    jout({"error": msg}, stderr=True)
    sys.exit(code)


_PACE_LOCK = threading.Lock()   # serializes THREADS; _FileLock below serializes PROCESSES


class _FileLock:
    """OS-level advisory lock on a sidecar file, so two PROCESSES can't interleave the .ratelimit
    read-modify-write.

    The threading.Lock above only ever covered threads inside one process, while pace()'s docstring
    promised "across concurrent callers" -- a scheduled `digest --snapshot` overlapping an
    interactive query could each read the same stamps, append only their own, and let the second
    write clobber the first (double-spending the hard 60/min budget); worse, a reader catching a
    half-written file hit the ValueError fallback and reset the stamps entirely. Locks a sidecar
    rather than .ratelimit itself because Windows byte-range locks would block our own rewrite of
    the locked file. Degrades to the old best-effort behavior on filesystems without locking.
    Windows uses the NON-blocking LK_NBLCK in a short spin: the blocking LK_LOCK retries internally
    at 1-second granularity (and gives up after ~10), which would add up to a second of latency per
    contended call around a lock held for ~1ms.

    BOUNDED ON BOTH PLATFORMS, and for the same reason. Windows' spin treated every OSError as
    contention, so a filesystem without byte-range locking (EINVAL) or a lock held by a suspended
    sibling spun at 100Hz forever, inside the thread lock, on every API call. POSIX looked safer but
    was worse: a bare blocking flock(LOCK_EX) waits with no ceiling at all, so one wedged process
    hangs every verb on the machine indefinitely -- and a blocking flock also deadlocks a *thread*
    that nests two locks on the same path, since flock is per open file description. Both branches
    now poll non-blocking until LOCK_MAX_WAIT, then proceed unlocked, which is the graceful
    degradation this docstring always promised.
    """
    def __init__(self, path):
        self.path, self.f, self.held = path, None, False

    def __enter__(self):
        try:
            self.f = open(self.path, "a+b")
            if os.name == "nt":
                import msvcrt

                def acquire():
                    self.f.seek(0)
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                def acquire():
                    fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)

            deadline = time.time() + LOCK_MAX_WAIT
            while True:
                try:
                    acquire()
                    self.held = True
                    break
                except OSError:
                    if time.time() >= deadline:
                        break   # unlockable or wedged: proceed best-effort, as documented
                    time.sleep(0.01)
        except Exception:
            pass
        return self

    def __exit__(self, *exc):
        if self.f is None:
            return
        if self.held:   # unlocking a lock we never took raises on Windows, and means nothing anyway
            try:
                if os.name == "nt":
                    import msvcrt
                    self.f.seek(0)
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.f, fcntl.LOCK_UN)
            except Exception:
                pass
        self.f.close()


def pace():
    """File-based token bucket enforcing the API's 60/min hard limit across concurrent callers --
    threads via _PACE_LOCK, other processes via _FileLock. A full bucket sleeps with the FILE lock
    released and re-checks after, so one process waiting out the window doesn't wedge every other
    caller for the duration; the in-process thread lock still holds across the sleep, which is the
    budget being enforced, not overhead."""
    _ensure_cfg_dir()
    with _PACE_LOCK:
        waited = 0.0
        while True:
            with _FileLock(RL_PATH + ".lock"):
                wait = _pace_locked()
            if wait <= 0:
                return
            # Bounded. The loop retries rather than taking a slot outright, so without a ceiling any
            # state it cannot drain is an indefinite silent hang -- every verb just stops, with no
            # message. _pace_locked already drops future-dated stamps, but a budget genuinely full
            # for longer than this means something is wrong with the file, not with our pacing.
            if waited >= PACE_MAX_WAIT:
                sys.stderr.write(
                    "warning: rate-limit budget at %s has been full for %ds, which should not happen "
                    "-- proceeding anyway. Delete that file if this repeats.\n" % (RL_PATH, int(waited)))
                return
            nap = min(wait, 60000) / 1000.0
            time.sleep(nap)
            waited += nap


def _pace_locked():
    """One locked read-modify-write: takes a slot and returns 0, or returns ms to wait and retry."""
    now = int(time.time() * 1000)
    stamps = []
    if os.path.exists(RL_PATH):
        try:
            with open(RL_PATH) as f:
                stamps = [int(x) for x in f.read().split() if x.strip()]
        except ValueError:
            stamps = []
    # Window is (now-60s, now]: the upper bound matters as much as the lower. A clock that ran ahead
    # and was then corrected (NTP step, a resumed VM, a dual-boot machine) leaves future-dated stamps
    # that the sliding window can never expire, so the caller's retry loop would compute a wait,
    # sleep, and find the same stamps forever -- every verb hanging with nothing printed.
    stamps = [s for s in stamps if now - 60000 < s <= now]
    if len(stamps) >= RL_LIMIT:
        return max(1, (stamps[0] + 60000) - now)
    stamps.append(now)
    with open(RL_PATH, "w") as f:
        f.write("\n".join(str(s) for s in stamps))
    return 0


def parallel(tasks):
    """Run independent API calls at the same time and return their results in order.

    Latency here is dominated by round trips (~0.4-0.8s each against a remote stack), so anything
    that doesn't depend on a previous answer should not wait for it. `pace()` still enforces the
    60/min budget across the threads, and a task that raises returns its exception rather than
    killing the batch, so a caller that treats one result as optional (a change log, a connector
    endpoint a scoped token can't read) keeps working.
    """
    if not tasks:
        return []
    if len(tasks) == 1:
        try:
            return [tasks[0]()]
        except Exception as e:  # noqa
            return [e]
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(tasks), 8)) as ex:
        futures = [ex.submit(t) for t in tasks]
        out = []
        for f in futures:
            try:
                out.append(f.result())
            except Exception as e:  # noqa
                out.append(e)
        return out


_SSL_CTX = {}   # one context per posture, built once -- see _ssl_context


def insecure_tls():
    """True only if certificate verification has been explicitly disabled for this stack."""
    if os.environ.get("MERIDIAN_INSECURE_TLS", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    try:
        with open(CFG_PATH, encoding="utf-8-sig") as f:
            return bool(json.load(f).get("insecure_tls"))
    except Exception:  # missing/unreadable config - fall back to the safe posture
        return False


def _ssl_context():
    """Cached TLS context; verification is ON by default.

    This client sends a bearer token and receives customer inventory -- names, departments, leaked
    credential counts, exploitable weaknesses -- so an unverified connection is a real exposure, not
    a convenience. Verification used to be off here to match the retired PowerShell helpers'
    -SkipCertificateCheck posture; that reason left with them.

    A stack with a self-signed certificate opts out via MERIDIAN_INSECURE_TLS=1 or
    `"insecure_tls": true` in config.json. `connect` reports which posture is in force, so an
    inherited opt-out can't stay invisible. Cached because building a context re-reads the system
    trust store, which cost ~1 syscall-heavy setup per API call.
    """
    insecure = insecure_tls()
    if insecure not in _SSL_CTX:
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        _SSL_CTX[insecure] = ctx
    return _SSL_CTX[insecure]


def _normalize_endpoint(endpoint):
    """Accept `CMDB/v2/...` as well as `/CMDB/v2/...`, and catch Git Bash path mangling.

    Git Bash (MSYS) rewrites an argument that starts with `/` into a Windows path, so
    `api '/CMDB/v2/data/cmdb'` silently arrives as `C:/Program Files/Git/CMDB/v2/data/cmdb` and the
    request dies with an unhelpful "URL can't contain control characters". Detect that and say what
    to do instead of letting it surface as a mystery.
    """
    ep = endpoint.replace("\\", "/")
    marker = ep.find("/CMDB/")
    if marker > 0 or (marker == -1 and "CMDB/" in ep and not ep.startswith("CMDB/")):
        die("Endpoint looks path-mangled by the shell: %r. Git Bash rewrites a leading '/' into a "
            "Windows path. Re-run with MSYS_NO_PATHCONV=1, or drop the leading slash "
            "(api CMDB/v2/...)." % endpoint)
    return ep if ep.startswith("/") else "/" + ep


# --- HTTP transport: pooled keep-alive connections -----------------------------------------------
#
# Each call used to open a fresh TCP+TLS connection via urlopen. The handshake measures ~79ms against
# a ~2.3s page fetch -- modest per call, but it compounds across the multi-call verbs. A *pool* rather
# than thread-locals is what actually captures it: `parallel()` builds a new ThreadPoolExecutor per
# batch, so thread-local connections would be thrown away on every fan-out.
#
# Keyed on (fqdn, tls-posture) so a `stacks switch` or an insecure_tls change can never hand back a
# connection to the wrong host or with the wrong verification.
_POOL: list = []
_POOL_LOCK = threading.Lock()
_POOL_MAX = 8   # parallel()'s worker ceiling; more idle sockets than that would just be waste
_TRANSPORT_ERRS = (http.client.HTTPException, ConnectionError, socket.timeout, OSError)


def _new_conn(fqdn, ctx):
    return http.client.HTTPSConnection(fqdn, context=ctx, timeout=60)


def _pool_get(key, fqdn, ctx):
    with _POOL_LOCK:
        while _POOL:
            k, conn = _POOL.pop()
            if k == key:
                return conn
            try:            # a stale entry for another stack/posture: close rather than reuse
                conn.close()
            except Exception:
                pass
    return _new_conn(fqdn, ctx)


def _pool_put(key, conn):
    with _POOL_LOCK:
        if len(_POOL) < _POOL_MAX:
            _POOL.append((key, conn))
            return
    try:
        conn.close()
    except Exception:
        pass


def _round_trip(fqdn, key, ctx, method, endpoint, data, headers):
    """One request over a pooled connection. Returns (status, text, location).

    A pooled socket can be closed by the server or an intermediary between calls, which surfaces as a
    transport error on the NEXT request rather than as a status code. That is the cost of pooling, not
    the caller's retry, so it is absorbed here with one fresh connection before giving up.
    """
    last = None
    for pooled in (True, False):
        conn = _pool_get(key, fqdn, ctx) if pooled else _new_conn(fqdn, ctx)
        try:
            conn.request(method, endpoint, body=data, headers=headers)
            resp = conn.getresponse()
            # Drain the body fully, or the socket can't be handed back for reuse.
            text = resp.read().decode("utf-8", "replace")
            loc = resp.getheader("Location")
            _pool_put(key, conn)
            return resp.status, text, loc
        except ssl.SSLCertVerificationError:
            try:
                conn.close()
            except Exception:
                pass
            raise                       # a trust problem, never a stale socket: don't reconnect
        except _TRANSPORT_ERRS as e:
            try:
                conn.close()
            except Exception:
                pass
            last = e
    raise last


def call(method, endpoint, body=None, retries=1):
    fqdn, api_token, action_token = load_config()
    endpoint = _normalize_endpoint(endpoint)
    is_ldg = "/data/ldg" in endpoint
    token = action_token if is_ldg else api_token
    if not token:
        if is_ldg:
            die("The /CMDB/v2/data/ldg endpoint needs an Action token. Use /CMDB/v2/data/cmdb instead.")
        die("No API token configured (env MERIDIAN_API_TOKEN or ~/.meridian/config.json).")
    data = body.encode("utf-8") if isinstance(body, str) else (json.dumps(body).encode("utf-8") if body is not None else None)
    ctx = _ssl_context()
    key = (fqdn, insecure_tls())
    headers = {"Content-Type": "application/json",
               "Authorization": "Bearer %s" % token,
               # Some Meridian stacks sit behind Cloudflare, which blocks the default UA (error 1010).
               "User-Agent": "Mozilla/5.0 (meridian-cli)"}
    last_err = None
    for attempt in range(retries + 1):
        pace()
        redirect_err = None
        try:
            status, text, loc = _round_trip(fqdn, key, ctx, method, endpoint, data, headers)
            # urlopen used to follow redirects transparently; http.client does not, so a trailing-slash
            # or canonicalisation redirect would otherwise become a mystery empty result. Same-host
            # only: these headers carry the API token, and it must never be replayed to another host.
            hops = 0
            while status in (301, 302, 303, 307, 308) and loc and hops < 3:
                nxt = urllib.parse.urlparse(loc)
                if nxt.netloc and nxt.netloc != fqdn:
                    redirect_err = ("HTTP %s: refused to follow a redirect to another host (%s) -- the "
                                    "API token must not be replayed off this stack." % (status, nxt.netloc))
                    break
                endpoint = (nxt.path or endpoint) + (("?" + nxt.query) if nxt.query else "")
                hops += 1
                pace()
                status, text, loc = _round_trip(fqdn, key, ctx, method, endpoint, data, headers)
        except ssl.SSLCertVerificationError as e:
            # A rejected certificate is a configuration/trust problem, not a blip: retrying just
            # doubles the wait. Say what to do rather than surfacing it as a generic 'unreachable'.
            raise RuntimeError(
                "TLS certificate verification failed for %s: %s. If this stack presents a "
                "self-signed or internally-issued certificate, set \"insecure_tls\": true in "
                "%s (or MERIDIAN_INSECURE_TLS=1) -- but only if you trust the network path, "
                "since the API token is sent over this connection."
                % (fqdn, getattr(e, "verify_message", None) or e, CFG_PATH))
        except Exception as e:  # noqa - transport failure; retry per `retries`
            last_err = str(e)
            time.sleep(2)
            continue
        if redirect_err:
            last_err = redirect_err
            break  # a misdirected redirect won't resolve itself on a retry
        if 200 <= status < 300:
            try:
                return json.loads(text)
            except ValueError:
                last_err = "HTTP %s: response was not JSON: %s" % (status, text[:200])
                break
        # Keep this string shape: classify_connect_error() parses "HTTP <code>" out of it.
        last_err = "HTTP %s: %s" % (status, text[:300])
        if status in (401, 403, 404):
            break  # not transient
        time.sleep(2)
    raise RuntimeError(last_err or "request failed")


# ---- query building (trivial in Python; no array-unwrap gotchas) ------------------------------
CLAUSE_TYPES = ("String", "Float", "Integer", "Binary", "List", "Datetime")

# Operators grouped by how many whitespace-separated words they occupy, because `--where` is parsed
# positionally and a multi-word operator otherwise pushes the type out of its slot. All are documented
# DSL operators (references/query-syntax.md). Being in this table means "parsed", not "works": the
# `within*` family is parsed only so it can be refused with an accurate message (see below), and
# `length gt/lt/eq` returned HTTP 400 on the demo stack -- those pass through, since a loud 400 from the
# API is a fine outcome and hard-coding a stack-specific rejection here would not be.
#
# Before this table existed the parser did a fixed `split(None, 3)`, so ONLY single-word operators
# worked. That silently made a documented recipe fail, made SKILL.md's own "certificates expiring in
# the next 30 days" sample prompt unanswerable through any verb, and -- worst -- meant `metrics add`
# could never save a time-windowed count, so nothing in the trend layer could track one. It also took
# out `not match` / `not in`, which are the DSL's only negation: there is no NOT wrapper, so "OS is
# not Windows" had no expressible form at all. The error even blamed the type slot, which was correct.
CLAUSE_OPERATORS = {
    3: ("not within past", "not within future"),
    2: ("not match", "not in", "not within", "within past", "within future",
        "length gt", "length lt", "length eq"),
    1: ("==", "!=", ">=", ">", "<=", "<", "match", "in", "within", "exists", "empty"),
}
# The windowed Datetime operators are parsed (so the message can be accurate) and then REFUSED, because
# measured against a live stack they do not work and one half of them fails silently:
#
#   within past / not within past      -> HTTP 400 "Invalid operator: within past"    (loud, fine)
#   within / within future / not ...   -> HTTP 200, and the window is IGNORED         (silent, not fine)
#
# `Expired_Datetime within future Datetime <N>, days` returned the same 52 records for N = 1, 30, 90 and
# 3650 -- i.e. every record where the field exists, regardless of window. The true count for 90 days,
# via an absolute range, is 51. So the operator yields a plausible number, off by a little, for a
# question it never actually asked -- and "52 certificates expire in the next 24 hours" is exactly the
# convincing wrong answer this tool is built not to give. Note the reference docs have the precedence
# backwards: they say prefer `within past` and fall back to `within`, but the long form is the one that
# errors and the short form is the one that lies.
#
# Absolute comparisons on the same field are exact and verified (`>= 2026-08-17 00:00:00` and
# `< 2026-08-17 00:00:00` partition the 52 records 52/0), so RELATIVE_DATE below is the supported route
# to the same questions.
# Mapped to the absolute form that asks the same question, because the suggestion is the actionable half
# of the refusal. Note the negations invert the comparison rather than the sign: "not within a trailing
# 90 days" is `< -90d` (older than that), not `>= -90d`.
WINDOWED_OPERATORS = {
    "within past":       (">=", "-"),
    "within":            (">=", "-"),
    "not within past":   ("<",  "-"),
    "not within":        ("<",  "-"),
    "within future":     ("<=", "+"),
    "not within future": (">",  "+"),
}

# `+90d` / `-30d` in a Datetime value slot, resolved to an absolute timestamp *client-side*. This is
# what makes a time-window question expressible at all: SKILL.md advertises "certificates expiring in
# the next 30 days" and, with the windowed operators unusable, an absolute range computed here is the
# only form the API answers correctly.
_RELATIVE_DATE = re.compile(r"^([+-])(\d+)([dw])$")
_RELATIVE_UNITS = {"d": 1, "w": 7}
# Day boundaries are chosen so an inclusive range reads the way the question is asked: ">= -30d" starts
# at the beginning of that day, "<= +90d" runs through the end of it. Only the ordering operators get
# relative values -- `==` against a timestamp has no sensible day-wide meaning, so it is refused rather
# than guessed at.
_RELATIVE_OPERATORS = (">=", ">", "<=", "<")


def resolve_relative_date(val, op, now=None):
    """(absolute "YYYY-MM-DD HH:MM:SS", problem) for a relative Datetime value like `+90d` / `-2w`.

    Returns `(None, None)` when `val` isn't relative at all, so an absolute date the caller typed passes
    straight through untouched. `now` is injectable because a date-dependent result has to be testable.

    The explicit time component is not cosmetic: measured on a live stack, a bare `2026-08-17` matched 51
    records with `>=` while `2026-08-17 00:00:00` matched 52, so a date-only value lands at some
    unspecified point inside the day. Emitting the boundary explicitly is what makes the range exact.
    """
    raw = (val or "").strip()
    # `today` is spelled out because the most common bound of all is "from now" / "through today", and
    # `-0d` is a wretched way to write it. It resolves to the same day boundary as `+0d`.
    m = _RELATIVE_DATE.match("+0d" if raw.lower() == "today" else raw)
    if not m:
        return None, None
    if op not in _RELATIVE_OPERATORS:
        return None, ("a relative date (%r) only works with %s -- %r compares against an instant, and a "
                      "whole-day window has no meaning there. Use an absolute "
                      "\"YYYY-MM-DD HH:MM:SS\" value instead."
                      % (val, ", ".join(_RELATIVE_OPERATORS), op))
    sign, n, unit = m.group(1), int(m.group(2)), m.group(3)
    days = n * _RELATIVE_UNITS[unit] * (1 if sign == "+" else -1)
    base = (now or datetime.datetime.now()) + datetime.timedelta(days=days)
    # Lower bounds open at the start of the day, upper bounds close at the end of it, so `>= -30d` and
    # `<= +90d` together describe an inclusive range in the terms the question was asked in.
    stamp = "00:00:00" if op in (">=", ">") else "23:59:59"
    return "%s %s" % (base.strftime("%Y-%m-%d"), stamp), None


def split_clause(clause):
    """(field, operator, type, value) for a `--where` clause, or None if it doesn't have that shape.

    Multi-word operators are matched longest-first against `CLAUSE_OPERATORS`, so the operator table
    decides where the type slot begins instead of a fixed token count. Matching against the table (and
    not by position) is what keeps a *value* from hijacking the parse: in
    `sourcetype in List not match` the 3- and 2-word candidates (`in List not`, `in List`) aren't
    operators, so it falls through to `in` and the value stays `not match`.

    `value` keeps its original internal spacing -- `split` is given a maxsplit so the tail is never
    re-joined, because a Datetime window is legitimately `"30, days"` and a String `match` value can
    contain anything.
    """
    for n in (3, 2, 1):
        parts = (clause or "").split(None, n + 2)
        if len(parts) < n + 2:
            continue
        op = " ".join(parts[1:1 + n])
        if op not in CLAUSE_OPERATORS[n]:
            continue
        return parts[0], op, parts[1 + n], (parts[n + 2] if len(parts) > n + 2 else None)
    return None


def clause_or_problem(clause):
    """(parsed clause, problem message) for `<Field> <operator> <Type> [value]` -- exactly one is None.

    Split out from `parse_clause` so a *stored* metric definition can be re-validated without exiting
    the process: a saved metric that stops parsing has to be reported as unavailable on that one metric,
    not kill the snapshot that was checking it. One implementation of the messages, two dispositions.
    """
    split = split_clause(clause)
    if split is None:
        # No known operator in the operator slot. Distinguish "too short to be a clause" from "the
        # operator is misspelled", since the fix is different and the old parser conflated them by
        # reporting whatever landed in the type slot.
        parts = (clause or "").split()
        if len(parts) < 3:
            return None, ("--where takes \"<Field> <operator> <Type> [value]\" and got %r. "
                          "Example: \"Risk_Score >= Float 500\". Types: %s."
                          % (clause, ", ".join(CLAUSE_TYPES)))
        # Only operators that can succeed: the windowed six are parsed so their refusal can be
        # specific, but offering them here would send the caller into that second refusal.
        known = ", ".join(sorted((o for v in CLAUSE_OPERATORS.values() for o in v
                                  if o not in WINDOWED_OPERATORS), key=lambda s: (len(s), s)))
        return None, ("%r is not an operator, in --where %r. Expected one of: %s. For a time window "
                      "use a relative Datetime value such as `>= Datetime -30d`."
                      % (parts[1], clause, known))
    field, op, typ, val = split
    if typ not in CLAUSE_TYPES:
        extra = ""
        if any(c.isdigit() for c in typ):
            extra = (" It looks like the value ended up in the type slot -- the data type goes "
                     "*before* the value, e.g. \"%s %s Float %s\"." % (field, op, typ))
        return None, ("%r is not a data type, in --where %r. Expected one of %s.%s"
                      % (typ, clause, ", ".join(CLAUSE_TYPES), extra))
    if op in WINDOWED_OPERATORS:
        # Refused, not passed through -- see WINDOWED_OPERATORS. Half of these 400 and half return
        # everything while ignoring the window, and the silent half is the dangerous one. Naming the
        # working alternative matters more than the refusal: the question is answerable, just not this way.
        direction, sign = WINDOWED_OPERATORS[op]
        return None, ("--where %r uses %r, which this API does not honour: the `past` spellings are "
                      "rejected outright and the others return every record while ignoring the window "
                      "entirely (measured: identical counts for 1, 30, 90 and 3650 days). Use an "
                      "absolute range instead -- relative values are resolved for you, e.g. "
                      "\"%s %s Datetime %s90d\". Absolute comparisons on the same field are exact."
                      % (clause, op, field, direction, sign))
    if typ == "Datetime" and val is not None:
        resolved, problem = resolve_relative_date(val, op)
        if problem:
            return None, "--where %r: %s" % (clause, problem)
        if resolved:
            val = resolved
    if typ in ("Float", "Integer") and val is not None:
        try:
            # Numeric values must go out as real JSON numbers; a quoted number returns 0 records with
            # no error, which is the same silent-wrongness trap as everything else in this file.
            val = float(val) if typ == "Float" else int(float(val))
        except ValueError:
            return None, ("--where %r declares type %s but %r isn't a number." % (clause, typ, val))
    return {"searchFieldName": field, "operator": op, "type": typ, "value": val}, None


def parse_clause(clause):
    """Parse `<Field> <operator> <Type> [value]`, checking the shape locally.

    Omitting the type used to build a query with the *value* in the type slot, which the API rejected
    with `Invalid operator: >=` -- blaming the operator, which was fine -- after spending a round trip
    to say so. The shape is checkable here, instantly, with an accurate message.
    """
    parsed, problem = clause_or_problem(clause)
    if problem:
        die(problem)
    return parsed


def clause_fields(clauses):
    """Field names referenced by --where clauses, for validation before any request goes out."""
    return [(c or "").split(None, 1)[0] for c in (clauses or []) if (c or "").strip()]


def _csv_cell(v):
    """Flatten a value for a spreadsheet cell. Multi-value fields are lists; `str(list)` would put
    Python syntax in a cell someone is about to sort and filter.

    Strings get formula-neutralized: cell values originate in the customer's environment (asset
    names, owner names -- transitively, whatever feeds their connectors), and this CSV is tuned to
    be double-clicked into Excel, where a value starting with = + - @ or a tab executes as a
    formula/DDE in the analyst's session. The leading apostrophe is Excel's own "this is text"
    marker. Numbers pass through untouched -- they are not strings, so a negative Risk_Score delta
    stays sortable."""
    if isinstance(v, (list, tuple)):
        v = "; ".join("" if x is None else str(x) for x in v)
    elif isinstance(v, dict):
        v = json.dumps(v, separators=(",", ":"))
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return "" if v is None else v


def out_path_problem(path, suffixes):
    """Why `path` must not be written as an output file, or None if it may.

    `report --out` and `--format csv --out` overwrite without asking, and nothing checked the target:
    `report --html --out scripts/meridian.py` replaced the script with a report, and a PDF render over
    SKILL.md replaces the skill's own instructions. The suffix is the load-bearing check (the doc
    generators' docout.py guard, same reasoning, reimplemented because that file does not ship).
    An existing file with the right suffix is still overwritten -- that is the regenerate case.
    """
    if not path.lower().endswith(tuple(suffixes)):
        return "--out %r must end in %s; refusing to overwrite a file of another kind." % (
            path, " or ".join(suffixes))
    if os.path.isdir(path):
        return "--out %r is a directory; give a file path." % path
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        return "--out %r: the directory %r does not exist." % (path, parent)
    return None


def out_in_skill_dir(path):
    """A warning when an output file would land inside the skill's own folder, else None.

    The commands in SKILL.md run from the skill folder (`python scripts/meridian.py`), so every
    relative `--out` lands in it -- and self-update moves every entry out of that folder and deletes
    the old tree, so a report or export saved there is gone at the next update, silently. Until then
    it is customer PII in a directory that is a git working tree on a maintainer's machine.

    A warning, not a refusal: the scheduling recipes shipped through v2.25.0 `cd` into the skill
    folder and write there, and a refusal would turn a scheduled job that loses old reports into one
    that produces none. The model reads stderr and moves the file; a scheduled job keeps running.
    """
    try:
        target = os.path.normcase(os.path.realpath(os.path.abspath(path)))
        root = os.path.normcase(INSTALL_REAL)
        if os.path.commonpath([target, root]) != root:
            return None
    except ValueError:      # different drives on Windows: certainly not inside
        return None
    return ("warning: --out %r is inside the skill folder (%s). Self-update replaces that folder and "
            "deletes anything saved in it, and customer data does not belong there. Write outputs to "
            "the user's own folder instead." % (path, INSTALL_DIR))


def emit(payload, rowkey, fmt=None, out_path=None):
    """Print a verb's result as JSON, or write its rows as CSV.

    The envelope is always printed as JSON, even in CSV mode. A spreadsheet cannot hold "37% of records
    are unaccounted for" or "this ranking may not be the true top-N", and silently dropping those would
    undo the whole point of measuring them -- so the rows go to CSV and the caveats stay visible.
    """
    if (fmt or "json") != "csv":
        jout(payload)
        return
    rows = payload.get(rowkey) or []
    cols = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    flat = [{k: _csv_cell(r.get(k)) for k in cols} for r in rows]
    # The header row goes through _csv_cell too: column names come from the API, and a customer-named
    # SmartLabel is as much customer data as a cell value.
    envelope = {k: v for k, v in payload.items() if k != rowkey}
    envelope["rowsWritten"] = len(flat)
    if out_path:
        # utf-8-sig: Excel reads a BOM-less UTF-8 CSV as the local codepage and mangles anything
        # non-ASCII in an asset name. The BOM is what makes a double-click open correctly.
        with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writerow({c: _csv_cell(c) for c in cols}); w.writerows(flat)
        envelope["csv"] = os.path.abspath(out_path)
        jout(envelope)
    else:
        w = csv.DictWriter(sys.stdout, fieldnames=cols, lineterminator="\n")
        w.writerow({c: _csv_cell(c) for c in cols}); w.writerows(flat)
        # stderr, so `... --format csv > out.csv` still yields a clean file with the caveats on screen.
        jout(envelope, stderr=True)


def select_fields(sel):
    """Field names from --select. Validated too: a typo here silently produced a column of nulls,
    which reads as "this field isn't populated on this stack" rather than "you misspelled it"."""
    return sel.replace(",", " ").split() if sel else []


def and_query(clauses, extra=None):
    groups = [[parse_clause(c)] for c in (clauses or [])]
    if extra:
        groups.append([extra])
    return groups


# A count only reads `totalRecords`, and every page carries it -- including one past the end, which
# carries no records. Page 0 with recordsPerPage 1 still downloads one whole record, and records can
# be huge: measured on a live stack, one high-risk asset count was 5.33MB/1.73s at page 0 and 86
# bytes/0.85s here, with the same total, across unfiltered, filtered, zero-match, OR-group,
# exists-gated and user-table queries. It is undocumented behaviour, so count_records() checks it:
# no integer `totalRecords`, or a 400/404/416/422, falls back to page 0. (`recordsPerPage: 0` is no
# substitute -- it silently returns 20 records.) Past COUNT_PAGE records the page is in range again
# and carries one record: slower, never wrong.
COUNT_PAGE = 99999


def count_records(table, query):
    """totalRecords for a query, without downloading a record. See COUNT_PAGE."""
    try:
        r = call("POST", "/CMDB/v2/data/cmdb",
                 {"table": table, "query": query, "paging": {"page": COUNT_PAGE, "recordsPerPage": 1}})
        n = r.get("totalRecords") if isinstance(r, dict) else None
        if isinstance(n, int) and not isinstance(n, bool):
            return n
    except Exception as e:
        if not re.match(r"HTTP (400|404|416|422)\b", str(e)):
            raise
    return call("POST", "/CMDB/v2/data/cmdb",
                {"table": table, "query": query, "paging": {"page": 0, "recordsPerPage": 1}})["totalRecords"]


def jlist(v):
    if v is None:
        return []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


# ---- verbs -----------------------------------------------------------------------------------
# Every endpoint whose response can carry connector credentials. `connector/profile` returns them
# outright; `system/metrics/connector` returns each run's `profile` as the whole profile object (see
# _profile_name), config and secret included -- the guard missed it until a security review. Both
# are read by `connectors` through its allow-list, so refusing them here loses nothing. The
# ingestion-detail prefix is refused on suspicion rather than proof: its `docker_cmd` is a
# connector's command line, the shape that carries a credential, and nobody needs it via `api`.
_CREDENTIAL_ENDPOINTS = ("/cmdb/v2/connector/profile", "/cmdb/v2/system/metrics/connector")
_CREDENTIAL_PREFIXES = ("/cmdb/v2/system/metrics/data-ingestion/detail/",)


def _is_credential_endpoint(endpoint):
    """True for any spelling of an endpoint that returns connector credentials.

    Compared DECODED, and repeatedly: the first version of this guard did one exact string match on
    the raw path, so `connector/profil%65` -- which the API itself decodes and serves -- sailed
    straight past it and printed service accounts, hosts and the encrypted password into the
    transcript. Doubly-encoded spellings (`%2565`) decode in two passes, hence the loop.

    Case-insensitive on the path even though the API is case-sensitive: a guard that only refuses
    the spelling that happens to work is a guard that fails open the day the server relaxes. Empty
    path segments are dropped for the same reason (`connector//profile`).
    """
    path = _canonical_path(endpoint)
    return path in _CREDENTIAL_ENDPOINTS or path.startswith(_CREDENTIAL_PREFIXES)


def _canonical_path(endpoint):
    """The endpoint's path the way the server will read it: decoded, lower-cased, no empty or dot
    segments, no query. Both `api` guards compare this, never the raw argument -- see
    _is_credential_endpoint for the spellings that got past a raw comparison."""
    path = _normalize_endpoint(endpoint).split("?", 1)[0].split("#", 1)[0]
    for _ in range(3):
        nxt = urllib.parse.unquote(path)
        if nxt == path:
            break
        path = nxt
    path = "/" + "/".join(seg for seg in path.split("/") if seg not in ("", "."))
    return path.rstrip("/").lower()


def api_endpoint_problem(endpoint):
    """Why an `api` endpoint is refused before either guard reads it, or None.

    Both guards compare _canonical_path, which drops `.` segments but cannot resolve `..` without
    guessing what the server does with it, and cuts the path at `#`. So `connector/x/../profile`
    compared as a different endpoint from the one a normalising proxy would serve, and
    `data/cmdb#/../../connector/test/async` read as the read-only query endpoint while http.client
    sent the whole string. No real endpoint needs `..`, `#`, `;` or a control character, so they are
    refused outright rather than interpreted.
    """
    raw = _normalize_endpoint(endpoint)
    path = raw.split("?", 1)[0]
    for _ in range(3):
        nxt = urllib.parse.unquote(path)
        if nxt == path:
            break
        path = nxt
    path = path.replace("\\", "/")
    if "#" in raw or "#" in path or ";" in path:
        return "`api` refuses endpoints containing `#` or `;` (%r): no API endpoint uses them." % endpoint
    if any(ord(c) < 32 or ord(c) == 127 for c in raw + path):
        return "`api` refuses endpoints containing control characters (%r)." % endpoint
    if ".." in path.split("/"):
        return ("`api` refuses endpoints with a `..` segment (%r): name the endpoint directly."
                % endpoint)
    return None


def config_path_problem(path):
    """Why a file argument is refused, or None.

    `report --input` and `api --body-file` read whatever file they are given, and the files that
    must never pass through them are the ones holding tokens. `report --input
    ~/.meridian/stacks.json --html` wrote every saved stack's token into the page, and `api -X POST
    CMDB/v2/data/cmdb --body-file ~/.meridian/stacks.json` counts as a read, so it would have sent
    them all to the active stack. The deny rule on file tools does not reach this script's own
    open(), so the check lives here. Nothing under CFG_DIR is ever an input, so all of it is refused.
    """
    try:
        target = os.path.normcase(os.path.realpath(path))
        home = os.path.normcase(os.path.realpath(CFG_DIR))
        inside = os.path.commonpath([target, home]) == home
    except ValueError:      # different drives on Windows: cannot be inside
        inside = False
    if inside:
        return ("%s is inside %s, which holds your saved stack tokens; it is never an input. Save the "
                "file somewhere else." % (path, CFG_DIR))
    return None


# POST is how this API takes a read query, so a POST is not by itself a write. These are the ones
# that change nothing; every other POST, and every PUT/PATCH/DELETE, can change the stack.
_READ_POST_ENDPOINTS = ("/cmdb/v2/data/cmdb", "/cmdb/v2/data/ldg", "/cmdb/v2/smartlabel/search")
# And the reverse: GETs that change the stack. `data-ingestion/run` starts a full ingestion run from
# every connector, and was treated as a read because it is a GET.
_WRITE_GET_ENDPOINTS = ("/cmdb/v2/system/data-ingestion/run",)


def api_write_problem(method, endpoint, allow_write=False):
    """Why this `api` call needs --allow-write, or None if it can run as-is.

    SKILL.md and api-reference.md both said "POST/PUT/DELETE change the stack -- confirm with the
    user first", and nothing enforced it: `api -X PUT CMDB/v2/connector/profile/service` disables a
    connector's services in one call, and `api -X DELETE` removes whatever it names. A flag the
    caller has to add is what turns that sentence into a step -- the model cannot reach the write
    without deciding to, and the refusal says what to confirm. Reads stay unflagged: GET, HEAD, and
    the POSTs in _READ_POST_ENDPOINTS, compared canonically so a spelling cannot turn a write into a
    "read". Unknown POSTs count as writes; the cost of that is one extra flag on a read nobody has
    listed yet, and the cost of the other default is an unconfirmed change to a customer's stack.
    """
    m = (method or "GET").upper()
    if allow_write:
        return None
    if m in ("GET", "HEAD"):
        if _canonical_path(endpoint) in _WRITE_GET_ENDPOINTS:
            return ("`api %s %s` starts a full ingestion run from every connector, despite being a "
                    "GET. Confirm this with the user, then re-run with --allow-write." % (m, endpoint))
        return None
    if m == "POST" and _canonical_path(endpoint) in _READ_POST_ENDPOINTS:
        return None
    return ("`api -X %s %s` can change the stack. Confirm this specific change with the user, then "
            "re-run with --allow-write." % (m, endpoint))


def cmd_api(a):
    # /CMDB/v2/connector/profile returns connector credentials (hosts, service accounts, an
    # encrypted password, a full nested config) despite the docs saying otherwise. `connectors`
    # reads it through an in-process field allow-list that the tests assert nothing
    # credential-shaped survives -- so that invariant has to hold HERE too, not just in SKILL.md
    # prose, or "show me the raw connector profile" drops credentials straight into a transcript.
    problem = api_endpoint_problem(a.endpoint)
    if problem:
        die(problem, 2)
    if _is_credential_endpoint(a.endpoint):
        die("That endpoint can return connector credentials (service accounts, hosts, config "
            "secrets). Use `connectors`, which reads connector data through a credential-stripping "
            "allow-list.")
    problem = api_write_problem(a.method, a.endpoint, getattr(a, "allow_write", False))
    if problem:
        die(problem, 2)
    body = None
    if a.body_file and config_path_problem(a.body_file):
        die(config_path_problem(a.body_file), 2)
    if a.body_file:
        with open(a.body_file, encoding="utf-8-sig") as f:
            body = f.read()
    elif a.body:
        body = a.body
    # No automatic retry for anything that could have side effects. `api` is the one verb that
    # reaches arbitrary endpoints -- including the Action-token /data/ldg path -- and a timeout
    # mid-response on a mutating POST retried blind would run the action twice. Every internal
    # POST is a read-only query, so this caution costs the normal verbs nothing.
    out = call(a.method, a.endpoint, body, retries=1 if a.method.upper() == "GET" else 0)
    jout(out)


def _fields_path():
    fqdn, _, _ = load_config()
    return os.path.join(CFG_DIR, "fields.%s.json" % re.sub(r"[^A-Za-z0-9._-]", "_", fqdn))


def _slim_meta(arr):
    return [{"fieldName": f.get("fieldName"), "dataType": f.get("dataType"),
             "displayName": f.get("displayName"), "fieldGroup": f.get("fieldGroup")} for f in arr]


_FIELD_MAP: dict = {}   # per-process memo, keyed by "asset"/"user"
_FIELD_MAP_LIVE: set = set()   # tables whose map was fetched from the API in this process, not read from disk
_FIELD_MAP_DISK: set = set()   # tables whose map was read from the disk cache -- the only kind that can be stale
_FIELD_MAP_LOCK = threading.Lock()  # cold-cache fills race under parallel(); see load_field_map


def load_field_map(table, allow_fetch=True):
    """{fieldName: dataType} for a table, fetching and caching metadata on first need.

    This used to be populated *only* by an explicit `refresh-fields` run, and field_type() answered
    "String" for everything when the file was missing. That is not a harmless default. `summary --by`
    picks its operator from the type -- `match` for List, `==` otherwise -- so on a fresh install a
    genuinely multi-valued field was queried with `==`, and the breakdown came back with records
    counted in several groups at once (measured on a demo stack: 38,943 accounted against a 34,229
    total). Field names are also validated against this map, so a typo can be told apart from a real
    zero. Both need the metadata to be present, so it is fetched rather than assumed.
    """
    key = "user" if table.startswith("user") else "asset"
    if key in _FIELD_MAP:
        return _FIELD_MAP[key]
    # The lock makes a cold fill happen once. This is a check-then-fetch, and it runs inside
    # parallel() fan-outs -- build_digest's summarize_by and top_n both need the asset map, and on a
    # fresh install both missed and both fetched the same ~1MB metadata, each burning a slot of the
    # 60/min budget the snapshot cadence was sized around. Warm calls return above without locking.
    with _FIELD_MAP_LOCK:
        if key in _FIELD_MAP:   # filled while we waited for the lock
            return _FIELD_MAP[key]
        path, disk = _fields_path(), None
        try:
            with open(path, encoding="utf-8-sig") as f:   # utf-8-sig: a pre-v2.2 cache may carry a BOM
                disk = json.load(f)
        except Exception:
            disk = None
        fields = (disk or {}).get(key)
        if fields:
            _FIELD_MAP_DISK.add(key)
        elif allow_fetch:
            fields = _fetch_field_meta(key, disk)
        m = {f.get("fieldName"): (f.get("dataType") or "String")
             for f in (fields or []) if f.get("fieldName")}
        _FIELD_MAP[key] = m
        return m


def _fetch_field_meta(key, disk):
    """One table's slimmed metadata from the API, written into the per-stack cache. None if unreachable."""
    try:
        fields = _slim_meta(call("GET", "/CMDB/v2/data/metadata/%s" % key)["metadata"])
        merged = dict(disk or {})
        merged[key] = fields
        merged.setdefault("fqdn", load_config()[0])
        _ensure_cfg_dir()
        with open(_fields_path(), "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)
    except Exception:      # a scoped token may not reach metadata; degrade, don't fail the query
        return None
    _FIELD_MAP_LIVE.add(key)
    return fields


def _refetch_field_map(table):
    """Re-read one table's metadata after a cached map missed a name. True if the field set changed.

    The disk cache never expired, so a field that appeared after it was written -- which is exactly what
    enabling a connector does: an HR connector brings `alias_dayforce_employee_*` and the customer's
    Dayforce SmartLabels -- was refused as "doesn't exist" by every verb until someone ran
    `refresh-fields` by hand. Measured on a live stack: the cache knew 280 user fields, the API had 282,
    and the missing two were the HR source's. Once per table per process, so a typo costs one metadata
    call rather than one per query. A changed field set drops the label and result caches for the same
    reason `refresh-fields` does: both are derived from this metadata.
    """
    key = "user" if table.startswith("user") else "asset"
    # Compare against the map this process is actually using -- loading it first also means a cold,
    # cache-less start (which fetches live) returns below instead of fetching the same metadata twice.
    # Only a map READ FROM DISK can be stale. One fetched live this run is current, and one placed in
    # the memo any other way (the offline suite does this) must never reach the network from here --
    # the first version refetched for those too, and the "offline" suite quietly called a live stack.
    old = set(load_field_map(key))
    if key not in _FIELD_MAP_DISK or key in _FIELD_MAP_LIVE:
        return False
    with _FIELD_MAP_LOCK:
        if key in _FIELD_MAP_LIVE:
            return False
        try:
            with open(_fields_path(), encoding="utf-8-sig") as f:
                disk = json.load(f)
        except (Exception, SystemExit):   # no config resolves: nothing to refetch against
            _FIELD_MAP_LIVE.add(key)
            return False
        fields = _fetch_field_meta(key, disk)
        _FIELD_MAP_LIVE.add(key)        # even on failure: never retry a blocked endpoint per query
        if fields is None:
            return False
        m = {f.get("fieldName"): (f.get("dataType") or "String") for f in fields if f.get("fieldName")}
        _FIELD_MAP[key] = m
    if set(m) != old:
        drop_labels_cache()
        drop_rescache()
        return True
    return False


# SmartLabel metadata declares its own type names, which are not the query DSL's. Only these two
# differ; everything else (Datetime/Float/Integer/List) passes through unchanged.
SMARTLABEL_TYPES = {"Str": "String", "Boolean": "Binary"}
_LABELS = None
_LABELS_LOCK = threading.Lock()   # cold-cache fills race under parallel(); see load_labels
# True when the labels in _LABELS could not be resolved to fields because the metadata they resolve
# against was unreachable. Such a result is never written to disk, and `labels` reports it rather than
# letting it read as "the customer hasn't defined that label".
_LABELS_PROVISIONAL = False


def _labels_path():
    fqdn, _, _ = load_config()
    return os.path.join(CFG_DIR, "labels.%s.json" % re.sub(r"[^A-Za-z0-9._-]", "_", fqdn))


def drop_labels_cache():
    """Forget the cached SmartLabels so the next need refetches them. True if a file was removed.

    A SmartLabel is the customer's own vocabulary and they keep adding to it, so this cache going stale
    is the expected case rather than the exception -- unlike field metadata, where a new field is rare.
    Nothing invalidated the file, so a label defined after the first `labels` run stayed invisible
    indefinitely: `refresh-fields` cleared the field map and never touched this.
    """
    global _LABELS, _LABELS_PROVISIONAL
    _LABELS, _LABELS_PROVISIONAL = None, False
    try:
        os.remove(_labels_path())
        return True
    except OSError:
        return False


def load_labels(allow_fetch=True):
    """The stack's SmartLabels, resolved to queryable fields. Cached per stack like field metadata.

    Worth having because a SmartLabel is the customer's *own* vocabulary for their environment -- and
    Meridian ships an `llmBusinessValue` description with each one, written to be read by a model. The
    skill previously ignored all of it and answered only in generic field names, so "which crown jewels
    are exposed?" had nowhere to land even when the customer had defined exactly that label.

    The table is resolved by looking the field up in asset/user metadata rather than by trusting
    `field_collection`, whose values are stack-specific names (one demo stack calls them
    `AWS_CMDB_Output` and `User_Combine`) and would not generalise.
    """
    global _LABELS, _LABELS_PROVISIONAL
    if _LABELS is not None:
        return _LABELS
    # Locked for the same reason as load_field_map: measure_metrics fans out up to 20 metrics, and
    # with a cold labels cache each SmartLabel metric's thread issued its own smartlabel/search call
    # (plus re-triggering the field-map fetches the labels resolve against). Warm calls never lock.
    with _LABELS_LOCK:
        if _LABELS is not None:   # filled while we waited for the lock
            return _LABELS
        path = _labels_path()
        try:
            with open(path, encoding="utf-8-sig") as f:
                _LABELS = json.load(f)["labels"]
                return _LABELS
        except Exception:
            pass
        if not allow_fetch:
            _LABELS = []
            return _LABELS
        # The search call and the two field-map loads are independent, so fan them out rather than
        # paying three round trips serially -- load_field_map() never raises (it degrades to {} on
        # its own), so only the search result needs an isinstance(Exception) check below.
        raw, amap, umap = parallel([
            lambda: call("POST", "/CMDB/v2/smartlabel/search", {"paging": {"page": 0, "recordsPerPage": 500}}),
            lambda: load_field_map("asset"),
            lambda: load_field_map("user"),
        ])
        if isinstance(raw, Exception):   # a scoped token may not reach SmartLabels; degrade rather than fail a query
            _LABELS = []
            return _LABELS
        # Each label's table is resolved from that metadata, so when it is unreachable every label comes back
        # with no table and `queryable: false` -- which reads exactly like "the customer never defined that
        # label". Caching it would let one 403 hide every SmartLabel permanently, and `labels --search` would
        # answer "the term may simply not be one of them" about a label that is sitting right there.
        _LABELS_PROVISIONAL = not (amap or umap)
        out = []
        for s in (raw if isinstance(raw, list) else raw.get("data") or []):
            fname = s.get("field_name")
            if not fname:
                continue
            table = "asset" if fname in amap else ("user" if fname in umap else None)
            declared = str(s.get("field_type") or "")
            out.append({"name": s.get("friendly_name") or fname, "field": fname, "table": table,
                        "type": SMARTLABEL_TYPES.get(declared, declared) or "String",
                        "purpose": (s.get("llmBusinessValue") or "").strip(),
                        "queryable": table is not None})
        out.sort(key=lambda x: (x["table"] or "zz", x["name"].lower()))
        if not _LABELS_PROVISIONAL:
            try:
                _ensure_cfg_dir()
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"fqdn": load_config()[0], "labels": out}, f, indent=2)
            except Exception:
                pass
        _LABELS = out
        return _LABELS


def find_label(term):
    """SmartLabels matching a business term, best first. Exact name, then substring, then fuzzy."""
    labels = [l for l in load_labels() if l["queryable"]]
    if not labels:
        return []
    t = (term or "").strip().lower()
    exact = [l for l in labels if l["name"].lower() == t or l["field"].lower() == t]
    sub = [l for l in labels if l not in exact and (t in l["name"].lower() or t in l["field"].lower())]
    near = []
    if not exact and not sub:
        names = {l["name"]: l for l in labels}
        import difflib
        near = [names[n] for n in difflib.get_close_matches(term, list(names), n=5, cutoff=0.55)]
    return exact + sub + near


def cmd_labels(a):
    if getattr(a, "refresh", False):
        drop_labels_cache()
    labels = load_labels()
    if not labels:
        jout({"labels": [], "note": "No SmartLabels readable on this stack - the endpoint "
                                                "may need a full User Generated token."}); return
    hits = find_label(a.search) if a.search else [l for l in labels if l["queryable"]]
    if a.table:
        hits = [l for l in hits if l["table"] == a.table]
    # The purpose text runs 180-300 characters each, so a full listing is mostly prose. Earlier
    # revisions truncated it to 110 characters instead of dropping it, which barely helped:
    # measured on the demo stack, 151 truncated blurbs were 16,947 of the payload's 42,895
    # characters -- 40%, ~4,200 tokens -- on a call whose job is "what vocabulary exists here".
    # The name and the field are the answer to that; the purpose is what you want once you have
    # narrowed, so it now appears in full on a search and not at all on a bare listing.
    detail = bool(a.search) or len(hits) <= 12
    rows = [{"name": l["name"], "field": l["field"], "table": l["table"], "type": l["type"],
             **({"purpose": l["purpose"]} if detail and l["purpose"] else {})}
            for l in hits]
    out = {"labelsDefined": len(labels), "queryable": sum(1 for l in labels if l["queryable"]),
           "shown": len(rows), "labels": rows}
    if _LABELS_PROVISIONAL:
        # Nothing resolved, because the field metadata it resolves against was unreachable. Reporting
        # these as "not queryable" -- or a search as "no match" -- would deny labels that exist.
        out["fieldMetadataUnavailable"] = True
        out["note"] = ("%d SmartLabels are defined, but asset/user field metadata was unreachable, so "
                       "none could be resolved to a queryable field. This is NOT evidence that a term "
                       "isn't one of their labels - say the lookup was unavailable, then retry, or run "
                       "`refresh-fields` followed by `labels --refresh`." % len(labels))
        jout(out); return
    if not detail and any(l["purpose"] for l in hits):
        # Say the text exists rather than letting its absence read as "these labels have no
        # description" -- the same reason a truncated result set carries `truncated`.
        out["purposeOmitted"] = ("Each label also carries the customer's own business-value "
                                 "description; pass --search <term> to read it for a specific one.")
    unq = [l["name"] for l in labels if not l["queryable"]]
    if unq:
        out["notQueryable"] = unq
    if a.search and not rows:
        out["note"] = ("No SmartLabel matches %r. These are the customer's own labels, so the term may "
                       "simply not be one of them - fall back to the generic fields. If they insist it "
                       "exists, it may postdate the cache: `labels --refresh`." % a.search)
    jout(out)


def cmd_refresh_fields(a):
    fqdn, _, _ = load_config()
    asset, user = parallel([lambda: call("GET", "/CMDB/v2/data/metadata/asset")["metadata"],
                            lambda: call("GET", "/CMDB/v2/data/metadata/user")["metadata"]])
    for r in (asset, user):
        if isinstance(r, Exception):
            raise r
    path = _fields_path()
    _ensure_cfg_dir()
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"fqdn": fqdn, "asset": _slim_meta(asset), "user": _slim_meta(user)}, f, indent=2)
    _FIELD_MAP.clear()
    # SmartLabel tables are resolved against this metadata, so a refresh of it has to re-resolve them --
    # otherwise a label cached as unqueryable against the old field list stays that way forever.
    # And the aggregate cache. `field_type()` picks the query operator for a breakdown, so a stale
    # field map produces a wrong one -- that is the `overcountedRecords` case, whose own note tells the
    # user to "run refresh-fields and re-run". Without dropping the cache here, the re-run would be
    # served the same bad breakdown from disk and the advice would silently not work.
    result = {"cached": path, "assetFields": len(asset), "userFields": len(user),
              "labelsCacheCleared": drop_labels_cache(), "resultCacheCleared": drop_rescache()}
    if a.search:
        s = a.search.lower()
        hit = lambda f: s in (f.get("fieldName") or "").lower() or s in (f.get("displayName") or "").lower()
        result["matches"] = ["[asset] %s (%s)" % (f["fieldName"], f["dataType"]) for f in asset if hit(f)] + \
                            ["[user] %s (%s)" % (f["fieldName"], f["dataType"]) for f in user if hit(f)]
    jout(result)


def field_type(table, field):
    return load_field_map(table).get(field) or "String"


def field_problem(table, *fields):
    """The message for the first field name this stack doesn't have, or None. Never exits.

    Same split as `clause_or_problem`, for the same reason: `metrics` re-validates a *stored* definition
    on every snapshot, and one metric naming a field that has since been removed must be reported
    unavailable rather than terminate the run that noticed.
    """
    m = load_field_map(table)
    if not m:
        return None     # metadata unavailable (e.g. a scoped token) - don't invent a failure
    if any(f not in m for f in fields if f) and _refetch_field_map(table):
        m = load_field_map(table)   # the cache was stale; judge against what the stack has now
    for field in [f for f in fields if f]:
        if field in m:
            continue
        exact_ci = [k for k in m if k.lower() == field.lower()]
        if exact_ci:
            return ("Field %r doesn't exist on this stack, but %r does -- field names are case-sensitive."
                    % (field, exact_ci[0]))
        import difflib
        near = difflib.get_close_matches(field, list(m), n=3, cutoff=0.6)
        hint = (" Did you mean %s?" % ", ".join(repr(n) for n in near)) if near else \
               (" Run `refresh-fields --search <term>` to find the right name.")
        return ("Field %r doesn't exist on the %s table of this stack (%d fields known).%s"
                % (field, "user" if table.startswith("user") else "asset", len(m), hint))
    return None


def check_fields(table, *fields):
    """Fail fast on a field name this stack doesn't have, naming the likely intended one.

    Without this a typo is indistinguishable from a genuine zero: `--where "Rsk_Score >= Float 500"`
    returned `totalRecords: 0`, and `top --field Rsk_Score` returned "no matches". Both are confident
    wrong answers to a reasonable question, and field names here are easy to get wrong (`sourcetype`
    lowercase, `Asset_Type` underscored). Case-only misses are called out separately because the API
    is case-sensitive and that is the most common way to get this wrong.
    """
    problem = field_problem(table, *fields)
    if problem:
        die(problem)


def top_n(table, field, n, where=None, select=None):
    """Top-N by a numeric field, returned rather than printed so `digest` can reuse it verbatim.

    Rebuilding this inline for the digest would mean a second copy of the threshold search, the tail
    paging and the truncation flag -- the drift risk CLAUDE.md warns about. One implementation.
    """
    class _A: pass
    a = _A(); a.table, a.field, a.top, a.where, a.select = table, field, n, where or [], select
    where = a.where or []
    check_fields(a.table, a.field, *clause_fields(where), *select_fields(a.select))
    calls = {"n": 0}
    calls_lock = threading.Lock()   # probe() is now called from parallel() threads

    def probe(t):
        """Count records at/above a threshold, using the cheapest possible page."""
        with calls_lock:
            calls["n"] += 1
        q = and_query(where, {"searchFieldName": a.field, "operator": ">=", "type": "Float", "value": float(t)})
        return count_records(a.table, q)

    # Start the descent at the rung that worked last time for this stack+table+field, so a
    # Risk_Score query stops probing 100000/10000 on a stack whose scores top out in the hundreds.
    # Nothing here can go wrong if the cache is stale: too high and we descend as before, too low
    # and the refinement below tightens it.
    start = _top_rung_start(a.table, a.field)
    hi = None      # a threshold known to yield FEWER than `top` records (an upper bracket)
    thresh = total = None
    # A warm cache usually matches on the very first probe, so speculating there would burn two calls
    # every time for nothing. Cold, the descent can run most of the ladder, and each rung is a full
    # round trip -- so probe a batch at once and keep the highest rung that fills. The wasted probes
    # in the winning batch are the price of not waiting for them one at a time.
    batch = 1 if start > 0 else 3
    i = start
    while i < len(TOP_LADDER):
        idxs = list(range(i, min(i + batch, len(TOP_LADDER))))
        if len(idxs) == 1:
            ns = [probe(TOP_LADDER[idxs[0]])]
        else:
            ns = parallel([(lambda k=k: probe(TOP_LADDER[k])) for k in idxs])
        hit = None
        for k, n in zip(idxs, ns):
            if isinstance(n, Exception):
                raise n
            if n >= a.top or TOP_LADDER[k] == 0:
                hit = (TOP_LADDER[k], n)
                break
            hi = TOP_LADDER[k]   # under-filled, so it brackets the threshold from above
        if hit:
            thresh, total = hit
            break
        i += batch
    if thresh is None or total == 0:
        return {"table": a.table, "field": a.field, "top": [], "note": "no matches"}
    rung = thresh

    # A warm cache hits on its first probe and so never learns that a higher rung would also fill:
    # a stack cached at 1000 kept reading three times the records that 3000 would have. When the hit is loose,
    # probe the rung above once -- a count, so it downloads no record (count_records) -- and climb
    # while it still fills. A tight hit skips it, so the usual warm run stays one probe. Past
    # TOP_REFINE_MIN the refinement below climbs anyway, so this only covers the range under it.
    if hi is None and start > 0 and total >= TOP_LOOSE_FACTOR * a.top and total <= TOP_REFINE_MIN:
        j = TOP_LADDER.index(rung) - 1
        while j >= 0:
            n = probe(TOP_LADDER[j])
            if n >= a.top:
                rung, thresh, total = TOP_LADDER[j], TOP_LADDER[j], n
                j -= 1
            else:
                hi = TOP_LADDER[j]
                break

    # There is no server-side sort, so the top N is only correct if we read EVERY record above the
    # threshold. That makes a loose threshold expensive (one call per 100 records), so tighten it
    # first -- but only when it pays for itself. Refining costs up to 4 probes (one to bracket, three
    # to bisect) and can save at most `pages - 1` fetches, so it is only worth doing past ~5 pages.
    # Gating at one page instead measurably LOST calls on mid-sized tails.
    if total > TOP_REFINE_MIN:
        if hi is None:  # started mid-ladder, so climb until a rung under-fills to bracket it
            j = TOP_LADDER.index(rung) - 1
            while j >= 0:
                n = probe(TOP_LADDER[j])
                if n >= a.top:
                    rung, thresh, total = TOP_LADDER[j], TOP_LADDER[j], n
                    j -= 1
                else:
                    hi = TOP_LADDER[j]
                    break
        lo = float(thresh)
        if hi is None:
            # Even the ladder's top rung filled. The ladder assumed 100000 bounds every field, but
            # epoch timestamps and byte counts live entirely above it, so nothing brackets from
            # above and the span math below would compute float(None) and kill the verb. Climb x10
            # until a probe under-fills; every one that still fills is itself a better threshold.
            cand = lo * 10
            for _ in range(8):   # 1e6..1e13 covers epoch millis; past that, refinement isn't worth more probes
                n = probe(cand)
                if n >= a.top:
                    lo, total = cand, n
                    cand *= 10
                else:
                    hi = cand
                    break
        # Three evenly-spaced probes at once narrow the interval to a quarter; three *sequential*
        # bisections reach an eighth. Same call budget, one round trip instead of three -- and the
        # only thing the tighter bound buys is a page or two fewer later, so the trade is worth it.
        # No bracket after the climb means the field's values outrun 1e13 -- fall through with the
        # best filling threshold and let the truncation note say the tail was cut, rather than crash.
        if total > max(a.top, 100) and hi is not None:
            span = float(hi) - lo
            pts = [lo + span * f for f in (0.25, 0.5, 0.75)]
            pts = [p for p in pts if lo < p < float(hi)]
            if pts:
                ns = parallel([(lambda p=p: probe(p)) for p in pts])
                for p, n in zip(pts, ns):   # ascending, so the last qualifying point wins
                    if isinstance(n, Exception):
                        continue
                    if n >= a.top:
                        lo, total = p, n
        thresh = lo
    _top_rung_save(a.table, a.field, rung)

    pages = (total + 99) // 100
    truncated = pages > TOP_MAX_PAGES
    npages = min(pages, TOP_MAX_PAGES)
    q = and_query(where, {"searchFieldName": a.field, "operator": ">=", "type": "Float", "value": float(thresh)})

    # The tail pages are independent -- page 3 doesn't depend on page 2 -- and the rows get sorted
    # client-side afterwards, so order doesn't matter either. Serially this was ~2.3s per page, so a
    # full 20-page tail spent most of a minute waiting on round trips it could have overlapped.
    def fetch(p):
        return call("POST", "/CMDB/v2/data/cmdb",
                    {"table": a.table, "query": q, "paging": {"page": p, "recordsPerPage": 100}})["data"]

    calls["n"] += npages
    fields = a.select.replace(",", " ").split() if a.select else (
        ["Owner_Name", a.field, "Risk_Level", "Owner_Department"] if a.table.startswith("user")
        else ["Asset_Name", a.field, "Risk_Level", "IP_Address", "OS"])
    # Projected per page as they land -- to the selected fields plus the ranked one, which the sort
    # below needs even when a --select omits it. Raw pages are ~1.1MB of 312-field records, and a
    # 20-page tail held ~22MB of them just to keep 5 fields each.
    keep = list(dict.fromkeys(fields + [a.field]))
    rows = []
    for r in parallel([(lambda p=p: fetch(p)) for p in range(npages)]):
        # A missing page silently shortens the tail, which is exactly how a top-N goes quietly wrong.
        if isinstance(r, Exception):
            raise r
        rows.extend({f: rec.get(f) for f in keep} for rec in r)
    rows.sort(key=lambda r: float(r.get(a.field) or 0), reverse=True)
    top = [{f: r.get(f) for f in fields} for r in rows[:a.top]]
    out = {"table": a.table, "field": a.field, "matchedAtThreshold": thresh, "totalInTail": total,
           "apiCalls": calls["n"], "top": top}
    if truncated:
        out["truncated"] = True
        out["note"] = ("Tail spans %d records; only the first %d were read, so this may not be the "
                       "true top %d. Narrow with --where." % (total, TOP_MAX_PAGES * 100, a.top))
    return out


def cmd_top(a):
    # straddle: the ladder descent plus up to TOP_MAX_PAGES tail pages spans long enough for a merge
    # to land mid-ranking, and a ranking half from each LDG is not a ranking of either.
    out = with_currency([_table_kind(a.table)], lambda: top_n(a.table, a.field, a.top, a.where, a.select),
                        straddle=True)
    emit(out, "top", getattr(a, "format", None), getattr(a, "out", None))


def cmd_list(a):
    multi = bool(a.all or max(1, a.limit) > 100) and not a.count_only
    out = with_currency([_table_kind(a.table)], lambda: list_records(a), straddle=multi)
    if a.count_only:
        jout(out); return
    emit(out, "rows", getattr(a, "format", None), getattr(a, "out", None))


def list_records(a):
    """`list`'s result, returned rather than printed so the rebuild stamp can be read beside it."""
    where = a.where or []
    check_fields(a.table, *clause_fields(where), *select_fields(a.select))
    key = "Owner_Name" if a.table.startswith("user") else "Asset_Name"
    q = and_query(where) if where else [[{"searchFieldName": key, "operator": "exists", "type": "String", "value": None}]]
    # Every page response carries totalRecords, so a request one page can satisfy (the default
    # --limit 50) reads count AND rows from a single page-0 fetch. The count-first shape survives
    # for the other cases on purpose: --all needs the total before it can size the page range, and
    # a multi-page list is *faster* with the cheap count up front (all data pages then go out in
    # one concurrent wave; merging would serialize page 0 ahead of the rest).
    pages, api_calls = [], 1
    if a.count_only or a.all or max(1, a.limit) > 100:
        total = count_records(a.table, q)
        if a.count_only:
            return {"table": a.table, "where": where, "totalRecords": total}
    else:
        r0 = call("POST", "/CMDB/v2/data/cmdb", {"table": a.table, "query": q, "paging": {"page": 0, "recordsPerPage": 100}})
        total, pages = r0["totalRecords"], [r0["data"]]
    fields = a.select.replace(",", " ").split() if a.select else (
        ["Owner_Name", "displayName", "Owner_Department", "Risk_Score", "Risk_Level"] if a.table.startswith("user")
        else ["Asset_Name", "IP_Address", "Lucidum_Asset_Type", "OS", "Risk_Score", "Risk_Level"])
    # `--all` means every matching record, up to the safety ceiling. The old hard cap of 300 made
    # "show me all X" unanswerable the moment a filter matched more than that -- 1,201 KEV assets
    # returned 300 with no way to reach the rest. The ceiling exists because a page is ~1.1MB and one
    # API call: LIST_MAX_RECORDS pages is most of the 60/min budget, so past it the answer says to
    # narrow rather than quietly stalling behind the rate limiter for minutes.
    want = total if a.all else max(1, a.limit)
    cap = min(want, LIST_MAX_RECORDS)
    npages = min((cap + 99) // 100, (total + 99) // 100)
    tail = range(len(pages), npages)
    for r in parallel([(lambda p=p: call("POST", "/CMDB/v2/data/cmdb",
                        {"table": a.table, "query": q, "paging": {"page": p, "recordsPerPage": 100}})["data"])
                       for p in tail]):
        if isinstance(r, Exception):
            raise r
        pages.append(r)
    api_calls += len(tail)
    # Projected per page as they land: a raw page is ~1.1MB of 312-field records, so `list --all`
    # used to hold up to ~55MB of them before throwing away every unselected field anyway.
    rows = []
    for page in pages:
        rows.extend({f: r.get(f) for f in fields} for r in page)
    rows = rows[:cap]
    out = {"table": a.table, "where": where, "totalRecords": total, "shown": len(rows),
           "truncated": total > len(rows), "apiCalls": api_calls, "rows": rows}
    if out["truncated"]:
        out["note"] = (
            ("Read %d of %d matching records -- the ceiling is %d (%d pages, most of the 60/min API "
             "budget). Narrow with --where, or use `report` for a shareable full extract."
             % (len(rows), total, LIST_MAX_RECORDS, LIST_MAX_RECORDS // 100))
            if want > LIST_MAX_RECORDS else
            ("Showing %d of %d matching records. Pass --all for every match (up to %d), or raise "
             "--limit." % (len(rows), total, LIST_MAX_RECORDS)))
    return out


def cmd_summary(a):
    if a.metrics:
        jout(with_currency(["asset", "user"], stack_metrics, sections=METRICS_HISTORICAL_SECTIONS)); return
    if not a.by:
        die("Provide --by <field> or --metrics.")
    t = _table_kind(a.table)
    refresh = getattr(a, "refresh", False)
    if refresh or not rescache_scope_live("summary_by", RESCACHE_TTL, _summary_scope(a.table, a.by, a.where)):
        # Nothing cached can answer this under ANY rebuild, so the stamp isn't needed before the work
        # (it only keys the cache read) and is read beside it, as with_currency() does. The entry is
        # written afterwards, and only when the re-read shows no rebuild landed: an answer flagged
        # rebuildDuringQuery must not be served from cache as though it were clean.
        before, out = parallel([ldg_rebuild, lambda: summarize_by(a.table, a.by, a.where, refresh=True)])
        if isinstance(out, Exception):
            raise out
        before = _stamp_or_unknown(before)
        after = ldg_rebuild()
        rebuilt = (before.get(t) or {}).get("rebuiltUtc")
        if rebuilt and (after.get(t) or {}).get("rebuiltUtc") == rebuilt:
            summary_cache_put(out, a.table, a.by, a.where, rebuilt)
    else:
        # Sequential: the stamp is part of the cache key, so it has to be known before the cache can
        # be asked. A cache hit made no calls, so there is nothing to straddle.
        before = ldg_rebuild()
        out = summarize_by(a.table, a.by, a.where, rebuilt=(before.get(t) or {}).get("rebuiltUtc"))
        after = None if out.get("fromCache") else ldg_rebuild()
    emit(attach_currency(out, data_currency(before, [t], after)),
         "groups", getattr(a, "format", None), getattr(a, "out", None))


# /CMDB/v2/system/metrics/data returns today's totals beside a 30-day AVERAGE of them. The average
# describes the past, so it is labelled as such wherever it travels -- the "vs 30-day average" arrow
# is otherwise the most natural way for a historical figure to read as current.
METRICS_HISTORICAL_SECTIONS = {
    key: {"class": "historical", "source": "Meridian's 30-day average of daily totals"}
    for key in ("metrics.avg30DaysAssetCount", "metrics.avg30DaysUserCount")}


def stack_metrics():
    m, lic = parallel([lambda: call("GET", "/CMDB/v2/system/metrics/data"),
                       lambda: call("GET", "/CMDB/v2/system/metrics/license")])
    for r in (m, lic):
        if isinstance(r, Exception):
            raise r
    return {"metrics": m, "license": lic}


def summarize_by(table, by, where=None, refresh=False, rebuilt=None):
    """Group-by with its completeness verdict, returned rather than printed so `digest` reuses it.

    `rebuilt` is the table's LDG rebuild stamp (`ldg_rebuild()[table]["rebuiltUtc"]`) and is part of
    the cache key, so an entry from before a merge is a miss after it -- a TTL alone would serve a
    pre-merge count as current for up to RESCACHE_TTL. **No stamp, no cache**, read or write: an
    unknown rebuild cannot be matched against anything, and keying on None would reopen that hole.

    The stratified sample, the sum-vs-total check for single-valued fields and the coverage query for
    List fields all live here once. A digest that reimplemented any of it would be the silent-omission
    bug again, in a second place.

    Cached per stack for RESCACHE_TTL. This is the most expensive verb per token of output measured:
    `--by Risk_Level` is 11 calls and ~47 MB for 85 tokens, and `--by sourcetype` is 33 calls -- which
    does not fit the hard 60/min budget, so it spent 92.6s waiting on the pacer. Value discovery reads
    8 x 100-record pages at ~5.87 MB each purely to learn which values exist, and the answer for a
    given field changes on the stack's daily ingest cadence, not within a session.

    **An incomplete breakdown is cached too, deliberately** -- its `complete: false`, `note` and
    `unaccountedRecords` travel with it, so a cached read carries the same caveats as a fresh one.
    What is NOT cached is a breakdown whose coverage check could not run at all (`complete` absent),
    since re-running may well answer it.

    **`total` counts only records that have a value for `by`** (the `exists` gate), so records matching
    `where` with the field empty sit outside every group. `whereTotal` / `recordsWithoutField` state
    that gap, separately from `complete` -- see the comment where they are set for why.

    **A dotted `by` (`"Details.OS"`, `"Owner_Status.Is_Admin"`) reaches into a per-source Embed_List.**
    `--by`'s stratified sample used to always come back with `distinctValuesSeen: 0` for these: a fetched
    record stores `rec["Details"] = [{"OS": ..., ...}, ...]`, never a literal top-level `"Details.OS"`
    key, so the naive `rec.get(by)` used to read values found nothing, ever, regardless of sample size.
    `out["nested"]` marks when this path was taken. It also forces the List-style coverage-query
    completeness check regardless of the leaf's declared type, and skips the `exists`-based gate/
    denominator entirely -- see the comment where `nested` is computed for why `exists` measurably
    undercounts on these paths and the parent field's own `exists` is no substitute.
    """
    class _A: pass
    a = _A(); a.table, a.by, a.where = table, by, where or []
    where = a.where or []
    ck = {"table": table, "by": by, "where": sorted(where), "rebuilt": rebuilt}
    cached, age = (None, None) if refresh or not rebuilt else rescache_get("summary_by", RESCACHE_TTL, **ck)
    # An entry cached before the where-only count existed carries no answer to it, and a missing key
    # reads as nothing to report rather than as unknown -- so it is a miss. The nested path never
    # carries one, and never needed it.
    if cached is not None and ("whereTotal" in cached or "." in a.by):
        return _stamp_cache(cached, age)
    check_fields(a.table, a.by, *clause_fields(where))
    byType = field_type(a.table, a.by)
    op = "match" if byType == "List" else "=="
    # A dotted field (`Owner_Status.Is_Admin`) lives inside a per-source Embed_List. `match`/`==` resolve
    # it correctly server-side (checked against a live stack), but `exists` does not: measured 4 records
    # for `Owner_Status.Is_Admin exists` against 23 for `== Binary 1`, because most identities carry that
    # sub-field null on the sources that don't report it and `exists` only passes when the array has no
    # null entries for it. Querying the *parent* field's `exists` doesn't help either -- `Owner_Status
    # exists` was 66016 of 66016, true for every identity that has any embedded entry at all. There is no
    # reliable single-call "has this sub-field" filter, so a nested path skips the gate entirely and
    # samples the plain `where`-filtered population instead of a falsely narrow "exists" one.
    nested = "." in a.by
    parent_field, child_field = (a.by.split(".", 1) if nested else (None, None))
    if nested:
        q = and_query(where)
    else:
        exists = {"searchFieldName": a.by, "operator": "exists", "type": "String", "value": None}
        q = and_query(where, exists)

    def page_body(p, n=100):
        return {"table": a.table, "query": q, "paging": {"page": p, "recordsPerPage": n}}

    # Page 0 goes first because its totalRecords decides how many pages exist -- and it doubles as
    # the group-by denominator, so the separate count call this used to make was pure duplication.
    # Beside it, when the `exists` gate applies, one count of `where` alone: the gate is what keeps
    # records with no value out of the denominator, so it is also what hides them. Independent of
    # page 0, so it costs no round trip. Optional -- a failed count is flagged below, never fatal.
    # With no --where this is an empty query, which counts every record: checked live, it equals the
    # `Asset_Name exists` / `Owner_Name exists` count `list` uses, on both tables.
    if nested:
        first, where_total = call("POST", "/CMDB/v2/data/cmdb", page_body(0)), None
    else:
        first, where_total = parallel([
            lambda: call("POST", "/CMDB/v2/data/cmdb", page_body(0)),
            lambda: count_records(a.table, and_query(where))])
        if isinstance(first, Exception):
            raise first
    total = first["totalRecords"]
    resps = [first]
    lastpage = max(0, (total + 99) // 100 - 1)
    nsample = min(SUMMARY_SAMPLE_PAGES, lastpage + 1)
    # Spread the sample across the WHOLE result set instead of reading the first N pages. Records come
    # back clustered by source, not shuffled -- measured: page 0 held 3 distinct sourcetypes, page 1
    # held 4 -- so consecutive pages keep re-seeing the same values while entire categories sit further
    # in. Stratifying at identical cost found a 14,000-record source that the first-8-pages sample
    # missed completely.
    if nsample > 1:
        step = lastpage / float(nsample - 1)
        picks = sorted({int(round(i * step)) for i in range(1, nsample)})
        # Independent pages. Serially these cost ~2.3s each against a remote stack; concurrently the
        # whole sample costs roughly one page's latency.
        for r in parallel([(lambda p=p: call("POST", "/CMDB/v2/data/cmdb", page_body(p)))
                           for p in picks]):
            if not isinstance(r, Exception):
                resps.append(r)
    sampled = sum(len(r.get("data") or []) for r in resps)

    freq = {}
    for r in resps:
        for rec in r.get("data") or []:
            if nested:
                # A record carries `Details.OS` as `rec["Details"] = [{"OS": ..., ...}, ...]`, never as
                # a literal top-level `"Details.OS"` key -- so the plain `rec.get(a.by)` below always
                # missed, which is why `summary --by "Details.OS"` used to report 0 distinct values on
                # every run regardless of sample size. Each entry can carry its own value (one per
                # source), so all of them are collected, the same as a genuine List field's values.
                entries = rec.get(parent_field)
                if isinstance(entries, dict):
                    entries = [entries]
                values = [e.get(child_field) for e in entries if isinstance(e, dict)] \
                    if isinstance(entries, list) else []
            else:
                v = rec.get(a.by)
                values = (v if isinstance(v, list) else [v]) if v is not None else []
            for x in values:
                if x is None:
                    continue
                freq[str(x)] = freq.get(str(x), 0) + 1
    seen = set(freq)
    # High cardinality used to return no groups at all -- "break assets down by OS" (51 values) gave
    # nothing. Counting all of them would be one call each, so instead count the biggest ones the sample
    # saw and let the coverage check below state exactly how many records the rest hold. Partial and
    # honest beats empty.
    capped = len(seen) > SUMMARY_MAX_GROUPS
    if capped:
        vals = sorted(sorted(freq, key=lambda k: freq[k], reverse=True)[:SUMMARY_MAX_GROUPS])
    else:
        vals = sorted(seen)
    counts = parallel([(lambda v=v: count_records(
                            a.table, and_query(where, {"searchFieldName": a.by, "operator": op,
                                                       "type": byType, "value": v})))
                       for v in vals])
    groups = [{"value": v, "count": c, "percent": round(100.0 * c / total, 1) if total else 0}
              for v, c in zip(vals, counts) if not isinstance(c, Exception)]
    groups.sort(key=lambda g: g["count"], reverse=True)
    out = {"table": a.table, "by": a.by, "fieldType": byType, "total": total,
           "sampledForValues": sampled, "distinctValuesSeen": len(seen), "groups": groups}
    if nested:
        out["nested"] = True
    failed = [v for v, c in zip(vals, counts) if isinstance(c, Exception)]
    if failed:
        out["countsUnavailable"] = failed
    if capped:
        out["groupsCapped"] = SUMMARY_MAX_GROUPS

    # Which values EXIST is discovered from a sample; each value's count is then exact. So a category
    # the sample never reached used to vanish from the breakdown with nothing said -- a posture answer
    # that looked complete and wasn't. For a single-valued field the counts partition the records, so
    # comparing their sum against the total detects that exactly, at no extra call. A nested field is
    # excluded even when its leaf type isn't List: one record's embedded array can carry the sub-field
    # on several source entries at once, so it is exactly as multi-valued at the record level as a real
    # List field is, and the sum-vs-total check would be comparing against the wrong shape of total.
    if byType != "List" and not capped and not nested:
        accounted = sum(g["count"] for g in groups)
        out["accountedRecords"] = accounted
        # `== total`, not `>= total`. An over-count is not a success: on a single-valued field the
        # counts partition the records, so a sum ABOVE the total means records are landing in several
        # groups -- which is the signature of a field that is really multi-valued being queried as if
        # it weren't. Treating `>=` as complete blessed exactly that failure (38,943 accounted against
        # a 34,229 total, reported as complete) instead of catching it.
        out["complete"] = accounted == total and not failed
        if accounted < total:
            out["unaccountedRecords"] = total - accounted
            out["note"] = (
                "%d of %d records (%.1f%%) hold a value that did not appear in the %d-record sample "
                "used to discover values, so those categories are missing below. Narrow with --where "
                "to bring them into range, or query a specific value directly."
                % (total - accounted, total, 100.0 * (total - accounted) / total, sampled))
        elif accounted > total:
            out["overcountedRecords"] = accounted - total
            out["note"] = (
                "Counts sum to %d against a total of %d, so records are being counted in more than one "
                "group. %r is typed %s here but behaves as multi-valued, which usually means this "
                "stack's field metadata is stale -- run `refresh-fields` and re-run. Treat the "
                "percentages as unreliable until then."
                % (accounted, total, a.by, byType))
    else:
        # A List record, or a record whose value lives inside a per-source embedded list, can match
        # several values, so sum-vs-total proves nothing -- which is why this branch had no completeness
        # check at all, and why a breakdown could omit 37% of the inventory in silence (measured: 12,680
        # of 34,229 records, with a 14,000-record source missing outright). Instead, count the records
        # matching ANY discovered value in one OR query -- several objects in
        # a single inner array -- and the shortfall against the total is exact. Same guarantee the
        # single-valued branch gets, for one extra call.
        covered = None
        if vals:
            try:
                or_group = [{"searchFieldName": a.by, "operator": op, "type": byType, "value": v}
                            for v in vals]
                covered = count_records(a.table, and_query(where) + [or_group])
            except Exception:
                covered = None
        overlap = ("Counts sum to more than the total because one record can hold several values. "
                   if sum(g["count"] for g in groups) > total else "")
        if covered is None:
            out["note"] = overlap + "Coverage could not be verified on this run."
        else:
            out["coveredRecords"] = covered
            out["complete"] = covered >= total and not failed and not capped
            if covered < total:
                out["unaccountedRecords"] = total - covered
                out["note"] = (
                    overlap +
                    "%d of %d records (%.1f%%) hold only values missing from this breakdown - the "
                    "%d-record sample never reached them%s. Narrow with --where, or query a specific "
                    "value directly."
                    % (total - covered, total, 100.0 * (total - covered) / total, sampled,
                       " (and %d of %d discovered values were dropped by the group cap)"
                       % (len(seen) - len(vals), len(seen)) if capped else ""))
            else:
                # Not "every record" when the gate applies: records with no value are outside `total`,
                # and the without-field sentence below would contradict it.
                out["note"] = overlap + ("Every record falls in at least one group below." if nested
                                         else "Every record with a value for %r falls in at least one "
                                              "group below." % a.by)
    if capped and "note" not in out:
        out["note"] = ("%d distinct values seen in the sample; showing the %d largest. Narrow with "
                       "--where for a complete breakdown." % (len(seen), len(vals)))

    # The `exists` gate means `total`, every percentage, `accountedRecords` and `complete` describe
    # only the records that HAVE a value for `by`. A record matching --where with the field empty was
    # in no group and no count, so the breakdown read as the whole population when it wasn't (measured:
    # about one in five of a filtered server population, reported complete). The where-only count
    # states the gap.
    #
    # `complete` deliberately keeps its meaning -- the groups account for every record that has the
    # field -- rather than turning false here. It answers a different question: the discovery
    # sample's reach, whose remedy is narrowing the query. An unpopulated field is a fact about the
    # data with no such remedy, and often by design (a cloud-provider field on on-prem servers).
    # Folding the two together would mark most breakdowns incomplete for good, have the digest label
    # a data fact "records unaccounted", and give snapshots written before this a different
    # definition of `complete` from those after, which a trend would compare as if they matched.
    # `recordsWithoutField` is the separate signal. Absent -- on older records, or the nested path,
    # which has no gate -- means unknown, never 0.
    if not nested:
        if isinstance(where_total, int) and where_total >= total:
            out["whereTotal"] = where_total
            out["recordsWithoutField"] = where_total - total
            if where_total > total:
                gap = where_total - total
                out["note"] = (
                    "%d of %d %s (%.1f%%) have no value for %r and are in no group below; the total, "
                    "percentages and completeness cover only the %d that do."
                    % (gap, where_total, "records matching --where" if where else "records",
                       100.0 * gap / where_total, a.by, total)
                    + (" " + out["note"] if out.get("note") else ""))
        else:
            # A failed count, or one below the gated total (the two reads straddled a change). Either
            # way the gap is unknown -- flagged, and kept out of the cache so a re-run can answer it.
            out["whereTotalUnavailable"] = True
            out["note"] = (
                "Records matching the query with no value for %r could not be counted on this run, "
                "so some may be missing from every group below." % a.by
                + (" " + out["note"] if out.get("note") else ""))
    summary_cache_put(out, table, by, where, rebuilt)
    return out


def _summary_scope(table, by, where):
    """What a summary --by entry answers, without the rebuild that versions it."""
    return _rescache_key("summary_by_scope", table=table, by=by, where=sorted(where or []))


def summary_cache_put(out, table, by, where, rebuilt):
    """Cache a breakdown under its rebuild stamp -- if it answered its own coverage question.

    `complete` present means the coverage question was answered, either way. Absent means the check
    itself could not run, and caching that would fix an unanswered question in place for the TTL. An
    unknown without-field count is the same kind of unanswered question. No stamp, no cache."""
    if "complete" in out and rebuilt and not out.get("whereTotalUnavailable"):
        rescache_put("summary_by", out, scope=_summary_scope(table, by, where),
                     table=table, by=by, where=sorted(where or []), rebuilt=rebuilt)


def build_digest(table="asset", by=None, field="Risk_Score", top=5, refresh=False):
    """One command that assembles a whole periodic posture review, for scheduled delivery.

    Everything else in this CLI is pull: someone has to think to ask. A digest is the same information
    on a cadence -- totals, what's feeding them, the current risk shape, and who is at the top of it --
    so a weekly exec PDF costs one scheduled command rather than a person remembering.

    Composed from the existing building blocks (`stack_metrics`, `summarize_connectors`, `summarize_by`,
    `top_n`), so every completeness caveat and health verdict comes through unchanged rather than being
    approximated here.

    Returns the payload rather than printing it, so `snapshot` can reduce the very same result to a
    history record without paying for a second round of calls -- the same split `cmd_top` and
    `cmd_summary` already have.

    `refresh=True` bypasses the aggregate cache, and **`take_snapshot` always passes it**. A snapshot
    is not a view, it is the row a trend will later compare: writing an hour-old breakdown under
    today's date would manufacture a flat segment out of a value that was never measured today, and a
    flat line is the single most convincing wrong answer this tool can give. Interactive `digest`
    (including the one piped to `report`) may serve from cache, because its output is stamped with
    `fromCache`/`cacheAgeSeconds` and read by someone who can see the age.
    """
    by = by or "Risk_Level"
    # The stamp is read ahead of the breakdown, whose cache is keyed on it, but inside the batch, so
    # the other four sections don't wait for it -- the same overlap with_currency() makes. Re-read
    # last: a digest is ~20 calls across five sections, and one assembled across a merge would mix two
    # LDGs in one document.
    stamp = {}

    def breakdown_section():
        stamp["before"] = ldg_rebuild()
        return summarize_by(table, by, refresh=refresh,
                            rebuilt=(stamp["before"].get(_table_kind(table)) or {}).get("rebuiltUtc"))
    parts = parallel([
        lambda: stack_metrics(),
        lambda: summarize_connectors(refresh=refresh),
        breakdown_section,
        lambda: top_n("user", field, top),
        lambda: top_n("asset", field, top),
    ])
    before = _stamp_or_unknown(stamp.get("before", RuntimeError("the stamp read did not run")))
    metrics, connectors, breakdown, topusers, topassets = parts
    out = {"generated": "digest", "stack": load_config()[0], "table": table,
           "rankedBy": field, "topN": top}
    problems = []
    for label, val, key in [("metrics", metrics, "metrics"), ("connectors", connectors, "connectors"),
                            ("breakdown", breakdown, "breakdown"), ("topUsers", topusers, "topUsers"),
                            ("topAssets", topassets, "topAssets")]:
        if isinstance(val, Exception):
            # A digest runs unattended, so a failed section must be named rather than silently absent --
            # otherwise a scheduled report quietly shrinks and nobody notices which part stopped working.
            problems.append({"section": key, "error": str(val)[:200]})
        else:
            out[key] = val
    if problems:
        out["sectionsUnavailable"] = problems
    m = (out.get("metrics") or {}).get("metrics") or {}
    out["headline"] = {
        "assets": m.get("assetCount"), "users": m.get("userCount"),
        "assets30DayAvg": m.get("avg30DaysAssetCount"), "users30DayAvg": m.get("avg30DaysUserCount"),
        "connectorsEnabled": (out.get("connectors") or {}).get("summary", {}).get("connectorsEnabled"),
        "connectorsFailing": (out.get("connectors") or {}).get("summary", {}).get("failing"),
        "breakdownComplete": (out.get("breakdown") or {}).get("complete"),
    }
    sections = dict(METRICS_HISTORICAL_SECTIONS)
    sections.update({key: {"class": "historical", "source": "Meridian's 30-day average of daily totals"}
                     for key in ("headline.assets30DayAvg", "headline.users30DayAvg")})
    return attach_currency(out, data_currency(before, ["asset", "user"], ldg_rebuild()), sections)


def cmd_digest(a):
    # `--snapshot` makes this a measurement, not a view, so it computes fresh even though the plain
    # digest may serve from cache: the payload printed here is the same object appended to history,
    # and a cached breakdown written under today's date is a value that was never measured today.
    # This is the scheduled path (`digest --snapshot` on a cron/Task Scheduler), so it is the one that
    # would have accumulated the damage silently.
    snap = getattr(a, "snapshot", False)
    out = build_digest(a.table, a.by, a.field, a.top, refresh=snap or getattr(a, "refresh", False))
    if snap:
        # Appended from the payload just computed, so this costs zero extra API calls. Folded into the
        # same document rather than printed after it, because `digest | report` parses one JSON object.
        out["snapshot"] = take_snapshot(digest=out)
    jout(out)


# --- Snapshot history: local storage for trends ------------------------------------------------
# /CMDB/v2/system/metrics/data is the only history the API offers, and it returns a 30-day *average*
# of totals -- no series, no per-day values, nothing for vulnerabilities, risk distribution or
# per-label counts. There is no aggregation endpoint and no field projection either. So any trend
# beyond "assets vs their 30-day average" has to be built from state persisted here.
# Full design, including the phases still to land: design/trends.md.

SNAPSHOT_SCHEMA = 1

# distinctValuesSeen above this makes a breakdown expensive enough to say so out loud. Read
# summarize_by: a breakdown costs one page-0 call, up to SUMMARY_SAMPLE_PAGES stratified sample pages,
# and then one call per discovered group (capped at SUMMARY_MAX_GROUPS) -- so Risk_Level's 3 groups
# cost ~11 calls while a 40-group field costs ~48, most of the hard 60/min budget in a single field.
# Snapshots run on a cadence and unattended, and a scheduled job that quietly starts failing sections
# is the one thing this must not do. Anything else worth trending belongs in the metric list, at one
# call each.
SNAPSHOT_WARN_GROUPS = 12


def _snapshots_path():
    # One history file per stack. config.json holds only the *active* stack and `stacks switch`
    # mirrors into it, so a single shared file would blend two customers' trend lines -- the exact
    # failure the multi-stack design exists to prevent.
    fqdn, _, _ = load_config()
    return os.path.join(CFG_DIR, "snapshots.%s.jsonl" % re.sub(r"[^A-Za-z0-9._-]", "_", fqdn))


def append_snapshot(rec, path=None):
    """Append one record as a single line; returns the path written.

    JSONL rather than a JSON array: appending never rewrites history, one corrupt line can be skipped
    instead of losing the whole file, and it avoids a read-modify-write -- the same hazard that forced
    a threading.Lock around `.ratelimit`. One f.write() on a handle opened "a", so nothing can
    interleave a half record. Written utf-8 and read utf-8-sig, matching the convention that already
    saved config.json and topcache from BOM breakage.
    """
    path = path or _snapshots_path()
    if os.path.dirname(os.path.abspath(path)) == os.path.abspath(CFG_DIR):
        _ensure_cfg_dir()
    else:
        os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    # os.open so a NEW history file is born owner-only on POSIX: under --with-names it accumulates
    # raw customer identifiers, and the default umask would leave it world-readable. The mode only
    # applies at creation, so an existing file's permissions are respected either way.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, separators=(",", ":"), sort_keys=True) + "\n")
    return path


def load_snapshots(path=None):
    """(records oldest-first, skipped) from this stack's history.

    A record whose schema this build does not recognise is skipped *with a stated reason* and never
    coerced. The tempting alternative -- a try/except that falls back to "no history" -- turns a
    forward-incompatible file into a silent zero, which is the same wrong-answer class as a flat-line
    trend: nothing about the output looks partial.
    """
    path = path or _snapshots_path()
    recs, skipped = [], []
    try:
        with open(path, encoding="utf-8-sig") as f:
            lines = f.readlines()
    except OSError:
        return recs, skipped
    for i, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            skipped.append({"line": i, "reason": "line is not valid JSON"})
            continue
        if not isinstance(rec, dict):
            skipped.append({"line": i, "reason": "line is not a snapshot object"})
            continue
        if rec.get("schema") != SNAPSHOT_SCHEMA:
            skipped.append({"line": i, "reason": "schema %r was written by a different version of this "
                                                 "tool and is not readable here (this build reads "
                                                 "schema %d)" % (rec.get("schema"), SNAPSHOT_SCHEMA)})
            continue
        recs.append(rec)
    return recs, skipped


# A JSONL file grows without bound. 400 records is ~8 years of weeklies or ~13 months of dailies, which
# is longer than anyone will keep asking, and small enough that reading the whole file stays instant.
SNAPSHOT_MAX_RECORDS = 400


def prune_snapshots(keep=None, path=None):
    """Keep the newest `keep` records and report exactly what was dropped.

    Pruning is never silent. A history that quietly loses its early end changes what a long trend
    *means* -- "no change since June" is a different claim when June is no longer in the file -- and the
    output would look identical either way.

    Unreadable lines are dropped by this rewrite, so they are reported too. The file is replaced via a
    temp file and os.replace: this is the one operation in the whole feature that can destroy history,
    so a half-written result must not be able to become the file.
    """
    keep = SNAPSHOT_MAX_RECORDS if keep is None else int(keep)
    path = path or _snapshots_path()
    recs, skipped = load_snapshots(path)
    if keep < 1:
        return {"pruned": False, "error": "--keep must be at least 1; got %d." % keep}
    if len(recs) <= keep and not skipped:
        return {"pruned": False, "kept": len(recs), "keep": keep, "path": path,
                "note": "Nothing to prune: %d record(s) held, cap %d." % (len(recs), keep)}
    dropped = recs[:max(0, len(recs) - keep)]
    kept = recs[len(dropped):]
    tmp = path + ".tmp"
    # Same owner-only creation as append_snapshot: the rewrite replaces the history file, so a
    # default-umask temp here would silently strip the 0600 the file was born with. Remove any
    # leftover first and create exclusively: O_TRUNC on an existing temp keeps THAT file's mode
    # (and follows a symlink planted at the fixed name), so the 0600 would never apply.
    try:
        os.remove(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for r in kept:
            f.write(json.dumps(r, separators=(",", ":"), sort_keys=True) + "\n")
    os.replace(tmp, path)
    out = {"pruned": True, "keep": keep, "kept": len(kept), "dropped": len(dropped), "path": path}
    if dropped:
        out["droppedRange"] = {"from": dropped[0].get("stackDate"), "to": dropped[-1].get("stackDate")}
        out["note"] = ("Dropped %d record(s) covering %s to %s. A trend can no longer reach before %s, "
                       "so treat any earlier comparison as unavailable rather than unchanged."
                       % (len(dropped), dropped[0].get("stackDate"), dropped[-1].get("stackDate"),
                          kept[0].get("stackDate") if kept else "now"))
    if skipped:
        out["droppedUnreadable"] = skipped
    if kept:
        out["oldest"], out["newest"] = kept[0].get("stackDate"), kept[-1].get("stackDate")
    return out


def snapshots_summary(path=None):
    """count, oldest, newest and bytes -- what `snapshots list` reports."""
    path = path or _snapshots_path()
    recs, skipped = load_snapshots(path)
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    out = {"path": path, "records": len(recs), "cap": SNAPSHOT_MAX_RECORDS, "bytes": size,
           "oldest": recs[0].get("stackDate") if recs else None,
           "newest": recs[-1].get("stackDate") if recs else None,
           "distinctStackDates": len({r.get("stackDate") for r in recs if r.get("stackDate")}),
           "withMetrics": sum(1 for r in recs if r.get("metrics")),
           "withRebuildStamp": sum(1 for r in recs if _rebuild_key(r) is not None),
           "withEntities": sum(1 for r in recs if r.get("entities")),
           "storingNames": sum(1 for r in recs if (r.get("entities") or {}).get("namesStored"))}
    out["dataCurrency"] = {"class": "historical", "source": "local snapshots",
                           "from": out["oldest"], "to": out["newest"]}
    if skipped:
        out["unreadableLines"] = skipped
    if not recs:
        out["note"] = ("No history yet. Take one with `digest --snapshot` (free) or `snapshot`; there is "
                       "no way to backfill, because the API keeps no history.")
    elif out["distinctStackDates"] < 2:
        out["note"] = ("Only %d distinct stack date, so `trend` has nothing to compare yet -- snapshots "
                       "are compared on the stack's own ingest date." % out["distinctStackDates"])
    if out["storingNames"]:
        out["privacyNote"] = ("%d record(s) hold raw customer identifiers (taken with --with-names), so "
                              "this file should be covered by the same permissions.deny rule that "
                              "protects config.json." % out["storingNames"])
    return out


def cmd_snapshots(a):
    if a.snapshots_cmd == "list":
        jout(snapshots_summary()); return
    jout(prune_snapshots(a.keep))


# A connector row is one (connector, profile) pair -- two profiles of the same integration are two
# separate things that can break separately. Keying the name lists on the display name alone collapsed
# them: a stack with two degraded CrowdStrike profiles reported `degraded: 8` beside seven names, which
# reads as a connector that went unnamed. Worse, `_coverage_verdict` diffs those lists as SETS, so one
# of a same-named pair recovering while the other stayed broken produced no diff at all -- a silent
# "nothing changed" over a real regression, and `connector-regression`'s standing count (taken from
# len(failingNames)) under-reported by one for every collision.
#
# Stamped into the record so a trend can tell the two schemes apart. Records written before this key
# existed carry bare names, and diffing those against qualified ones would report every connector as
# both entered and resolved -- see _coverage_verdict, which refuses that comparison rather than
# manufacturing it.
COVERAGE_IDENTITY = "connector+profile"


def _coverage_ident(row):
    """The stable per-instance identity of a connector row: "Connector (profile)".

    Always qualified, never conditionally. Qualifying only the names that happen to collide today
    would make identity depend on the rest of the population: remove one of two CrowdStrike profiles
    and the survivor's identity silently changes from qualified to bare, which is exactly one spurious
    "entered the failing set" per cleanup, unflagged. A stable identity costs one noisier render and
    tells the operator *which* profile broke, which the bare name never could.
    """
    name = (row.get("connector") or "").strip()
    prof = (row.get("profile") or "").strip()
    if not name:
        # A nameless row used to be filtered out entirely, which is the other half of the count/name
        # mismatch: it vanished from the list while still being counted. Name it as unknown instead.
        return "(unnamed connector: %s)" % prof if prof else "(unnamed connector)"
    return "%s (%s)" % (name, prof) if prof else name


def _snapshot_coverage(conn, problems):
    """The connector-health half of a snapshot. Mandatory, and null rather than absent when unreadable.

    A delta is only meaningful if coverage was comparable at both ends. If a vulnerability connector
    was healthy in July and failing in August, "critical vulns down 40%" is not an improvement -- it is
    a broken scanner. The number is real; only the interpretation is wrong, and nothing about the
    output looks partial. So a trend has to be able to tell "coverage was fine" from "we could not
    check", which means recording the difference instead of defaulting to zero failures.
    """
    if not conn:
        return None
    fetched = conn.get("fetched") or {}
    if fetched.get("profiles") != "ok":
        # Without /connector/profile there are no connector rows at all, so the tally reads "0 enabled,
        # 0 failing" -- indistinguishable from a stack where everything is healthy. Refuse to record
        # that as coverage; a later trend would compare two empty failing-sets and call it unchanged.
        problems.append({"section": "coverage",
                         "error": "connector health unreadable: %s"
                                  % _short(str(fetched.get("profiles") or "not fetched"), 160)})
        return None
    s = conn.get("summary") or {}
    rows = conn.get("connectors") or []
    cov = {
        "connectorsEnabled": s.get("connectorsEnabled"), "healthy": s.get("healthy"),
        "degraded": s.get("degraded"), "failing": s.get("failing"), "idle": s.get("idle"),
        # A LIST, not a set: one entry per counted row, so len(failingNames) == failing holds by
        # construction rather than by luck. A genuine duplicate identity is a data anomaly worth
        # seeing, not something to collapse until the count stops adding up.
        "failingNames": sorted(_coverage_ident(c) for c in rows if c.get("health") == "failing"),
        # A connector that went ok -> degraded shrinks a count exactly like one that went to failing,
        # so a trend needs to see it too. Kept in its own list rather than folded into failingNames,
        # which would misname it. (design/trends.md §3)
        "degradedNames": sorted(_coverage_ident(c) for c in rows if c.get("health") == "degraded"),
        "coverageIdentity": COVERAGE_IDENTITY,
        "lastIngestAt": s.get("lastIngestUtc"),
    }
    if fetched.get("ingestion") != "ok":
        # The health verdicts still stand (they come from the profile endpoint), but "idle" and
        # lastIngestAt do not -- summarize_connectors deliberately declines to call anything idle when
        # the run history was unreadable. Say so rather than let a future trend trust those two.
        cov["ingestionUnreadable"] = True
    return cov


def _snapshot_breakdown(b):
    """summarize_by's output trimmed to counts, with every completeness field carried through.

    `groups` becomes a {value: count} map because a trend looks one category up across many snapshots,
    which the list form would turn into a scan per lookup. Nothing about completeness is dropped: a
    trend built on an incomplete breakdown inherits that incompleteness and must be able to say so.
    """
    out = {"table": b.get("table"), "by": b.get("by"), "fieldType": b.get("fieldType"),
           "total": b.get("total"),
           "groups": {g["value"]: g.get("count") for g in (b.get("groups") or []) if "value" in g}}
    for k in ("complete", "accountedRecords", "unaccountedRecords", "coveredRecords",
              "overcountedRecords", "groupsCapped", "distinctValuesSeen", "sampledForValues",
              "countsUnavailable", "whereTotal", "recordsWithoutField", "whereTotalUnavailable"):
        if k in b:
            out[k] = b[k]
    return out


def _snapshot_ranking(t):
    """A top-N trimmed to counts.

    The ranked rows themselves are asset and user names -- real customer PII -- so they are
    deliberately not written to disk. "Trimmed to counts" (design/trends.md §5) is a policy line, not
    a size optimisation. Per-entity history arrives in phase 4 behind a salted hash, so that "which
    assets got worse?" can be answered without customer names in the history file (§7).
    """
    out = {"table": t.get("table"), "field": t.get("field"), "top": len(t.get("top") or []),
           "matchedAtThreshold": t.get("matchedAtThreshold"), "totalInTail": t.get("totalInTail"),
           "truncated": bool(t.get("truncated"))}
    if t.get("note"):
        out["note"] = t["note"]
    return out


def _breakdown_cost_warnings(breakdowns):
    """Loud, measured warnings for a breakdown too expensive to take on a cadence. See SNAPSHOT_WARN_GROUPS."""
    out = []
    for b in breakdowns:
        n = b.get("distinctValuesSeen") or 0
        if n <= SNAPSHOT_WARN_GROUPS:
            continue
        counted = min(n, SUMMARY_MAX_GROUPS)
        calls = 1 + SUMMARY_SAMPLE_PAGES + counted + (1 if b.get("fieldType") == "List" else 0)
        out.append({"breakdown": b.get("by"), "distinctValuesSeen": n, "estimatedCalls": calls,
                    "warning": "Breakdown by %r saw %d distinct values, so capturing it costs about %d "
                               "of the 60 calls per minute this API allows -- a 3-group field like "
                               "Risk_Level costs ~11. On a cadence that leaves little budget for the "
                               "rest of the snapshot. Track a specific count as a metric (one call "
                               "each) instead of trending a high-cardinality breakdown."
                               % (b.get("by"), n, calls)})
    return out


def _warn_breakdown_cost(table, by):
    """Warn *before* spending the calls, from what the last snapshot measured for the same field.

    Cardinality cannot be known in advance -- field metadata carries fieldName/dataType/displayName and
    nothing about values, so there is no way to ask how many an OS field has. The only honest
    pre-check is the previous measurement, which costs no API call.
    """
    try:
        recs, _ = load_snapshots()
    except Exception:
        return
    for rec in reversed(recs):
        for b in rec.get("breakdowns") or []:
            if b.get("table") == table and b.get("by") == by:
                for w in _breakdown_cost_warnings([b]):
                    sys.stderr.write("warning: %s\n" % w["warning"])
                return


def snapshot_record(d, measured=None, entities=None):
    """Reduce a digest payload to the history record in design/trends.md §3.

    `measured` is `measure_metrics()` output (Option B). Named distinctly because the local `metrics`
    below is the *stack's* metrics endpoint -- two unrelated things that both want that word.

    Every number here comes from the digest's building blocks verbatim -- `stack_metrics`,
    `summarize_connectors`, `summarize_by`, `top_n`. Re-deriving a breakdown or a ranking would be the
    silent-omission bug in a third place, and worse here than anywhere else: a snapshot is read back
    weeks later by something that cannot tell an approximation from a measurement.
    """
    metrics = (d.get("metrics") or {}).get("metrics") or {}
    problems = [dict(p) for p in (d.get("sectionsUnavailable") or [])]
    rec = {
        "schema": SNAPSHOT_SCHEMA,
        # Both clocks, because the stack's ingest date and the operator's machine can disagree and the
        # 30-day average is stack-side. Trends compare on stackDate; takenAt says when we asked.
        "takenAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stackDate": metrics.get("date"),
        "fqdn": d.get("stack") or load_config()[0],
        "coverage": _snapshot_coverage(d.get("connectors"), problems),
        "totals": ({"assets": metrics.get("assetCount"), "users": metrics.get("userCount"),
                    "assets30DayAvg": metrics.get("avg30DaysAssetCount"),
                    "users30DayAvg": metrics.get("avg30DaysUserCount")} if d.get("metrics") else None),
        "breakdowns": [_snapshot_breakdown(d["breakdown"])] if d.get("breakdown") else [],
        "rankings": [_snapshot_ranking(d[k]) for k in ("topUsers", "topAssets") if d.get(k)],
    }
    # Additive-optional within "schema": 1, like `metrics` and `entities`: which LDG rebuild this record
    # measured (design/data-currency.md 4.5). It is what lets `trend` tell two rebuilds on one stack
    # date apart, and one rebuild seen on two dates as one. Written only when known; an unreadable stamp
    # is recorded as such, never as a guess, and such a record falls back to the stackDate rule.
    cur = d.get("dataCurrency") or {}
    if cur.get("class") == "current" and cur.get("ldgRebuiltUtc"):
        rec["ldgRebuiltUtc"] = dict(cur["ldgRebuiltUtc"])
    elif cur:
        rec["ldgRebuildUnknown"] = cur.get("reason") or "the rebuild stamp was unreadable"
    if measured:
        # Additive-optional within "schema": 1 -- a phase-1 record simply has no `metrics` key, and a
        # schema bump to introduce one would have made every earlier snapshot unreadable.
        rec["metrics"] = measured
        failed = [x["name"] for x in measured if x.get("ok") is False]
        if failed:
            # Into `problems`, which is what lands in sectionsUnavailable below -- appending to the
            # record directly would be clobbered by that assignment.
            problems.append({"section": "metrics", "error": "did not resolve: %s" % ", ".join(failed)})
    if entities is not None:
        # Additive-optional within "schema": 1, like `metrics`. Note the salt itself is never written --
        # only `saltId`, which says which salt produced the hashes without being able to reverse them.
        rec["entities"] = entities
    warnings = _breakdown_cost_warnings(rec["breakdowns"])
    if warnings:
        rec["warnings"] = warnings
    if problems:
        rec["sectionsUnavailable"] = problems
    return rec


def take_snapshot(digest=None, table="asset", by=None, field="Risk_Score", top=5, with_metrics=True,
                  entities=None, allow_large=False, with_names=False, salt=None):
    """Append one snapshot to this stack's history and report what was written.

    Pass an already-computed `digest` payload and the aggregate half costs **zero** API calls; otherwise
    it computes one. Keep breakdown capture to at most one or two low-cardinality fields -- see
    SNAPSHOT_WARN_GROUPS for the measured reason.

    Defined metrics are captured by default, at one call each, rather than behind an opt-in flag. An
    opt-in would mean a cron'd `digest --snapshot` never captured the metrics the operator deliberately
    defined, so every trend on them would answer `notTracked` forever -- the quiet degradation to
    "totals only" that §6.1 of the design exists to prevent. It costs nothing when nothing is defined.
    """
    # The three phases -- digest, metric counts, entity capture -- share no data, so they go out in
    # one parallel() batch instead of back to back (serially the digest's ~5-7s, the metrics' 1-3s
    # and the entities' 3-5s simply added up, on a verb that runs on a cadence). The caches they
    # race for (field map, labels) are lock-guarded in their loaders. Failure semantics are
    # unchanged: a digest or metrics failure raises as before, a failed entity capture is reported
    # as entitiesUnavailable rather than dropped.
    jobs, keys = [], []
    if digest is None:
        _warn_breakdown_cost(table, by or "Risk_Level")
        # refresh=True: a snapshot must measure, never re-serve. See build_digest.
        jobs.append(lambda: build_digest(table, by, field, top, refresh=True)); keys.append("digest")
    if with_metrics:
        jobs.append(lambda: measure_metrics(load_metrics())); keys.append("metrics")
    ents, ent_problem, warned_names = None, None, False
    if entities:
        spec, ent_problem = parse_entity_scope(entities, allow_large)
        if spec:
            if with_names and not _names_already_warned():
                # Loud, and once: the history file stops being anonymous the moment this runs, and the
                # operator needs to know before it accumulates weeks of customer identifiers.
                warned_names = True
                sys.stderr.write(
                    "warning: --with-names writes raw customer identifiers into %s. That file then holds "
                    "customer data and should be covered by the same permissions.deny rule that protects "
                    "config.json. The default (a salted hash) gives the same deltas without it.\n"
                    % _snapshots_path())
            sys.stderr.write("note: capturing scope %s -- about %d page(s), ~%ss.\n"
                             % (spec["scope"], spec["pages"], spec["estimatedSeconds"]))
            try:
                # Resolved before the fan-out: entity_salt() is a read-modify-write of config.json,
                # which should not run concurrently with anything else.
                ent_salt = salt if salt is not None else entity_salt()
            except Exception as e:
                ents, ent_problem = None, _short(str(e), 200)
            else:
                jobs.append(lambda: capture_entities(spec, ent_salt, with_names)); keys.append("entities")
    results = dict(zip(keys, parallel(jobs)))
    if "digest" in results:
        if isinstance(results["digest"], Exception):
            raise results["digest"]
        digest = results["digest"]
    measured = None
    if "metrics" in results:
        if isinstance(results["metrics"], Exception):
            raise results["metrics"]
        measured = results["metrics"]
    if "entities" in results:
        if isinstance(results["entities"], Exception):
            ents, ent_problem = None, _short(str(results["entities"]), 200)
        else:
            ents = results["entities"]
    rec = snapshot_record(digest, measured, ents)
    path = append_snapshot(rec)
    recs, skipped = load_snapshots(path)
    out = {"written": True, "path": path, "schema": SNAPSHOT_SCHEMA,
           "takenAt": rec["takenAt"], "stackDate": rec["stackDate"],
           "historyRecords": len(recs),
           # Mandatory and explicit: a null here is a fact a later trend has to honour, not an omission.
           "coverageRecorded": rec["coverage"] is not None,
           "breakdownsCaptured": [b.get("by") for b in rec["breakdowns"]],
           "metricsCaptured": len([m for m in (measured or []) if m.get("ok")]),
           "metricCalls": len(measured or [])}
    if rec.get("ldgRebuiltUtc"):
        out["ldgRebuiltUtc"] = rec["ldgRebuiltUtc"]
    if isinstance(digest, dict) and digest.get("dataCurrency"):
        # The measurement just taken is current as of its rebuild; it becomes historical once read back.
        out["dataCurrency"] = digest["dataCurrency"]
    if ents:
        out["entitiesCaptured"] = {"scope": ents["scope"], "count": ents["count"],
                                   "saltId": ents["saltId"], "namesStored": ents["namesStored"],
                                   "cutoffScore": ents.get("cutoffScore")}
        if warned_names:
            out["entitiesCaptured"]["privacyWarningShown"] = True
    if ent_problem:
        # Named rather than silently skipped: a scope the operator asked for and did not get would
        # otherwise surface weeks later as "no entity scope was captured".
        out["entitiesUnavailable"] = ent_problem
    if len(recs) > SNAPSHOT_MAX_RECORDS:
        # Enforced here so an unattended cadence cannot grow the file forever -- and REPORTED, because
        # losing the early end of a history silently changes what a long trend means.
        pruned = prune_snapshots(SNAPSHOT_MAX_RECORDS, path)
        out["pruned"] = pruned
        out["historyRecords"] = pruned.get("kept", out["historyRecords"])
    unresolved = [{"metric": m["name"], "error": m.get("error")} for m in (measured or [])
                  if m.get("ok") is False]
    if unresolved:
        # Named here as well as in the record: a metric that stopped resolving is the thing an operator
        # most needs told, since the alternative reading of a missing number is "it went to zero".
        out["metricsUnresolved"] = unresolved
    if skipped:
        # Unreadable lines are named rather than dropped in silence -- a history that quietly shrinks
        # changes what a trend over it means.
        out["historySkipped"] = skipped
    if rec.get("sectionsUnavailable"):
        out["sectionsUnavailable"] = rec["sectionsUnavailable"]
    for w in rec.get("warnings") or []:
        sys.stderr.write("warning: %s\n" % w["warning"])
        out.setdefault("warnings", []).append(w)
    return out


def cmd_snapshot(a):
    jout(take_snapshot(table=a.table, by=a.by, field=a.field, top=a.top,
                                   with_metrics=getattr(a, "metrics", True),
                                   entities=getattr(a, "entities", None),
                                   allow_large=getattr(a, "allow_large_scope", False),
                                   with_names=getattr(a, "with_names", False)))


# --- Option B: user-defined metrics -----------------------------------------------------------
# A metric is a named count the customer cares about, captured on every snapshot so it can be trended.
# It exists because a breakdown is the wrong tool for this: ~11 API calls for Risk_Level and ~48 for a
# 40-group field, against ONE for a metric (query with recordsPerPage: 1 and read totalRecords, the
# trick top_n's probe() already uses). Twenty metrics therefore cost twenty calls, and the extensible
# surface of the whole feature is this list rather than the breakdown set.
#
# A `smartLabel` metric resolves through find_label(), so a customer can trend their OWN vocabulary --
# "our crown jewels exposure" -- which is the payoff from the SmartLabel work.

METRICS_SCHEMA = 1
# Cap so a snapshot stays inside the hard 60 req/min budget with room for the rest of the digest
# (~20 calls). Twenty metrics plus a digest is ~40; a higher cap would start failing sections unattended.
MAX_METRICS = 20


def _metrics_path():
    fqdn, _, _ = load_config()
    return os.path.join(CFG_DIR, "metrics.%s.json" % re.sub(r"[^A-Za-z0-9._-]", "_", fqdn))


def load_metrics():
    """This stack's metric definitions. Missing or unreadable file -> no metrics, not an error."""
    try:
        with open(_metrics_path(), encoding="utf-8-sig") as f:
            doc = json.load(f)
    except Exception:
        return []
    if doc.get("schema") != METRICS_SCHEMA:
        return []
    return [m for m in (doc.get("metrics") or []) if isinstance(m, dict) and m.get("name")]


def save_metrics(metrics):
    _ensure_cfg_dir()
    with open(_metrics_path(), "w", encoding="utf-8") as f:
        json.dump({"schema": METRICS_SCHEMA, "fqdn": load_config()[0], "metrics": metrics}, f, indent=2)
    return _metrics_path()


def metric_query(mt):
    """(query body, problem) for a metric definition. Validates without issuing a request.

    This is the single validator, used at `metrics add` time AND on every read. Validating only at add
    time would not be enough on its own -- a field can be removed from a stack after a metric is saved --
    and validating only at snapshot time is far worse: a metric that silently counts 0 every week,
    unattended, is the exact wrong-answer class the rest of this tool guards against, and a zero reads
    as good news so nobody investigates it.
    """
    table = mt.get("table") or "asset"
    if mt.get("smartLabel"):
        hits = find_label(mt["smartLabel"])
        if not hits:
            return None, ("No SmartLabel matches %r on this stack. These are the customer's own labels, "
                          "so it may have been renamed or removed since this metric was saved."
                          % mt["smartLabel"])
        lab = hits[0]
        # The operator depends on the label's type, and getting this wrong is silent. A Binary label
        # means "this applies", so it is `== true` -- measured on a demo stack, the API treats `true`,
        # `"True"`, `"Yes"` and `1` as identical (14,048 records each), so the JSON boolean is safe.
        # Every other type must be `exists`, NOT `== null`: measured on a String label, `== null`
        # returned 32,597 of 34,270 records -- it matches the records where the label is ABSENT -- while
        # `exists` returned the 1,673 it actually applies to. Both are plausible-looking counts, and the
        # wrong one is 20x larger and would have been recorded weekly, forever, as a real number.
        if lab["type"] == "Binary":
            clause = {"searchFieldName": lab["field"], "operator": "==", "type": "Binary", "value": True}
        else:
            clause = {"searchFieldName": lab["field"], "operator": "exists",
                      "type": lab["type"], "value": None}
        # A label's own metadata carries its table and DSL type; trust those over the stored `table`,
        # since asking the asset table for a user label would count zero and look like an answer.
        return ({"table": lab["table"] or table, "query": [[clause]],
                 "paging": {"page": 0, "recordsPerPage": 1}}, None)
    clauses = mt.get("where") or []
    if not clauses:
        return None, "Metric %r has neither --where clauses nor a --smart-label." % mt.get("name")
    parsed = []
    for c in clauses:
        p, problem = clause_or_problem(c)
        if problem:
            return None, problem
        parsed.append(p)
    problem = field_problem(table, *[p["searchFieldName"] for p in parsed])
    if problem:
        return None, problem
    return ({"table": table, "query": [[p] for p in parsed],
             "paging": {"page": 0, "recordsPerPage": 1}}, None)


def measure_metric(mt):
    """One metric's current count, in exactly one API call. Never returns a fabricated 0.

    A metric that cannot be resolved records `ok: false` with the reason. The alternative -- writing
    `count: 0` -- would put a plausible, cheerful, wrong number into the permanent history, and a trend
    over it would report a real-looking decline to zero.
    """
    out = {"name": mt.get("name"), "label": mt.get("label") or mt.get("name")}
    if mt.get("smartLabel"):
        out["smartLabel"] = mt["smartLabel"]
    body, problem = metric_query(mt)
    if problem:
        return dict(out, count=None, ok=False, error=problem)
    try:
        out["count"] = count_records(body["table"], body["query"])
    except Exception as e:
        return dict(out, count=None, ok=False, error=_short(str(e), 200))
    out["ok"] = True
    return out


def measure_metrics(metrics):
    """Every metric's count, one call each, issued concurrently. Failures come back as ok: false."""
    if not metrics:
        return []
    results = parallel([(lambda mt=mt: measure_metric(mt)) for mt in metrics])
    out = []
    for mt, r in zip(metrics, results):
        if isinstance(r, Exception):
            out.append({"name": mt.get("name"), "label": mt.get("label") or mt.get("name"),
                        "count": None, "ok": False, "error": _short(str(r), 200)})
        else:
            out.append(r)
    return out


def add_metric(name, label, table, where=None, smart_label=None, origin=None, existing=None):
    """Validate a metric definition and save it. Returns (record, problem) -- exactly one is None.

    Validation happens HERE, before the definition is ever stored, and refuses rather than warns. A
    metric is read back unattended every snapshot; one that could never resolve would produce a
    permanently broken series that looks like data.
    """
    metrics = load_metrics() if existing is None else existing
    if not name or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", name):
        return None, ("Metric name %r is not usable. Use letters, digits, dot, dash or underscore, "
                      "starting with a letter or digit." % name)
    if where and smart_label:
        return None, "Give either --where clauses or --smart-label, not both."
    replacing = next((m for m in metrics if m.get("name") == name), None)
    if replacing is None and len(metrics) >= MAX_METRICS:
        # Refuse rather than evict. Which metric to drop is the operator's call: silently discarding one
        # would end a series someone has been accumulating for months, with nothing said.
        return None, ("The %d-metric cap is reached, so %r was not added. Nothing was evicted -- ending "
                      "a series someone has been accumulating is not a decision this tool should make "
                      "silently. Remove one with `metrics rm <name>` and retry. Currently tracked: %s."
                      % (MAX_METRICS, name, ", ".join(sorted(m["name"] for m in metrics))))
    rec = {"name": name, "label": label or name, "table": table or "asset"}
    if smart_label:
        rec["smartLabel"] = smart_label
    else:
        rec["where"] = list(where or [])
    if origin:
        rec["origin"] = origin
    _, problem = metric_query(rec)
    if problem:
        return None, problem
    metrics = [m for m in metrics if m.get("name") != name] + [rec]
    save_metrics(metrics)
    return rec, None


def cmd_metrics(a):
    if a.metrics_cmd == "list":
        metrics = load_metrics()
        rows = []
        for mt in metrics:
            _, problem = metric_query(mt)
            row = {k: v for k, v in mt.items()}
            row["resolves"] = problem is None
            if problem:
                row["problem"] = problem
            rows.append(row)
        out = {"tracked": len(metrics), "cap": MAX_METRICS, "path": _metrics_path(), "metrics": rows}
        broken = [r["name"] for r in rows if not r["resolves"]]
        if broken:
            # Named, not silently counted as zero on the next snapshot.
            out["notResolving"] = broken
            out["note"] = ("%d metric(s) no longer resolve against this stack. They will be recorded as "
                           "unavailable rather than as 0, and a trend will say so." % len(broken))
        if len(metrics) >= MAX_METRICS:
            out["capReached"] = True
        jout(out); return
    if a.metrics_cmd == "rm":
        metrics = load_metrics()
        if not any(m.get("name") == a.name for m in metrics):
            die("No metric named %r. Tracked: %s."
                % (a.name, ", ".join(sorted(m["name"] for m in metrics)) or "none"))
        save_metrics([m for m in metrics if m.get("name") != a.name])
        jout({"removed": a.name, "tracked": len(metrics) - 1,
                          "note": "History already captured for this metric is kept; it simply stops "
                                  "being measured from the next snapshot on."}); return
    rec, problem = add_metric(a.name, a.label, a.table, a.where, a.smart_label,
                              origin="derived" if a.derived else None)
    if problem:
        die(problem)
    out = {"added": rec, "tracked": len(load_metrics()), "cap": MAX_METRICS}
    if a.derived:
        # A derived metric is registered in answer to a question that could not be answered, so the
        # answer needs today's value to offer as a baseline.
        baseline, stamp = parallel([lambda: measure_metric(rec), ldg_rebuild])
        if isinstance(baseline, Exception):
            raise baseline
        out["baseline"] = baseline
        out = attach_currency(out, data_currency(_stamp_or_unknown(stamp), [_table_kind(rec.get("table"))]))
    jout(out)


# --- Option C: scoped entity deltas -----------------------------------------------------------
# "Which assets got worse?" is the genuinely useful question and the expensive one. Two hard rules.
#
# SCOPE IS MANDATORY AND BOUNDED. The full inventory is 34k records = ~343 pages = ~13 minutes and ~6x
# the 60/min rate budget, so there is no "all". The default is the top 500 by a score (5 pages, ~12s);
# anything larger is refused unless explicitly allowed, with its measured cost stated first.
#
# IDENTITY IS A PII DECISION, NOT A SCHEMA ONE. A delta only needs "same entity across time", which a
# salted hash gives without putting a single customer name on disk. The salt lives with the credentials
# and NEVER in the snapshot file -- a snapshot carrying both the salt and the hashes would be a
# plaintext list of customer assets with extra steps. To name the movers, re-query live at answer time
# (`trend --name-entities`), so names come from the API and stay in memory.

ENTITY_SCOPE_DEFAULT_MAX = 500      # 5 pages, ~12s. Past this, the operator has to ask for it.
ENTITY_ID_FIELD = {"asset": "Asset_Name", "user": "Owner_Name"}
ENTITY_DIFF_SAMPLE = 25             # per-bucket cap on ids listed in a trend; counts are always exact


def _scope_cost(n):
    pages = max(1, (int(n) + 99) // 100)
    return pages, round(pages * 2.3, 1)


def parse_entity_scope(scope, allow_large=False):
    """(spec, problem) for `top<N>:<table>:<field>` or `label:<Label>:<field>`.

    There is deliberately no unbounded form. An operator who wants "every asset" is asking for ~343
    pages and ~13 minutes behind the rate limiter, which in an inventory tool reads as a hang.
    """
    parts = (scope or "").split(":")
    if len(parts) != 3:
        return None, ("--entities takes \"top<N>:<table>:<field>\" (e.g. top500:asset:Risk_Score) or "
                      "\"label:<SmartLabel>:<field>\", and got %r. There is no unbounded scope: the full "
                      "inventory is ~343 pages and ~13 minutes, about 6x the 60 calls/minute budget."
                      % scope)
    head, mid, field = parts[0].strip(), parts[1].strip(), parts[2].strip()
    if head.lower().startswith("top"):
        try:
            n = int(head[3:])
        except ValueError:
            return None, "%r is not a top-N scope; expected e.g. top500:asset:Risk_Score." % scope
        if n < 1:
            return None, "A scope of %d entities is not a scope." % n
        table = "user" if mid.startswith("user") else "asset"
        spec = {"kind": "top", "n": n, "table": table, "field": field}
    elif head.lower() == "label":
        hits = find_label(mid)
        if not hits:
            return None, ("No SmartLabel matches %r on this stack, so that scope cannot be resolved. "
                          "These are the customer's own labels; check `labels --search`." % mid)
        lab = hits[0]
        # A label's membership size is not knowable in advance, so it takes the same default bound as a
        # top-N scope and the flag raises it to the existing LIST_MAX_RECORDS ceiling. Gating labels
        # behind the flag outright would refuse a twelve-member label for costing nothing.
        spec = {"kind": "label", "label": lab["name"], "labelField": lab["field"],
                "labelType": lab["type"], "table": lab["table"] or "asset", "field": field,
                "n": LIST_MAX_RECORDS if allow_large else ENTITY_SCOPE_DEFAULT_MAX}
    else:
        return None, "%r is not a scope this tool understands (expected top<N>: or label:)." % scope
    pages, secs = _scope_cost(spec["n"])
    if spec["n"] > LIST_MAX_RECORDS:
        # The existing ceiling, reused rather than raised: 50 pages is most of one minute's budget.
        return None, ("A scope of %d entities is above the %d ceiling (%d pages, ~%ss, and more than "
                      "the 60 calls/minute budget allows). Narrow the scope."
                      % (spec["n"], LIST_MAX_RECORDS, pages, secs))
    if spec["n"] > ENTITY_SCOPE_DEFAULT_MAX and not allow_large:
        return None, ("A scope of %d entities costs about %d pages and ~%ss, against %d pages for the "
                      "%d default. Pass --allow-large-scope to accept that cost."
                      % (spec["n"], pages, secs, _scope_cost(ENTITY_SCOPE_DEFAULT_MAX)[0],
                         ENTITY_SCOPE_DEFAULT_MAX))
    spec["scope"] = "%s:%s:%s" % (head, mid, field)
    spec["pages"], spec["estimatedSeconds"] = pages, secs
    return spec, None


def entity_salt():
    """This stack's hashing salt, generated once and stored beside its credentials.

    Kept per stack in `stacks.json` and mirrored into `config.json` exactly like a token, for two
    reasons. It survives `stacks switch` -- a regenerated salt makes every earlier entity record
    incomparable, which would silently report the whole population as appeared-and-disappeared. And two
    stacks never share one, so identical asset names in two customers' histories do not produce
    identical hashes on the same operator's machine.
    """
    cfg = {}
    try:
        with open(CFG_PATH, encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    salt = cfg.get("entity_salt")
    if salt:
        return salt
    import secrets
    salt = secrets.token_hex(32)
    reg = load_stacks()
    name = reg.get("active")
    if name and name in reg.get("stacks", {}):
        reg["stacks"][name]["entity_salt"] = salt
        try:
            save_stacks(reg)
        except Exception:
            pass
    cfg["entity_salt"] = salt
    try:
        # This is a read-modify-write of the file holding the API token, and a truncated rewrite
        # would cost the user their credentials to add a hashing salt -- a bad trade at any
        # probability. _private_write also keeps the perms owner-only: the old temp file was
        # created with default umask, so this rewrite silently reverted a hand-applied chmod 600.
        _private_write(CFG_PATH, cfg)
    except Exception:
        pass
    return salt


def salt_id(salt):
    """A short public fingerprint of the salt. Lets a snapshot declare WHICH salt produced its hashes
    without the file carrying anything that could reverse them."""
    import hashlib
    return hashlib.sha256(("saltid:" + (salt or "")).encode("utf-8")).hexdigest()[:8]


def hash_entity(salt, value):
    import hashlib
    return hashlib.sha256(((salt or "") + str(value)).encode("utf-8")).hexdigest()[:16]


def capture_entities(spec, salt, with_names=False):
    """{id: score} for a bounded scope, plus the cutoff that produced it.

    Reuses `top_n` rather than paging again: it already owns the threshold search, the concurrent tail
    fetch and the truncation flag. `select` is narrowed to the identity field and the score, so the
    ~1.1MB pages are not carried around wholesale.
    """
    table, field = spec["table"], spec["field"]
    idf = ENTITY_ID_FIELD.get(table, "Asset_Name")
    where = None
    if spec["kind"] == "label":
        # Same operator rule as a SmartLabel metric: Binary means "applies", everything else `exists`.
        # `== null` on a String label matches where it is ABSENT (measured: 32,597 of 34,270).
        where = (["%s == Binary true" % spec["labelField"]] if spec["labelType"] == "Binary"
                 else ["%s exists %s" % (spec["labelField"], spec["labelType"])])
    r = top_n(table, field, spec["n"], where=where, select="%s,%s" % (idf, field))
    rows = r.get("top") or []
    scores, names = {}, {}
    for row in rows:
        ident = row.get(idf)
        if ident is None:
            continue
        key = str(ident) if with_names else hash_entity(salt, ident)
        try:
            scores[key] = float(row.get(field) or 0)
        except (TypeError, ValueError):
            continue
        if with_names:
            names[key] = str(ident)
    out = {"scope": spec["scope"], "table": table, "field": field, "count": len(scores),
           "saltId": salt_id(salt), "namesStored": bool(with_names),
           "matchedAtThreshold": r.get("matchedAtThreshold"),
           "requested": spec["n"], "scores": scores}
    if scores:
        # The lowest score captured. A top-N scope is a MOVING WINDOW, so this is what makes an
        # `appeared`/`disappeared` verdict interpretable rather than misleading.
        out["cutoffScore"] = min(scores.values())
    if r.get("truncated"):
        out["truncated"] = True
        out["note"] = r.get("note")
    if with_names:
        # Stated in the record itself, so anyone reading the file later knows what it holds.
        out["privacyNote"] = ("This record stores raw customer identifiers because --with-names was "
                              "passed. Cover this file with the same permissions.deny rule that protects "
                              "config.json.")
    return out


def _names_already_warned():
    """True if a with-names snapshot was already taken for this stack.

    The warning is worth making loudly once and then not on every scheduled run. The history file is
    itself the record of whether it has been said, so this needs no extra state.
    """
    try:
        recs, _ = load_snapshots()
    except Exception:
        return False
    return any((r.get("entities") or {}).get("namesStored") for r in recs)


# --- Trends: comparing two snapshots ----------------------------------------------------------
# The correctness constraint that shapes this whole verb: A DELTA IS ONLY MEANINGFUL IF COVERAGE WAS
# COMPARABLE AT BOTH ENDS.
#
# If a vulnerability connector was healthy on 1 July and failing on 1 August, "critical vulns down 40%"
# is not an improvement -- it is a broken scanner. The number is real; only the interpretation is
# wrong. That is the same silently-wrong-answer class this file has been hardened against six times
# (an unaccounted 37% of a breakdown, a mis-typed field over-counting groups, a typo returning
# totalRecords: 0, a stale field map picking the wrong operator, a capped breakdown returning nothing,
# a labels 403 reading as "no such label"). A trend line is the easiest place in the whole tool to
# manufacture one, because nothing about a percentage looks partial.
#
# So every refusal below is load-bearing, not a nicety: fewer than two comparable snapshots is
# `insufficientHistory` rather than 0%, unreadable coverage is `unverifiable` rather than computed, a
# changed failing-connector set is flagged for a human to judge, and something never captured is
# `notTracked` rather than "no change". What this deliberately does NOT do is guess which metric
# depends on which connector -- that mapping does not exist in the API, and inventing it would present
# a guess as fact. Flag the change; let the human judge relevance.

def _pct(before, after):
    """Percentage change, or None when there isn't one.

    None rather than 0 when the baseline is 0: "up from nothing" has no percentage, and printing 0%
    there would say "no change" about the one case where everything changed.
    """
    if before in (None, 0) or after is None:
        return None
    return round(100.0 * (after - before) / float(before), 1)


def _delta_row(kind, name, before, after, extra=None):
    row = {"kind": kind, "name": name, "from": before, "to": after,
           "change": (after - before) if isinstance(before, (int, float)) and isinstance(after, (int, float)) else None,
           "percentChange": _pct(before, after)}
    if row["percentChange"] is None and row["change"] is not None:
        row["percentNote"] = "no percentage: the earlier value is %s" % ("zero" if before == 0 else "unknown")
    row.update(extra or {})
    return row


def _rebuild_key(r):
    """A snapshot's LDG rebuild identity, or None when it was not recorded: every record written before
    v2.27.0, and any taken while the stamp was unreadable. There is no backfill, so None is common."""
    s = r.get("ldgRebuiltUtc")
    if not isinstance(s, dict) or not s or not all(isinstance(v, str) and v for v in s.values()):
        return None
    return tuple(sorted(s.items()))


def _usable_snapshots(recs):
    """(point map, unusable list). One record per DATA POINT, keyed by a label that sorts in time order.

    Compare on the stack's timeline, never `takenAt`: the stack's ingest date and the operator's clock
    can disagree, and the counts plus the 30-day average are all stack-side. A record whose stackDate
    is missing (the metrics section failed) cannot be placed on that timeline at all, so it is named
    as unusable rather than quietly ordered by the local clock instead.

    **A data point is one LDG rebuild** (design/data-currency.md 4.5). The LDG changes only when a
    merger run completes, so two snapshots of one rebuild are one measurement of one point -- the last
    one *taken* wins -- and treating them as two is how a flat line gets manufactured. That used to be
    approximated as "one point per stackDate", which holds on a once-a-day stack and fails both ways
    elsewhere: a stack rebuilding every 4 hours has several genuine points on one date, and a snapshot
    taken after midnight but before that day's merge has a new date and the same LDG.

    Where every record on a date carries `ldgRebuiltUtc`, points are keyed on the rebuild. Where any
    does not (all history before v2.27.0), that date keeps the old rule unchanged -- no backfill, so old
    history behaves exactly as it did. A date holding one point is labelled with the date alone, which
    is also every label a pre-v2.27.0 history can produce; only a date holding several gains a
    ` rebuild <utc>` suffix, which still sorts after the bare date and still compares against a
    `--since` date correctly.
    """
    dated, unusable = [], []
    for r in recs:
        if not r.get("stackDate"):
            unusable.append({"takenAt": r.get("takenAt"),
                             "reason": "no stackDate: the stack's own date was unreadable when this "
                                       "snapshot was taken, so it cannot be placed on the timeline"})
            continue
        dated.append(r)
    all_keyed = {}
    for r in dated:
        all_keyed[r["stackDate"]] = all_keyed.get(r["stackDate"], True) and _rebuild_key(r) is not None
    points = {}
    for r in dated:
        ident = ("rebuild", _rebuild_key(r)) if all_keyed[r["stackDate"]] else ("date", r["stackDate"])
        prev = points.get(ident)
        if prev is None or (r.get("takenAt") or "") >= (prev.get("takenAt") or ""):
            points[ident] = r
    per_date = {}
    for ident, r in points.items():
        per_date.setdefault(r["stackDate"], []).append((ident, r))
    by_point = {}
    for day, items in per_date.items():
        if len(items) == 1:
            by_point[day] = items[0][1]
            continue
        for ident, r in items:
            # Only rebuild-keyed points can share a date (a date is all-keyed or it is one date-keyed
            # point), but the label must not depend on that holding.
            label = ("%s rebuild %s" % (day, max(v for _, v in ident[1])) if ident[0] == "rebuild"
                     else "%s rebuild unknown" % day)
            by_point[label] = r
    return by_point, unusable


def _coverage_verdict(a, b):
    """Compare the two endpoints' coverage. Returns (verdict, blocked).

    `blocked` is True when the trend must not be computed at all -- which is only ever the case for
    unreadable coverage, never for *changed* coverage. A known change is reportable with a flag; an
    unknown one is not reportable at all.
    """
    ca, cb = a.get("coverage"), b.get("coverage")
    if ca is None or cb is None:
        which = ("both endpoints" if ca is None and cb is None
                 else "the earlier endpoint (%s)" % a.get("stackDate") if ca is None
                 else "the later endpoint (%s)" % b.get("stackDate"))
        return ({"comparable": False,
                 "reason": "connector health was not readable at %s, so there is no way to tell whether "
                           "a change in these numbers reflects the environment or a data source that "
                           "stopped reporting" % which}, True)
    v = {"comparable": True, "coverageChanged": False,
         "from": {"failing": ca.get("failing"), "degraded": ca.get("degraded"),
                  "connectorsEnabled": ca.get("connectorsEnabled")},
         "to": {"failing": cb.get("failing"), "degraded": cb.get("degraded"),
                "connectorsEnabled": cb.get("connectorsEnabled")}}
    ia, ib = ca.get("coverageIdentity"), cb.get("coverageIdentity")
    if ia != ib:
        # Same trap as a rotated entity salt, in connector clothing: the two ends name connectors
        # differently, so no name in one can be matched against a name in the other. Diffing them
        # would report every connector as both entered and resolved -- a total-churn event that did
        # not happen. The COUNTS survive the scheme change untouched, so `comparable` stays true and
        # `blocked` stays false: a metric threshold has no stake in how connectors are named, and
        # making it unevaluable over this would be its own false alarm.
        v["namesComparable"] = False
        v["coverageChanged"] = True
        v["namesIncomparableReason"] = (
            "the two snapshots identify connectors differently (%s and %s), so no name in one can be "
            "matched against the other. Which connectors entered or left the failing set cannot be "
            "determined across this boundary -- the counts below still can be. Snapshots taken from "
            "here on share the newer scheme; there is no way to re-identify the older records."
            % (ia or "bare connector name", ib or "bare connector name"))
        v["note"] = ("Connector identity changed between the two endpoints, so no statement about "
                     "which connectors changed state is available for this window. Treat the "
                     "per-connector picture as unknown rather than unchanged.")
        return v, False
    fa, fb = set(ca.get("failingNames") or []), set(cb.get("failingNames") or [])
    da, db = set(ca.get("degradedNames") or []), set(cb.get("degradedNames") or [])
    changed = {}
    for key, before, after in (("failing", fa, fb), ("degraded", da, db)):
        gained, lost = sorted(after - before), sorted(before - after)
        if gained:
            changed[key + "Added"] = gained
        if lost:
            changed[key + "Resolved"] = lost
    if changed:
        v["coverageChanged"] = True
        v.update(changed)
        # Deliberately no metric-to-connector attribution: the API does not expose which data source
        # feeds which field, so naming one would be a guess wearing the clothes of a fact.
        v["note"] = ("Connector health differs between the two endpoints, so a movement below may be a "
                     "change in what was being reported rather than a change in the environment. "
                     "Which of these connectors feeds which number is not something this API exposes, "
                     "so judge relevance yourself before reading any percentage as real.")
    for side, cov in (("from", ca), ("to", cb)):
        if cov.get("ingestionUnreadable"):
            v.setdefault("ingestionUnreadable", []).append(side)
    return v, False


def _trend_breakdowns(a, b, table=None, by=None, caveats=None):
    """Per-category deltas, inheriting each endpoint's completeness verdict.

    A trend built on an incomplete breakdown is itself incomplete, and has to say so: if 37% of the
    records never made it into either endpoint's groups, a category moving by 5% may be entirely an
    artefact of which records the discovery sample happened to reach.
    """
    rows, tracked = [], set()
    index = {}
    for rec, side in ((a, "from"), (b, "to")):
        for bd in rec.get("breakdowns") or []:
            index.setdefault((bd.get("table"), bd.get("by")), {})[side] = bd
    for (tbl, fld), pair in sorted(index.items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or "")):
        if (table and tbl != table) or (by and fld != by):
            continue
        tracked.add((tbl, fld))
        if "from" not in pair or "to" not in pair:
            # Captured at one end only. Not a delta of any kind -- and emphatically not a category
            # that "appeared" -- so it is reported as untracked at the other end.
            missing = "the earlier snapshot" if "from" not in pair else "the later snapshot"
            (caveats if caveats is not None else []).append(
                {"breakdown": "%s.%s" % (tbl, fld),
                 "note": "not captured in %s, so no comparison is possible for it" % missing})
            continue
        ba, bb = pair["from"], pair["to"]
        incomplete = [s for s, x in (("from", ba), ("to", bb)) if x.get("complete") is False]
        shared = {}
        for src in (ba, bb):
            for k in src.get("groups") or {}:
                shared[k] = True
        for value in sorted(shared):
            before, after = (ba.get("groups") or {}).get(value), (bb.get("groups") or {}).get(value)
            extra = {"table": tbl, "field": fld}
            if before is None or after is None:
                # A category present at one end only. The other end's count is unknown, not zero --
                # the discovery sample may simply never have reached it (see summarize_by's stratified
                # sample), so subtracting from an assumed 0 would invent the entire movement.
                extra["note"] = ("this category was not in the %s snapshot's groups, which means its "
                                 "count there is unknown, not zero"
                                 % ("earlier" if before is None else "later"))
                rows.append({"kind": "breakdown", "name": value, "from": before, "to": after,
                             "change": None, "percentChange": None, **extra})
                continue
            if incomplete:
                extra["incompleteAt"] = incomplete
                extra["note"] = ("the %s breakdown did not account for every record, so this movement "
                                 "inherits that incompleteness" % " and ".join(incomplete))
            rows.append(_delta_row("breakdown", value, before, after, extra))
    return rows, tracked


_NOT_TRACKED_NOTE = (
    "Reported as not captured, deliberately NOT as zero or unchanged. There is no backfill: the API "
    "holds no history, so a thing that was not being captured is unanswerable for the past rather than "
    "flat.")


def _trend_metrics(a, b, want=None, caveats=None):
    """Deltas for named metrics (Option B, phase 3), and `notTracked` for anything never captured.

    There is no backfill -- the API holds no history -- so a metric that was not being captured is
    *unanswerable*, not unchanged. Rendering that as 0, 0%, "no change" or a flat line is the most
    plausible-looking version of the wrong-answer class this file keeps fixing, because nothing about
    a flat line looks partial. So it returns notTracked and the caller says so in words.
    """
    rows, not_tracked = [], []
    idx = {}
    for rec, side in ((a, "from"), (b, "to")):
        for mt in rec.get("metrics") or []:
            if mt.get("name"):
                idx.setdefault(mt["name"], {})[side] = mt
    names = [want] if want else sorted(idx)
    for name in names:
        pair = idx.get(name) or {}
        if "from" not in pair or "to" not in pair:
            # PHASE 3 HOOK: `metrics add --derived` attaches here. It adds (a) today's value as a
            # baseline, so an unanswerable question still returns a number, and (b) registration of
            # the metric so the same question is answerable from the next snapshot on -- reported in
            # the output, because an implicit write to the user's config must be visible.
            not_tracked.append({"metric": name,
                                "reason": ("not captured in %s, and the API keeps no history to "
                                           "backfill from, so this cannot be compared"
                                           % ("either snapshot" if not pair else
                                              "the earlier snapshot" if "from" not in pair else
                                              "the later snapshot"))})
            continue
        ma, mb = pair["from"], pair["to"]
        failed = [s for s, x in (("from", ma), ("to", mb)) if x.get("ok") is False]
        if failed:
            # A metric that stopped resolving is unavailable, never zero. A zero reads as good news,
            # so nobody investigates it.
            not_tracked.append({"metric": name, "unavailableAt": failed,
                                "reason": "the metric did not resolve at %s (%s), so its count there "
                                          "is unknown rather than zero"
                                          % (" and ".join(failed),
                                             (ma if "from" in failed else mb).get("error") or "no reason recorded")})
            continue
        rows.append(_delta_row("metric", name, ma.get("count"), mb.get("count"),
                               {"label": mb.get("label") or ma.get("label")}))
    return rows, not_tracked


def _trend_rankings(a, b, table=None, caveats=None):
    """Tail-size deltas, but only where the threshold is identical at both ends.

    `totalInTail` is "how many records score at or above matchedAtThreshold", and `top`'s ladder picks
    that threshold per run. Comparing a tail measured at >=1000 against one measured at >=300 would
    produce a large, confident and completely meaningless movement -- so a differing threshold is
    reported as not comparable instead of being differenced anyway.
    """
    rows = []
    idx = {}
    for rec, side in ((a, "from"), (b, "to")):
        for rk in rec.get("rankings") or []:
            idx.setdefault((rk.get("table"), rk.get("field")), {})[side] = rk
    for (tbl, fld), pair in sorted(idx.items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or "")):
        if table and tbl != table:
            continue
        if "from" not in pair or "to" not in pair:
            continue
        ra, rb = pair["from"], pair["to"]
        if ra.get("matchedAtThreshold") != rb.get("matchedAtThreshold"):
            (caveats if caveats is not None else []).append(
                {"ranking": "%s.%s" % (tbl, fld),
                 "note": "not compared: the two snapshots counted their tails at different thresholds "
                         "(>=%s and >=%s), so the counts measure different things"
                         % (ra.get("matchedAtThreshold"), rb.get("matchedAtThreshold"))})
            continue
        extra = {"table": tbl, "field": fld, "atOrAbove": rb.get("matchedAtThreshold")}
        truncated = [s for s, x in (("from", ra), ("to", rb)) if x.get("truncated")]
        if truncated:
            extra["truncatedAt"] = truncated
        # The table belongs in the name, not only in the `table` column: asset and user rankings on the
        # same field otherwise arrive as two identically-labelled rows in a CSV.
        rows.append(_delta_row("ranking", "%s %s at or above %s" % (tbl, fld, rb.get("matchedAtThreshold")),
                               ra.get("totalInTail"), rb.get("totalInTail"), extra))
    return rows


def _trend_entities(a, b):
    """appeared / disappeared / worsened / improved, or a plain statement of why not.

    Three refusals, all of them the same class of trap:

    A MISMATCHED saltId is refused outright. Diffing across a rotated salt would report every entity as
    both appeared and disappeared -- a 100% churn event that never happened, and the most alarming
    possible way to be wrong.

    A DIFFERENT SCOPE is not compared. top500 against top1000 shares no cutoff, so the extra 500 would
    all read as newly appeared.

    A MOVING CUTOFF is stated. A top-N scope is a window, not a population: an entity leaving the top 500
    has not left the inventory, it has dropped below the cutoff. If the two ends cut off at different
    scores, some of appeared/disappeared is that movement rather than anything about the entities.
    """
    ea, eb = a.get("entities"), b.get("entities")
    if not ea or not eb:
        which = ("neither snapshot" if not ea and not eb else
                 "the earlier snapshot" if not ea else "the later snapshot")
        return {"comparable": False,
                "reason": "no entity scope was captured in %s, so per-entity movement cannot be "
                          "compared. Capture it with `snapshot --entities top500:asset:Risk_Score`; "
                          "there is no way to backfill it." % which}
    if ea.get("saltId") != eb.get("saltId"):
        return {"comparable": False, "saltMismatch": True,
                "reason": "the two snapshots hashed their entity ids with different salts (%s and %s), so "
                          "no id in one can be matched against the other. Diffing them would report every "
                          "entity as both appeared and disappeared -- a 100%% churn event that did not "
                          "happen. A salt is generated once per stack; a rotated or restored credential "
                          "file is the usual cause."
                          % (ea.get("saltId"), eb.get("saltId"))}
    if ea.get("scope") != eb.get("scope"):
        return {"comparable": False,
                "reason": "the two snapshots captured different scopes (%r and %r), which share no "
                          "cutoff, so the difference between them is mostly the scope change."
                          % (ea.get("scope"), eb.get("scope"))}
    if ea.get("namesStored") != eb.get("namesStored"):
        return {"comparable": False,
                "reason": "one snapshot stored raw identifiers and the other stored hashes, so their ids "
                          "are not the same kind of thing and cannot be matched."}
    sa, sb = ea.get("scores") or {}, eb.get("scores") or {}
    appeared = sorted(set(sb) - set(sa))
    disappeared = sorted(set(sa) - set(sb))
    worsened, improved, unchanged = [], [], 0
    for k in sorted(set(sa) & set(sb)):
        d = sb[k] - sa[k]
        if d > 0:
            worsened.append({"id": k, "from": sa[k], "to": sb[k], "change": round(d, 2)})
        elif d < 0:
            improved.append({"id": k, "from": sa[k], "to": sb[k], "change": round(d, 2)})
        else:
            unchanged += 1
    worsened.sort(key=lambda x: -x["change"])
    improved.sort(key=lambda x: x["change"])
    out = {"comparable": True, "scope": ea.get("scope"), "table": ea.get("table"),
           "field": ea.get("field"), "saltId": ea.get("saltId"),
           "identities": "names" if ea.get("namesStored") else "salted hashes",
           # Stated, not assumed. Detecting a field's polarity is not something the API supports, and
           # guessing it would relabel every improvement as a regression.
           "scoreDirection": "a higher %s is treated as worse" % ea.get("field"),
           "counts": {"inEarlier": len(sa), "inLater": len(sb), "appeared": len(appeared),
                      "disappeared": len(disappeared), "worsened": len(worsened),
                      "improved": len(improved), "unchanged": unchanged},
           "appeared": appeared[:ENTITY_DIFF_SAMPLE],
           "disappeared": disappeared[:ENTITY_DIFF_SAMPLE],
           "worsened": worsened[:ENTITY_DIFF_SAMPLE],
           "improved": improved[:ENTITY_DIFF_SAMPLE]}
    # The lists are sampled but the counts above are exact, so a truncation is stated rather than
    # leaving a reader to assume 25 is the whole story.
    for k in ("appeared", "disappeared", "worsened", "improved"):
        if out["counts"][k] > ENTITY_DIFF_SAMPLE:
            out.setdefault("listsTruncated", {})[k] = out["counts"][k]
    ca, cb = ea.get("cutoffScore"), eb.get("cutoffScore")
    out["cutoffScore"] = {"from": ca, "to": cb}
    if (appeared or disappeared) and ca is not None and cb is not None and ca != cb:
        out["cutoffMoved"] = True
        out["note"] = ("This scope is the top %s by %s, which is a WINDOW rather than a population, and "
                       "its cutoff moved from %s to %s. An entity listed as disappeared has dropped below "
                       "the cutoff, which is not the same as leaving the inventory -- and some of the "
                       "appeared/disappeared counts are that cutoff moving rather than anything about the "
                       "entities themselves."
                       % (ea.get("requested") or "N", ea.get("field"), ca, cb))
    elif appeared or disappeared:
        out["note"] = ("This scope is a window rather than a population: an entity listed as disappeared "
                       "has dropped out of the top %s by %s, which is not the same as leaving the "
                       "inventory." % (ea.get("requested") or "N", ea.get("field")))
    if not ea.get("namesStored"):
        out["namingNote"] = ("Ids are salted hashes, so they carry no customer data. Pass "
                            "`--name-entities` to look the current ones up live -- names then come from "
                            "the API at answer time and are never written to the history file.")
    return out


def name_entities(diff, limit=ENTITY_DIFF_SAMPLE):
    """Resolve hashed ids to current names by re-querying live. Names stay in memory, never on disk.

    Only entities still present in the scope can be named -- a disappeared one is, by definition, no
    longer in the result set the query returns, so it stays a hash. Said, rather than left as a gap.
    """
    if not diff.get("comparable") or diff.get("identities") == "names":
        return
    spec, problem = parse_entity_scope(diff["scope"], allow_large=True)
    if problem:
        diff["namingUnavailable"] = problem
        return
    salt = entity_salt()
    if salt_id(salt) != (diff.get("saltId") or salt_id(salt)):
        diff["namingUnavailable"] = "the current salt does not match the one these ids were hashed with"
        return
    try:
        idf = ENTITY_ID_FIELD.get(spec["table"], "Asset_Name")
        r = top_n(spec["table"], spec["field"], spec["n"], select="%s,%s" % (idf, spec["field"]))
    except Exception as e:
        diff["namingUnavailable"] = _short(str(e), 160)
        return
    lookup = {}
    for row in r.get("top") or []:
        ident = row.get(idf)
        if ident is not None:
            lookup[hash_entity(salt, ident)] = str(ident)
    named, unnamed = {}, 0
    for bucket in ("appeared", "worsened", "improved", "disappeared"):
        for item in (diff.get(bucket) or [])[:limit]:
            key = item["id"] if isinstance(item, dict) else item
            if key in lookup:
                named.setdefault(bucket, {})[key] = lookup[key]
            else:
                unnamed += 1
    diff["names"] = named
    diff["namesNote"] = ("Looked up live just now, from the API, and not stored. %d id(s) could not be "
                         "named because they are no longer in the scope -- a disappeared entity is not in "
                         "the current result set by definition." % unnamed)


def _trend_series(snaps, table=None, by=None, metric=None):
    """Per-date values across the compared window, one entry per usable stack date, for charting.

    ABSENCE IS null, NEVER 0 -- this is the trend layer's most load-bearing rule carried into the
    series shape. A metric that wasn't captured on a date, a total a snapshot didn't record, a
    breakdown that wasn't taken, a group missing from a breakdown: each yields null at that
    position, which a renderer must draw as a BREAK in the line.

    This shipped once with an exception -- a group absent from a `complete: true` breakdown was
    charted as 0, on the theory that complete groups partition the records. It was wrong twice over.
    For a `List` field `complete` means "covered >= total" (a record can carry several values), which
    proves nothing about a value the sample never discovered. And `_trend_breakdowns` never made that
    exception for ANY field type, so the chart said 0 while the delta table on the same page said
    "unknown, not zero" -- and the chart is the one people read. A source the stratified sample
    happened to miss for one week drew a line falling to zero, which reads as a connector that
    stopped ingesting. No exception: an absent group is unknown.
    """
    dates = [r.get("stackDate") for r in snaps]
    out = {"dates": dates}
    # Per-date connector health. _coverage_verdict only examines the two ENDPOINTS, so an
    # intermediate date taken while half the connectors were failing was plotted unexamined: totals
    # dip, both endpoints are clean and comparable, and the chart shows a sharp V with no flag -- a
    # data-source outage rendered as an environment change, which is the exact reading the coverage
    # verdict exists to refuse. Absent coverage is null here too, never a healthy-looking 0.
    # Measured against the ENDPOINTS, not against perfection: _coverage_verdict has already declared
    # those two comparable, so they are the baseline this window is being read against. A stack that
    # permanently runs one failing connector would otherwise flag every date, and a flag on every
    # point says nothing. What matters is a middle date that dipped BELOW the compared baseline.
    ends = [r.get("coverage") or {} for r in (snaps[0], snaps[-1])]
    base_fail = max((c.get("failing") or 0) for c in ends)
    base_deg = max((c.get("degraded") or 0) for c in ends)
    flags = []
    for i, r in enumerate(snaps[1:-1], start=1):
        cov = r.get("coverage")
        if cov is None:
            flags.append({"date": dates[i], "reason": "connector coverage was not readable"})
            continue
        fail, deg = cov.get("failing") or 0, cov.get("degraded") or 0
        if fail > base_fail or deg > base_deg:
            flags.append({"date": dates[i], "failing": fail, "degraded": deg,
                          "reason": "%d connector(s) failing, %d degraded — worse than at either end "
                                    "of this window (%d failing, %d degraded)"
                                    % (fail, deg, base_fail, base_deg)})
    if flags:
        out["coverageFlags"] = flags
    totals = {}
    for key in ("assets", "users"):
        vals = [(r.get("totals") or {}).get(key) for r in snaps]
        if any(v is not None for v in vals):
            totals[key] = vals
    if totals:
        out["totals"] = totals
    labels = {}
    for r in snaps:
        for mt in (r.get("metrics") or []):
            if mt.get("name") and (not metric or mt.get("name") == metric):
                labels.setdefault(mt["name"], mt.get("label") or mt["name"])
    if labels:
        mseries = []
        for nm in sorted(labels):
            vals = []
            for r in snaps:
                mt = next((x for x in (r.get("metrics") or []) if x.get("name") == nm), None)
                vals.append(mt.get("count") if mt and mt.get("ok") else None)
            mseries.append({"name": nm, "label": labels[nm], "values": vals})
        out["metrics"] = mseries
    seen = {}
    for r in snaps:
        for bd in (r.get("breakdowns") or []):
            if not bd.get("by") or (table and bd.get("table") != table) or (by and bd.get("by") != by):
                continue
            seen.setdefault((bd.get("table"), bd["by"]), set()).update((bd.get("groups") or {}).keys())
    bseries = []
    for (tb, fld), groups in sorted(seen.items(), key=lambda kv: (kv[0][0] or "", kv[0][1])):
        gvals = {g: [] for g in groups}
        incomplete = []
        for r in snaps:
            bd = next((x for x in (r.get("breakdowns") or [])
                       if x.get("table") == tb and x.get("by") == fld), None)
            if bd is None:
                for g in groups:
                    gvals[g].append(None)
                continue
            if not bd.get("complete"):
                incomplete.append(r.get("stackDate"))
            gs = bd.get("groups") or {}
            for g in groups:
                gvals[g].append(gs[g] if g in gs else None)   # absent == unknown; see the docstring
        entry = {"table": tb, "by": fld, "groups": gvals}
        if incomplete:
            # Present-but-incomplete is different from absent: the counts are real exact counts, but
            # the breakdown didn't account for every record that date, and a chart must say so.
            entry["incompleteDates"] = incomplete
        bseries.append(entry)
    if bseries:
        out["breakdowns"] = bseries
    return out


def compute_trend(recs, skipped=None, since=None, metric=None, table=None, by=None):
    """Compare the earliest snapshot at/after `since` with the most recent one. Never fabricates a delta.

    Labelled `historical` on every path, refusals included: everything here is read back from local
    snapshots, so none of it is the stack's current state, however recent the last snapshot is."""
    out = _compute_trend(recs, skipped, since, metric, table, by)
    frm, to = (out.get("from") or {}).get("stackDate"), (out.get("to") or {}).get("stackDate")
    if not frm:
        days = sorted({r.get("stackDate") for r in recs if r.get("stackDate")})
        frm, to = (days[0], days[-1]) if days else (None, None)
    out["dataCurrency"] = {"class": "historical", "source": "local snapshots", "from": frm, "to": to}
    return out


def _compute_trend(recs, skipped=None, since=None, metric=None, table=None, by=None):
    out = {"generated": "trend", "snapshotsRead": len(recs)}
    if skipped:
        out["historySkipped"] = skipped
    by_date, unusable = _usable_snapshots(recs)
    if unusable:
        out["snapshotsUnusable"] = unusable
    dates = sorted(by_date)
    if since:
        eligible = [d for d in dates if d >= since]
        out["since"] = since
    else:
        eligible = dates
    # Fewer than two *distinct stack dates* is insufficient history, not a 0% flat line. Two snapshots
    # taken hours apart read the same daily ingest, so they are one point on the stack's timeline; a
    # 0% between them would be the flattest, most convincing wrong answer this verb could produce.
    # A point is a rebuild where one was recorded (see _usable_snapshots), so the count that decides
    # sufficiency is data points; distinctStackDates stays what its name says.
    out["dataPoints"] = len(eligible)
    if len(eligible) < 2:
        out["insufficientHistory"] = True
        out["distinctStackDates"] = len({by_date[k]["stackDate"] for k in eligible})
        out["stackDates"] = eligible
        out["note"] = (
            "%s Snapshots are compared on the stack's own ingest date, and %d of those is not a trend. "
            "Take snapshots on a cadence (`meridian.py digest --snapshot`) and this becomes answerable; "
            "there is no way to backfill, because the API keeps no history to backfill from."
            % (("No snapshots at or after %s." % since) if since and not eligible else
               ("Only %d distinct stack date%s in this history%s."
                % (len(eligible), "" if len(eligible) == 1 else "s",
                   " at or after %s" % since if since else "")),
               len(eligible)))
        out["changes"] = []
        # An explicitly requested metric is still reported as notTracked here, so phase 3's loop can
        # still attach a baseline and register it. This is the COMMON case for the question-to-metric
        # loop, not an edge one: someone asks "how has our KEV exposure trended since June?" precisely
        # when there is little history AND the metric was never defined. Returning only
        # insufficientHistory would answer "come back later" and leave the metric list just as empty,
        # so the same question would be just as unanswerable next month.
        if metric and not any(mt.get("name") == metric and mt.get("ok")
                              for r in recs for mt in (r.get("metrics") or [])):
            out["notTracked"] = [{"metric": metric,
                                  "reason": "no snapshot in this history captured a metric named %r, and "
                                            "the API keeps no history to backfill from" % metric}]
            out["notTrackedNote"] = _NOT_TRACKED_NOTE
        return out
    a, b = by_date[eligible[0]], by_date[eligible[-1]]
    out["from"] = {"stackDate": a.get("stackDate"), "takenAt": a.get("takenAt")}
    out["to"] = {"stackDate": b.get("stackDate"), "takenAt": b.get("takenAt")}
    for end, rec in (("from", a), ("to", b)):
        if rec.get("ldgRebuiltUtc"):
            out[end]["ldgRebuiltUtc"] = rec["ldgRebuiltUtc"]
    out["distinctStackDates"] = len({by_date[k]["stackDate"] for k in eligible})
    out["coverage"], blocked = _coverage_verdict(a, b)
    if blocked:
        # Unverifiable, so nothing is computed. Reporting the percentages "with a caveat" would be the
        # same mistake: the number is the part people read.
        out["unverifiable"] = True
        out["changes"] = []
        out["note"] = ("This trend is not verifiable, so no change has been computed. %s"
                       % out["coverage"]["reason"])
        return out
    caveats = []
    rows = []
    ta, tb = a.get("totals") or {}, b.get("totals") or {}
    for key, label in (("assets", "assets"), ("users", "users"),
                       ("assets30DayAvg", "assets (stack 30-day average)"),
                       ("users30DayAvg", "users (stack 30-day average)")):
        if ta.get(key) is None or tb.get(key) is None:
            caveats.append({"total": label, "note": "not captured at both endpoints, so not compared"})
            continue
        rows.append(_delta_row("total", label, ta[key], tb[key]))
    brows, tracked = _trend_breakdowns(a, b, table, by, caveats)
    rows += brows
    if by and not any(f == by for _, f in tracked):
        # Asked for a breakdown nobody was capturing. Same rule as an untracked metric: unanswerable,
        # not unchanged.
        out.setdefault("notTracked", []).append(
            {"breakdown": by, "reason": "no snapshot in this history captured a breakdown by %r, and "
                                        "the API keeps no history to backfill from" % by})
    mrows, not_tracked = _trend_metrics(a, b, metric, caveats)
    rows += mrows
    if not_tracked:
        out.setdefault("notTracked", []).extend(not_tracked)
    rows += _trend_rankings(a, b, table, caveats)
    out["changes"] = rows
    # Per-date series for charting (additive-optional key, schema unchanged). Deliberately only on
    # this computed path: a refusal (insufficientHistory, unverifiable) carries no series, because a
    # chart drawn over unverifiable coverage is the percentage-with-a-caveat mistake in picture form.
    out["series"] = _trend_series([by_date[d] for d in eligible], table, by, metric)
    if a.get("entities") or b.get("entities"):
        out["entities"] = _trend_entities(a, b)
    if caveats:
        out["caveats"] = caveats
    if out.get("notTracked"):
        out["notTrackedNote"] = _NOT_TRACKED_NOTE
    return out


def resolve_not_tracked(out, name, label=None, table=None, where=None, smart_label=None):
    """The question-to-metric loop: turn an unanswerable trend into a baseline plus a fix.

    THERE IS NO BACKFILL. The API holds no history, so a trend can only answer what was already being
    captured -- which means the decision about what to store has to happen *before* the question is
    asked. An empty metric list therefore makes every tier-2 trend permanently unanswerable, and the
    whole feature quietly degrades to "totals only".

    So an unanswerable question writes the metric that makes it answerable next time: state plainly that
    it was not captured, give today's value as a baseline, and register the metric. Two rules hold:
    registering is an implicit write to the user's config, so it is REPORTED in the output; and the
    derived metric is validated exactly like a hand-added one, so a question that could not be answered
    never leaves a permanently broken metric behind.

    What this must never do is render the absence as `0`, `0%`, "no change" or a flat line. That is the
    most plausible-looking version of the wrong-answer class this file has been hardened against six
    times, because nothing about a flat line looks partial.
    """
    entry = next((nt for nt in out.get("notTracked") or [] if nt.get("metric") == name), None)
    if entry is None or entry.get("unavailableAt"):
        return   # tracked, or it resolved-and-failed, which is a different report entirely
    defined = next((m for m in load_metrics() if m.get("name") == name), None)
    if defined is None and (where or smart_label):
        rec, problem = add_metric(name, label, table, where, smart_label, origin="derived")
        if problem:
            # Refused, and said why -- at the cap this is where "which one should I drop?" comes from.
            # Nothing is written, so an invalid definition cannot leave a broken metric behind.
            entry["registrationRefused"] = problem
            entry["registered"] = False
        else:
            defined = rec
            entry["registered"] = rec
            entry["registeredNote"] = (
                "%r has been added to this stack's tracked metrics (%s), so this question becomes a real "
                "trend from the next snapshot on. Say so in the answer: this wrote to the user's config."
                % (name, _metrics_path()))
    if defined is not None:
        # A definition exists (just registered, or defined but not yet snapshotted), so today's value is
        # one call away. A baseline is what makes "not captured" useful rather than merely honest.
        entry["baseline"] = measure_metric(defined)
        entry["baselineNote"] = ("Today's value, measured now. It is a BASELINE, not a trend: there is "
                                 "nothing to compare it against yet.")


def cmd_trend(a):
    recs, skipped = load_snapshots()
    out = compute_trend(recs, skipped, since=a.since, metric=a.metric, table=a.table, by=a.by)
    if a.metric:
        resolve_not_tracked(out, a.metric, label=a.derive_label, table=a.table or a.derive_table,
                            where=a.derive_where, smart_label=a.derive_smart_label)
    if getattr(a, "name_entities", False) and out.get("entities"):
        # The stamp is read beside the lookups rather than after them. When no name comes back it goes
        # unused, which costs one small call; waiting to find out would cost its full latency every time.
        named, stamp = parallel([lambda: name_entities(out["entities"]), ldg_rebuild])
        if isinstance(named, Exception):
            raise named
        if out["entities"].get("names") is not None:
            # The deltas are historical; the names were looked up live just now. A mixed payload, so
            # the live part is labelled -- with a real stamp, not a bare "current".
            try:
                spec, _ = parse_entity_scope(out["entities"].get("scope"), allow_large=True)
            except Exception:  # noqa - labelling must never fail the verb
                spec = None
            tables = [_table_kind(spec["table"])] if spec else ["asset", "user"]
            live = data_currency(_stamp_or_unknown(stamp), tables)
            live["source"] = "names looked up live; the deltas themselves are from local snapshots"
            out["dataCurrency"] = dict(out["dataCurrency"], sections={"entities.names": live})
    out["stack"] = load_config()[0]
    if getattr(a, "format", None) == "csv":
        # The envelope prints alongside the CSV so its caveats stay visible; hundreds of per-date
        # series values would bury exactly the flags it exists to surface. The series is for
        # `report`'s charts, which read the JSON form.
        out.pop("series", None)
    # Through emit(), so `--format csv` still prints the envelope: a spreadsheet column cannot carry
    # `coverageChanged` or `insufficientHistory`, and a CSV of percentages with those dropped is
    # precisely the artefact this verb exists to prevent.
    emit(out, "changes", getattr(a, "format", None), getattr(a, "out", None))


# --- alerts (MVP, dormant: no SKILL.md routing yet) -------------------------------------------
# Alerting is a PURE FUNCTION over a trend plus the latest snapshot. It issues no requests of its own,
# so it cannot introduce a new wrong-answer class, and it inherits every refusal the trend layer already
# computes (`insufficientHistory`, `unverifiable`, `notTracked`, `coverageChanged`).
#
# THE WHOLE DESIGN IS THE THIRD VERDICT. A rule is `firing`, `clear`, or `unevaluable` -- never just the
# first two. A metric that stopped resolving, a history too short to compare, unreadable coverage: each
# means the condition COULD NOT BE TESTED, which is not the same as passing it. Two-state alerting would
# report "all clear" in every one of those cases, and alerting is precisely the layer people stop
# watching because it is supposed to watch for them. A broken alert that reads as healthy is the worst
# failure this file can ship, worse than a firing one, because nothing about it looks wrong.
#
# Measured on a live stack while designing this: a rule on `kev-critical` was unevaluable (the metric was
# not captured in the earlier snapshot) at the same moment a connector entered the failing set. A
# conventional two-state implementation would have printed "3 rules checked, 0 firing".
ALERTS_SCHEMA = 1
MAX_ALERTS = 20
# `above`/`below` read the LATEST snapshot, so they work from the first snapshot on; `coverage-regressed`
# is inherently a comparison and needs two distinct stack dates.
ALERT_CONDITIONS = ("coverage-regressed", "above", "below")

# Exit codes are a BIT FIELD, deliberately starting at 4: `die()` already owns 2 (validation/usage) and
# main()'s catch-all owns 1 (internal error). Reusing those would make "an alert is firing"
# indistinguishable from "you passed a bad argument" to the scheduler that has to route the result.
# 0 = everything evaluated and nothing firing. 4 = firing. 8 = something unevaluable. 12 = both.
# Not an ordered severity: collapsing it would force a choice about whether a firing alert outranks a
# broken one, and either answer hides half the picture.
ALERT_EXIT_FIRING = 4
ALERT_EXIT_UNEVALUABLE = 8


def _alert_num(n):
    """Thousands-separated, and integral floats without the trailing `.0`.

    `--value` is parsed as a float so `0.5` works, which otherwise renders a threshold of 1500 as
    "1500.0" -- it reads like a precision the operator did not ask for.
    """
    if isinstance(n, float) and n.is_integer():
        n = int(n)
    return "{:,}".format(n) if isinstance(n, int) else str(n)


def _alerts_path():
    fqdn, _, _ = load_config()
    return os.path.join(CFG_DIR, "alerts.%s.json" % re.sub(r"[^A-Za-z0-9._-]", "_", fqdn))


def _alertstate_path():
    """Where per-rule firing state lives. Reserved unwritten in the MVP so adding it was additive
    rather than a schema change, exactly as `metrics` and `entities` were added inside `"schema": 1`;
    `alerts notify` (below) is what now reads and writes it. `alerts eval` still never touches it --
    only delivery cares about change, not evaluation. The shape:

        {"schema": 1, "fqdn": "...",
         "rules": {"<rule name>": {"firstSeen": "<stackDate>", "lastVerdict": "firing|clear|unevaluable",
                                   "lastSeen": "<stackDate>"}}}

    It exists for CHANGE DETECTION, not suppression -- classifying a firing alert as new / unchanged
    since <date> / resolved, so a delivery can lead with what changed while `render_alerts_*` still
    lists everything. Suppression would be a way for this tool to go quiet, and a suppression bug is a
    silent failure; nothing here may ever drop a verdict from the rendered output -- see
    `update_alertstate` for where change is decided and `deliver_alerts` for where it is acted on.
    """
    fqdn, _, _ = load_config()
    return os.path.join(CFG_DIR, "alertstate.%s.json" % re.sub(r"[^A-Za-z0-9._-]", "_", fqdn))


def load_alerts():
    """Configured rules. `[]` ONLY when no rules file exists; a malformed one raises.

    This deliberately does NOT follow `load_metrics`' `except Exception: return []`. For metrics that
    degrades into a visible `notTracked` on the next trend, but for alerts an empty list means "nothing
    is being watched", and reporting a corrupt rules file as "no rules configured" would send the
    operator off to define rules that already exist while the real fault -- an unreadable file -- goes
    unmentioned. Absent and unreadable are different states and must read differently.

    Read `utf-8-sig`: an install upgraded from <=v2.1 can still have BOM'd files in this directory, and
    a plain `utf-8` read would throw inside a `try/except` that returns [] -- silently unwatched.
    """
    path = _alerts_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8-sig") as f:
            doc = json.load(f)
    except Exception as e:
        raise RuntimeError("the alert rules file at %s could not be read (%s). No rule was evaluated. "
                           "Fix or delete the file -- this is not the same as having no rules."
                           % (path, e))
    if not isinstance(doc, dict) or doc.get("schema") != ALERTS_SCHEMA:
        raise RuntimeError("the alert rules file at %s is not a schema-%d rules document. No rule was "
                           "evaluated. Fix or delete the file -- this is not the same as having no rules."
                           % (path, ALERTS_SCHEMA))
    return [r for r in (doc.get("rules") or []) if isinstance(r, dict) and r.get("name")]


def save_alerts(rules):
    _ensure_cfg_dir()
    payload = {"schema": ALERTS_SCHEMA, "fqdn": load_config()[0], "rules": rules}
    # Same atomic write as the other config-dir files -- it takes the OBJECT and dumps it itself. A
    # truncated rules file would silently become "no alerts configured", which is the quietest possible
    # way for alerting to stop; `nothingChecked` covers the read side of that, this covers the write.
    _private_write(_alerts_path(), payload)
    return _alerts_path()


def alert_rule_problem(rule):
    """Message for the first thing wrong with a rule definition, or None. Never exits.

    Validated at `add` time AND on every eval, for the reason `metric_query` documents: a metric can be
    deleted after a rule referencing it is saved, and a rule pointing at nothing must be reported rather
    than quietly never firing.
    """
    name = rule.get("name")
    if not name:
        return "an alert rule needs a --name."
    cond = rule.get("condition")
    if cond not in ALERT_CONDITIONS:
        return ("%r is not an alert condition, in rule %r. Expected one of: %s."
                % (cond, name, ", ".join(ALERT_CONDITIONS)))
    if cond == "coverage-regressed":
        return None
    metric = rule.get("metric")
    if not metric:
        return "rule %r uses %r, which needs --metric <name>." % (name, cond)
    if not any(m.get("name") == metric for m in load_metrics()):
        tracked = ", ".join(sorted(m["name"] for m in load_metrics())) or "none"
        return ("rule %r targets a metric named %r, which is not tracked on this stack (tracked: %s). "
                "Define it with `metrics add` first -- a rule pointing at an untracked metric can never "
                "fire, and would report as clear." % (name, metric, tracked))
    if not isinstance(rule.get("value"), (int, float)) or isinstance(rule.get("value"), bool):
        return "rule %r uses %r, which needs a numeric threshold value." % (name, cond)
    return None


def add_alert(name, condition, metric=None, value=None, include_degraded=False, existing=None):
    """(rule, problem) -- validated before it is saved."""
    rules = load_alerts() if existing is None else existing
    rec = {"name": name, "condition": condition}
    if metric:
        rec["metric"] = metric
    if value is not None:
        rec["value"] = value
    if include_degraded:
        rec["includeDegraded"] = True
    problem = alert_rule_problem(rec)
    if problem:
        return None, problem
    others = [r for r in rules if r.get("name") != name]
    if len(others) >= MAX_ALERTS:
        return None, ("this stack already tracks %d alert rules (the cap). Remove one with "
                      "`alerts rm <name>` first." % MAX_ALERTS)
    save_alerts(others + [rec])
    return rec, None


def _latest_metric(latest, name):
    """(metric record, problem) for one metric in a snapshot record."""
    for mt in (latest or {}).get("metrics") or []:
        if mt.get("name") == name:
            if mt.get("ok") is False:
                # Never a zero. A metric that stopped resolving has an UNKNOWN count, and a zero would
                # read as good news on a threshold rule.
                return None, ("the metric did not resolve when the latest snapshot was taken (%s), so "
                              "its count is unknown rather than zero"
                              % (mt.get("error") or "no reason recorded"))
            if not isinstance(mt.get("count"), (int, float)):
                return None, "the latest snapshot recorded no count for this metric"
            return mt, None
    return None, ("no metric named %r was captured in the latest snapshot, and the API keeps no history "
                  "to backfill from" % name)


def alert_window_since(dates, window="daily", since=None):
    """The `since` to hand `compute_trend`, for a sorted list of distinct stack dates.

    Day-over-day is the default because "what changed since yesterday" is the alerting question. The
    trend layer's own default compares the EARLIEST snapshot with the latest, which as an alert window
    means a baseline receding further into the past every day: a regression that happened once keeps
    firing forever and the rule stops carrying information about now.

    Expressed as a `since` rather than as a change to `compute_trend`, which already selects the earliest
    date at or after it. An explicit `--since` always wins; `full` restores whole-history comparison.
    """
    if since is not None:
        return since
    if window == "daily" and len(dates or []) >= 2:
        return sorted(dates)[-2]
    return None


def _cap_names(names, cap=5):
    """Names for a message, with the remainder DISCLOSED rather than dropped.

    A fleet-wide move is 55 names long, and pasting all of them into a chat line buries the sentence
    that matters. Same shape as `profile`'s linked-asset cap: show the first few, say how many more
    there are -- never silently truncate, because a shortened list that does not say it is shortened
    is a wrong answer about the size of the event.
    """
    names = list(names)
    if len(names) <= cap:
        return ", ".join(names)
    return "%s and %d more" % (", ".join(names[:cap]), len(names) - cap)


def _degraded_context(cov, standing, rule):
    """(extra row fields, sentence) describing the DEGRADED picture on a `coverage-regressed` row.

    Informational ONLY -- nothing here changes firing/clear/unevaluable -- and it rides on every verdict
    whether or not the rule opted into `includeDegraded`.

    Measured by replaying this verb day-by-day over real history: on 2026-09-12 -> 13 fifty-five
    connectors entered the degraded set at once, and the row read "no connector entered the failing set
    since 2026-09-12 - but 3 are STILL failing". Correct about the failing set, silent about all 55. The
    verdict was right and the row still read as an all-clear, which is the flat-line problem the trend
    layer exists to refuse, relocated into an alert. So the degraded movement travels WITH the row
    instead of being dropped along with the flag that decides whether it fires.
    """
    fields, parts = {}, []
    entered = sorted(cov.get("degradedAdded") or [])
    left = sorted(cov.get("degradedResolved") or [])
    watches_degraded = bool(rule.get("includeDegraded"))
    # When the rule opted in, `degradedAdded` is already on the row as the evidence that FIRED it.
    # Repeating it under a second key would make the payload state the same thing twice with two
    # different meanings -- what fired, and what merely happened.
    if entered and not watches_degraded:
        fields["degradedEntered"] = entered
    if left:
        fields["degradedLeft"] = left
    count, names = standing.get("degraded"), standing.get("degradedNames")
    if isinstance(count, int):
        fields["stillDegradedCount"] = count
    if names is not None:
        fields["stillDegraded"] = sorted(names)
    if count is None and names is None:
        # Never a zero. A snapshot that did not record the degraded set is not saying none are degraded.
        fields["stillDegradedUnknown"] = True

    if entered and not watches_degraded:
        parts.append("%d connector%s also entered the DEGRADED set: %s — this rule watches the "
                     "failing set only, so that did not affect the verdict."
                     % (len(entered), "" if len(entered) == 1 else "s", _cap_names(entered)))
    if left:
        parts.append("%d left the degraded set." % len(left))
    if isinstance(count, int) and count:
        parts.append("%d degraded in total." % count)
    elif fields.get("stillDegradedUnknown"):
        parts.append("The standing degraded count was not readable in this snapshot, so this is not a "
                     "statement that none are degraded.")
    return fields, (" ".join(parts) if parts else None)


def _attach_degraded(row, cov, standing, rule):
    """Fold `_degraded_context` into a row, in place. BOTH verdicts go through here: a `firing` row that
    carried the degraded picture while a `clear` row did not would leave the reassuring verdict as the
    less informative one, which is exactly backwards."""
    extra, sentence = _degraded_context(cov, standing, rule)
    row.update(extra)
    if sentence:
        row["message"] = "%s %s" % (row["message"], sentence)


def evaluate_alerts(trend, latest, rules):
    """Verdicts for each rule. PURE -- no I/O, no requests, no clock.

    `trend` supplies the comparison and every refusal already computed there; `latest` supplies present
    values, so an absolute threshold works from the FIRST snapshot rather than waiting for history. That
    split is deliberate: making `above 1500` unevaluable just because there is only one snapshot would be
    wrong -- the value is known -- while `coverage-regressed` genuinely cannot be answered without two.
    """
    out = {"generated": "alerts", "rules": len(rules),
           "firing": [], "clear": [], "unevaluable": []}
    window = {}
    if (trend or {}).get("from"):
        window["from"] = trend["from"].get("stackDate")
    if (trend or {}).get("to"):
        window["to"] = trend["to"].get("stackDate")
    if trend and trend.get("distinctStackDates") is not None:
        window["distinctStackDates"] = trend["distinctStackDates"]
    if trend and trend.get("dataPoints") is not None:
        window["dataPoints"] = trend["dataPoints"]
    if window.get("from") and window.get("to"):
        # State the ACTUAL span. The default window is "the last two snapshots", which is day-over-day
        # only if a snapshot was taken both days -- a laptop asleep over a weekend makes the same two
        # records three days apart. Calling that "since yesterday" would be a false claim, and a 3-day
        # movement read as a 1-day one is the kind of quiet wrongness that needs no help spreading.
        try:
            d0 = datetime.date.fromisoformat(window["from"])
            d1 = datetime.date.fromisoformat(window["to"])
            window["days"] = (d1 - d0).days
            # "No snapshot in between" is only true when the window holds exactly the two endpoints --
            # i.e. the daily window. With `--window full` there are intermediate dates, and claiming
            # otherwise would be a false statement about the operator's own history.
            if window["days"] > 1 and window.get("dataPoints", window.get("distinctStackDates")) == 2:
                window["consecutive"] = False
                window["note"] = ("these are the two most recent snapshots, %d days apart -- no snapshot "
                                  "was taken on the days between, and there is no way to backfill them"
                                  % window["days"])
        except (TypeError, ValueError):
            pass
    if window:
        out["window"] = window
    cov = (trend or {}).get("coverage") or {}
    if cov.get("coverageChanged"):
        out["coverageChanged"] = True

    def unevaluable(rule, reason):
        out["unevaluable"].append({
            "rule": rule.get("name"), "condition": rule.get("condition"),
            "metric": rule.get("metric"), "reason": reason,
            # Spelled out in the payload, not just implied by which list it is in. Whatever renders this
            # downstream -- a chat answer, a channel post, someone's jq -- must not be able to read an
            # unevaluable rule as a passing one.
            "note": "This rule did NOT pass. Its condition could not be tested at all."})

    for rule in rules:
        problem = alert_rule_problem(rule)
        if problem:
            unevaluable(rule, problem)
            continue
        cond = rule["condition"]
        if cond == "coverage-regressed":
            if not trend or trend.get("unverifiable"):
                unevaluable(rule, (cov.get("reason")
                                   or "connector health was not readable at both ends of the window"))
                continue
            if trend.get("insufficientHistory"):
                unevaluable(rule, "a regression is a comparison, and this history has %d distinct stack "
                                  "date(s); there is no way to backfill the missing one"
                                  % (trend.get("distinctStackDates") or 0))
                continue
            if not cov.get("comparable"):
                unevaluable(rule, cov.get("reason") or "coverage was not comparable across the window")
                continue
            if not cov.get("namesComparable", True):
                # The counts are fine and a threshold rule can still use them, but THIS rule is
                # entirely a question about which connectors changed state -- which is the one thing
                # an identity-scheme change makes unanswerable. `clear` here would report "no
                # connector entered the failing set" on evidence that cannot support it.
                unevaluable(rule, cov.get("namesIncomparableReason")
                            or "connector identities are not comparable across the window")
                continue
            added = list(cov.get("failingAdded") or [])
            kinds = {"failingAdded": added}
            if rule.get("includeDegraded"):
                deg = list(cov.get("degradedAdded") or [])
                if deg:
                    kinds["degradedAdded"] = deg
            # STANDING state, read from the latest snapshot -- not the delta. This rule detects CHANGE,
            # so on a day-over-day window a connector that has been failing for a fortnight produces no
            # change and the rule is legitimately `clear`. Reporting that as a bare "clear" would let
            # "nothing failed since yesterday" be read as "no connectors are failing", which is the same
            # reassuring wrong answer this whole verb exists to refuse -- just relocated from the
            # unevaluable case to the clear one. So every verdict carries the standing count, and `clear`
            # says it in words.
            standing = ((latest or {}).get("coverage") or {})
            still = sorted(standing.get("failingNames") or [])
            hit = [v for v in kinds.values() if v]
            if hit:
                names = sorted({n for v in kinds.values() for n in v})
                row = {
                    "rule": rule["name"], "condition": cond,
                    **{k: v for k, v in kinds.items() if v},
                    "stillFailing": still,
                    "message": ("%d connector%s entered the %s set since %s: %s. Anything %s feeds is "
                                "now stale.%s"
                                % (len(names), "" if len(names) == 1 else "s",
                                   "failing/degraded" if rule.get("includeDegraded") else "failing",
                                   window.get("from") or "the earlier snapshot", ", ".join(names),
                                   "it" if len(names) == 1 else "they",
                                   (" %d connector%s failing in total." % (len(still), " is" if len(still) == 1
                                                                          else "s are"))
                                   if still else ""))}
                _attach_degraded(row, cov, standing, rule)
                out["firing"].append(row)
            else:
                row = {"rule": rule["name"], "condition": cond, "stillFailing": still}
                if still:
                    row["message"] = ("no connector entered the failing set since %s — but %d %s STILL "
                                      "failing: %s. This rule watches for change, so it stays clear "
                                      "while they keep failing."
                                      % (window.get("from") or "the earlier snapshot", len(still),
                                         "is" if len(still) == 1 else "are", ", ".join(still)))
                elif standing.get("failing") is None:
                    # Coverage counts were comparable (the trend said so) but this record carries no
                    # names -- so "0 failing" is not something that can be asserted here.
                    row["message"] = ("no connector entered the failing set since %s. The standing count "
                                      "was not readable in this snapshot, so this is not a statement that "
                                      "none are failing." % (window.get("from") or "the earlier snapshot"))
                else:
                    row["message"] = ("no connector entered the failing set since %s, and none are "
                                      "failing now." % (window.get("from") or "the earlier snapshot"))
                _attach_degraded(row, cov, standing, rule)
                out["clear"].append(row)
            continue
        # above / below -- present value against a fixed threshold.
        mt, problem = _latest_metric(latest, rule["metric"])
        if problem:
            unevaluable(rule, problem)
            continue
        observed, threshold = mt["count"], rule["value"]
        fired = observed > threshold if cond == "above" else observed < threshold
        row = {"rule": rule["name"], "condition": cond, "metric": rule["metric"],
               "label": mt.get("label"), "observed": observed, "threshold": threshold,
               "at": (latest or {}).get("stackDate")}
        if out.get("coverageChanged"):
            # Flagged, not blocked -- matching the trend layer, which blocks on UNREADABLE coverage and
            # only flags CHANGED coverage. Which connector feeds which metric is not something the API
            # exposes, so a stronger claim here would be a guess. Carried per-rule in the payload for
            # anything consuming the JSON; the markdown says it once at the foot instead of on every row.
            row["caveat"] = ("connector health changed across this window, so treat a movement in this "
                             "number as possibly a reporting change rather than a real one")
        # A `clear` row gets a message too. An empty cell in the rendered table is indistinguishable from
        # a rendering failure, and "no message" is not a thing this verb is allowed to say about a rule.
        row["message"] = ("%s: %s, %s a threshold of %s"
                          % (mt.get("label") or rule["metric"], _alert_num(observed),
                             "above" if observed > threshold else
                             "below" if observed < threshold else "exactly at",
                             _alert_num(threshold)))
        (out["firing"] if fired else out["clear"]).append(row)

    out["summary"] = {"rules": len(rules), "firing": len(out["firing"]),
                      "clear": len(out["clear"]), "unevaluable": len(out["unevaluable"])}
    code = 0
    if out["firing"]:
        code |= ALERT_EXIT_FIRING
    if out["unevaluable"]:
        code |= ALERT_EXIT_UNEVALUABLE
    if not rules:
        # An empty rule set is the whole-run version of `unevaluable`, and it exits non-zero for the same
        # reason. Exiting 0 here would tell a scheduler "healthy" when the truth is "nothing was checked"
        # -- and a stack whose rules were never configured, or whose rules file was lost, looks exactly
        # like a stack with nothing wrong. That is the failure this verb exists to prevent, so it must not
        # be the failure the verb itself ships with.
        code |= ALERT_EXIT_UNEVALUABLE
        out["nothingChecked"] = True
    out["exitCode"] = code
    if not rules:
        out["note"] = ("No alert rules are configured for this stack, so NOTHING was checked. An empty "
                       "rule set is not an all-clear; this run cannot tell you anything about the stack.")
    elif out["unevaluable"]:
        n = len(out["unevaluable"])
        out["note"] = ("%d rule%s could not be evaluated. That is not the same as passing: until the "
                       "reason is fixed, %s condition%s untested and this run cannot tell you whether "
                       "%s hold." % (n, "" if n == 1 else "s", "that" if n == 1 else "those",
                                     " is" if n == 1 else "s are", "it does" if n == 1 else "they do"))
    return out


def render_alerts(v, fmt="markdown"):
    """Verdicts -> text. PURE, and separate from evaluation on purpose.

    Keeping this split means a future Teams/Slack/email renderer is a new branch here plus a transport,
    with nothing to change in `evaluate_alerts`. Interleaving formatting into evaluation is what makes
    every later delivery target a rewrite.
    """
    if fmt == "json":
        return json.dumps(v, indent=2)
    s, w = v["summary"], v.get("window") or {}
    dot = {"firing": "\U0001f534", "unevaluable": "⚪", "clear": "\U0001f7e2"}
    head = []
    if s["firing"]:
        head.append("%s %d alert%s firing" % (dot["firing"], s["firing"], "" if s["firing"] == 1 else "s"))
    if s["unevaluable"]:
        head.append("%s %d could not be evaluated" % (dot["unevaluable"], s["unevaluable"]))
    if v.get("nothingChecked"):
        # Never "all clear" here. The headline is the part that gets read, and a green headline over a
        # stack with no rules configured is the same lie as a two-state alert reporting a broken rule as
        # passing -- just one level up.
        head = ["%s nothing checked — no alert rules configured" % dot["unevaluable"]]
    if not head:
        head.append("%s all clear — %d rule%s evaluated"
                    % (dot["clear"], s["clear"], "" if s["clear"] == 1 else "s"))
    span = ""
    if w.get("from"):
        span = " — %s → %s" % (w["from"], w["to"])
        if w.get("days") == 1:
            span += " (day over day)"
        elif w.get("consecutive") is False:
            # Named, not glossed. The daily window is "the last two snapshots", and only a reader who is
            # told the gap can judge whether a movement across it means what they assume.
            span += " (%d days — no snapshot in between)" % w["days"]
        elif w.get("days"):
            span += " (%d days, %d snapshots)" % (w["days"], w.get("distinctStackDates") or 0)
    lines = ["**%s**%s" % (" · ".join(head), span)]
    lines.append("")
    lines.append("| | Rule | What happened |")
    lines.append("|---|---|---|")
    for kind in ("firing", "unevaluable", "clear"):
        for r in v[kind]:
            if kind == "unevaluable":
                what = "**Could not be evaluated** — %s. This is not \"under threshold\"." % r["reason"]
            else:
                # No per-row caveat here: with coverageChanged it would repeat on every row and then
                # again at the foot. Said once, below. The JSON keeps it per-rule.
                what = r.get("message") or ""
            lines.append("| %s | **%s** | %s |" % (dot[kind], r["rule"], what))
    if v.get("coverageChanged"):
        lines += ["", "⚠️ Connector health changed across this window. Which connector feeds "
                      "which number is not something the API exposes, so judge relevance before reading "
                      "any movement as real."]
    if v.get("note"):
        lines += ["", v["note"]]
    return "\n".join(lines)


# --- alerts delivery (Slack / Teams / email) ---------------------------------------------------
# Still dormant in the same sense the rest of this section is: reachable only via `alerts notify`,
# no SKILL.md routing. Building this now is safe ahead of the two evaluation-semantics decisions
# still parked from the MVP (degraded->failing coverage sensitivity, clear-under-coverageChanged)
# because delivery only ever reads evaluate_alerts()'s existing output -- it cannot change what a
# rule decides, only whether the decision gets sent anywhere.
#
# ON-CHANGE DELIVERY, using the alertstate file the MVP reserved but deliberately left unwritten.
# This is where it starts being written. A rule's verdict differing from the last SAVED state is a
# change; nothing else is -- so a connector that has been failing for a week does not re-notify on
# every run, but the message itself (`render_alerts_*`) still lists it every time it DOES send,
# exactly like `render_alerts`'s own "STILL failing" standing-state text. Change decides whether to
# send; it must never decide what the message says.
NOTIFY_TARGETS = ("slack", "teams", "email")

NOTIFY_ENV_HINTS = {
    "slack": "MERIDIAN_ALERT_SLACK_WEBHOOK",
    "teams": "MERIDIAN_ALERT_TEAMS_WEBHOOK",
    "email": "MERIDIAN_ALERT_EMAIL_SMTP_HOST and MERIDIAN_ALERT_EMAIL_TO (optionally "
             "..._SMTP_PORT / ..._FROM / ..._SMTP_USER / ..._SMTP_PASS / ..._STARTTLS=0)",
}


def _alert_head_span(v):
    """(headline fragments incl. dot, span suffix) -- the exact wording every renderer leads with,
    factored out so "N firing" / "nothing checked" / "all clear" cannot drift between the markdown
    table, Slack, Teams, and email. Mirrors `render_alerts`'s own head/span construction rather than
    replacing it: that function already has exact-substring test coverage this must not risk.
    """
    dot = {"firing": "\U0001f534", "unevaluable": "⚪", "clear": "\U0001f7e2"}
    s, w = v["summary"], v.get("window") or {}
    head = []
    if s["firing"]:
        head.append("%s %d alert%s firing" % (dot["firing"], s["firing"], "" if s["firing"] == 1 else "s"))
    if s["unevaluable"]:
        head.append("%s %d could not be evaluated" % (dot["unevaluable"], s["unevaluable"]))
    if v.get("nothingChecked"):
        head = ["%s nothing checked — no alert rules configured" % dot["unevaluable"]]
    if not head:
        head.append("%s all clear — %d rule%s evaluated"
                    % (dot["clear"], s["clear"], "" if s["clear"] == 1 else "s"))
    span = ""
    if w.get("from"):
        span = " — %s → %s" % (w["from"], w["to"])
        if w.get("days") == 1:
            span += " (day over day)"
        elif w.get("consecutive") is False:
            span += " (%d days — no snapshot in between)" % w["days"]
        elif w.get("days"):
            span += " (%d days, %d snapshots)" % (w["days"], w.get("distinctStackDates") or 0)
    return head, span


def _alert_rows(v):
    """(kind, dot, rule name, plain message) for every rule, firing first -- shared by the Slack/
    Teams/email renderers below. `render_alerts`'s own markdown table builds its rows independently,
    for the same zero-risk-to-existing-tests reason as `_alert_head_span`.
    """
    dot = {"firing": "\U0001f534", "unevaluable": "⚪", "clear": "\U0001f7e2"}
    rows = []
    for kind in ("firing", "unevaluable", "clear"):
        for r in v[kind]:
            if kind == "unevaluable":
                text = "Could not be evaluated — %s. This is not \"under threshold\"." % r["reason"]
            else:
                text = r.get("message") or ""
            rows.append((kind, dot[kind], r["rule"], text))
    return rows


def _slack_esc(s):
    """Slack's three control characters. Rule names and messages can carry connector names from the
    customer's environment, and unescaped, `<!channel>` pings a whole channel and `<https://x|y>`
    renders a disguised link -- the Slack-shaped version of the unescaped-HTML bug."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_alerts_slack(v):
    """Slack incoming-webhook payload. Slack's markup is mrkdwn, not markdown -- single asterisks for
    bold, no table syntax -- so this builds its own text rather than reusing `render_alerts`'s table.
    """
    head, span = _alert_head_span(v)
    lines = ["*%s*%s" % (_slack_esc(" · ".join(head)), _slack_esc(span)), ""]
    for kind, dot, rule, text in _alert_rows(v):
        lines.append("%s *%s* — %s" % (dot, _slack_esc(rule), _slack_esc(text)))
    if v.get("coverageChanged"):
        lines += ["", "⚠️ Connector health changed across this window. Which connector feeds which "
                      "number is not something the API exposes, so judge relevance before reading any "
                      "movement as real."]
    if v.get("note"):
        lines += ["", _slack_esc(v["note"])]
    return {"text": "\n".join(lines)}


def _teams_esc(s):
    """Neutralise Adaptive Card markdown links in text that came from the customer's environment.

    A TextBlock renders markdown, so a connector named `[Reset your password](https://…)` would arrive
    as a clickable link inside an alert -- in the one message a team is primed to act on. Breaking the
    `](` adjacency is enough: the text still reads the same, and no link forms.
    """
    return str(s).replace("](", "] (")


def render_alerts_teams(v):
    """Payload for a Teams Workflows webhook: a `message` with one Adaptive Card attachment.

    Microsoft retired the Office 365 Connector webhooks; a Teams webhook today is a Workflows (Power
    Automate) flow on the "When a Teams webhook request is received" trigger. Its default template,
    "Post to a channel when a webhook request is received", loops over `attachments` and posts each one
    with "Post card in a chat or channel" -- so a body without them is accepted with **HTTP 202 and
    posts nothing**. That is what the first version sent (`{title, text}`, a guess made before any flow
    existed): every Teams alert would have been recorded as delivered, and because delivery is
    on-change only, never retried. The envelope below is the shape Microsoft documents for this
    trigger (Teams platform docs, "Create an Incoming Webhook") and the one its Q&A answers give for
    the silent-202 case. A top-level `text` is kept for a flow built from scratch that reads that field
    instead -- the template ignores it.

    Still not verified against a live flow, and it cannot be from here: that needs a Teams channel
    with a Workflows webhook. A 202 is also not proof of a post -- the trigger acknowledges before the
    flow runs -- so the check is the card appearing in the channel.
    """
    head, span = _alert_head_span(v)
    headline = "%s%s" % (" · ".join(head), span)
    blocks = [{"type": "TextBlock", "text": headline, "weight": "Bolder", "size": "Medium", "wrap": True}]
    lines = []
    for kind, dot, rule, text in _alert_rows(v):
        line = "%s **%s** — %s" % (dot, _teams_esc(rule), _teams_esc(text))
        lines.append(line)
        blocks.append({"type": "TextBlock", "text": line, "wrap": True})
    notes = []
    if v.get("coverageChanged"):
        notes.append("⚠️ Connector health changed across this window; judge relevance before reading "
                     "any movement as real.")
    if v.get("note"):
        notes.append(_teams_esc(v["note"]))
    for n in notes:
        blocks.append({"type": "TextBlock", "text": n, "wrap": True, "isSubtle": True})
    card = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
            "version": "1.4", "body": blocks, "msteams": {"width": "Full"}}
    # The top-level `text` is for hand-built flows, some of which post it through an action that
    # renders HTML. Only `<` and `>` are escaped: they are what make markup, and an `&` left alone
    # keeps the text readable in the flows that show it as plain text. The card body is Adaptive
    # Card markdown and is escaped separately.
    fallback = "\n\n".join([headline] + lines + notes).replace("<", "&lt;").replace(">", "&gt;")
    return {"type": "message",
            "text": fallback,
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                             "contentUrl": None, "content": card}]}


def render_alerts_email(v):
    """(subject, plain-text body, html body). Both bodies, not just HTML: some corporate mail gateways
    strip the HTML part, so plain text is the real fallback, not decoration. Every rule name and
    message is `_esc()`-ed in the HTML body -- they can carry customer environment data (connector
    names), and this file has already shipped the unescaped-customer-data bug once (`_simple_table`
    handed dicts instead of escaped cells).
    """
    head, span = _alert_head_span(v)
    headline = "%s%s" % (" · ".join(head), span)
    stack = v.get("stack") or ""
    subject = "Meridian alerts%s: %s" % (" (%s)" % stack if stack else "", headline)
    text_lines = [headline, ""]
    html_rows = []
    for kind, dot, rule, text in _alert_rows(v):
        text_lines.append("%s %s — %s" % (dot, rule, text))
        html_rows.append("<tr><td>%s</td><td><b>%s</b></td><td>%s</td></tr>"
                         % (dot, _esc(rule), _esc(text)))
    if v.get("coverageChanged"):
        text_lines += ["", "Connector health changed across this window; judge relevance before "
                           "reading any movement as real."]
    if v.get("note"):
        text_lines += ["", v["note"]]
    html = ("<html><body><p><b>%s</b></p><table border=\"1\" cellpadding=\"4\">%s</table>%s</body></html>"
           % (_esc(headline), "".join(html_rows),
              ("<p>%s</p>" % _esc(v["note"])) if v.get("note") else ""))
    return subject, "\n".join(text_lines), html


def _notify_target_config(target):
    """One target's delivery config from env vars only, or None if not configured.

    Env-only by design (not `~/.meridian/config.json`): no webhook URL or SMTP credential is ever
    written to disk, unlike the Meridian token itself. The tradeoff is that a target can silently stop
    being configured if a shell profile changes -- `deliver_alerts` reports `configured` on every call
    so that is visible in the output, not silent.
    """
    if target == "slack":
        url = os.environ.get("MERIDIAN_ALERT_SLACK_WEBHOOK")
        return {"webhook": url} if url else None
    if target == "teams":
        url = os.environ.get("MERIDIAN_ALERT_TEAMS_WEBHOOK")
        return {"webhook": url} if url else None
    if target == "email":
        host = os.environ.get("MERIDIAN_ALERT_EMAIL_SMTP_HOST")
        to = os.environ.get("MERIDIAN_ALERT_EMAIL_TO")
        if not host or not to:
            return None
        return {
            "host": host,
            "port": int(os.environ.get("MERIDIAN_ALERT_EMAIL_SMTP_PORT", "587")),
            "from": os.environ.get("MERIDIAN_ALERT_EMAIL_FROM") or to.split(",")[0].strip(),
            "to": [a.strip() for a in to.split(",") if a.strip()],
            "user": os.environ.get("MERIDIAN_ALERT_EMAIL_SMTP_USER"),
            "password": os.environ.get("MERIDIAN_ALERT_EMAIL_SMTP_PASS"),
            "starttls": os.environ.get("MERIDIAN_ALERT_EMAIL_STARTTLS", "1") != "0",
        }
    return None


def notify_configs(targets=NOTIFY_TARGETS):
    return {t: _notify_target_config(t) for t in targets}


def _post_webhook(url, payload, timeout=10):
    """POST JSON to an arbitrary URL and return (status, body).

    Deliberately NOT `call()`: no Authorization header, no pooled connection keyed to the Meridian
    fqdn, no same-host-only redirect handling -- none of that machinery belongs anywhere near a
    third-party webhook, and reusing it would risk the Meridian bearer token reaching whoever controls
    the URL. TLS uses the platform default verifying context; there is no insecure-mode escape hatch
    the way there is for the Meridian connection, because there is no reason a chat webhook would need
    one. `urllib.request` is imported here, not at module level -- the top of this file deliberately
    avoids it (see the import-time comment there) so every other verb keeps paying zero cost for it.
    """
    import urllib.request
    # https only: the payload names the customer's failing connectors and alert state, and a plain
    # http URL (a typo, or a copied test endpoint) would send it over the network in clear. Raised,
    # not returned, so deliver_alerts reports it as that target's error like any other failure.
    if urllib.parse.urlsplit(url).scheme.lower() != "https":
        raise ValueError("webhook URL must be https://; refusing to send alert data unencrypted")
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


_SMTP_CLIENT = None  # set lazily in _send_email; swappable in tests, same pattern as `_round_trip`


def _send_email(cfg, subject, text_body, html_body, timeout=15):
    """Send one message over SMTP. Independent of `call()` for the same reason `_post_webhook` is --
    a different host, different credentials, no Meridian token anywhere near it. `smtplib`/
    `email.message` are imported lazily for the same startup-cost reason as `urllib.request` above.
    """
    import smtplib, email.message
    global _SMTP_CLIENT
    client = _SMTP_CLIENT or smtplib.SMTP
    msg = email.message.EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from"]
    msg["To"] = ", ".join(cfg["to"])
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    # STARTTLS=0 is for an unauthenticated internal relay. Logging in without it would send the SMTP
    # password in clear, so that combination is refused before connecting, not after.
    if cfg.get("user") and not cfg.get("starttls", True):
        raise ValueError("refusing SMTP login without TLS: MERIDIAN_ALERT_EMAIL_STARTTLS=0 is only "
                         "for an unauthenticated relay; unset _SMTP_USER/_SMTP_PASS or enable STARTTLS")
    with client(cfg["host"], cfg["port"], timeout=timeout) as s:
        if cfg.get("starttls", True):
            s.starttls(context=ssl.create_default_context())
        if cfg.get("user"):
            s.login(cfg["user"], cfg.get("password") or "")
        s.send_message(msg)


def update_alertstate(prev, v, stack_date):
    """(new state, changes) -- pure. A rule's verdict differing from the last SAVED state is the only
    thing that counts as a change; a rule absent from `prev` (first run, or just added) always counts
    too, so the first notify after `alerts add` still delivers. This is what makes on-change delivery
    possible without suppressing anything from the rendered message -- that always lists every current
    rule regardless of this function's answer, which only decides whether sending it is warranted.
    """
    prev_rules = (prev or {}).get("rules") or {}
    stack_date = stack_date or "unknown"
    current = {}
    for kind in ("firing", "unevaluable", "clear"):
        for r in v[kind]:
            current[r["rule"]] = kind
    new_rules, changes = {}, []
    for name, verdict in current.items():
        old = prev_rules.get(name)
        if old is None or old.get("lastVerdict") != verdict:
            changes.append({"rule": name, "from": (old or {}).get("lastVerdict"), "to": verdict})
            first_seen = stack_date
        else:
            first_seen = old.get("firstSeen") or stack_date
        new_rules[name] = {"firstSeen": first_seen, "lastVerdict": verdict, "lastSeen": stack_date}
    for name, old in prev_rules.items():
        if name not in current:
            changes.append({"rule": name, "from": old.get("lastVerdict"), "to": None})
    return {"schema": ALERTS_SCHEMA, "fqdn": load_config()[0], "rules": new_rules}, changes


def load_alertstate():
    """Prior per-rule verdict state, or None if never written. A malformed file raises rather than
    reporting "no prior state" -- silently resetting change detection would notify on every run
    forever with no explanation, the same absent-vs-unreadable distinction `load_alerts` makes for the
    rules file.
    """
    path = _alertstate_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8-sig") as f:
            doc = json.load(f)
    except Exception as e:
        raise RuntimeError("the alert state file at %s could not be read (%s). Delete it to restart "
                           "change detection -- that is safe, it only means the next notify treats "
                           "every current verdict as new." % (path, e))
    if not isinstance(doc, dict) or doc.get("schema") != ALERTS_SCHEMA:
        raise RuntimeError("the alert state file at %s is not a schema-%d state document."
                           % (path, ALERTS_SCHEMA))
    return doc


def save_alertstate(state):
    _private_write(_alertstate_path(), state)


def deliver_alerts(v, targets=NOTIFY_TARGETS, force=False, stack_date=None):
    """Update change-detection state, then send to whichever configured targets warrant it.

    Never lets one target's failure hide delivery to -- or the verdict of -- the others: `sent[target]`
    carries an error string rather than raising, because a broken Slack webhook must not also cancel
    the Teams/email delivery, or make this look like nothing fired at all.

    **The new state is saved only once at least one target accepted the message.** It used to be
    saved first, and delivery is on-change only, so a change that reached nobody -- every webhook
    down, SMTP unreachable, nothing configured yet -- was recorded as told and never sent again:
    "tell me when a connector breaks" went quiet exactly when it mattered. Unsent, the change stays
    pending and the next notify sends it. A target that failed while another succeeded is not
    retried (state is per rule, not per target); its error is in `sent`.
    """
    prev = load_alertstate()
    new_state, changes = update_alertstate(prev, v, stack_date)
    configs = notify_configs(targets)
    report = {"changed": bool(changes), "changes": changes,
             "configured": {t: bool(configs.get(t)) for t in targets},
             "sent": {}, "skipped": {}}
    if not (force or changes):
        save_alertstate(new_state)      # nothing to deliver; only lastSeen moves
        report["stateSaved"] = True
        report["note"] = ("no rule's verdict changed since the last notify attempt; nothing sent "
                          "(use --force to resend regardless)")
        return report
    for target in targets:
        cfg = configs.get(target)
        if not cfg:
            report["skipped"][target] = "not configured (set %s)" % NOTIFY_ENV_HINTS[target]
            continue
        try:
            if target == "slack":
                status, _body = _post_webhook(cfg["webhook"], render_alerts_slack(v))
                report["sent"][target] = {"status": status}
            elif target == "teams":
                status, _body = _post_webhook(cfg["webhook"], render_alerts_teams(v))
                report["sent"][target] = {"status": status}
            elif target == "email":
                subject, text_body, html_body = render_alerts_email(v)
                _send_email(cfg, subject, text_body, html_body)
                report["sent"][target] = {"status": "sent", "to": cfg["to"]}
        except Exception as e:
            report["sent"][target] = {"error": str(e)}
    delivered = any("error" not in r for r in report["sent"].values())
    report["stateSaved"] = delivered
    if delivered:
        save_alertstate(new_state)
    elif changes:
        report["note"] = ("nothing was delivered, so the change stays pending: the next notify sends "
                          "it again")
    return report


def _current_alert_verdict(window="daily", since=None):
    """The setup `alerts eval` and `alerts notify` both need: load rules, load history, pick the
    window, evaluate. Factored out so notify can't drift from what eval reports as the current state.
    """
    rules = load_alerts()
    recs, skipped = load_snapshots()
    by_date, _ = _usable_snapshots(recs)
    dates = sorted(by_date)
    latest = by_date[dates[-1]] if dates else None
    since_ = alert_window_since(dates, window, since)
    trend = compute_trend(recs, skipped, since=since_)
    v = evaluate_alerts(trend, latest, rules)
    v["stack"] = load_config()[0]
    v["stackDate"] = (latest or {}).get("stackDate")
    # Every verdict is computed from local snapshots, the "latest" included -- it is the newest
    # snapshot, not the stack now. Labelled so a clear verdict is never read as a live all-clear.
    v["dataCurrency"] = dict(trend.get("dataCurrency") or {"class": "historical", "source": "local snapshots"})
    if not recs:
        v["note"] = ("No snapshots exist for this stack, so no rule could be evaluated. Take one with "
                    "`digest --snapshot`; there is no way to backfill.")
    return v


def cmd_alerts(a):
    if a.alerts_cmd == "list":
        rules = load_alerts()
        rows = []
        for r in rules:
            row = dict(r)
            problem = alert_rule_problem(r)
            row["valid"] = problem is None
            if problem:
                row["problem"] = problem
            rows.append(row)
        out = {"configured": len(rules), "cap": MAX_ALERTS, "path": _alerts_path(), "rules": rows}
        broken = [r["name"] for r in rows if not r["valid"]]
        if broken:
            out["invalid"] = broken
            out["note"] = ("%d rule(s) cannot be evaluated as written. They will report as unevaluable, "
                           "never as clear." % len(broken))
        jout(out); return
    if a.alerts_cmd == "rm":
        rules = load_alerts()
        if not any(r.get("name") == a.name for r in rules):
            die("No alert rule named %r. Configured: %s."
                % (a.name, ", ".join(sorted(r["name"] for r in rules)) or "none"))
        save_alerts([r for r in rules if r.get("name") != a.name])
        jout({"removed": a.name, "configured": len(rules) - 1}); return
    if a.alerts_cmd == "add":
        rec, problem = add_alert(a.name, a.condition, metric=a.metric, value=a.value,
                                 include_degraded=a.include_degraded)
        if problem:
            die(problem)
        jout({"added": rec, "configured": len(load_alerts()), "cap": MAX_ALERTS})
        return
    if a.alerts_cmd == "notify":
        if a.to:
            targets = [t.strip() for t in a.to.split(",") if t.strip()]
            bad = [t for t in targets if t not in NOTIFY_TARGETS]
            if bad:
                die("Unknown notify target(s): %s. Expected one of: %s."
                    % (", ".join(bad), ", ".join(NOTIFY_TARGETS)))
        else:
            targets = list(NOTIFY_TARGETS)
        v = _current_alert_verdict(getattr(a, "window", "daily"), a.since)
        report = deliver_alerts(v, targets=targets, force=a.force, stack_date=v.get("stackDate"))
        report["exitCode"] = v["exitCode"]
        jout(report)
        # Same bit field as `eval` -- a scheduler chaining the two must see the same "is anything
        # wrong" signal regardless of whether a delivery actually went out.
        sys.exit(v["exitCode"])

    # eval
    v = _current_alert_verdict(getattr(a, "window", "daily"), a.since)
    if a.format == "json":
        jout(v)
    else:
        print(render_alerts(v, a.format))
    # Exit code last, and always -- a scheduler reads this, not the payload.
    sys.exit(v["exitCode"])


def resolve(name, table, key, search_fields):
    """Find one entity from either its exact key or a human/partial name.

    The exact-key lookup is tried first, but when the caller passes something that plainly isn't a
    key - `Owner_Name`/`Asset_Name` values have no spaces, while people type "Firstname L" - the
    fuzzy lookup is issued at the same time instead of after it. That turns two serial round trips
    into one, and the exact hit still wins if the stack does happen to key on a spaced name.
    """
    exact_body = {"table": table, "query": [[{"searchFieldName": key, "operator": "==", "type": "String", "value": name}]], "paging": {"page": 0, "recordsPerPage": 25}}
    org = [{"searchFieldName": f, "operator": "match", "type": "String", "value": name} for f in search_fields]
    fuzzy_body = {"table": table, "query": [org], "paging": {"page": 0, "recordsPerPage": 50}}
    if re.search(r"\s", name or ""):
        r, fz = parallel([lambda: call("POST", "/CMDB/v2/data/cmdb", exact_body),
                          lambda: call("POST", "/CMDB/v2/data/cmdb", fuzzy_body)])
        if isinstance(r, Exception):
            if isinstance(fz, Exception):
                raise r
            return fz
        if r["totalRecords"] >= 1:
            return r
        return r if isinstance(fz, Exception) else fz
    r = call("POST", "/CMDB/v2/data/cmdb", exact_body)
    if r["totalRecords"] < 1:
        r = call("POST", "/CMDB/v2/data/cmdb", {"table": table, "query": [org], "paging": {"page": 0, "recordsPerPage": 50}})
    return r


def _ascii_blast_radius(data):
    """Render a profile as a terminal 'blast radius' view — emoji severity dots + block-bar risk
    sizing — for CLI use where the SVG graph / PDF can't display. The bar length encodes risk score
    (i.e. the SVG's node size) so the same visual story survives into plain text. Printable string."""
    RULE = "=" * 62

    def edot(level):
        l = str(level or "").lower()
        if "3" in l or "high" in l or "crit" in l:
            return "🔴"
        if "2" in l or "med" in l:
            return "🟡"
        if "1" in l or "low" in l:
            return "🟢"
        return "⚪"

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _num(v):
        f = _f(v)
        return ("%g" % f) if f is not None else str(v)

    ident = data.get("identity", {}) or {}
    risk = data.get("risk", {}) or {}
    assets = data.get("linkedAssets") or []

    scores = [s for s in ([_f(risk.get("score"))] + [_f(a.get("risk")) for a in assets]) if s is not None]
    mx = max(scores) if scores else 100.0
    mx = mx or 100.0

    def bar(v, width=10):
        f = _f(v)
        if f is None:
            return " " * width
        n = int(round(width * max(0.0, min(f / mx, 1.0))))
        return "█" * n + "░" * (width - n)

    L = [RULE]
    if data.get("type") == "asset":
        name = _shortid(", ".join(jlist(ident.get("hostName"))) or ident.get("assetName") or "asset")
        v = data.get("vulnerabilities", {}) or {}
        enc = "❌ unencrypted" if str(ident.get("encrypted")) in ("0", "0.0") else \
              ("🔒 encrypted" if ident.get("encrypted") not in (None, "", "null") else "—")
        au = (data.get("associatedUsers") or {}).get("highRiskUsers") or []
        L += ["  BLAST RADIUS · %s" % name, RULE, "",
              "  %s %s   risk %s · %s   %s" % (edot(risk.get("level")), name, _num(risk.get("score")),
                                                    risk.get("level") or "—", bar(risk.get("score"))),
              "     %s" % " · ".join(x for x in [ident.get("os"), ", ".join(jlist(ident.get("ip"))),
                                                      ", ".join(jlist(ident.get("cloud")))] if x),
              "     %s KEV · %s critical vulns · %s" % (v.get("kevCount"), v.get("critical"), enc),
              "", "  ASSOCIATED HIGH-RISK USERS — blast radius",
              "     %s" % (", ".join(au) if au else "none recorded (contained)"), RULE]
        return "\n".join(L)

    # --- user ---
    th = data.get("threats", {}) or {}
    posture = data.get("posture", {}) or {}
    srcs = [s for s in (data.get("sourceSystems") or []) if s]
    emails = ident.get("emails") or []
    name = ident.get("displayName") or ident.get("ownerName") or "User"

    L += ["  BLAST RADIUS · %s" % name, RULE, "",
          "  %s %s   risk %s · %s   %s" % (edot(risk.get("level")), name, _num(risk.get("score")),
                                                risk.get("level") or "—", bar(risk.get("score")))]
    sub = " · ".join(x for x in [ident.get("title"), ident.get("department"), ident.get("location")] if x)
    if sub:
        L.append("     %s" % sub)
    stab = data.get("stability") or {}
    if stab.get("oscillating"):
        L.append("     ⚠ identity unstable (dedup) — %d fields oscillating" % len(stab.get("fields") or []))

    L += ["", "  IDENTITIES (%d)" % len(srcs)]
    for i in range(0, len(srcs), 3):
        L.append("     ⚪ %s" % "   ".join(srcs[i:i + 3]))
    if emails:
        personal = any(d in str(e).lower() for e in emails
                       for d in ("gmail.", "yahoo.", "hotmail.", "outlook.com", "proton"))
        L.append("     ✉  %d emails%s" % (len(emails), " (incl. personal)" if personal else ""))

    leaked = _to_int(th.get("leakedCredentialCount"))
    dlp = th.get("dlpBehavioral") or []
    if leaked or dlp:
        L += ["", "  THREATS"]
        if leaked:
            L.append("     🔴 %d leaked credentials" % leaked)
        for d in dlp:
            m = re.match(r"^\s*\[([^\]]+)\]\s*(.*)$", str(d))
            sev = (m.group(1) if m else "").lower()
            txt = (m.group(2) if m else str(d))
            emo = "🔴" if sev.startswith("crit") else "🟡"
            tag = "crit" if sev.startswith("crit") else "mod" if sev.startswith("mod") else sev
            L.append("     %s %s%s" % (emo, txt[:56] + ("…" if len(txt) > 56 else ""),
                                       ("  [%s]" % tag) if tag else ""))

    mfa_off = [m.get("source") for m in (posture.get("mfa") or []) if str(m.get("configured")).lower() == "no"]
    nc = posture.get("nonCompliance") or []
    if mfa_off or nc:
        L += ["", "  POSTURE"]
        if mfa_off:
            L.append("     🟠 MFA disabled: %s" % ", ".join(mfa_off))
        if nc:
            L.append("     🟠 non-compliance ×%d  (e.g. %s)" % (len(nc), str(nc[0])[:40]))

    if assets:
        cap_suffix = (" -- showing %d of %d" % (data.get("linkedAssetsShown"), data.get("linkedAssetsTotal"))
                     if data.get("linkedAssetsTruncated") else "")
        L += ["", "  ASSETS — blast radius   (bar = risk score = node size)%s" % cap_suffix]
        for a in assets:
            os_ = a.get("os") or "asset"
            os_ = os_ if len(os_) <= 18 else os_[:17] + "…"
            kev = a.get("kev")
            kevn = "—" if kev in (None, "", "null") else _to_int(kev)
            enc = "❌ unenc" if str(a.get("encrypted")) in ("0", "0.0") else \
                  ("🔒 enc" if a.get("encrypted") not in (None, "", "null") else "—")
            eol = " · EOL" if _is_eol(a.get("os")) else ""
            L.append("     %s %-18s risk %3s  %s  %s KEV  %s%s"
                     % (edot(a.get("level")), os_, _num(a.get("risk")), bar(a.get("risk")), kevn, enc, eol))
        shared = sorted({u for a in assets for u in (a.get("otherHighRiskUsers") or [])})
        L.append("       ↳ %s" % ("shared with high-risk users: " + ", ".join(shared) if shared
                                       else "none shared with other high-risk users (contained)"))

    L.append(RULE)
    return "\n".join(L)


# `Vuln_List` is an Embed_List of per-CVE objects (CVE, Score, epss, epss_percentile, Is_Fixable,
# Is_KEV, Name, lucidum_vuln_risk) already present in the record `profile` has fetched, so reading it
# costs no extra call. It is also unbounded -- 140 entries on one measured host, each carrying a
# 300-character `Name` -- so the detail list is capped and the cap is disclosed. Counts are always
# derived from the FULL list, never from the truncated view, or a cap would quietly shrink a finding.
PROFILE_VULN_DETAIL_MAX = 25    # worst-first, so the cap keeps the entries worth reading
PROFILE_VULN_CVES_MAX = 12      # per named-CVE list (notFixable / highEpss); the count carries the rest
HIGH_EPSS_PERCENTILE = 0.9      # "top 10% of exploit probability" - EPSS percentile, not raw score

# Same shape as the vuln-detail cap above, for the same reason: a user profile's `linkedAssets` is
# unbounded (measured live: one identity with 30 linked assets was 5,988 of a 13,040-char profile,
# 46% of the payload) and, unlike the per-CVE array, it IS read by default -- `_derive_insights`,
# `_blast_radius_svg` and `_ascii_blast_radius` all render it unconditionally. So this cannot go to
# zero by default the way vuln detail does; it goes to the worst-first slice instead.
PROFILE_LINKED_ASSETS_SHOW = 10       # shown by default, worst-first by risk
PROFILE_LINKED_ASSETS_FETCH_CAP = 50  # the query's own page size -- named so the gap below is checkable


def _vuln_summary(x, with_detail=False):
    """The `vulnerabilities` block of an asset profile, including per-CVE severity/exploitability.

    Without the per-CVE detail the asset rules can only see KEV counts, so a host whose risk is
    entirely CVSS- and EPSS-driven renders as "no critical exposure signals" -- observed on a live
    host carrying a CVSS 9.0 alongside an unfixable 8.8 RCE, neither of them KEV-listed.

    `detail` is OFF by default and that is a context-cost decision, not a caution. Measured, the array
    was 8,563 of an asset profile's 12,569 characters -- 68% of the payload, ~2,100 tokens -- and
    nothing read it: not `_derive_insights`, not `_profile_html`, not `_ascii_blast_radius`, not
    `compare`. The rules consume the counts and the named CVE lists (~500 characters all in), which are
    always present. Roughly 5,000 of those characters were 25 copies of a 200-char CVE description.
    """
    vulns = [v for v in (x.get("Vuln_List") or []) if isinstance(v, dict)]
    scores = [s for s in (_to_float(v.get("Score")) for v in vulns) if s is not None]
    nofix = [v.get("CVE") for v in vulns if str(v.get("Is_Fixable")) in ("0", "0.0") and v.get("CVE")]
    epss = [v.get("CVE") for v in vulns
            if (_to_float(v.get("epss_percentile")) or 0) >= HIGH_EPSS_PERCENTILE and v.get("CVE")]
    # Rank by Meridian's own composite first, then exploit probability, then severity -- a missing
    # value sorts last rather than as a 0, so an unscored CVE never displaces a scored one.
    ranked = sorted(vulns, reverse=True,
                    key=lambda v: (_to_float(v.get("lucidum_vuln_risk")) or -1.0,
                                   _to_float(v.get("epss_percentile")) or -1.0,
                                   _to_float(v.get("Score")) or -1.0))
    out = {"kevCount": x.get("Count_KEV"), "kevs": jlist(x.get("KEV")),
           "critical": x.get("Count_Critical_Severity_Vuln"),
           "high": x.get("Count_High_Severity_Vuln"),
           # `scoredCount` distinguishes "no CVE carried a CVSS" from "the CVSS was 0" -- the
           # findings below decline to quote a max when nothing was scored.
           "maxCvss": (max(scores) if scores else None), "scoredCount": len(scores),
           "notFixableCount": len(nofix), "notFixable": nofix[:PROFILE_VULN_CVES_MAX],
           "highEpssCount": len(epss), "highEpss": epss[:PROFILE_VULN_CVES_MAX],
           # Kept either way: how many CVEs the record carries is a real fact about the host, and
           # omitting it would make "no detail shown" indistinguishable from "no CVEs".
           "detailTotal": len(vulns)}
    if not with_detail:
        if vulns:
            out["detailNote"] = ("Per-CVE detail omitted to keep the answer small; pass --vuln-detail "
                                 "for the worst %d. The counts and named CVEs above are complete."
                                 % min(len(vulns), PROFILE_VULN_DETAIL_MAX))
        return out
    out["detailTruncated"] = len(vulns) > PROFILE_VULN_DETAIL_MAX
    out["detail"] = [{"cve": v.get("CVE"), "cvss": v.get("Score"),
                      "epssPercentile": v.get("epss_percentile"), "fixable": v.get("Is_Fixable"),
                      "isKev": v.get("Is_KEV"), "vulnRisk": v.get("lucidum_vuln_risk"),
                      "name": _short(v.get("Name"), 200)}
                     for v in ranked[:PROFILE_VULN_DETAIL_MAX]]
    return out


def build_profile(name, type_, vuln_detail=False, linked_detail=False):
    """One entity's full profile, returned rather than printed so `compare` can reuse it in-process.

    Returns the same dict `profile` prints: {"error": ...}, {"ambiguous": ...}, or the profile.
    `compare` used to get this by shelling out to `meridian.py profile` once per name -- two
    interpreter startups, two fresh TLS handshakes (the keep-alive pool is per-process), and the two
    profiles' round-trip waves run back to back instead of overlapped. Same returning-function-plus-
    printing-wrapper split as top_n/cmd_top and summarize_by/cmd_summary, for the same reason.

    `linked_detail` caps a USER profile's `linkedAssets` to the worst `PROFILE_LINKED_ASSETS_SHOW` by
    risk, same idea as `vuln_detail` above it -- except `_derive_insights` runs on the FULL fetched
    list before the cap is applied, so "17 high-risk assets", "19 unencrypted" etc. stay correct
    regardless of how much of the list is actually serialized. `linkedAssetsTotal` is always present
    (even uncapped) so a renderer never has to infer completeness from array length alone.
    """
    if type_ == "user":
        r = resolve(name, "user", "Owner_Name", ["Owner_Name", "displayName"])
        if r["totalRecords"] < 1:
            return {"error": "No user matched '%s'." % name}
        if r["totalRecords"] > 1:
            cands = sorted([{"ownerName": u.get("Owner_Name"), "displayName": u.get("displayName"),
                             "department": u.get("Owner_Department"), "risk": u.get("Risk_Score"), "level": u.get("Risk_Level")}
                            for u in r["data"]], key=lambda c: float(c["risk"] or 0), reverse=True)
            trunc = r["totalRecords"] > len(cands)
            note = "Several users matched '%s'." % name
            if trunc:
                note += " Showing %d of %d - list capped, ranking not authoritative; narrow the name." % (len(cands), r["totalRecords"])
            return {"ambiguous": True, "matchCount": r["totalRecords"], "shown": len(cands),
                    "truncated": trunc, "candidates": cands, "note": note}
        u = r["data"][0]
        assets = jlist(u.get("Asset_Name"))
        # The linked-asset lookup and the change log don't depend on each other, so they go together.
        jobs = []
        if assets:
            org = [{"searchFieldName": "Asset_Name", "operator": "==", "type": "String", "value": n} for n in assets]
            jobs.append(lambda: call("POST", "/CMDB/v2/data/cmdb", {"table": "asset", "query": [org],
                        "paging": {"page": 0, "recordsPerPage": PROFILE_LINKED_ASSETS_FETCH_CAP}}))
        else:
            jobs.append(lambda: {"data": []})
        # The name comes back from the API, not the caller: unencoded, an `&` truncated the id (the
        # server saw someone ELSE's change log), and a non-ASCII name raised inside http.client --
        # silently costing the stability signal for exactly the names most likely to be unusual.
        jobs.append(lambda: call("GET", "/CMDB/v2/data/cmdb/user/change?id=%s"
                                 % urllib.parse.quote(str(u.get("Owner_Name") or ""), safe="")))
        asset_resp, ch = parallel(jobs)

        linked, linked_true_total = [], 0
        if not isinstance(asset_resp, Exception):
            # totalRecords is the API's own match count for the `Asset_Name in [...]` query -- it can
            # exceed what actually came back once the linked-asset count passes the fetch cap above.
            linked_true_total = asset_resp.get("totalRecords")
            if linked_true_total is None:
                linked_true_total = len(asset_resp.get("data") or [])
            for ad in asset_resp.get("data", []):
                linked.append({"asset": ad.get("Asset_Name"), "risk": ad.get("Risk_Score"), "level": ad.get("Risk_Level"),
                               "os": ad.get("OS"), "ip": jlist(ad.get("IP_Address")), "kev": ad.get("Count_KEV"),
                               "encrypted": ad.get("Is_Encrypted"), "dataClass": ad.get("Data_Classification"),
                               "otherHighRiskUsers": [x for x in jlist(ad.get("High_Risk_User")) if x != u.get("Owner_Name")]})
            # Worst-first, same ranking rule as vuln detail's `ranked` -- a missing score sorts last
            # rather than as a 0, so an unscored asset never displaces a genuinely high-risk one.
            linked.sort(key=lambda a: _to_float(a.get("risk")) if _to_float(a.get("risk")) is not None else -1.0,
                       reverse=True)
        threats = jlist(u.get("Threat_List"))
        mfa = [{"source": m.get("Source"), "configured": m.get("Status")} for m in (u.get("Is_MFA_Configured") or []) if isinstance(m, dict)]
        stability = {"oscillating": False}
        if not isinstance(ch, Exception):   # the change log is optional - a 404 here isn't a failure
            by_field = {}
            for c in (ch if isinstance(ch, list) else []):
                by_field.setdefault(c.get("field"), 0)
                by_field[c.get("field")] += 1
            osc = [{"field": k, "changeCount": v} for k, v in by_field.items() if v >= 3]
            if osc:
                stability = {"oscillating": True, "fields": osc,
                             "note": "Fields change repeatedly - likely identity-dedup conflict; confirm one real person."}
        out = {"type": "user",
               "identity": {"ownerName": u.get("Owner_Name"), "displayName": u.get("displayName"),
                            "title": u.get("Owner_Job_Title"), "department": u.get("Owner_Department"),
                            "manager": u.get("Owner_Manager"), "location": u.get("Location_Country_Name"),
                            "emails": jlist(u.get("Owner_Email"))},
               "risk": {"score": u.get("Risk_Score"), "level": u.get("Risk_Level"), "ranking": u.get("Risk_STD"),
                        "factors": jlist(u.get("Risk_Reasons")), "dataClass": u.get("Data_Classification")},
               "posture": {"mfa": mfa, "countNoMfa": u.get("Count_No_MFA"),
                           "nonCompliance": jlist(u.get("Non_Compliance")), "countNonCompliance": u.get("Count_Non_Compliance")},
               "threats": {"dlpBehavioral": [t for t in threats if "Leaked Password" not in t],
                           "leakedCredentialCount": len([t for t in threats if "Leaked Password" in t])},
               "sourceSystems": jlist(u.get("sourcetype")), "linkedAssets": linked, "stability": stability}
        # Insights first, over every fetched asset -- _derive_insights' counts (high-risk, unencrypted,
        # EOL) must never be computed from an already-truncated list, or a cap silently shrinks a
        # finding the same way an un-gated `--vuln-detail` payload used to.
        out["findings"], out["recommendations"] = _derive_insights(out)
        fetched = len(linked)
        out["linkedAssetsTotal"] = linked_true_total
        if linked_detail:
            out["linkedAssetsShown"] = fetched
        else:
            out["linkedAssets"] = linked[:PROFILE_LINKED_ASSETS_SHOW]
            out["linkedAssetsShown"] = len(out["linkedAssets"])
        out["linkedAssetsTruncated"] = out["linkedAssetsShown"] < linked_true_total
        if out["linkedAssetsTruncated"]:
            if linked_detail:
                # Every fetched asset is shown, but the fetch itself hit its page-size ceiling --
                # disclosed rather than silent, same rule as `list`'s `truncated`/`note`.
                out["note"] = ("%d assets are linked to this identity; only the first %d could be "
                               "fetched (page-size ceiling), and findings above reflect only those %d."
                               % (linked_true_total, fetched, fetched))
            else:
                out["note"] = ("Showing the %d highest-risk of %d fetched linked assets (sorted by "
                               "risk); findings above were computed over all %d. Pass --linked-detail "
                               "for the full fetched list.%s"
                               % (out["linkedAssetsShown"], fetched, fetched,
                                  (" (%d assets are linked in total; only %d could be fetched.)"
                                   % (linked_true_total, fetched)) if linked_true_total > fetched else ""))
        return out
    else:
        r = resolve(name, "asset", "Asset_Name", ["Asset_Name", "Host_Name", "FQDN"])
        if r["totalRecords"] < 1:
            return {"error": "No asset matched '%s'." % name}
        if r["totalRecords"] > 1:
            cands = sorted([{"assetName": x.get("Asset_Name"), "hostName": jlist(x.get("Host_Name")),
                             "ip": jlist(x.get("IP_Address")), "risk": x.get("Risk_Score"), "level": x.get("Risk_Level")}
                            for x in r["data"]], key=lambda c: float(c["risk"] or 0), reverse=True)
            trunc = r["totalRecords"] > len(cands)
            return {"ambiguous": True, "matchCount": r["totalRecords"], "shown": len(cands), "truncated": trunc,
                    "candidates": cands, "note": "Several assets matched '%s'." % name}
        x = r["data"][0]
        ips = jlist(x.get("IP_Address"))
        out = {"type": "asset",
               "identity": {"assetName": x.get("Asset_Name"), "hostName": jlist(x.get("Host_Name")),
                            "fqdn": jlist(x.get("FQDN")), "ip": ips, "os": x.get("OS"),
                            "cloud": jlist(x.get("sourcetype")), "encrypted": x.get("Is_Encrypted"),
                            "owner": x.get("Owner_Name"), "ownerDepartment": x.get("Owner_Department"),
                            "publicIps": [i for i in ips if not _is_private_ip(i)]},
               "risk": {"score": x.get("Risk_Score"), "level": x.get("Risk_Level"), "ranking": x.get("Risk_STD"),
                        "factors": jlist(x.get("Risk_Reasons")), "dataClass": x.get("Data_Classification")},
               "vulnerabilities": _vuln_summary(x, with_detail=vuln_detail),
               "associatedUsers": {"highRiskUsers": jlist(x.get("High_Risk_User"))}}
        out["findings"], out["recommendations"] = _derive_insights(out)
        return out


# A user profile's `stability` verdict is derived from the user's change log: past field changes,
# not current state. Labelled, so "this identity oscillates" is never read as a live reading.
PROFILE_HISTORICAL_SECTIONS = {"stability": {"class": "historical",
                                             "source": "the user's change log (past field changes)"}}


def _profile_tables(type_):
    # A user profile also reads the assets linked to it; an asset profile reads only the asset table.
    return ["user", "asset"] if type_ == "user" else ["asset"]


def cmd_profile(a):
    out = with_currency(_profile_tables(a.type),
                        lambda: build_profile(a.name, a.type, vuln_detail=getattr(a, "vuln_detail", False),
                                              linked_detail=getattr(a, "linked_detail", False)),
                        sections=PROFILE_HISTORICAL_SECTIONS if a.type == "user" else None)
    if out.get("error"):
        jout(out); return
    if out.get("ambiguous"):
        jout(out); return
    if getattr(a, "ascii", False):
        print(_ascii_blast_radius(out))
    else:
        jout(out)


def cmd_compare(a):
    # Both profiles in one process, overlapped. Shelling out to `meridian.py profile` per name paid
    # two interpreter startups, two fresh TLS handshakes (the keep-alive pool is per-process) and
    # ran the profiles' round-trip waves serially -- ~2-3s of pure wait per compare. Two *threads*
    # here share the pool and the pacer; two concurrent *processes* measured slower, not faster.
    p1, p2, stamp = parallel([lambda: build_profile(a.name1, a.type),
                              lambda: build_profile(a.name2, a.type), ldg_rebuild])
    for p in (p1, p2):
        if isinstance(p, Exception):
            raise p
    for nm, p in ((a.name1, p1), (a.name2, p2)):
        if p.get("ambiguous") or p.get("error"):
            jout({"error": "Could not uniquely resolve '%s'." % nm, "detail": p}); return
    sections = None
    if a.type == "user":
        sections = {"%s.%s" % (side, k): v for side in ("profileA", "profileB")
                    for k, v in PROFILE_HISTORICAL_SECTIONS.items()}
    jout(attach_currency({"type": a.type, "a": a.name1, "b": a.name2, "profileA": p1, "profileB": p2},
                         data_currency(stamp if isinstance(stamp, dict) else {}, _profile_tables(a.type)),
                         sections))


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, list):
        return _esc(", ".join(map(str, v)))
    return _esc(v)


def _encrypted_cell(v):
    """Render an at-rest encryption flag for an HTML table: No / Yes / unknown.

    `Is_Encrypted` arrives as a float (`0.0` / `1.0`) or is absent. Both HTML branches used to
    hand it to `_esc` directly, so a report showed `0.0`, `1.0`, or -- worst -- the literal
    string `None` for an asset no source reported on. An absent value is **unknown**, never "No"
    and never encrypted, so it renders as an em dash: the same rule the ASCII view and the
    coverage metrics already follow, and the reason this returns a dash rather than a verdict.

    Display only. Callers deciding *findings* must keep testing the raw value -- `_derive_insights`
    keys "Asset is not encrypted at rest" off `str(v) in ("0", "0.0")`, so normalising the payload
    instead of the cell would silently drop that finding.
    """
    if str(v) in ("0", "0.0"):
        return "<b style='color:#C0392B'>No</b>"
    if v in (None, "", "null"):
        return "&mdash;"
    if str(v) in ("1", "1.0"):
        return "Yes"
    return _esc(v)     # some sources report "True"/"Yes" -- show what they said, don't guess


def _shortid(v):
    """Shorten a long opaque ID (OCI/EC2 hash) to a readable tail so it doesn't dominate table
    width; leave human-readable names and short IDs intact."""
    s = str(v)
    if len(s) > 24 and " " not in s:
        return "…" + s[-16:]
    return s


def _sev_class(level):
    l = str(level or "").lower()
    if "3" in l or "high" in l or "crit" in l:
        return "s-crit"
    if "2" in l or "med" in l:
        return "s-med"
    return "s-low"


def _which_trusted(name):
    """`shutil.which(name)` without the two ways it runs a file someone left in a folder.

    On Windows `shutil.which` searches the CURRENT directory before PATH, and tries every PATHEXT
    extension -- so a `chrome.bat` in whatever folder the assistant's shell is in was found as the
    browser, and `report` executed it with a PDF path as its argument. Verified with Python 3.14:
    `_find_browser()` returned `.\\chrome.BAT`. (It hid on the maintainer's machine because the
    assistant's shell sets NoDefaultCurrentDirectoryInExePath=1, which suppresses the cwd search; an
    ordinary shell does not.) A `.bat`/`.cmd` also runs through cmd.exe, whose argument parsing is
    its own injection surface.

    So: only absolute PATH entries (an empty or relative one means "the current directory" by
    another name), and on Windows only `.exe`. No browser ships as a batch file, so nothing real is
    lost.
    """
    exts = (".exe",) if os.name == "nt" else ("",)
    for d in os.get_exec_path():
        d = d.strip().strip('"')
        if not d or not os.path.isabs(d):
            continue
        for ext in exts:
            cand = os.path.join(d, name + ext)
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
    return None


def _find_browser():
    """Locate a Chromium binary for print-to-PDF.

    Don't add a path cache here. Measured: this search reads ~250ms in isolation, but ~205ms of that
    is `import shutil`, which the report path has already paid by the time it runs -- so caching the
    answer changed `report` wall time by nothing (3000ms vs 3005ms across a cold/warm pair). The 4s
    report is ~2.5s of Chrome startup, which is not reducible from here either: on a trivial page,
    adding --no-first-run/--disable-extensions/--disable-sync and friends measured 2557ms against
    2551ms. Only a persistent browser process would move it, and that means a daemon.
    """
    found = None
    for c in ["chrome", "google-chrome", "chromium", "chromium-browser", "msedge", "microsoft-edge"]:
        found = _which_trusted(c)
        if found:
            break
    if not found:
        for p in [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                  r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                  r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                  r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  "/usr/bin/chromium", "/usr/bin/google-chrome"]:
            if os.path.exists(p):
                found = p
                break
    return found


def _assets_dir():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")


def _load_css():
    css_path = os.path.join(_assets_dir(), "cyderes-report.css")
    if os.path.exists(css_path):
        with open(css_path, encoding="utf-8-sig") as f:
            return f.read()
    return "body{font-family:Arial,sans-serif}"


def _branded():
    """True when this install carries the Cyderes brand assets.

    Public builds ship without them (see scripts/make-public.py). A build that cannot render the
    wordmark must not assert it in text either: "cyderes" spelled out in the masthead, or a footer
    naming Cyderes, is the same trademark use as the artwork with the artwork taken away. So this
    gates the words too, not only the SVG -- which is the whole reason it exists rather than each
    call site testing for its own file.
    """
    return os.path.exists(os.path.join(_assets_dir(), "cyderes-logo.svg"))


def _logo_svg():
    """Inline the official Cyderes wordmark SVG (Cybervolt fill, for the black masthead).

    Falls back to nothing at all, not to a typographic wordmark -- see _branded(). The masthead's
    product slot still names Meridian, which is what the report is about, and naming the system a
    tool queries is nominative use rather than branding.
    """
    p = os.path.join(_assets_dir(), "cyderes-logo.svg")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return f.read()
    return ""


def _currency_line(data):
    """The report's data-currency line, in SKILL.md's label forms (design/data-currency.md 5.1).

    A payload with no `dataCurrency` (written before v2.27.0, or `api` output) says so rather than
    printing nothing: a report with no currency line reads as current, which is the one thing an
    unknown must never say. No emoji: the word carries the meaning, and a PDF may lack the glyph."""
    cur = data.get("dataCurrency") if isinstance(data, dict) else None
    if not isinstance(cur, dict):
        return "Data currency not recorded in this input"
    if cur.get("class") == "current":
        at = cur.get("ldgRebuiltUtc") or {}
        if len(set(at.values())) == 1:
            line = "Data as of %s (latest Meridian rebuild)" % currency_label_utc(next(iter(at.values())))
        else:
            line = "; ".join("%s as of %s" % ("Assets" if t == "asset" else "Users", currency_label_utc(v))
                             for t, v in sorted(at.items()))
        if cur.get("lastRebuildFailed"):
            line += " (the newest rebuild failed; this is the one before it)"
        return line
    if cur.get("class") == "historical":
        return "Historical \u00b7 %s, %s to %s" % (cur.get("source") or "past data",
                                                   cur.get("from") or "?", cur.get("to") or "?")
    return "Data currency could not be confirmed: %s" % (cur.get("reason") or "no reason recorded")


def _producer_note():
    """The report footer's attribution line, neutral when the brand assets are absent."""
    who = "Cyderes Meridian" if _branded() else "meridiancs"
    return "Generated by the %s skill. Verify against the live stack before acting." % who


def _meridian_svg():
    """Inline the official Meridian wordmark (white, for the black masthead). Falls back to text."""
    p = os.path.join(_assets_dir(), "meridian-logo.svg")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return f.read()
    return "Meridian"


def _font_face_css():
    """Embed the bundled Cyderes brand-alternate fonts (Space Grotesk / Space Mono, both OFL) as
    data URIs so the PDF is on-brand on any machine, not just ones with the fonts installed."""
    import base64
    fdir = os.path.join(_assets_dir(), "fonts")
    faces = [
        ("Space Grotesk", "SpaceGrotesk-Regular.ttf", 400),
        ("Space Grotesk", "SpaceGrotesk-Medium.ttf", 500),
        ("Space Grotesk", "SpaceGrotesk-Bold.ttf", 700),
        ("Space Mono", "SpaceMono-Regular.ttf", 400),
    ]
    out = []
    for fam, fn, wt in faces:
        p = os.path.join(fdir, fn)
        if os.path.exists(p):
            with open(p, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            out.append("@font-face{font-family:'%s';font-weight:%d;font-style:normal;font-display:swap;"
                       "src:url(data:font/ttf;base64,%s) format('truetype');}" % (fam, wt, b64))
    return "\n".join(out)


def _rows_html(rows):
    """Render a Cyderes-branded table. Detects the risk fields for severity dots / score bars /
    KEV pills; renders everything else as plain cells."""
    if not rows:
        return "<p>No rows.</p>"
    cols = list(rows[0].keys())
    kevcol = next((c for c in cols if c.lower() in ("count_kev", "kev", "kevcount")), None)
    scorecol = next((c for c in cols if "risk_score" in c.lower() or c.lower() == "score"), None)
    levelcol = next((c for c in cols if "risk_level" in c.lower() or c.lower() == "level"), None)
    maxscore = 0.0
    if scorecol:
        for r in rows:
            try:
                maxscore = max(maxscore, float(r.get(scorecol) or 0))
            except (TypeError, ValueError):
                pass
    maxscore = maxscore or 1.0
    head = "<th class='rank'>#</th>"
    for c in cols:
        cls = " class='num'" if (c == kevcol or c == scorecol or "count" in c.lower()) else ""
        head += "<th%s>%s</th>" % (cls, _esc(c))
    body = []
    for i, r in enumerate(rows, 1):
        tds = ["<td class='rank'>%d</td>" % i]
        for c in cols:
            v = r.get(c)
            if c == levelcol:
                sc = _sev_class(v)
                tds.append("<td><span class='sev %s'><span class='dot'></span>%s</span></td>" % (sc, _cell(v)))
            elif c == scorecol:
                try:
                    fv = float(v or 0)
                except (TypeError, ValueError):
                    fv = 0.0
                w = max(3, round(100 * fv / maxscore))
                disp = ("%g" % fv)
                tds.append("<td><div class='score'><span class='v'>%s</span><span class='bar'><span style='width:%d%%'></span></span></div></td>" % (_esc(disp), w))
            elif c == kevcol:
                try:
                    kv = int(float(v)) if v not in (None, "", "null") else 0
                except (TypeError, ValueError):
                    kv = 0
                tds.append("<td class='num'>%s</td>" % ("<span class='kev hot'>%d</span>" % kv if kv > 0 else "<span class='kev none'>&mdash;</span>"))
            else:
                if "name" in c.lower() or "asset" in c.lower():
                    disp = ", ".join(_shortid(x) for x in v) if isinstance(v, list) else _shortid(v) if v is not None else ""
                    tds.append("<td class='name'>%s</td>" % _esc(disp))
                elif "ip" in c.lower():
                    tds.append("<td class='ip'>%s</td>" % _cell(v))
                elif "count" in c.lower():
                    tds.append("<td class='num'>%s</td>" % _cell(v))
                else:
                    tds.append("<td>%s</td>" % _cell(v))
        body.append("<tr>%s</tr>" % "".join(tds))
    # No overflow wrapper: in print that clips off-page. table-layout:fixed fits the page width.
    return "<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (head, "".join(body))


def _sev_span(level):
    return "<span class='sev %s'><span class='dot'></span>%s</span>" % (_sev_class(level), _esc(level or "—"))


def _simple_table(headers, rows):
    if not rows:
        return "<p style='color:#8a847f;font-size:11px'>None.</p>"
    th = "".join("<th>%s</th>" % _esc(h) for h in headers)
    body = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % c for c in r) for r in rows)
    return "<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (th, body)


def _to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _to_float(v):
    """Float or None — deliberately distinct from `_to_int`'s 0, so a missing CVSS or EPSS reads as
    unknown rather than harmless. A vulnerability whose score defaults to 0 sorts as the safest thing
    on the host, which is the same silent-wrongness trap as a null KEV count reading as clean."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_eol(os_str):
    s = str(os_str or "").lower()
    return any(k in s for k in ["windows xp", "server 2012", "server 2008", "windows 7",
                                "ubuntu 16.04", "ubuntu 14.", "ubuntu 12.", "centos 6", "mac os x 10.1"])


# Words that mean "top tier" in a Risk_Level label. The demo stack spells the tiers `1-low` /
# `2-medium` / `3-high`, but the *number* of tiers is not guaranteed, so the leading ordinal proves
# nothing on its own -- tier 3 of 3 is the top, tier 3 of 5 is the middle. Match the word instead.
_TOP_RISK_WORDS = ("critical", "high", "severe")


def _is_top_risk_tier(level):
    """True when a `Risk_Level` label names the top tier.

    Earlier revisions tested `"3" in level`, which fires on any label whose text merely contains a
    3 and says nothing about where that tier sits in the scale. The word is the reliable signal;
    callers that want a defensible ranking should quote `Risk_STD` (a 0-100 percentile) alongside it.
    """
    s = str(level or "").strip().lower()
    return any(w in s for w in _TOP_RISK_WORDS)


def _is_private_ip(ip):
    """True for any address that is not a routable public IPv4 literal.

    Used to spot internet-reachable hosts from `IP_Address` alone, because `Is_Public` is unpopulated
    on some stacks and a host with a routable address would otherwise read as un-exposed. Anything
    this can't positively identify as public -- IPv6, a hostname, a malformed value -- returns True,
    so the caller under-claims exposure rather than inventing it.
    """
    parts = str(ip or "").strip().split(".")
    if len(parts) != 4:
        return True                     # not an IPv4 literal - don't claim it's public
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        return True
    if not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return True
    return (a in (0, 10, 127) or a >= 224                    # unspecified, RFC1918, loopback, multicast/reserved
            or (a == 172 and 16 <= b <= 31)                  # RFC1918
            or (a == 192 and b == 168)                       # RFC1918
            or (a == 169 and b == 254)                       # link-local
            or (a == 100 and 64 <= b <= 127))                # RFC6598 carrier-grade NAT


# Leading words that mean the finding starts with prose, not a product name.
_GENERIC_LEAD = {"the", "a", "an", "no", "not", "this", "user", "users", "account", "accounts",
                 "access", "system", "group", "role", "missing", "invalid", "inactive", "expired",
                 "excessive", "stale", "orphan", "orphaned", "unable", "unused", "privileged"}


def _finding_source(items, fallback="your access-governance tool"):
    """Name the tool that raised a set of compliance findings, read from the findings themselves.

    `Non_Compliance` values normally lead with the product ("SailPoint user does not have an active
    lifecycle state..."), so the name is in the data - and on a stack where that field is fed by a
    different governance tool, hardcoding one vendor would send the analyst to the wrong console.
    Only trusted when every finding leads with the same non-generic capitalised word, so a finding
    that simply starts with a capital letter can't be mistaken for a product name.
    """
    leads = set()
    for s in items:
        m = re.match(r"\s*([A-Za-z][A-Za-z0-9.&-]*)", str(s or ""))
        if not m:
            return fallback
        w = m.group(1)
        if not w[:1].isupper() or w.lower() in _GENERIC_LEAD:
            return fallback
        leads.add(w)
    return leads.pop() if len(leads) == 1 else fallback


def _plur(n, one, many):
    """"1 vulnerability" / "4 vulnerabilities" — these findings are read by people, and SKILL.md §4
    holds the answer to ordinary prose standards. Avoids the "vulnerability(ies)" hedge."""
    return "%d %s" % (n, one if n == 1 else many)


def _named_cves(v, key):
    """(count, names) for a capped CVE list in a `vulnerabilities` block.

    Reads `<key>Count` when present so a finding states the TRUE total even though the list beside it
    is capped at PROFILE_VULN_CVES_MAX; falls back to the list length for a hand-built dict (the
    offline tests and `compare` both construct these directly).
    """
    names = [c for c in (v.get(key) or []) if c]
    n = v.get(key + "Count")
    return (_to_int(n) if n is not None else len(names)), names


def _cve_eg(names, n=4):
    """` (CVE-…, CVE-…)` for a finding, or "" when no names survived. Never renders an empty pair of
    parentheses, and never implies the list is exhaustive when it isn't."""
    if not names:
        return ""
    shown = ", ".join(str(c) for c in names[:n])
    return " (%s%s)" % (shown, ", …" if len(names) > n else "")


def _derive_insights(data):
    """Turn a profile into (findings, recommendations) — the analyst reasoning encoded as rules so
    every profile report ships with interpretation, not just data. Ordered most-severe first."""
    findings, recs = [], []
    risk = data.get("risk", {}) or {}
    lvl = str(risk.get("level") or "").lower()

    if data.get("type") == "asset":
        ident = data.get("identity", {}) or {}
        v = data.get("vulnerabilities", {}) or {}
        kev = _to_int(v.get("kevCount"))
        if kev > 0:
            findings.append("Carries %d known-exploited vulnerabilities (CISA KEV) — flaws confirmed exploited in the wild." % kev)
            recs.append("Patch the %d known-exploited vulnerabilities first, then re-scan to confirm closure." % kev)
        # Severity, fixability and exposure rules. KEV membership alone misses a whole class of
        # genuinely dangerous host: a CVSS 9.0 with no patch path is not "no action required" just
        # because CISA hasn't listed it, and that host previously rendered as entirely clean.
        crit, high = _to_int(v.get("critical")), _to_int(v.get("high"))
        maxcvss = _to_float(v.get("maxCvss"))
        if crit or high:
            bits = ", ".join(b for b in ["%d critical" % crit if crit else "",
                                         "%d high" % high if high else ""] if b)
            # No score parsed is *unknown*, not 0 -- say so rather than dropping the clause silently,
            # so "max CVSS" never reads as absent-because-low.
            qual = ((" (max CVSS %.1f)" % maxcvss) if maxcvss is not None
                    else " (no CVSS reported on this host's CVE detail)")
            noun = "vulnerability" if (crit + high) == 1 else "vulnerabilities"
            findings.append("Carries %s severity %s%s — a direct path to compromise while "
                            "unpatched." % (bits, noun, qual))
            recs.append("Remediate the %s severity %s; treat the highest CVSS first." % (bits, noun))
        nofix_n, nofix = _named_cves(v, "notFixable")
        if nofix_n:
            findings.append("%s %s no available fix%s — %s cannot be closed by patching, only by "
                            "upgrading or replacing the affected software."
                            % (_plur(nofix_n, "vulnerability", "vulnerabilities"),
                               "has" if nofix_n == 1 else "have", _cve_eg(nofix),
                               "it" if nofix_n == 1 else "these"))
            recs.append("Plan a version upgrade or replacement for the software carrying %s — no patch exists."
                        % ", ".join(str(c) for c in nofix[:4]))
        epss_n, epss = _named_cves(v, "highEpss")
        if epss_n:
            findings.append("%s %s in the top %d%% of exploit probability (EPSS)%s."
                            % (_plur(epss_n, "vulnerability", "vulnerabilities"),
                               "sits" if epss_n == 1 else "sit",
                               round((1 - HIGH_EPSS_PERCENTILE) * 100), _cve_eg(epss)))
        pub = [i for i in (ident.get("publicIps") or []) if i]
        if pub and (crit or high or kev):
            findings.append("Reachable on a routable public address (%s) while carrying unpatched "
                            "high/critical vulnerabilities — exposure and exploitability coincide."
                            % ", ".join(pub[:3]))
            recs.append("Confirm whether public exposure on %s is required; restrict or firewall it if not."
                        % ", ".join(pub[:3]))
        if str(ident.get("encrypted")) in ("0", "0.0"):
            findings.append("Asset is not encrypted at rest.")
            recs.append("Enable encryption at rest.")
        if _is_eol(ident.get("os")):
            findings.append("Runs an end-of-life operating system (%s) that can no longer be patched." % ident.get("os"))
            recs.append("Isolate, rebuild, or decommission this host — its OS (%s) is unsupported." % ident.get("os"))
        factors = [str(f) for f in (risk.get("factors") or [])]
        dc = str(risk.get("dataClass") or "").lower()
        if dc in ("private", "restricted", "confidential") or \
                any("private data" in f.lower() or "sensitive" in f.lower() for f in factors):
            findings.append("Holds %s-classified data, so any compromise of this host is a data-exposure "
                            "event." % (risk.get("dataClass") or "sensitive"))
        if any("threat" in f.lower() for f in factors):
            findings.append("'Threats Detected' is an active risk driver on this asset — the API does not "
                            "carry the detection detail, so confirm what fired in the endpoint console.")
            recs.append("Pull this host's detection history from the endpoint console to establish what fired.")
        au = (data.get("associatedUsers") or {}).get("highRiskUsers") or []
        if au:
            findings.append("Shared with %d other high-risk user(s), widening the blast radius." % len(au))
        # Orienting statement, so it leads. Triggered on the tier *word*, never a bare `"3" in level`
        # -- see _is_top_risk_tier -- and quotes Risk_STD only when the stack actually reports it.
        if _is_top_risk_tier(risk.get("level")):
            rank = _to_float(risk.get("ranking"))
            findings.insert(0, "In the top risk tier (%s%s)."
                            % (risk.get("level"), (", ranked %.1f of 100" % rank) if rank is not None else ""))
        if not findings:
            findings.append("No critical exposure signals detected in this snapshot.")
        if not recs:
            recs.append("No immediate action required; continue standard monitoring.")
        return findings, recs

    # --- user ---
    th = data.get("threats", {}) or {}
    leaked = _to_int(th.get("leakedCredentialCount"))
    dlp = th.get("dlpBehavioral") or []
    mfa_off = [m.get("source") for m in (data.get("posture", {}).get("mfa") or []) if str(m.get("configured")).lower() == "no"]
    nc = data.get("posture", {}).get("nonCompliance") or []
    assets = data.get("linkedAssets") or []
    emails = data.get("identity", {}).get("emails") or []
    stab = data.get("stability") or {}

    if stab.get("oscillating"):
        findings.append("Record is unstable — identity-defining fields flip repeatedly (a dedup conflict), so this "
                        "point-in-time snapshot may understate the true risk.")
        recs.append("Resolve the identity-resolution conflict first, in whichever IAM / identity-governance source "
                    "feeds these records: confirm whether this is one person or two merged records before acting "
                    "on point-in-time risk.")
    if "3" in lvl or "high" in lvl:
        findings.append("Currently in the top risk tier (%s, rank %s of 100)." % (risk.get("level"), risk.get("ranking")))
    if leaked > 0 and mfa_off:
        findings.append("%d leaked-credential exposures combined with MFA disabled on %s — a realistic account-takeover path." % (leaked, ", ".join(mfa_off)))
        recs.append("Force a password reset and enforce MFA on %s." % ", ".join(mfa_off))
    elif leaked > 0:
        findings.append("%d leaked-credential exposures found for this identity." % leaked)
        recs.append("Force a password reset and confirm MFA is enforced.")
    elif mfa_off:
        findings.append("MFA is disabled on %s." % ", ".join(mfa_off))
        recs.append("Enforce MFA on %s." % ", ".join(mfa_off))
    if dlp:
        findings.append("%d data-movement / behavioral alert(s) indicate possible exfiltration (e.g. %s)." % (len(dlp), dlp[0]))
        recs.append("Investigate the data-movement alerts through your DLP / insider-risk process before they escalate.")
    personal = [e for e in emails if any(d in str(e).lower() for d in ("gmail.", "yahoo.", "hotmail.", "outlook.com", "proton"))]
    if personal:
        findings.append("A personal email (%s) is linked to this corporate identity." % personal[0])
        recs.append("Verify the personal address (%s) genuinely belongs to this user and is not a bad identity merge." % personal[0])
    unenc = [x for x in assets if str(x.get("encrypted")) in ("0", "0.0")]
    eol = [x for x in assets if _is_eol(x.get("os"))]
    kev_total = sum(_to_int(x.get("kev")) for x in assets)
    high_assets = [x for x in assets if _is_top_risk_tier(x.get("level"))]
    if high_assets:
        findings.append("Linked to %d high-risk asset(s) — this asset blast radius persists regardless of which identity state is showing." % len(high_assets))
    if unenc:
        ips = "; ".join(", ".join(jlist(x.get("ip"))) for x in unenc)
        findings.append("%d linked asset(s) are unencrypted while holding sensitive data (%s) — a direct data-exposure path." % (len(unenc), ips))
        recs.append("Encrypt the unencrypted linked assets at rest (%s)." % ips)
    if kev_total > 0:
        recs.append("Patch the %d known-exploited vulnerabilities across the linked servers." % kev_total)
    if eol:
        oses = ", ".join(sorted({str(x.get("os")) for x in eol}))
        findings.append("Linked asset(s) run end-of-life operating systems (%s) that cannot be patched." % oses)
        recs.append("Replace or decommission the end-of-life linked hosts (%s)." % oses)
    if nc:
        findings.append("%d governance/compliance gaps — this account's access has effectively never been reviewed (e.g. %s)." % (len(nc), nc[0]))
        # Name the tool from the findings, not from whichever one this stack's demo data happened to
        # use, and let the finding above carry the specifics instead of a fixed category list.
        recs.append("Run access certification in %s and resolve the %d finding(s) it raised on this account."
                    % (_finding_source(nc), len(nc)))
    if not findings:
        findings.append("No critical exposure signals in this snapshot; maintain routine monitoring.")
    if not recs:
        recs.append("No immediate action required; continue standard monitoring.")
    return findings, recs


def _blast_radius_svg(data):
    """Build a self-contained hub-and-spoke 'blast radius' graph for a USER profile: the user at
    the center, linked identities (left), active threats (top), linked assets (right), and posture
    gaps (bottom), with edges to each. Colors follow the report's semantic severity palette; text
    inherits the report font. Returns an HTML section string, or '' for asset profiles / no data."""
    if data.get("type") == "asset":
        return ""
    ident = data.get("identity", {}) or {}
    risk = data.get("risk", {}) or {}
    th = data.get("threats", {}) or {}
    posture = data.get("posture", {}) or {}
    assets = data.get("linkedAssets") or []
    srcs = [s for s in (data.get("sourceSystems")
            or [i.get("source") for i in (data.get("identities") or []) if isinstance(i, dict)]) if s]

    import math
    CRIT, HIGH, LOW, GRAY, EDGE = "#C0392B", "#E07B39", "#2E8B57", "#8a847f", "#c9bfb2"
    TINT = {CRIT: "#fbeceb", HIGH: "#fcf1e8", LOW: "#e9f4ee", GRAY: "#f1eeeb"}

    def sev_color(level):
        l = str(level or "").lower()
        if "3" in l or "high" in l or "crit" in l:
            return CRIT
        if "2" in l or "med" in l:
            return HIGH
        return LOW

    def trunc(s, n):
        s = str(s)
        return s if len(s) <= n else s[:n - 1] + "…"

    BASE_R = 9.0  # radius for unscored nodes (identity / threat / posture)

    def risk_radius(score, default):
        """Map a 0-100 risk score to a node radius so size encodes magnitude. Nodes without a
        numeric score (identities, threats, posture) fall back to `default` (the baseline)."""
        try:
            v = max(0.0, min(float(score), 100.0))
        except (TypeError, ValueError):
            return default
        return 12.0 + (v / 100.0) * 22.0  # 12 (score 0) → 34 (score 100)

    name = ident.get("displayName") or ident.get("ownerName") or "User"
    leaked = _to_int(th.get("leakedCredentialCount"))
    dlp = th.get("dlpBehavioral") or []
    mfa_off = [m.get("source") for m in (posture.get("mfa") or []) if str(m.get("configured")).lower() == "no"]
    nc = posture.get("nonCompliance") or []

    # --- Build the node/edge graph: each identity, asset, threat and posture gap is its own node,
    #     all linked to the central user; shared assets add asset→other-user edges (true blast radius).
    nodes, edges = {}, []

    def add(nid, label, color, r, group):
        nodes[nid] = {"label": label, "color": color, "r": r, "group": group}

    # Node radius scales with risk score (user + assets carry one); unscored nodes use the baseline.
    add("u", trunc(name, 18), sev_color(risk.get("level")), risk_radius(risk.get("score"), 26.0), "user")
    for i, s in enumerate(srcs[:8]):
        add("s%d" % i, trunc(s, 16), GRAY, BASE_R, "identity"); edges.append(("u", "s%d" % i))
    for i, a in enumerate(assets[:6]):
        aid = "a%d" % i
        unenc = " · unenc" if str(a.get("encrypted")) in ("0", "0.0") else ""
        kev = a.get("kev")
        kevn = 0 if kev in (None, "", "null") else _to_int(kev)
        lab = "%s (r%s%s%s)" % (trunc(a.get("os") or "asset", 14), a.get("risk"),
                                " · %dKEV" % kevn if kevn else "", unenc)
        add(aid, trunc(lab, 26), sev_color(a.get("level")), risk_radius(a.get("risk"), 13.0), "asset")
        edges.append(("u", aid))
        for ou in (a.get("otherHighRiskUsers") or []):
            ouid = "ou_" + re.sub(r"\W", "", str(ou))[:14]
            if ouid not in nodes:
                add(ouid, trunc(ou, 14), CRIT, BASE_R + 1, "user")
            edges.append((aid, ouid))
    if leaked:
        add("leak", "%d leaked creds" % leaked, CRIT, BASE_R, "threat"); edges.append(("u", "leak"))
    for i, d in enumerate(dlp[:3]):
        add("dlp%d" % i, trunc(re.sub(r"^\[[^\]]*\]", "", str(d)), 20), CRIT, BASE_R, "threat")
        edges.append(("u", "dlp%d" % i))
    for i, s in enumerate(mfa_off[:3]):
        add("m%d" % i, "MFA off: %s" % trunc(s, 10), HIGH, BASE_R, "posture"); edges.append(("u", "m%d" % i))
    if nc:
        add("nc", "non-compliance (%d)" % len(nc), HIGH, BASE_R, "posture"); edges.append(("u", "nc"))

    ids = list(nodes.keys())
    n = len(ids)

    # Deterministic PRNG (LCG) seeded from the name so the same profile always lays out the same.
    seed = [(sum(ord(c) for c in name) * 2654435761 + 1) & 0x7fffffff]

    def rnd():
        seed[0] = (1103515245 * seed[0] + 12345) & 0x7fffffff
        return seed[0] / 0x7fffffff

    # Initial placement: user at origin, everyone else on a jittered ring.
    pos = {}
    for i, nid in enumerate(ids):
        if nid == "u":
            pos[nid] = [0.0, 0.0]
        else:
            ang = 2 * math.pi * i / max(n - 1, 1) + rnd() * 0.7
            rad = 115 + rnd() * 70
            pos[nid] = [rad * math.cos(ang), rad * math.sin(ang)]

    # Fruchterman-Reingold: repulsion between all nodes, attraction along edges, cooling schedule.
    k = math.sqrt((640.0 * 400.0) / max(n, 1)) * 0.9
    t = 95.0
    for _ in range(340):
        disp = {nid: [0.0, 0.0] for nid in ids}
        for i in range(n):
            for j in range(i + 1, n):
                a, b = ids[i], ids[j]
                dx = pos[a][0] - pos[b][0]; dy = pos[a][1] - pos[b][1]
                d2 = dx * dx + dy * dy
                if d2 < 0.01:
                    dx = rnd() - 0.5; dy = rnd() - 0.5; d2 = dx * dx + dy * dy + 0.01
                d = math.sqrt(d2); f = k * k / d
                ux, uy = dx / d, dy / d
                disp[a][0] += ux * f; disp[a][1] += uy * f
                disp[b][0] -= ux * f; disp[b][1] -= uy * f
        for a, b in edges:
            dx = pos[a][0] - pos[b][0]; dy = pos[a][1] - pos[b][1]
            d = math.sqrt(dx * dx + dy * dy) or 0.01
            f = d * d / k
            ux, uy = dx / d, dy / d
            disp[a][0] -= ux * f; disp[a][1] -= uy * f
            disp[b][0] += ux * f; disp[b][1] += uy * f
        for nid in ids:
            if nid == "u":
                pos[nid] = [0.0, 0.0]; continue  # pin the user central
            dx, dy = disp[nid]
            dl = math.sqrt(dx * dx + dy * dy) or 0.01
            step = min(dl, t)
            pos[nid][0] += dx / dl * step
            pos[nid][1] += dy / dl * step
        t = max(t * 0.94, 2.0)

    # Label side (away from center) + fit the whole drawing (nodes AND labels) into the target box.
    ux0 = pos["u"][0]
    xs, ys = [], []
    for nid in ids:
        x, y = pos[nid]; r = nodes[nid]["r"]; lw = len(nodes[nid]["label"]) * 6.2 + 10
        nodes[nid]["side"] = "r" if x >= ux0 else "l"
        if nid == "u":
            xs += [x - lw / 2, x + lw / 2]; ys += [y - r - 8, y + r + 32]
        elif nodes[nid]["side"] == "r":
            xs += [x - r, x + r + lw]; ys += [y - r - 8, y + r + 8]
        else:
            xs += [x - r - lw, x + r]; ys += [y - r - 8, y + r + 8]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    TX0, TX1, TY0, TY1 = 60, 720, 54, 470
    s = min((TX1 - TX0) / max(maxx - minx, 1), (TY1 - TY0) / max(maxy - miny, 1))
    offx = TX0 + ((TX1 - TX0) - (maxx - minx) * s) / 2 - minx * s
    offy = TY0 + ((TY1 - TY0) - (maxy - miny) * s) / 2 - miny * s

    def P(nid):
        return pos[nid][0] * s + offx, pos[nid][1] * s + offy

    svg_edges = []
    for a, b in edges:
        ax, ay = P(a); bx, by = P(b)
        svg_edges.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="1.2"/>'
                         % (ax, ay, bx, by, EDGE))

    svg_nodes = []
    for nid in ids:
        x, y = P(nid); nd = nodes[nid]; r = nd["r"]; col = nd["color"]
        svg_nodes.append('<circle cx="%.1f" cy="%.1f" r="%g" fill="%s" stroke="%s" stroke-width="%s"/>'
                         % (x, y, r, TINT.get(col, "#eee"), col, 2.2 if nid == "u" else 1.4))
        if nid == "u":
            svg_nodes.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="12.5" font-weight="700" fill="%s">%s</text>'
                             % (x, y + r + 15, col, _esc(nd["label"])))
            svg_nodes.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="10.5" fill="#111">%s · risk %s</text>'
                             % (x, y + r + 29, _esc(risk.get("level") or "—"), _esc(risk.get("score"))))
        elif nd["side"] == "r":
            svg_nodes.append('<text x="%.1f" y="%.1f" text-anchor="start" font-size="10.5" fill="#1a1a1a">%s</text>'
                             % (x + r + 5, y + 3.5, _esc(nd["label"])))
        else:
            svg_nodes.append('<text x="%.1f" y="%.1f" text-anchor="end" font-size="10.5" fill="#1a1a1a">%s</text>'
                             % (x - r - 5, y + 3.5, _esc(nd["label"])))

    # Legend.
    leg_items = [(CRIT, "high / critical / threat"), (HIGH, "needs attention"), (LOW, "low"), (GRAY, "identity")]
    lx, legend = 60, []
    for col, lab in leg_items:
        legend.append('<circle cx="%g" cy="530" r="6" fill="%s" stroke="%s" stroke-width="1.4"/>'
                      '<text x="%g" y="534" font-size="11" fill="#4c4a48">%s</text>'
                      % (lx, TINT.get(col, "#eee"), col, lx + 12, _esc(lab)))
        lx += 24 + len(lab) * 6.4 + 24
    legend.append('<text x="60" y="552" font-size="10" fill="#8a847f">Node size scales with risk '
                  'score (user &amp; assets); identity, threat and posture nodes are shown at baseline size.</text>')

    svg = ('<svg viewBox="0 0 780 566" role="img" style="width:100%%;max-width:770px;height:auto;display:block;margin:2px auto 0" '
           'xmlns="http://www.w3.org/2000/svg">'
           '<title>Blast-radius network graph for %s</title>'
           '<desc>Force-directed graph with the user at the center linked to identity, threat, asset '
           'and posture nodes; node size scales with risk score; asset-to-user edges show shared '
           'assets (blast radius).</desc>'
           '<g stroke-linecap="round">%s</g>%s%s</svg>'
           % (_esc(name), "".join(svg_edges), "".join(svg_nodes), "".join(legend)))
    return "<div style='break-inside:avoid;margin:4px 0 2px'>%s</div>" % svg


def _digest_html(data):
    """Render a `digest` as branded report sections. Returns (subtitle, statcards_html, body_html) --
    the same contract _profile_html uses, so cmd_report treats both the same way."""
    head = data.get("headline", {}) or {}
    conn = (data.get("connectors") or {}).get("summary", {}) or {}
    brk = data.get("breakdown") or {}
    sec = []

    # Escaped, because everything this formats lands in HTML that nothing else escapes: the stat
    # cards interpolate it raw and _simple_table does not escape cells. A number is safe; anything
    # else is whatever the API returned, and a string there was injected into the report verbatim.
    def n(v):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return "{:,}".format(v)
        return "—" if v is None else _esc(v)

    # A digest is read unattended, so the arrow matters: it is the only thing that says direction.
    def delta(now, avg):
        if not isinstance(now, (int, float)) or not isinstance(avg, (int, float)) or not avg:
            return "—"
        d = now - avg
        arrow = "▲" if d > 0 else ("▼" if d < 0 else "▬")
        return "%s %s (%+.1f%% vs 30-day avg)" % (arrow, n(now), 100.0 * d / avg)

    statcards = "".join("<div class='stat'><div class='n'>%s</div><div class='l'>%s</div></div>" % (a, b) for a, b in [
        (n(head.get("assets")), "assets"), (n(head.get("users")), "users"),
        (n(conn.get("connectorsEnabled")), "connectors enabled"),
        (n(conn.get("failing")), "connectors failing")])

    # _simple_table takes rows as positional cell lists (a dict would render its KEYS), and it does not
    # escape cells -- so anything originating in the customer's environment is escaped here.
    sec.append("<div class='section-label'>Inventory</div>" + _simple_table(
        ["Measure", "Now vs 30-day average (Historical)"],
        [["Assets", delta(head.get("assets"), head.get("assets30DayAvg"))],
         ["Users", delta(head.get("users"), head.get("users30DayAvg"))]]))

    if conn:
        sec.append("<div class='section-label'>Data coverage</div>" + _simple_table(
            ["Healthy", "Degraded", "Failing", "Idle", "Records last run"],
            [[n(conn.get("healthy")), n(conn.get("degraded")), n(conn.get("failing")),
              n(conn.get("idle")), n(conn.get("recordsLastRun"))]]))
        fails = (data.get("connectors") or {}).get("failures") or []
        if fails:
            sec.append(_simple_table(["Failing connector", "Services", "Reason"],
                [[_esc(f.get("service") or f.get("profile") or "—"), n(f.get("serviceCount")),
                  _esc(_short(f.get("message"), 90))] for f in fails[:6]]))

    if brk.get("groups"):
        # Escaped piecewise: n() already escapes, so escaping the joined label would do it twice.
        label = _esc("Breakdown by %s" % brk.get("by"))
        if brk.get("complete") is False:
            un = brk.get("unaccountedRecords") or brk.get("overcountedRecords")
            label += " — incomplete, %s records unaccounted" % n(un)
        if brk.get("recordsWithoutField"):
            label += " — %s records have no value and are not shown" % n(brk["recordsWithoutField"])
        elif brk.get("whereTotalUnavailable"):
            # Unattended: a count that failed must say so, or the breakdown reads as the whole population.
            label += " — records with no value could not be counted, so some may be missing"
        sec.append("<div class='section-label'>%s</div>" % label + _rows_html(brk["groups"]))

    for key, title in [("topUsers", "Riskiest users"), ("topAssets", "Riskiest assets")]:
        blk = data.get(key) or {}
        if blk.get("top"):
            t = title + (" — ranking may be partial" if blk.get("truncated") else "")
            sec.append("<div class='section-label'>%s</div>" % _esc(t) + _rows_html(blk["top"]))

    missing = data.get("sectionsUnavailable") or []
    if missing:
        sec.append("<div class='section-label'>Sections unavailable</div>" + _simple_table(
            ["Section", "Why"], [[_esc(m.get("section")), _esc(_short(m.get("error"), 100))] for m in missing]))

    return "Posture digest — %s" % (data.get("stack") or "Meridian"), statcards, "".join(sec)


def _profile_html(data):
    """Render an investigation profile (from `meridian.py profile`) as branded report sections.
    Returns (subtitle, statcards_html, body_html)."""
    ident = data.get("identity", {}) or {}
    risk = data.get("risk", {}) or {}
    lvl = str(risk.get("level") or "")
    is_high = _is_top_risk_tier(lvl)      # the tier word, not any label containing a 3
    sec = []

    if data.get("type") == "asset":
        name = ident.get("assetName") or "Asset"
        subtitle = "Asset risk profile — %s" % _shortid(name)
        vuln = data.get("vulnerabilities", {}) or {}
        cards = [("Risk Score", risk.get("score"), is_high), ("Risk Level", risk.get("level"), is_high),
                 ("KEVs", vuln.get("kevCount"), False), ("Critical vulns", vuln.get("critical"), False)]
        # Label and table are wrapped as one unit: in a multi-subject report this pair spans both
        # columns, and spanning a bare table strands its label at the top of a column.
        sec.append("<div class='assetbox'><div class='section-label'>Asset</div>" + _simple_table(
            ["Host", "IP", "OS", "Cloud", "Encrypted"],
            [[_esc(", ".join(jlist(ident.get("hostName"))) or _shortid(name)), _esc(", ".join(jlist(ident.get("ip")))),
              _esc(ident.get("os") or ""), _esc(", ".join(jlist(ident.get("cloud")))),
              _encrypted_cell(ident.get("encrypted"))]]) + "</div>")
        if risk.get("factors"):
            sec.append("<div class='section-label'>Risk factors</div><p>%s</p>" % _esc(" · ".join(risk["factors"])))
        au = (data.get("associatedUsers") or {}).get("highRiskUsers") or []
        sec.append("<div class='section-label'>Associated high-risk users (blast radius)</div><p>%s</p>" % (_esc(", ".join(au)) if au else "None recorded."))
    else:
        name = ident.get("displayName") or ident.get("ownerName") or "User"
        subtitle = "User risk profile — %s%s" % (name, (" · " + ident.get("department")) if ident.get("department") else "")
        th = data.get("threats", {}) or {}
        cards = [("Risk Score", risk.get("score"), is_high),
                 ("Risk Level", "%s · rank %s" % (risk.get("level"), risk.get("ranking")), is_high),
                 # linkedAssetsTotal is the true count -- len(linkedAssets) is only what's serialized
                 # after the worst-first cap, and would otherwise silently understate this stat card
                 # exactly the way a truncated `list`/`top` count must never be presented as complete.
                 ("Linked assets", data.get("linkedAssetsTotal", len(data.get("linkedAssets") or [])), False),
                 ("Leaked creds", th.get("leakedCredentialCount", 0), (th.get("leakedCredentialCount", 0) or 0) > 0)]
        # Identity summary
        sec.append("<div class='section-label'>Identity</div>" + _simple_table(
            ["Owner", "Title", "Department", "Location", "Emails"],
            [[_esc(ident.get("ownerName") or ""), _esc(ident.get("title") or "—"), _esc(ident.get("department") or "—"),
              _esc(ident.get("location") or "—"), _esc(", ".join(jlist(ident.get("emails"))))]]))
        # Blast-radius graph — visual overview of the identity/threat/asset/posture spokes.
        graph = _blast_radius_svg(data)
        if graph:
            # Grouped with its label so the pair floats as one figure in a multi-subject report;
            # floating the graph alone stranded the label beside an unrelated section.
            sec.append("<div class='graphbox'><div class='section-label'>Blast radius</div>"
                       + graph + "</div>")
        # Risk factors
        if risk.get("factors"):
            sec.append("<div class='section-label'>Risk factors</div><p>%s</p>" % _esc(" · ".join(risk["factors"])))
        # Linked source identities (identity blast radius)
        mfa_map = {m.get("source"): m.get("configured") for m in (data.get("posture", {}).get("mfa") or []) if isinstance(m, dict)}
        idrows = [[_esc(i.get("source") or ""), "<span class='name'>%s</span>" % _esc(i.get("account") or ""),
                   _esc(i.get("status") or "—"), _esc(mfa_map.get(i.get("source"), "—"))]
                  for i in (data.get("identities") or [])]
        sec.append("<div class='section-label'>Linked source identities (identity blast radius)</div>" +
                   _simple_table(["Source", "Account", "Status", "MFA"], idrows))
        # Threats
        dlp = th.get("dlpBehavioral") or []
        threat_bits = ["<b>%s</b> leaked-credential exposures" % th.get("leakedCredentialCount", 0)]
        if dlp:
            threat_bits.append("<b>%d</b> data-movement / behavioral alerts:<br>&nbsp;&nbsp;• %s" % (len(dlp), "<br>&nbsp;&nbsp;• ".join(_esc(x) for x in dlp)))
        sec.append("<div class='section-label'>Threats</div><p>%s</p>" % "<br>".join(threat_bits))
        # Non-compliance
        nc = data.get("posture", {}).get("nonCompliance") or []
        sec.append("<div class='section-label'>Governance &amp; compliance</div>" +
                   ("<ul>%s</ul>" % "".join("<li>%s</li>" % _esc(x) for x in nc) if nc else "<p>No non-compliance findings.</p>"))
        # Linked assets — blast radius
        arows = []
        for x in (data.get("linkedAssets") or []):
            arows.append([
                "<span class='name'>%s</span>" % _esc(_shortid(x.get("asset") or "")),
                _sev_span(x.get("level")),
                _esc(x.get("os") or ""),
                "<span class='ip'>%s</span>" % _esc(", ".join(jlist(x.get("ip")))),
                _esc(x.get("kev") if x.get("kev") not in (None, "", "null") else "—"),
                _encrypted_cell(x.get("encrypted")),
                _esc(", ".join(x.get("otherHighRiskUsers") or []) or "—"),
            ])
        cap_note = ("<p style='font-size:12px'>Showing %d of %d linked assets (highest risk first). %s</p>"
                   % (data.get("linkedAssetsShown"), data.get("linkedAssetsTotal"), _esc(data.get("note") or ""))
                   ) if data.get("linkedAssetsTruncated") else ""
        sec.append("<div class='section-label'>Linked assets — asset blast radius</div>" + cap_note +
                   _simple_table(["Asset", "Level", "OS", "IP", "KEVs", "Encrypted", "Other high-risk users"], arows))

    # Stability / oscillation warning (identity-dedup conflict)
    stab = data.get("stability") or {}
    callout = ""
    if stab.get("oscillating"):
        flds = ", ".join("%s (×%s)" % (f.get("field"), f.get("changeCount")) for f in (stab.get("fields") or []))
        callout = ("<div class='callout' style='border-left-color:#C0392B;background:#fbeceb;border-color:#f3c9c5'>"
                   "<strong>⚠ Record unstable — identity-resolution conflict.</strong> "
                   "<em>Historical: from the user's change log.</em> This identity's defining fields "
                   "change repeatedly in the change log (%s), meaning two source personas are being merged into one record. "
                   "The point-in-time snapshot below may understate risk — investigate both states and confirm this is one "
                   "real person before acting.</div>" % _esc(flds))

    # Insights + recommendations (analyst reasoning, derived from the data — always included).
    findings, recs = _derive_insights(data)
    findings_html = ("<div class='section-label'>Key findings</div><ul class='insights'>%s</ul>"
                     % "".join("<li>%s</li>" % _esc(f) for f in findings))
    recs_html = ("<div class='section-label'>Recommended actions</div><ol class='recs'>%s</ol>"
                 % "".join("<li>%s</li>" % _esc(r) for r in recs))

    body = "\n".join([callout, findings_html] + sec + [recs_html])
    # A null metric rendered as an empty card, which reads as a broken report rather than as an
    # unknown -- and KEV count is null on any asset no scanner reported. An em dash says
    # "not reported"; a 0 would say "clean", which is the one thing it must never say.
    statcards = "".join(
        "<div class='stat%s'><div class='n'>%s</div><div class='l'>%s</div></div>"
        % ((" crit" if crit else ""), ("&mdash;" if val is None else _esc(val)), _esc(lbl))
        for lbl, val, crit in cards)
    return subtitle, statcards, body


_EMPTY_TABLE = "<p style='color:#8a847f;font-size:11px'>None.</p>"


def _profile_subject(data):
    """(kind, name, descriptor) for one profile's banner in a multi-subject report.

    Read from the payload; nothing here is inferred. A score of None prints "not reported" rather
    than a number, same rule as everywhere else.
    """
    ident = data.get("identity") or {}
    risk = data.get("risk") or {}
    score = risk.get("score")
    score_s = "not reported" if score is None else ("%g" % score)
    tail = "Risk %s (%s)" % (_esc(score_s), _esc(risk.get("level") or "unknown"))
    if data.get("type") == "user":
        name = ident.get("displayName") or ident.get("ownerName") or "(unnamed)"
        bits = [b for b in (ident.get("title"), ident.get("department")) if b]
        return "Identity", name, "%s<br>%s" % (" &middot; ".join(_esc(b) for b in bits), tail)
    name = ident.get("assetName") or "(unnamed)"
    bits = [b for b in (ident.get("os"), ", ".join(jlist(ident.get("ip"))), risk.get("dataClass")) if b]
    return "Linked asset", name, "%s<br>%s" % (" &middot; ".join(_esc(str(b)) for b in bits), tail)


def _multi_profile_html(docs):
    """Render several profiles as one document. Returns (subtitle, statcards, body) -- the same
    contract _profile_html/_digest_html use, so cmd_report treats it the same way.

    Each subject keeps its own banner, stat row and full profile body; nothing is re-derived and no
    figure is aggregated across subjects, because a "total KEVs" across an identity and its assets
    would double-count the same finding from two directions.

    `statcards` comes back empty on purpose: per-subject rows live in the body, and a summary row at
    the top would be exactly that misleading aggregate.
    """
    blocks, toc = [], []
    for d in docs:
        _, statcards, body = _profile_html(d)
        kind, name, desc = _profile_subject(d)
        is_user = d.get("type") == "user"
        # A single-subject report can afford to show that a section was checked and came back
        # empty; stacked several deep those lines are just gaps. Only label+"None." pairs go --
        # an asset's "Associated high-risk users: None recorded." is a real finding (no lateral
        # path) and uses different placeholder text, so it survives.
        body = re.sub(r"<div class='section-label'>[^<]*</div>" + re.escape(_EMPTY_TABLE), "", body)
        toc.append("<b>%s</b> <span class='k'>%s</span>" % (_esc(_shortid(name)), kind.lower()))
        blocks.append(
            "<section class='subj%s'>"
            "<div class='subject'><span class='k'>%s</span><span class='v'>%s</span>"
            "<span class='sub'>%s</span></div>"
            "<div class='stats'>%s</div>"
            "<div class='pbody%s'>%s</div></section>"
            % (" identity" if is_user else "", _esc(kind), _esc(name), desc, statcards,
               "" if is_user else " cols", body))
    n_assets = sum(1 for d in docs if d.get("type") == "asset")
    n_users = len(docs) - n_assets
    subtitle = "%d subjects: %d identity, %d linked asset%s" % (
        len(docs), n_users, n_assets, "" if n_assets == 1 else "s")
    body = ("<div class='toc'><span class='lead'>In this report</span>%s</div>\n%s"
            % ("<span class='sep'>/</span>".join(toc), "\n".join(blocks)))
    return subtitle, "", body


# Line-series colors for trend charts, from the report stylesheet's own palette. The brand lime
# (#D4FC68) is deliberately absent: the stylesheet marks it accent-only, and severity colors carry
# meaning in these reports, so the first series gets ink, not alarm.
_CHART_COLORS = ["#000000", "#E07B39", "#C0392B", "#8a847f", "#b9bbc0"]


def _date_offsets(dates):
    """0..1 positions for `dates`, proportional to elapsed time. Index-spaced if they don't parse."""
    def parse(d):
        try:
            y, m, dd = str(d).split("-")
            return datetime.date(int(y), int(m), int(dd)).toordinal()
        except Exception:
            return None

    ords = [parse(d) for d in dates]
    if any(o is None for o in ords) or ords != sorted(ords) or ords[-1] == ords[0]:
        n = float(len(dates) - 1) or 1.0
        return [i / n for i in range(len(dates))]
    span = float(ords[-1] - ords[0])
    return [(o - ords[0]) / span for o in ords]


def _line_chart_svg(dates, series, height=210, flagged=None):
    """One inline-SVG line chart. `series` is [(label, values, color)]; values align with `dates`.

    The load-bearing rule, inherited from the whole trend layer: a null value BREAKS the line.
    Consecutive non-null runs render as separate segments with a visible gap between them, because a
    line bridged (or dropped to zero) across a date that was never captured is the most convincing
    possible wrong answer a picture can give. Every real point gets a dot, so an isolated value
    between two gaps is still visible. Returns "" when there is nothing plottable.

    The x-axis is spaced by DATE, not by index. Index spacing drew Jul 1, Jul 2 and Aug 5 as three
    evenly-spaced points joined by an ordinary-looking line -- a five-week hole in the cadence
    rendered as one routine step, with both the slope and the shape wrong. A null at a captured date
    is only half of "absence"; the uncaptured date is the more common half, and this is what makes
    it visible. Falls back to index spacing if the dates don't parse.
    """
    vals = [v for _, vs, _ in series for v in vs if v is not None]
    if len(dates) < 2 or not vals:
        return ""
    W, H, ML, MR, MT, MB = 760, height, 62, 14, 10, 24
    lo, hi = float(min(vals)), float(max(vals))
    pad = (hi - lo) * 0.08
    lo, hi = lo - pad, hi + pad
    if hi - lo <= 0:
        # Tested AFTER padding, not before: at large magnitudes a +/-1.0 nudge is below the ULP, so
        # `lo == hi` stayed true and y() divided by zero. Widen relatively, with an absolute floor
        # for the zero case.
        span = max(abs(hi) * 0.01, 1.0)
        lo, hi = lo - span, hi + span
    if min(vals) >= 0:
        lo = max(lo, 0.0)   # these are counts; a padded axis must not imply negative records exist

    offs = _date_offsets(dates)

    def x(i):
        return ML + offs[i] * (W - ML - MR)

    def y(v):
        return MT + (H - MT - MB) * (1.0 - (float(v) - lo) / (hi - lo))

    parts = ['<svg viewBox="0 0 %d %d" role="img" style="width:100%%;max-width:%dpx;height:auto;display:block">' % (W, H, W)]
    for f in (0.0, 1.0 / 3, 2.0 / 3, 1.0):
        gv = lo + (hi - lo) * f
        gy = y(gv)
        parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#e8e1da" stroke-width="1"/>' % (ML, gy, W - MR, gy))
        parts.append('<text x="%d" y="%.1f" text-anchor="end" font-size="10" fill="#8a847f">%s</text>'
                     % (ML - 6, gy + 3, "{:,.0f}".format(gv)))
    for i, d in enumerate(dates):
        if d in (flagged or ()):
            # A muted vertical band, drawn under the lines: the reader sees WHERE coverage was
            # reduced without the mark competing with the data.
            parts.append('<rect x="%.1f" y="%d" width="5" height="%d" fill="#E07B39" opacity="0.16"/>'
                         % (x(i) - 2.5, MT, H - MT - MB))
    # Chosen by POSITION, tracking the pixel range each label actually occupies. With date spacing
    # two dates a week apart in a two-month window sit ~30px apart, so index-based selection smeared
    # them together -- and centre-to-centre spacing isn't enough either, because the first label is
    # left-anchored and the last right-anchored, so each extends fully to one side. The last date
    # always keeps its slot; earlier labels yield to it.
    LW = 62.0   # rendered width of a YYYY-MM-DD label at font-size 10

    def span(i):
        xi = x(i)
        if i == 0:
            return (xi, xi + LW)
        if i == len(dates) - 1:
            return (xi - LW, xi)
        return (xi - LW / 2, xi + LW / 2)

    last_end, label_idx = None, []
    for i in range(len(dates) - 1):
        a0, a1 = span(i)
        if last_end is None or a0 >= last_end + 6:
            label_idx.append(i)
            last_end = a1
    final = len(dates) - 1
    while label_idx and span(label_idx[-1])[1] + 6 > span(final)[0]:
        label_idx.pop()
    label_idx.append(final)
    for i in label_idx:
        # Edge labels anchor inward so the first and last dates don't clip at the chart borders.
        anchor = "start" if i == 0 else ("end" if i == len(dates) - 1 else "middle")
        parts.append('<text x="%.1f" y="%d" text-anchor="%s" font-size="10" fill="#8a847f">%s</text>'
                     % (x(i), H - 8, anchor, _esc(dates[i])))
    for _, vs, color in series:
        seg = []
        for i, v in enumerate(vs):
            if v is None:
                if len(seg) > 1:
                    parts.append('<polyline fill="none" stroke="%s" stroke-width="2" points="%s"/>'
                                 % (color, " ".join("%.1f,%.1f" % p for p in seg)))
                seg = []
            else:
                seg.append((x(i), y(v)))
        if len(seg) > 1:
            parts.append('<polyline fill="none" stroke="%s" stroke-width="2" points="%s"/>'
                         % (color, " ".join("%.1f,%.1f" % p for p in seg)))
        for i, v in enumerate(vs):
            if v is not None:
                parts.append('<circle cx="%.1f" cy="%.1f" r="2.6" fill="%s"/>' % (x(i), y(v), color))
    parts.append("</svg>")
    return "".join(parts)


def _series_gap_note(series):
    """The one-line honesty footnote, present only when a plotted series actually has a gap."""
    if any(v is None for _, vs, _ in series for v in vs):
        return ("<p style='color:#8a847f;font-size:10px;margin:2px 0 0'>A break in a line is a date "
                "this value was not captured — a gap is unknown, never zero.</p>")
    return ""


def _chart_block(title, dates, named_values, note="", coverage_flags=None):
    """Section label + legend + chart + gap note. `named_values` is [(label, values)]."""
    series = [(lab, vs, _CHART_COLORS[i % len(_CHART_COLORS)]) for i, (lab, vs) in enumerate(named_values)]
    svg = _line_chart_svg(dates, series, flagged=[f.get("date") for f in (coverage_flags or [])])
    if not svg:
        return ""
    if coverage_flags:
        # Named on the chart itself, not just in a caveats section at the end: a dip on a date when
        # connectors were failing reads as an environment change unless the picture says otherwise.
        note += ("<p style='color:#8a847f;font-size:10px;margin:2px 0 0'>Marked dates had reduced "
                 "connector coverage (%s) — a movement there may be the data sources, not the "
                 "environment.</p>"
                 % _esc("; ".join("%s: %s" % (f.get("date"), f.get("reason")) for f in coverage_flags[:4])))
    legend = "".join(
        "<span style='display:inline-block;margin-right:14px;font-size:10px;color:#333'>"
        "<span style='display:inline-block;width:10px;height:10px;background:%s;border-radius:2px;"
        "margin-right:4px;vertical-align:-1px'></span>%s</span>" % (color, _esc(lab))
        for lab, _, color in series)
    return ("<div class='section-label'>%s</div><div style='margin:2px 0 14px'>%s%s%s%s</div>"
            % (_esc(title), legend, svg, _series_gap_note(series), note))


def _trend_html(data):
    """Render a `trend` payload as branded report sections. Returns (subtitle, statcards_html,
    body_html) -- the same contract as _digest_html/_profile_html, so cmd_report treats all three
    the same way.

    Two rules carry the trend layer's discipline into the picture: a refusal renders AS the refusal
    (a branded page saying why there is no trend), never as an empty chart; and gaps in a series
    break the line -- _line_chart_svg owns that one.
    """
    def n(v):
        # Escaped on the non-numeric branch: this feeds _simple_table, which does NOT escape its
        # cells, and the values come from the snapshot history file. Everything reaching it today is
        # numeric, but the surrounding code escapes at the call site for exactly this reason, and
        # this was the one substitution that didn't.
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return "{:,}".format(v)
        return _esc(v) if v is not None else "—"

    frm, to = data.get("from") or {}, data.get("to") or {}
    window = "%s → %s" % (frm.get("stackDate") or "?", to.get("stackDate") or "?")

    if data.get("insufficientHistory") or data.get("unverifiable"):
        why = "insufficient history" if data.get("insufficientHistory") else "coverage not verifiable"
        statcards = "".join("<div class='stat'><div class='n'>%s</div><div class='l'>%s</div></div>" % (a, b) for a, b in [
            (n(data.get("snapshotsRead")), "snapshots read"),
            (n(data.get("dataPoints", data.get("distinctStackDates"))), "data points"),
            ("—", why)])
        body = ("<div class='section-label'>Why there is no trend</div><p style='font-size:12px'>%s</p>"
                % _esc(data.get("note") or why))
        return "Historical · trend — %s" % why, statcards, body

    coverage = data.get("coverage") or {}
    verdict = "coverage changed" if coverage.get("coverageChanged") else "comparable"
    statcards = "".join("<div class='stat%s'><div class='n'>%s</div><div class='l'>%s</div></div>" % (c, a, b) for c, a, b in [
        ("", _esc(window), "window (stack dates)"),
        ("", n(data.get("distinctStackDates")), "distinct stack dates"),
        (" crit" if coverage.get("coverageChanged") else "", verdict, "connector coverage"),
        ("", n(len(data.get("changes") or [])), "comparisons")])

    sec = []
    series = data.get("series") or {}
    dates = series.get("dates") or []
    totals = series.get("totals") or {}
    flags = series.get("coverageFlags") or []
    if totals:
        sec.append(_chart_block("Inventory totals", dates, sorted(totals.items()), coverage_flags=flags))
    for mt in series.get("metrics") or []:
        sec.append(_chart_block("Metric — %s" % (mt.get("label") or mt.get("name")), dates,
                                [(mt.get("label") or mt.get("name"), mt.get("values") or [])],
                                coverage_flags=flags))
    for bd in series.get("breakdowns") or []:
        groups = bd.get("groups") or {}
        def last_val(vs):
            return next((v for v in reversed(vs) if v is not None), 0)
        ranked = sorted(groups.items(), key=lambda kv: -last_val(kv[1]))
        note = ""
        if len(ranked) > 8:
            note = ("<p style='color:#8a847f;font-size:10px;margin:2px 0 0'>Showing the 8 largest of "
                    "%d groups (by latest value).</p>" % len(ranked))
            ranked = ranked[:8]
        if bd.get("incompleteDates"):
            note += ("<p style='color:#8a847f;font-size:10px;margin:2px 0 0'>On %s this breakdown did "
                     "not account for every record; those counts are exact but not exhaustive.</p>"
                     % _esc(", ".join(bd["incompleteDates"])))
        sec.append(_chart_block("%s by %s" % (bd.get("table") or "asset", bd.get("by") or "?"),
                                dates, ranked, note, coverage_flags=flags))

    rows = data.get("changes") or []
    if rows:
        # _simple_table takes positional cell lists and does NOT escape -- everything
        # customer-originated (names, group values) is escaped here.
        cells = []
        for r in rows:
            pct = r.get("percentChange")
            chg = r.get("change")
            cells.append([_esc(r.get("kind") or ""), _esc(r.get("name") or ""),
                          n(r.get("from")), n(r.get("to")),
                          ("%+d" % chg if isinstance(chg, (int, float)) and not isinstance(chg, bool) else n(chg)),
                          ("%+.1f%%" % pct if isinstance(pct, (int, float)) else "—"),
                          _esc(r.get("note") or ("incomplete at %s" % ", ".join(r["incompleteAt"])
                                                 if r.get("incompleteAt") else ""))])
        sec.append("<div class='section-label'>Changes, %s</div>%s"
                   % (_esc(window), _simple_table(["Kind", "Name", "From", "To", "Change", "%", "Notes"], cells)))

    ents = data.get("entities") or {}
    if ents:
        if not ents.get("comparable"):
            sec.append("<div class='section-label'>Entity movement</div><p style='font-size:11px'>%s</p>"
                       % _esc(ents.get("reason") or "not comparable"))
        else:
            c = ents.get("counts") or {}
            sec.append("<div class='section-label'>Entity movement — %s</div>%s"
                       % (_esc(ents.get("scope") or ""),
                          _simple_table(["Appeared", "Disappeared", "Worsened", "Improved", "Unchanged"],
                                        [[n(c.get("appeared")), n(c.get("disappeared")), n(c.get("worsened")),
                                          n(c.get("improved")), n(c.get("unchanged"))]])))
            movers = [[_esc(w.get("name") or w.get("id") or ""), n(w.get("from")), n(w.get("to")),
                       "%+g" % w.get("change") if isinstance(w.get("change"), (int, float)) else n(w.get("change"))]
                      for w in (ents.get("worsened") or [])[:10]]
            if movers:
                sec.append("<div class='section-label'>Largest regressions (%s)</div>%s"
                           % (_esc(ents.get("identities") or ""),
                              _simple_table(["Entity", "From", "To", "Change"], movers)))
            if ents.get("note"):
                sec.append("<p style='color:#8a847f;font-size:10px'>%s</p>" % _esc(ents["note"]))

    notes = []
    if coverage.get("note"):
        notes.append(coverage["note"])
    for cv in data.get("caveats") or []:
        notes.append(cv.get("note") or json.dumps(cv))
    for nt in data.get("notTracked") or []:
        notes.append("Not tracked: %s — %s" % (nt.get("metric") or nt.get("breakdown"), nt.get("reason") or ""))
    if notes:
        sec.append("<div class='section-label'>Caveats</div>" +
                   "".join("<p style='font-size:11px;margin:2px 0'>%s</p>" % _esc(t) for t in notes))

    return ("Historical · trend, %s — %s" % (window, data.get("stack") or "Meridian"),
            statcards, "".join(sec))


def _print_to_pdf(browser, tmp_html, out):
    """One browser print-to-PDF attempt, retried once with the legacy headless flag.

    check=True is what makes the retry reachable: an older Chrome rejects --headless=new with a
    NONZERO EXIT, not an exception, so without it the except branch never fired and the report
    silently degraded to HTML on exactly the browsers the fallback flag was written for. The retry
    itself stays uncheck'd -- success is judged from the output file either way.
    """
    import subprocess
    file_url = "file:///" + os.path.abspath(tmp_html).replace("\\", "/")
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
           "--print-to-pdf=%s" % os.path.abspath(out), file_url]
    try:
        subprocess.run(cmd, timeout=60, capture_output=True, check=True)
    except Exception:
        cmd[1] = "--headless"  # older headless flag
        subprocess.run(cmd, timeout=60, capture_output=True)


def cmd_report(a):
    """Render a Cyderes-branded PDF (or HTML with --html) from any verb's JSON output."""
    paths = a.input if isinstance(a.input, list) else ([a.input] if a.input else [])
    for p in paths:
        if config_path_problem(p):
            die(config_path_problem(p), 2)
    if len(paths) > 1:
        # Several inputs only make sense for profiles -- one document, several subjects. Combining
        # a `top` with a `digest` has no meaning, so it is refused by name rather than rendered
        # into something that looks deliberate.
        docs = []
        for p in paths:
            d = json.loads(open(p, encoding="utf-8-sig").read())
            if not (isinstance(d, dict) and d.get("type") in ("user", "asset")
                    and "identity" in d and "risk" in d):
                die("%s is not a profile payload. Multiple --input files are only supported for "
                    "`profile` output (one document, several subjects); everything else takes one "
                    "file." % p, 2)
            docs.append(d)
        lines = list(dict.fromkeys(_currency_line(d) for d in docs))
        return _write_report(a, "Meridian Report", *_multi_profile_html(docs), currency=" | ".join(lines))

    raw = sys.stdin.read() if not paths else open(paths[0], encoding="utf-8-sig").read()
    data = json.loads(raw)
    title = a.title or "Meridian Report"
    subtitle, rows, stats = "", [], []
    profile_mode = isinstance(data, dict) and data.get("type") in ("user", "asset") and "identity" in data and "risk" in data
    digest_mode = isinstance(data, dict) and data.get("generated") == "digest"
    trend_mode = isinstance(data, dict) and data.get("generated") == "trend"
    if trend_mode:
        profile_mode = True     # same multi-section rendering path as digest/profile
        subtitle, statcards_pre, findings_pre = _trend_html(data)
    elif digest_mode:
        profile_mode = True     # same multi-section rendering path, not a row table
        subtitle, statcards_pre, findings_pre = _digest_html(data)
    elif profile_mode:
        subtitle, statcards_pre, findings_pre = _profile_html(data)
    elif isinstance(data, dict) and "top" in data:
        rows = data["top"]
        subtitle = "Top %d by %s in %s" % (len(rows), data.get("field"), data.get("table"))
        stats = [("Rows", len(rows)), ("Ranked field", data.get("field")), ("Tail at", "≥ %s" % data.get("matchedAtThreshold")), ("In tail", data.get("totalInTail"))]
    elif isinstance(data, dict) and "rows" in data:
        rows = data["rows"]
        subtitle = "%s matching filter in %s" % (data.get("totalRecords"), data.get("table"))
        stats = [("Matches", data.get("totalRecords")), ("Shown", data.get("shown")), ("Table", data.get("table")), ("Truncated", "yes" if data.get("truncated") else "no")]
    elif isinstance(data, dict) and "groups" in data:
        rows = data["groups"]
        subtitle = "%s grouped by %s" % (data.get("table"), data.get("by"))
        stats = [("Total", data.get("total")), ("Groups", len(rows)), ("Grouped by", data.get("by")), ("Table", data.get("table"))]
    else:
        rows = data if isinstance(data, list) else [data]

    if profile_mode:
        statcards = statcards_pre
        findings_html = findings_pre
        section_label = ""  # profile sections carry their own labels
    else:
        statcards = "".join(
            "<div class='stat%s'><div class='n'>%s</div><div class='l'>%s</div></div>"
            % ((" crit" if str(v).lower() in ("yes",) else ""), _esc("" if v is None else v), _esc(l))
            for l, v in (stats[:4] if stats else []))
        findings_html = _rows_html(rows)
        section_label = "<div class=\"section-label\">Findings</div>"
    return _write_report(a, title, subtitle, statcards, findings_html, section_label,
                         currency=_currency_line(data))


def _write_report(a, default_title, subtitle, statcards, body, section_label="", currency=""):
    """Assemble the branded page and write it as PDF (or HTML). Shared by every report shape,
    including the multi-subject one, so there is a single copy of the browser + cleanup logic."""
    title = a.title or default_title
    fqdn = ""
    try:
        fqdn, _, _ = load_config()
    except SystemExit:
        pass
    gen = a.date or ""
    # The CSP is defence in depth: every customer string is escaped, but headless Chrome renders
    # this page from file:// with scripts and network enabled, so nothing it contains may run code
    # or fetch anything. Fonts are data: URIs and the logo is inline SVG, so this blocks nothing.
    html = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; font-src data:; img-src data:">
<title>%s</title>
<style>%s
%s</style></head><body>
<div class="masthead">
  <div class="brandrow"><span class="wordmark">%s</span>
    <span class="product">%s</span></div>
  <h1>%s</h1>
  <div class="meta">%s &middot; stack <code>%s</code>%s</div>
  %s
</div>
%s
%s
%s
<div class="report-footer"><span>%s</span><span class="conf">Confidential</span></div>
</body></html>""" % (
        _esc(title), _font_face_css(), _load_css(), _logo_svg(), _meridian_svg(), _esc(title), _esc(subtitle), _esc(fqdn or "n/a"),
        (" &middot; " + _esc(gen)) if gen else "",
        ("<div class='meta currency'>%s</div>" % _esc(currency)) if currency else "",
        ("<div class='stats'>%s</div>" % statcards) if statcards else "",
        section_label,
        body,
        _esc(_producer_note()))

    out = a.out
    if a.html or out.lower().endswith(".html"):
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
        jout({"written": os.path.abspath(out), "format": "html"}); return

    # Render to PDF via headless Chrome/Edge.
    browser = _find_browser()
    tmp_html = out + ".tmp.html"
    with open(tmp_html, "w", encoding="utf-8") as f:
        f.write(html)
    if not browser:
        os.replace(tmp_html, out + ".html")
        jout({"written": os.path.abspath(out + ".html"), "format": "html",
                          "note": "No Chrome/Edge found for PDF conversion; wrote HTML instead."}); return
    try:
        try:
            _print_to_pdf(browser, tmp_html, out)
        except Exception:
            pass  # a hung or twice-failed browser must not crash the verb; `ok` is judged from the file
        ok = os.path.exists(out) and os.path.getsize(out) > 800
    finally:
        # Always. The tmp HTML carries the same customer PII as the PDF, and unlike the PDF it is
        # not gitignored by the *.pdf rule -- before the finally, a double browser failure orphaned
        # it in the working tree with no mention in the output.
        try:
            os.remove(tmp_html)
        except OSError:
            pass
    if ok:
        jout({"written": os.path.abspath(out), "format": "pdf", "renderer": os.path.basename(browser)})
    else:
        with open(out + ".html", "w", encoding="utf-8") as f:
            f.write(html)
        jout({"written": os.path.abspath(out + ".html"), "format": "html", "note": "PDF conversion failed; wrote HTML."})


# ---- connector / data-coverage summary --------------------------------------------------------
# "What data will these answers be based on?" takes two endpoints, because they say different things:
#   /CMDB/v2/connector/profile         - what is CONFIGURED, which services are ENABLED, and whether
#                                        each one's last connection test passed.
#   /CMDB/v2/system/metrics/connector  - what actually INGESTED, with per-run record counts.
# Neither alone is the answer: a connector can be enabled and green yet have contributed nothing
# (never ran), and a source can still hold data in the stack with no profile configured for it today.
# The profile payload also carries hosts, proxies and an encrypted password - only the fields
# extracted below ever leave this module, so raw credentials never reach the transcript.

def _short(v, n=200):
    """Collapse a message to one line and cap it - connector errors can be whole stack traces."""
    if v is None:
        return None
    s = v if isinstance(v, str) else json.dumps(v)
    s = re.sub(r"\s+", " ", s).strip()
    return None if not s else (s[:n] + "..." if len(s) > n else s)


# Connector messages are the one free-text field the profile allow-list keeps, and connector/SDK
# errors routinely echo exactly what the allow-list drops: "Login failed for user 'svc_x'",
# "HTTPSConnectionPool(host='10.1.2.3')", a URL with user:pass@ in it. Those messages are quoted in
# the mandatory preflight on every session, cached, and rendered into digest PDFs, so the allow-list
# alone is not the boundary it looks like. Two layers: the profile's OWN credential-shaped values
# are cut out of its messages wherever they appear (_profile_secrets), and generic shapes are cut
# regardless of source. Scrub BEFORE _short(): truncating first can leave half a value behind.
REDACTED = "[redacted]"
_SCRUB_PATTERNS = (
    # scheme://user:pass@host -> scheme://[redacted]@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@"), r"\1" + REDACTED + "@"),
    # Authorization-header shapes
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + REDACTED),
    # key=value / key: value for credential-named keys, quoted or not
    (re.compile(r"(?i)\b((?:client[_-]?)?secret|password|passwd|pwd|api[_-]?key|access[_-]?key|"
                r"secret[_-]?key|(?:access|refresh|auth|api|session)?[_-]?token)"
                r"(\s*[=:]\s*)([\"']?)[^\s\"'&,;)}]+"), r"\1\2\3" + REDACTED),
)
# Key names (split on _ - and camelCase) whose string values are credential-shaped. Anything under
# such a key is collected, so "proxy": {"host": ...} is covered without naming "proxy.host".
_SECRET_KEY_PARTS = {"host", "hostname", "user", "username", "login", "account", "password",
                     "passwd", "pwd", "secret", "token", "key", "apikey", "proxy", "url", "uri",
                     "endpoint", "tenant", "client", "arn", "vault", "domain", "server", "dsn",
                     "email", "ip", "address", "ref", "credential", "credentials", "auth"}
# Keys the allow-list already emits: their values are shown anyway, so never cut them from text.
_SECRET_KEY_SAFE = {"display_name", "bridge_name", "profile_name", "group", "service", "status",
                    "activity", "message"}
_SECRET_VALUE_SKIP = {"true", "false", "none", "null", "http", "https", "default", "encrypt",
                      "required"}


def _key_parts(k):
    k = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(k))
    return {p for p in re.split(r"[_\-\s.]+", k.lower()) if p}


def _profile_secrets(obj, _inherit=False):
    """Credential-shaped string values anywhere in one raw connector profile, for _scrub().

    Collected by key name, not by the values' own shape -- a service account or an internal host
    looks like any other word. Deliberately over-inclusive: a region or tenant name cut from an
    error message costs a little diagnostic detail, a leaked username does not come back.
    """
    found = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _SECRET_KEY_SAFE:
                continue
            found |= _profile_secrets(v, _inherit or bool(_key_parts(k) & _SECRET_KEY_PARTS))
    elif isinstance(obj, list):
        for v in obj:
            found |= _profile_secrets(v, _inherit)
    elif _inherit and isinstance(obj, str):
        v = obj.strip()
        if len(v) >= 4 and v.lower() not in _SECRET_VALUE_SKIP:
            found.add(v)
            if "://" in v:   # also the host and user a URL carries, which errors quote bare
                u = urllib.parse.urlsplit(v)
                found |= {x for x in (u.hostname, u.username) if x and len(x) >= 4}
    return found


def _scrub(text, secrets=()):
    """Cut credential-shaped content out of a connector message. None/empty pass through."""
    if not text:
        return text
    s = text if isinstance(text, str) else json.dumps(text)
    for v in sorted(secrets, key=len, reverse=True):
        s = re.sub(re.escape(v), REDACTED, s, flags=re.IGNORECASE)
    for pat, rep in _SCRUB_PATTERNS:
        s = pat.sub(rep, s)
    return s


# `event_messages` on a connector run is the connector's own log lines verbatim, e.g. one entry of
# {"WARNING": "2026-08-26 04:26:19 | WARNING  | loguru._logger:warning:1979 - no local data
# template..."} per record processed - so a single failing lookup can repeat dozens of times with
# byte-identical text. Neither _short() (which just truncates the raw dump) nor silence (which is how
# a `degraded`-but-ingesting connector's warning went unexplained before this) is useful to a reader;
# strip the timestamp/module/line-number framing and dedupe so "why is this connector degraded" has an
# answer instead of a wall of loguru noise or nothing at all.
_LOG_LINE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T][\d:.]+\s*\|\s*\w+\s*\|\s*[\w.]+:\w+:\d+\s*-\s*")

# A Python traceback arrives already collapsed onto one line, so _short()'s front truncation keeps the
# call frames and throws away the exception -- the only part that says what went wrong. Measured on a
# live stack: 22 connectors shared one message whose visible 200 chars were
#   'Traceback (most recent call last): File "aws_org.py", line 73, ... botocore/paginate.py ...'
# while the discarded tail read 'botocore.errorfactory.AccessDeniedException: An error occurred
# (AccessDeniedException) when calling the ListAccounts operation: You don't have permissions to
# access this resource.' -- an actionable AWS permission finding, invisible on 22 connectors. §1.5
# requires the message say what actually happened, so keep the exception, not the frames.
_TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\)")
_TB_FRAME_RE = re.compile(r'File "[^"]*", line \d+, in \S+')
# The last such match is the raised exception; earlier ones are re-raises or a summary prefix.
_TB_EXC_RE = re.compile(r"(?:[A-Za-z_][\w.]*\.)?[A-Z]\w*"
                        r"(?:Error|Exception|Warning|Exit|Interrupt|Denied|Timeout|NotFound|Failure)"
                        r"\s*:\s*\S")


def _traceback_cause(text):
    """Reduce a one-line Python traceback to the exception it ended with; pass anything else through.

    Deliberately applied before the dedupe in _friendly_event_message, not after: two connectors that
    failed the same way through different code paths carry different frame lists but the identical
    exception, so extracting first lets them collapse into one message instead of two.
    """
    if not text or not _TRACEBACK_RE.search(text):
        return text
    hits = list(_TB_EXC_RE.finditer(text))
    if hits:
        return text[hits[-1].start():].strip()
    # Nothing recognisable as an exception line. Drop the frames and keep what is left rather than
    # falling back to the head, which is the framing sentence and never the cause.
    return re.sub(r"\s+", " ", _TB_FRAME_RE.sub("", text)).strip() or text


def _friendly_event_message(raw, limit=2, secrets=(), cap=True):
    """Reduce `event_messages` to the distinct human-readable text a reader would want, capped to
    `limit` distinct messages (join with '; '). Handles the {LEVEL: line} list shape and a plain
    string alike; returns None for nothing usable. Each line is _scrub()-ed; `cap=False` skips the
    final _short() for a caller that scrubs again with more secrets and must truncate only after."""
    if not raw:
        return None
    entries = raw if isinstance(raw, list) else [raw]
    seen, out = set(), []
    for e in entries:
        text = next(iter(e.values()), None) if isinstance(e, dict) else e if isinstance(e, str) else None
        if not text:
            continue
        text = re.sub(r"\s+", " ", _LOG_LINE_PREFIX_RE.sub("", str(text))).strip()
        text = _scrub(_traceback_cause(text), secrets)
        if text and text not in seen:
            seen.add(text)
            out.append(text)
        if len(out) >= limit:
            break
    if not out:
        return None
    return _short("; ".join(out)) if cap else "; ".join(out)


_NOT_ACRONYM = {"log", "and", "the", "for", "new", "raw"}   # short words that aren't initialisms
_ACRONYM = {"sepm", "dhcp", "ldap", "saml", "scim", "siem", "vpn", "kev"}   # longer ones that are


def _pretty_name(service):
    """`sepm_computers` -> `SEPM Computers`, for ingesting sources with no configured profile."""
    words = [w for w in re.split(r"[_\s]+", service or "") if w]
    return " ".join(w.upper() if w.lower() in _ACRONYM or (len(w) <= 3 and w.lower() not in _NOT_ACRONYM)
                    else w.capitalize() for w in words) or (service or "?")


def _profile_name(v):
    """metrics/connector returns `profile` as a plain name, or as the entire profile object (which
    embeds the connector's credential config) - take only the name."""
    return v.get("profile_name") if isinstance(v, dict) else v


def _test_health(status):
    """Connection-test status on a profile service -> ok | fail | unknown."""
    s = (status or "").upper()
    return "ok" if s == "OK" else ("unknown" if s in ("", "UNKNOWN") else "fail")


def _run_health(status):
    """Ingestion-run status -> ok | warn | fail. The API mixes casing and returns compound values
    ('Success', 'SUCCESS', 'Warning', 'Warning&Error', 'Error', 'FAILED')."""
    s = (status or "").upper()
    if s == "SUCCESS":
        return "ok"
    if s == "WARNING":
        return "warn"
    return "fail" if s else "unknown"


def _fetch_connector_profiles():
    """(configured connectors reduced to the non-sensitive fields, every profile's credential-shaped
    values). The second half never leaves summarize_connectors: it exists so run messages, fetched
    concurrently from another endpoint, can be scrubbed of the same values. Every profile's values
    scrub every message -- an error in one connector can quote another's host."""
    raw = call("GET", "/CMDB/v2/connector/profile", retries=0)
    profiles = raw.get("connectorProfiles") or []
    secrets = set()
    for p in profiles:
        secrets |= _profile_secrets(p)
    out = []
    for p in profiles:
        svcs = [{"service": s.get("service"),
                 "displayName": s.get("display_name") or _pretty_name(s.get("service")),
                 "enabled": s.get("activity") is True,
                 "testStatus": (s.get("status") or "UNKNOWN").upper(),
                 "message": _short(_scrub(s.get("message"), secrets))}
                for s in (p.get("services_list") or [])]
        out.append({"connector": p.get("display_name") or p.get("bridge_name"),
                    "bridge": p.get("bridge_name"), "profile": p.get("profile_name"),
                    "group": p.get("group"), "services": svcs})
    return out, secrets


def _fetch_connector_runs():
    """Latest ingestion run per (service, profile), plus counts of the runs that aren't ingestion,
    and whether the read is known-incomplete. `size=2000` is a page size, not a history depth -- a
    stack can hold more than one page of runs, and this endpoint returns them unsorted, so reading
    only page 0 risks an arbitrary slice rather than the newest one. Page 0 gives `totalPages`; the
    rest are fetched concurrently up to CONNECTOR_RUNS_MAX_PAGES, same tail-fetch shape as `list`."""
    r0 = call("GET", "/CMDB/v2/system/metrics/connector?size=2000&page=0", retries=0)
    content = list(r0.get("content") or [])
    total_pages = r0.get("totalPages") or 1
    npages = min(total_pages, CONNECTOR_RUNS_MAX_PAGES)
    for r in parallel([(lambda p=p: call("GET", "/CMDB/v2/system/metrics/connector?size=2000&page=%d" % p,
                        retries=0)) for p in range(1, npages)]):
        if isinstance(r, Exception):
            raise r
        content.extend(r.get("content") or [])
    truncated = total_pages > CONNECTOR_RUNS_MAX_PAGES
    ingest, actions, pipeline = {}, {}, {}
    for r in content:
        platform = r.get("platform")
        # 'action' rows are outbound Scheduled Actions and 'ML-ENGINE' rows are the internal
        # asset/user mergers - neither brings data in, so they're counted, not listed as sources.
        bucket = actions if platform == "action" else pipeline if platform == "ML-ENGINE" else ingest
        key = (r.get("bridge_name"), _profile_name(r.get("profile")))
        prev = bucket.get(key)
        if prev is None or (r.get("_time") or 0) > (prev.get("_time") or 0):
            bucket[key] = {"service": r.get("bridge_name"), "profile": _profile_name(r.get("profile")),
                           "platform": platform, "status": r.get("status"),
                           "records": r.get("output_records"), "utc": r.get("_utc"),
                           "_time": r.get("_time") or 0,
                           # Uncapped: summarize_connectors scrubs again with every profile's
                           # values, then truncates. The run's own embedded profile scrubs now.
                           "message": _friendly_event_message(
                               r.get("event_messages"), cap=False,
                               secrets=_profile_secrets(r.get("profile"))
                               if isinstance(r.get("profile"), dict) else ())}
    return ingest, list(actions.values()), list(pipeline.values()), truncated


def _last_ingest(matched):
    """Roll several service runs up into one per-connector ingestion verdict. When that verdict
    isn't clean, `notes` says why - a connector with 35 services and one erroring one used to report
    only the rolled-up status ("Success"/"Warning"), with the actual cause nowhere in the response;
    a reader could see *that* something was off but never *what*. Grouped by distinct message, worst
    (fail) before warn, biggest service count first - same shape as _group_failures, so a caller
    already reading `failures[]` doesn't learn a second convention."""
    worst = "ok"
    for r in matched:
        h = _run_health(r["status"])
        if h == "fail" or (h == "warn" and worst == "ok"):
            worst = "fail" if h == "fail" else "warn"
    newest = max(matched, key=lambda r: r["_time"])
    out = {"health": worst, "status": newest["status"], "utc": newest["utc"],
           "records": sum(r["records"] or 0 for r in matched), "sources": len(matched)}
    if worst != "ok":
        groups = {}
        for r in matched:
            if _run_health(r["status"]) != "ok":
                key = (_run_health(r["status"]), r["message"] or "(no message reported)")
                groups.setdefault(key, []).append(r["service"])
        notes = [{"severity": sev, "message": msg, "services": svcs} for (sev, msg), svcs in groups.items()]
        notes.sort(key=lambda n: (n["severity"] != "fail", -len(n["services"])))
        out["notes"] = notes
    return out


def _group_failures(rows):
    """One credential problem usually fails every service on the connector with the identical
    message; collapse those into a single entry listing the affected services."""
    groups = []
    index = {}
    for r in rows:
        key = (r["connector"], r["profile"], r["kind"], r["status"], r["message"])
        g = index.get(key)
        if g is None:
            g = {"connector": r["connector"], "profile": r["profile"], "kind": r["kind"],
                 "status": r["status"], "services": [], "message": r["message"]}
            index[key] = g
            groups.append(g)
        g["services"].append(r["service"])
    for g in groups:
        g["serviceCount"] = len(g["services"])
    groups.sort(key=lambda g: (g["kind"] != "connection-test", -g["serviceCount"]))
    return groups


def summarize_connectors(max_failures=12, max_other=15, brief=True, max_warnings=12, max_detail=10,
                         refresh=False, fetched_profiles=None):
    """Data-coverage summary: which connectors are enabled, whether they're succeeding, and how much
    data each last brought in. Degrades a half at a time - a token that can only read one of the two
    endpoints still gets the other.

    `brief` (the default, and what `connectors` / `connect --with-connectors` use) reshapes the result
    message-first via _brief_connectors -- same facts, no repeated message text. `--full` opts out.
    Brief is the default because this is the one call SKILL.md makes mandatory on every session, so
    its payload is pure overhead on every question the user actually asked.

    Cached per stack for RESCACHE_CONNECTOR_TTL: the two endpoints cost 5 calls, ~99 MB of transfer
    and ~5.9s, and describe a daily-cadence fact. The *merged* result is what gets cached, before
    `brief` is applied, so a cached read still serves either shape.

    **A partial fetch is never cached.** If either endpoint errored, `fetched` carries the reason
    instead of "ok", and storing that would let one transient 403 report a connector-less stack for a
    whole hour -- the same trap `_LABELS_PROVISIONAL` exists for, where an unreadable half is
    indistinguishable from an empty one.

    `fetched_profiles` is a `_fetch_connector_profiles()` result -- (profiles, secrets) -- that the caller
    already holds (`hr` reads it to find the HR connectors), so a cache miss doesn't read the profile
    endpoint a second time. The secrets travel with it because the run messages are scrubbed of them.
    """
    ck = {"max_failures": max_failures, "max_other": max_other}
    cached, age = (None, None) if refresh else rescache_get("connectors", RESCACHE_CONNECTOR_TTL, **ck)
    if cached is not None:
        out = _brief_connectors(cached, max_warnings, max_detail) if brief else cached
        return _stamp_cache(out, age)
    result = {"fetched": {}}
    profiles, runs, actions, pipeline, runs_truncated = [], {}, [], [], False
    # Two independent endpoints, ~0.6s each against a remote stack - fetch them at the same time.
    fetch_profiles = (lambda: fetched_profiles) if fetched_profiles is not None else _fetch_connector_profiles
    pr, rn = parallel([fetch_profiles, _fetch_connector_runs])
    secrets = set()
    if isinstance(pr, Exception):
        result["fetched"]["profiles"] = _short(str(pr), 160)
    else:
        (profiles, secrets), result["fetched"]["profiles"] = pr, "ok"
    if isinstance(rn, Exception):
        result["fetched"]["ingestion"] = _short(str(rn), 160)
    else:
        runs, actions, pipeline, runs_truncated = rn
        result["fetched"]["ingestion"] = "ok"
        for r in list(runs.values()) + list(actions) + list(pipeline):
            r["message"] = _short(_scrub(r.get("message"), secrets))
    del secrets   # credential values: nothing below may carry them into the result or the cache

    runs_ok = result["fetched"].get("ingestion") == "ok"
    owner = {}  # service name -> the configured connector that owns it (labels ingestion runs)
    for p in profiles:
        for s in p["services"]:
            owner.setdefault(s["service"], p)

    connectors, failures, claimed, claimed_svcs = [], [], set(), set()
    for p in profiles:
        enabled = [s for s in p["services"] if s["enabled"]]
        if not enabled:
            continue  # configured but switched off - not part of the data picture
        passing = [s for s in enabled if _test_health(s["testStatus"]) == "ok"]
        bad = [s for s in enabled if _test_health(s["testStatus"]) == "fail"]
        matched, mismatch = [], False
        for s in enabled:
            r = runs.get((s["service"], p["profile"]))
            if r is None:
                # A run can be filed under a differently named profile ('Default profile') than the
                # one configured today; fall back to the newest run for that service either way.
                cand = [v for k, v in runs.items() if k[0] == s["service"]]
                if cand:
                    r, mismatch = max(cand, key=lambda v: v["_time"]), True
            if r is not None:
                matched.append(r)
                claimed.add((r["service"], r["profile"]))
                claimed_svcs.add(r["service"])
        row = {"connector": p["connector"], "profile": p["profile"], "group": p["group"],
               "servicesEnabled": len(enabled), "servicesPassing": len(passing),
               "servicesFailing": len(bad)}
        ing = _last_ingest(matched) if matched else None
        if ing:
            row["lastIngest"] = ing
            if mismatch:
                row["ingestProfileInferred"] = True  # matched on service name, not profile name
        if bad and not passing:
            row["health"] = "failing"
        elif bad or (ing and ing["health"] != "ok"):
            row["health"] = "degraded"
        elif not matched:
            # Enabled and green with no run on record - but only call that idle if the run history
            # was actually readable; a blocked metrics endpoint isn't evidence of nothing ingesting.
            row["health"] = "idle" if runs_ok else "ok"
        else:
            row["health"] = "ok"
        connectors.append(row)
        for s in bad:
            failures.append({"connector": p["connector"], "profile": p["profile"],
                             "service": s["displayName"], "kind": "connection-test",
                             "status": s["testStatus"], "message": s["message"]})

    other, other_index = [], {}
    for key, r in runs.items():
        own = owner.get(r["service"])
        label = own["connector"] if own else _pretty_name(r["service"])
        # Skip runs already reported on a connector row - including the same service under another
        # profile name, which would otherwise list one connector twice.
        if key not in claimed and r["service"] not in claimed_svcs:
            o = other_index.get((label, r["profile"]))
            if o is None:
                o = {"source": label, "profile": r["profile"], "services": 0, "records": 0,
                     "status": r["status"], "health": "ok", "utc": r["utc"], "_time": 0,
                     "configured": bool(own)}
                other_index[(label, r["profile"])] = o
                other.append(o)
            o["services"] += 1
            o["records"] += r["records"] or 0
            if _run_health(r["status"]) == "fail" or (_run_health(r["status"]) == "warn" and o["health"] == "ok"):
                o["health"], o["status"] = _run_health(r["status"]), r["status"]
            if r["_time"] > o["_time"]:
                o["_time"], o["utc"] = r["_time"], r["utc"]
        if _run_health(r["status"]) == "fail":
            failures.append({"connector": label, "profile": r["profile"], "service": r["service"],
                             "kind": "ingestion", "status": r["status"], "message": r["message"]})
    other.sort(key=lambda o: -(o["records"] or 0))
    for o in other:
        o.pop("_time", None)
    failures = _group_failures(failures)

    ingest_records = sum((r["records"] or 0) for r in runs.values())
    newest = max((r["_time"] for r in runs.values()), default=0)
    newest_utc = next((r["utc"] for r in runs.values() if r["_time"] == newest), None) if newest else None
    tally = {h: sum(1 for c in connectors if c["health"] == h) for h in ("ok", "degraded", "failing", "idle")}
    result.update({
        "summary": {
            "connectorsEnabled": len(connectors),
            "servicesEnabled": sum(c["servicesEnabled"] for c in connectors),
            "servicesPassing": sum(c["servicesPassing"] for c in connectors),
            "servicesFailing": sum(c["servicesFailing"] for c in connectors),
            "healthy": tally["ok"], "degraded": tally["degraded"],
            "failing": tally["failing"], "idle": tally["idle"],
            "ingestingSources": len(runs), "recordsLastRun": ingest_records,
            "lastIngestUtc": newest_utc, "outboundActions": len(actions), "pipelineRuns": len(pipeline),
            "runHistoryTruncated": runs_truncated,
            "message": ("%d enabled connectors: %d healthy, %d degraded, %d failing, %d idle. "
                        "%d sources last ingested %s records%s.%s"
                        % (len(connectors), tally["ok"], tally["degraded"], tally["failing"], tally["idle"],
                           len(runs), "{:,}".format(ingest_records),
                           ", most recently %s" % newest_utc if newest_utc else "",
                           (" Run history exceeds %d pages -- some connectors' last-ingest data may be "
                            "incomplete." % CONNECTOR_RUNS_MAX_PAGES) if runs_truncated else "")),
        },
        "connectors": sorted(connectors, key=lambda c: (c["health"] != "failing", c["health"] != "degraded",
                                                        -(c.get("lastIngest", {}).get("records") or 0))),
        "failures": failures[:max_failures],
        "failuresTruncated": max(0, len(failures) - max_failures),
        "otherSources": other[:max_other],
        "otherSourcesTruncated": max(0, len(other) - max_other),
    })
    # Only a result with both halves read is worth remembering; see the docstring.
    if result["fetched"].get("profiles") == "ok" and result["fetched"].get("ingestion") == "ok":
        rescache_put("connectors", result, **ck)
    return _brief_connectors(result, max_warnings, max_detail) if brief else result


def _brief_connectors(result, max_warnings=12, max_detail=10):
    """Invert the coverage summary message-first, keeping every connector row.

    Measured on a live 58-connector stack: 50 of the 58 are ingesting *and* carrying a warning, and
    only 8 are in `failures[]`. So the shape that dominated the payload was a per-row `notes[]`
    repeating 143 note instances drawn from just 67 distinct messages -- one message appeared on 22
    separate connectors, and 8,892 of 18,023 message chars were byte-identical repeats. Quoting the
    same sentence 22 times is not just 22x the tokens, it also buries the actual finding, which is
    that 22 connectors share one cause.

    So `notes[]` moves off the row and becomes `warningGroups[]`, one entry per distinct message
    naming the connectors it affects. That is what SKILL.md §1.5 asks to be narrated ("quote the
    cleaned message next to the connector") and it now reads directly off the group instead of being
    reassembled from 50 rows.

    What is deliberately NOT dropped:

    * **Every connector row survives.** `_snapshot_coverage` derives `failingNames`/`degradedNames`
      from these rows and asserts `len(failingNames) == failing`; dropping a row would break that
      invariant and make a trend report connectors as entering and leaving coverage that never moved.
      Rows only get slimmer, never fewer.
    * **`connector`, `profile` and `health`** -- `_coverage_ident()` needs the first two and the
      trend layer needs the third.
    * **`summary` and `failures[]` verbatim** -- the digest renderer reads only those two, and the
      tri-state tally has to stay as-is for `design/trends.md`'s comparison.

    Dropped: `group` (nothing narrates it) and `lastIngest.sources`/`lastIngest.health` (the row's own
    `health` already carries the verdict, and `warningGroups` carries the cause).

    Two things are capped, both the way `failures[]`/`otherSources[]` already are -- a count of what
    was left out, never a silent trim:

    * `warningGroups` keeps the `max_warnings` worst/biggest causes. §1.5 narrates ~8 delivering rows
      and ~8 needing attention, so quoting all 67 distinct causes buys nothing and costs the most
      expensive third of the payload.
    * only the failing/degraded rows and the `max_detail` biggest ingesters keep `lastIngest`. The
      rest are the rollup line ("+ N more delivering data"), which needs a name and a record count,
      not a timestamp and a status string.

    Every row that warned keeps `warned: fail|warn` whether or not its cause survived the cap, because
    a row with no warning marker has to mean "the run was actually clean" -- §1.5 depends on that
    reading, and it is the same rule as everywhere else here: an elided detail is never an absence.
    """
    rows = result.get("connectors") or []
    failing_idents = {(f.get("connector"), f.get("profile")) for f in (result.get("failures") or [])}

    ids, groups = {}, []
    for row in rows:
        row.pop("group", None)
        ing = row.get("lastIngest")
        if not ing:
            continue
        notes = ing.pop("notes", None)
        ing.pop("sources", None)
        ing.pop("health", None)
        worst = None
        for n in notes or []:
            sev = n.get("severity")
            if sev == "fail" or worst is None:
                worst = sev if sev == "fail" else (worst or sev)
            msg = n.get("message") or "(no message reported)"
            key = (sev, msg)
            g = ids.get(key)
            if g is None:
                g = {"severity": sev, "message": msg, "connectors": []}
                ids[key] = g
                groups.append(g)
            # The connector's own identity, so a group reads as "these 22 connectors, this cause".
            # Qualified the same way _coverage_ident does it, so the two agree on what a row is.
            g["connectors"].append(_coverage_ident(row))
        if worst:
            row["warned"] = worst

    for g in groups:
        g["connectorCount"] = len(g["connectors"])
    # Worst first, then biggest group -- same ordering _last_ingest and _group_failures already use,
    # so a reader does not learn a third convention.
    groups.sort(key=lambda g: (g["severity"] != "fail", -g["connectorCount"]))
    kept = groups[:max_warnings]
    result["warningGroups"] = kept
    result["warningGroupsTruncated"] = max(0, len(groups) - max_warnings)
    # Instances, not distinct causes: "the warning on this many connectors is not quoted above".
    result["warningsUndetailed"] = sum(g["connectorCount"] for g in groups[max_warnings:])

    # `rows` is already sorted failing, then degraded, then biggest ingester first, so the detail
    # window is a prefix -- plus every row with a hard failure, wherever it sorted.
    detailed, delivering = 0, 0
    for row in rows:
        # A hard failure is always detailed, and does NOT spend the delivering-row budget: §1.5 wants
        # both tables populated, and the failing rows sort first, so charging them to the same counter
        # left the delivering table with two rows instead of max_detail.
        failed = (row["connector"], row["profile"]) in failing_idents
        if failed:
            keep = True
        else:
            keep = delivering < max_detail
            delivering += 1 if keep else 0
        if keep:
            detailed += 1
        elif "lastIngest" in row:
            ing = row["lastIngest"]
            # Records survive: the rollup's job is to show the value the stack is already getting.
            row["lastIngest"] = {"records": ing.get("records")}
            row.pop("ingestProfileInferred", None)
    result["connectorsDetailed"] = detailed
    result["shape"] = "brief"
    return result


def _preflight_coverage(cov, detail=10):
    """Roll the connector rows SKILL.md will not print into a compact `delivering[]` list.

    `connect --with-connectors` is the one call SKILL.md makes mandatory on every session, so its
    payload is pure overhead on whatever the user actually asked -- and §1.5 already caps what it
    renders at roughly 6-8 delivering rows plus a rollup line, and 6-8 needing attention. Measured on
    a live 58-connector stack, it was handed 58 full rows to print about 18 of them: 8,377 of the
    block's 12,933 chars described connectors that reach the answer only as "+ 40 more delivering
    data: ...". This aligns the payload with the contract rather than changing it -- §1.5 needs no
    edit, because every row it draws arrives unchanged.

    Kept intact, and which §1.5 rule each one serves:

    * **Every row in `failures[]`, plus anything `failing` outright** -- the needs-attention list.
      The split is `failures[]` membership recomputed here, exactly as §1.5's dot rule computes it,
      so a row can never move between the two lists by being reshaped.
    * **Every delivering row that still carries `lastIngest.status`** -- these are `_brief_connectors`'
      `max_detail` biggest ingesters, and they are precisely the rows the delivering table draws with
      their Records and Last-ingest columns. Rolling one up would blank a cell §1.5 prints.
    * **`summary`, `otherSources[]` and every count, verbatim**, and every fact in `failures[]` and
      `warningGroups[]` (re-referenced by `_index_warning_groups`, never dropped). warningGroups is
      how §1.5 rule 2 answers "what was the warning" for a row it does print, and the tally has to
      stay as-is for design/trends.md's comparison.

    A rolled-up row keeps `connector`, `profile`, its record count and `warned`. Names, because §1.5
    requires the rollup line to name connectors and says a bare count is not actionable; `profile`,
    because one connector routinely has several and the ident is (connector, profile); `warned`,
    because a row with no warning marker has to mean the run was clean -- the same rule
    `_brief_connectors` keeps, and the one mistake this shape could cause that the old one could not.

    Dropped from a rolled-up row: the service tallies and `health`. Both are implied for a row in this
    set -- it is here *because* it has no `failures[]` entry and is not `failing` -- and neither
    appears in the rollup line.

    Then `_index_warning_groups` states each warning cause once, by id, instead of repeating
    connector names inside `warningGroups[]` and messages inside `failures[]`.

    Presentation only, and at one boundary only: `cmd_connect`'s. `digest`/`snapshot` call
    `summarize_connectors()` in-process and never see this, so `_snapshot_coverage`'s
    len(failingNames) == failing invariant is untouched. Nothing parses this verb's stdout.
    """
    if not isinstance(cov, dict):
        return cov
    rows = cov.get("connectors")
    if not isinstance(rows, list) or not rows:
        return cov
    failing_idents = {_coverage_ident(f) for f in (cov.get("failures") or [])}

    keep, rolled = [], []
    for row in rows:
        attention = _coverage_ident(row) in failing_idents or row.get("health") == "failing"
        ing = row.get("lastIngest") or {}
        if attention or ing.get("status"):
            keep.append(row)
            continue
        slim = {"connector": row.get("connector"), "profile": row.get("profile")}
        if ing.get("records") is not None:
            slim["records"] = ing["records"]
        if row.get("warned"):
            slim["warned"] = row["warned"]
        rolled.append(slim)

    out = dict(cov)
    if rolled:
        out["connectors"] = keep
        out["delivering"] = rolled
        out["deliveringRolledUp"] = len(rolled)
        # Said in the payload as well as in this docstring: the reader of a preflight block is a
        # model deciding whether it has the whole picture, and "40 rows are over there in a shorter
        # form" is not something it should have to infer from a key name.
        out["deliveringNote"] = (
            "%d connectors with no failure and no elided-detail row are in `delivering[]` with their "
            "record counts and `warned` markers -- named, not dropped. Run `connectors` for full rows."
            % len(rolled))
    indexed = _index_warning_groups(out)
    if not rolled and not indexed:
        return cov
    out["shape"] = "preflight"
    return out


def _index_warning_groups(out):
    """State each warning cause once: groups get an `id`, and whatever names a cause points at it.

    Measured on the local 51-connector stack, after the rollup above: `warningGroups[].connectors`
    repeated 84 mentions of 51 connector idents -- 3,857 chars, the largest single block left in the
    preflight -- because every name already appears on its own row in `connectors[]`/`delivering[]`.
    And 7 of 10 `failures[]` messages were byte-identical to a warning group's message, since an
    ingestion run that fails is both a hard failure and a `fail`-severity cause. Each group now
    carries `id` ("w1", ...) and no member list; each row and each such failure carries
    `warningIds`. Measured on the same stack and data: 18,810 -> 15,693 chars (-16.6%), and the
    original groups and failure messages rebuild exactly from the ids.

    Only when it is smaller. A reference costs ~22 chars per row, a name ~4 plus its length, so on a
    stack of short idents ("Okta (Prod)") the index would grow the block it exists to shrink. The
    two serialisations are compared and the smaller one kept; SKILL.md reads either (a group's
    `connectors[]` is the `connectors` verb's shape anyway).

    Lossless or not at all. The member lists are rebuilt from rows, so a group ident that matches no
    row would silently lose its membership -- in that case nothing is indexed and the block passes
    through verbatim. `warned` stays on every row that has it: a row with `warned` and no
    `warningIds` is still one whose cause lost the `warningGroups` cap, never a clean run.

    Copies everything it changes. The caller's `cov` is summarize_connectors()' own result, and
    _snapshot_coverage must be able to derive the same coverage from it afterwards.
    """
    groups = out.get("warningGroups")
    if (not isinstance(groups, list) or not groups
            or not all(isinstance(g, dict) and isinstance(g.get("connectors"), list) for g in groups)):
        return False
    row_lists = {k: out[k] for k in ("connectors", "delivering") if isinstance(out.get(k), list)}
    idents = {_coverage_ident(r) for rows in row_lists.values() for r in rows}

    ids, by_message, slim = {}, {}, []
    for i, g in enumerate(groups, 1):
        gid = "w%d" % i
        if not set(g["connectors"]) <= idents:
            return False
        slim.append(dict({"id": gid}, **{k: v for k, v in g.items() if k != "connectors"}))
        for name in g["connectors"]:
            ids.setdefault(name, []).append(gid)
        # Groups are sorted fail-first, so a message carried at both severities resolves to the fail
        # one -- the right reading for a hard failure.
        by_message.setdefault(g.get("message"), gid)

    new = {"warningGroups": slim}
    for key, rows in row_lists.items():
        new[key] = [dict(r, warningIds=ids[_coverage_ident(r)]) if _coverage_ident(r) in ids else r
                    for r in rows]
    if isinstance(out.get("failures"), list):
        fails = []
        for f in out["failures"]:
            gid = by_message.get(f.get("message")) if isinstance(f, dict) and f.get("message") else None
            if gid:
                f = {k: v for k, v in f.items() if k != "message"}
                f["warningIds"] = [gid]
            fails.append(f)
        new["failures"] = fails
    new["warningIdsNote"] = (
        "Each cause is quoted once, in warningGroups[] by `id`; rows in connectors[]/delivering[] and "
        "entries in failures[] point to theirs via `warningIds`. `warned` with no `warningIds` means "
        "the cause was not quoted (warningsUndetailed), not a clean run.")
    # The note counts against the index: it only exists because the index does.
    size = lambda d: len(json.dumps({k: d.get(k) for k in new if k in d}))
    if size(new) >= size(out):
        return False
    out.update(new)
    return True


def _connectors_view(cov, full=False):
    """What `connectors` prints: the coverage block, with each warning cause stated once.

    The same lossless index the preflight uses (_index_warning_groups): measured on a live stack it
    took ~3,000 chars (~745 tokens) off every mid-session `connectors` call, and SKILL.md already
    reads both shapes. `--full` stays verbatim, since asking for everything means everything. A copy
    is indexed, never `cov` itself: it is summarize_connectors()' own result and may be the cached one.
    """
    if full or not isinstance(cov, dict):
        return cov
    out = dict(cov)
    return out if _index_warning_groups(out) else cov


def cmd_connectors(a):
    # The stamp is read fresh even when the coverage block comes from its hour-long cache: connector
    # health may be an hour old, but "which LDG are the answers about" must never be.
    jout(with_currency(["asset", "user"],
                       lambda: _connectors_view(
                           summarize_connectors(a.max_failures, a.max_other, brief=not a.full,
                                                max_warnings=a.max_warnings, max_detail=a.max_detail,
                                                refresh=a.refresh), full=a.full)))


# The HR / HCM systems in Meridian's connector catalog (/CMDB/v2/connector), by bridge_name, taken from
# the live 505-entry catalog on 2026-09-23. A fixed list, because nothing in the API says "HR":
#   * the catalog files every one of these under "Identity Access Management", beside Okta, Entra and
#     the PAM tools, so the `group` a connector row carries cannot tell an HR system from an IdP;
#   * product, bridge and data names diverge -- Dayforce is bridge `ceridian` and its records are
#     `dayforce_employee` -- so a search for the name the user said finds nothing;
#   * the descriptions don't separate them either: a regex for HR/HCM/payroll/workforce also matched
#     HYPR Passwordless and five connectors outside the group.
# Together those made "do we have HR data?" come back "no" on stacks with an HR connector enabled.
# iCIMS is deliberately absent: applicant tracking holds candidates, not employees, and counting it
# would answer a manager question with people who do not work there yet.
HR_BRIDGES = {"adp": "ADP", "bamboohr": "BambooHR", "ceridian": "Dayforce", "hibob": "HiBob",
              "sage_people": "Sage People", "ukg_pro": "UKG", "workday": "Workday"}
# Name tokens that mark a user-table field as HR-derived: SmartLabels the customer named after the
# system (say `Workday_Department_SmartLabel`), matched per underscore-separated token, lowercased.
# Per-source `alias_<sourcetype>_*` copies are matched by prefix from the configured services.
HR_FIELD_TOKENS = {"adp", "bamboohr", "ceridian", "dayforce", "hibob", "sage", "ukg", "workday"}


def _hr_field_names(fields, sourcetypes):
    prefixes = tuple("alias_%s_" % s for s in sourcetypes)
    out = []
    for name in fields or ():
        tokens = {t for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if t}
        if (prefixes and name.startswith(prefixes)) or tokens & HR_FIELD_TOKENS:
            out.append(name)
    return sorted(out)


def _hr_where(sourcetypes):
    if not sourcetypes:
        return None
    if len(sourcetypes) == 1:
        return "sourcetype match List %s" % sourcetypes[0]
    return "sourcetype in List %s" % ",".join(sourcetypes)


def hr_sources(refresh=False):
    """Which HR systems feed this stack, and how many user records each actually carries.

    The deterministic answer to "do we have HR data?". Configured HR connectors come from the profile
    endpoint (by bridge_name, see HR_BRIDGES). Their enabled services ARE the `sourcetype` values their
    records carry -- `jira_user`, `intune_user`, `knowbe4_user` all match one-for-one -- so each is
    counted EXACTLY with its own query, plus one OR query for the people any HR source knows (a person
    in two HR systems is one record). Not from `summary --by sourcetype`: that samples to discover
    values, and on a real stack where one source held over 90% of the user records, the sample never
    reached a small HR source. It says so (`unaccountedRecords`), but it reads as "not present".

    `state`: has_data / configured_no_data / configured_disabled / none_configured / unknown. Only
    none_configured means "no HR system here", and only when the profile endpoint was actually read;
    an unreadable endpoint or a failed count is `unknown`, never a zero.
    """
    result = {"recognized": sorted(HR_BRIDGES.values()), "fetched": {}}
    try:
        # The credential values ride along only to summarize_connectors, which scrubs run messages of
        # them; nothing in this function reads them, and `fetched_profiles` is dropped once it's done.
        fetched_profiles = _fetch_connector_profiles()
        profiles = fetched_profiles[0]
        result["fetched"]["profiles"] = "ok"
    except Exception as e:
        result["fetched"]["profiles"] = _short(str(e), 160)
        result.update(state="unknown", systems=[], hrSourcetypes=[], userRecords=None, where=None,
                      summary="Could not read the connector profiles, so whether an HR system is "
                              "configured is unknown -- which is not the same as none.")
        return result
    by_name = {v.lower(): k for k, v in HR_BRIDGES.items()}
    hr = [p for p in profiles
          if p.get("bridge") in HR_BRIDGES or (p.get("connector") or "").lower() in by_name]
    health = {}

    def health_then_fields():
        # Neither needs the counts, so both run beside them. They stay in sequence with each other: a
        # changed field set drops the result cache, and summarize_connectors writes to that same file,
        # so run concurrently the drop could land between its read and its write and be undone.
        #
        # An HR connector's own fields appear when it is enabled, i.e. after a field cache was likely
        # written -- and a stale list here would hide exactly the fields this verb exists to point at.
        try:
            health.update({(r.get("connector"), r.get("profile")): r
                           for r in summarize_connectors(brief=False, refresh=refresh,
                                                         fetched_profiles=fetched_profiles).get("connectors", [])})
            result["fetched"]["health"] = "ok"
        except Exception as e:
            result["fetched"]["health"] = _short(str(e), 160)
        _refetch_field_map("user")

    systems, sts = [], []
    for p in hr:
        bridge = p.get("bridge") if p.get("bridge") in HR_BRIDGES else by_name.get((p.get("connector") or "").lower())
        enabled = [s["service"] for s in p.get("services") or [] if s.get("enabled") and s.get("service")]
        row = {"system": HR_BRIDGES.get(bridge) or p.get("connector"), "connector": p.get("connector"),
               "profile": p.get("profile"), "bridge": p.get("bridge"),
               "servicesEnabled": len(enabled), "servicesDisabled": len(p.get("services") or []) - len(enabled),
               "sourcetypes": [{"sourcetype": s} for s in enabled]}
        systems.append(row)
        sts += [s for s in enabled if s not in sts]

    def count(q):
        return count_records("user", q)

    def match(s):
        return {"searchFieldName": "sourcetype", "operator": "match", "type": "List", "value": s}

    tasks = [(lambda s=s: count([[match(s)]])) for s in sts]
    if len(sts) > 1:
        tasks.append(lambda: count([[match(s) for s in sts]]))
    side = [health_then_fields] if hr else []
    got = parallel(tasks + side) if tasks or side else []
    fetched_profiles = None     # the credential values: nothing past this point may reach them
    if side and isinstance(got[-1], Exception):
        raise got[-1]      # only a bug gets here: health failures are caught inside
    got = got[:len(tasks)]
    for p, row in zip(hr, systems):
        h = health.get((p.get("connector"), p.get("profile")))
        if h:
            row["health"] = h.get("health")
            if h.get("lastIngest"):
                row["lastIngest"] = h["lastIngest"]
    per = dict(zip(sts, got[:len(sts)]))
    for row in systems:
        for st in row["sourcetypes"]:
            n = per.get(st["sourcetype"])
            if isinstance(n, Exception):
                st["userRecords"], st["error"] = None, _short(str(n), 160)
            else:
                st["userRecords"] = n
    union = got[-1] if len(sts) > 1 else (got[0] if got else None)
    result["userRecords"] = None if isinstance(union, Exception) or not sts else union

    counts = [st["userRecords"] for r in systems for st in r["sourcetypes"]]
    names = ", ".join(sorted({r["system"] for r in systems}))
    if not hr:
        state = "none_configured"
        summary = ("No HR system is configured on this stack (recognized: %s). Manager, department and "
                   "title fields may still be filled by an identity provider." % ", ".join(result["recognized"]))
    elif not sts:
        state = "configured_disabled"
        summary = "%s is configured, but every service on it is switched off." % names
    elif any(isinstance(c, int) and c > 0 for c in counts):
        state = "has_data"
        summary = "%s: %s user records carry HR data." % (
            names, "{:,}".format(result["userRecords"]) if isinstance(result["userRecords"], int) else "some")
        bad = sorted({r["system"] for r in systems if r.get("health") in ("failing", "degraded")})
        if bad:
            summary += " %s is not ingesting cleanly, so its data may be stale -- see lastIngest." % ", ".join(bad)
    elif any(c is None for c in counts):
        state = "unknown"
        summary = "%s is configured, but its records could not be counted -- unknown, not zero." % names
    else:
        state = "configured_no_data"
        summary = ("%s is configured but no user records carry its data -- the connector is not "
                   "delivering. See each system's health and lastIngest for why." % names)
    result.update(state=state, summary=summary, systems=systems, hrSourcetypes=sts, where=_hr_where(sts))
    m = load_field_map("user")
    result["hrFields"] = _hr_field_names(m, sts) if m else None
    if not m:
        result["hrFieldsNote"] = "user field metadata unreachable; HR-named fields unknown"
    return result


def cmd_hr(a):
    jout(with_currency(["user"], lambda: hr_sources(refresh=a.refresh)))


def classify_connect_error(err_str):
    """Map a call() error string to (state, http_status). Shared by cmd_connect, cmd_check and the
    smoke tests so the classification has one source of truth.

    ANCHORED on the "HTTP <code>: " prefix call() produces, which is what makes it safe for cmd_check
    to use: an unanchored search would find a status-like number anywhere in the response body, and
    these bodies carry error sub-codes (`"code":403001`) that would classify a 500 as FORBIDDEN.
    Falls back to an unanchored search so a wrapped or prefixed message still classifies rather than
    reading as `unreachable`, but the anchored match wins."""
    m = re.match(r"HTTP (\d{3}):", err_str or "") or re.search(r"\bHTTP (\d{3})\b", err_str or "")
    status = int(m.group(1)) if m else None
    state = ("auth_error" if status == 401 else "forbidden" if status == 403
             else "http_error" if status else "unreachable")
    return state, status


def diagnose_connection():
    """Resolve config, validate in one cheap call, and RETURN a single-state result dict.
    Never includes the token - only its source and last 4 chars.
    Shared by cmd_connect (prints it) and cmd_stacks_switch (annotates + prints it)."""
    # Resolve config WITHOUT load_config()'s die(), so 'not_configured' reports cleanly.
    fqdn = os.environ.get("MERIDIAN_FQDN")
    api_token = os.environ.get("MERIDIAN_API_TOKEN"); token_src = "env" if api_token else None
    action_token = os.environ.get("MERIDIAN_ACTION_TOKEN")
    if os.path.exists(CFG_PATH):
        try:
            with open(CFG_PATH, encoding="utf-8-sig") as f:
                c = json.load(f)
        except Exception:
            c = {}
        if not fqdn and c.get("fqdn"): fqdn = c["fqdn"]
        if not api_token and c.get("api_token"): api_token, token_src = c["api_token"], "config"
        if not action_token and c.get("action_token"): action_token = c["action_token"]
    if fqdn:
        fqdn = re.sub(r"^https?://", "", fqdn).rstrip("/")
    last4 = api_token[-4:] if api_token else None

    missing = ([] if fqdn else ["fqdn"]) + ([] if api_token else ["api_token"])
    if missing:
        return {
            "state": "not_configured", "fqdn": fqdn, "missing": missing,
            "configPath": CFG_PATH, "configExists": os.path.exists(CFG_PATH),
            "message": "Missing %s - Meridian isn't connected yet." % " and ".join(missing),
            "hint": "Collect the FQDN and a User Generated API token, then offer to save them to config.",
        }

    base = {"fqdn": fqdn, "tokenSource": token_src, "tokenLast4": last4,
            "actionTokenConfigured": bool(action_token), "configPath": CFG_PATH,
            # Reported on every connect: an opt-out set once in config would otherwise apply to
            # every later session silently, which is exactly how an insecure posture becomes default.
            "tlsVerified": not insecure_tls()}
    if insecure_tls():
        base["tlsWarning"] = ("Certificate verification is DISABLED for this stack (insecure_tls). "
                              "The API token is sent over an unverified connection - only keep this "
                              "if the certificate is self-signed and the network path is trusted.")
    try:
        out = call("GET", "/CMDB/v2/system/metrics/data", retries=0)
        base.update({"state": "connected", "assetCount": out.get("assetCount"), "userCount": out.get("userCount"),
                     "message": "Connected to %s - tracking %s assets and %s users." % (fqdn, out.get("assetCount"), out.get("userCount"))})
        return base
    except Exception as e:  # noqa
        s = str(e)
    state, status = classify_connect_error(s)
    msgs = {"auth_error": "Reached %s but the token was rejected (HTTP 401)." % fqdn,
            "forbidden": "Reached %s but this token is forbidden (HTTP 403)." % fqdn,
            "unreachable": "Couldn't reach %s (DNS/TLS/network error)." % fqdn,
            "http_error": "Reached %s but got HTTP %s." % (fqdn, status)}
    hints = {"auth_error": "Token expired/mistyped, account missing the Api_Users role, or an SSO account (SSO cannot use the API). Re-prompt for a fresh User Generated token.",
             "forbidden": "Usually a scoped/Limited token or a missing role. Try a full User Generated token.",
             "unreachable": "Check the FQDN is exact (no https://, no path) and the stack is reachable from this network. Re-prompt for the FQDN.",
             "http_error": "Inspect `detail` for the API error body."}
    base.update({"state": state, "httpStatus": status, "message": msgs[state], "hint": hints[state], "detail": s[:300]})
    return base


def cmd_asof(a):
    """When the LDG was last rebuilt, and nothing else: the one-call check before reusing Meridian
    data that is already in the conversation (design/data-currency.md, SKILL.md rule 5)."""
    stamp = ldg_rebuild()
    out = dict(stamp)
    out["dataCurrency"] = data_currency(stamp, [t for t, _ in LDG_MERGERS])
    cur = out["dataCurrency"]
    if cur["class"] == "current":
        at = cur["ldgRebuiltUtc"]
        out["message"] = ("Data as of %s (latest Meridian rebuild)" % currency_label_utc(at["asset"])
                          if at["asset"] == at["user"] else "Assets as of %s; users as of %s"
                          % (currency_label_utc(at["asset"]), currency_label_utc(at["user"])))
    else:
        out["message"] = "Data currency could not be confirmed: %s" % cur["reason"]
    jout(out)


def cmd_connect(a):
    """Onboarding preflight: resolve config, validate in one cheap call, report a single state.
    With --with-connectors, append the data-coverage summary the skill opens a session with. The
    summary is best-effort: a scoped token that can't read the connector endpoints still connects."""
    want = getattr(a, "with_connectors", False)
    if not want:
        out, stamp = parallel([diagnose_connection, ldg_rebuild])
        if isinstance(out, Exception):
            raise out
        if out.get("state") == "connected":
            out = attach_currency(out, data_currency(stamp, ["asset", "user"]))
        jout(out); return
    # The coverage lookup doesn't depend on the validation call, and this runs on the first question
    # of every session - so all three round trips go out together instead of one after another. On a
    # bad token the coverage result is simply discarded.
    fresh = getattr(a, "refresh", False)
    out, cov, stamp = parallel([diagnose_connection, lambda: summarize_connectors(refresh=fresh), ldg_rebuild])
    if isinstance(out, Exception):
        raise out
    if out.get("state") == "connected":
        # Read fresh, never derived from the coverage block: that block is cached for an hour, and a
        # stamp taken from it would be up to an hour stale -- the exact failure this field exists for.
        out = attach_currency(out, data_currency(stamp, ["asset", "user"]))
        if isinstance(cov, Exception):
            out["connectors"] = {"unavailable": _short(str(cov), 200)}
        elif getattr(a, "coverage_full", False):
            out["connectors"] = cov
        else:
            out["connectors"] = _preflight_coverage(cov)
    jout(out)


def _env_override_warning():
    """Switching writes config.json, but env vars win over it - warn if they'd mask the switch."""
    masked = [v for v in ("MERIDIAN_FQDN", "MERIDIAN_API_TOKEN") if os.environ.get(v)]
    if masked:
        return ("Environment variable(s) %s are set and override config.json - the switch won't take "
                "effect until you clear them." % " and ".join(masked))
    return None


def cmd_stacks_list(a):
    reg = load_stacks()
    rows = []
    for name, s in reg.get("stacks", {}).items():
        tok = s.get("api_token") or ""
        rows.append({"name": name, "active": name == reg.get("active"), "fqdn": s.get("fqdn"),
                     "tokenLast4": tok[-4:] if tok else None, "actionToken": bool(s.get("action_token"))})
    jout({"active": reg.get("active"), "storePath": STACKS_PATH, "stacks": rows})


def _resolve_token_arg(tok):
    """The token for `stacks add`, from the safest available channel.

    A bare `--token <value>` lands the bearer token in shell history and OS process listings
    (`ps`, Windows 4688 command-line auditing), so `--token -` reads it from stdin and omitting
    the flag falls back to MERIDIAN_API_TOKEN -- neither of which is captured by either.
    """
    if tok == "-":
        tok = sys.stdin.readline().strip()
        if not tok:
            die("--token - was given but stdin was empty.")
        return tok
    if tok:
        return tok
    tok = (os.environ.get("MERIDIAN_API_TOKEN") or "").strip()
    if tok:
        return tok
    die("stacks add needs a token: --token <value>, --token - (read from stdin, keeping it out of "
        "shell history and process listings), or the MERIDIAN_API_TOKEN environment variable.")


def cmd_stacks_add(a):
    """Add or update ONE named stack without touching the others."""
    reg = load_stacks()
    fqdn = _clean_fqdn(a.fqdn)
    token = _resolve_token_arg(a.token)
    existed = a.name in reg["stacks"]
    # Merge into the existing entry, never replace it. A wholesale replacement dropped entity_salt
    # on a routine token rotation -- and a lost salt makes every earlier entity snapshot permanently
    # incomparable (the next capture regenerates one and diffs report the whole population as
    # appeared-and-disappeared). Keys this command wasn't given (the salt, a saved action token,
    # anything future) survive an update untouched.
    entry = dict(reg["stacks"].get(a.name) or {})
    entry["fqdn"] = fqdn
    entry["api_token"] = token
    action = a.action_token
    if action == "-":
        # Same stdin channel as `--token -`, for the more powerful of the two tokens, which had no
        # way to stay out of argv. Read AFTER the API token, so both can be piped as two lines.
        action = sys.stdin.readline().strip()
        if not action:
            die("--action-token - was given but stdin had no line for it (with --token - too, send "
                "the API token on the first line and the action token on the second).")
    if action:
        entry["action_token"] = action
    if getattr(a, "insecure_tls", False):
        # Set-only, like the action token: an update that omits the flag preserves the stored
        # posture rather than silently re-enabling verification on a stack that can't pass it.
        # Going back to verified TLS is `stacks rm` + re-add, a deliberate act.
        entry["insecure_tls"] = True
    reg["stacks"][a.name] = entry
    first = reg.get("active") is None
    if first:
        reg["active"] = a.name
    save_stacks(reg)
    if reg.get("active") == a.name:
        # First stack, or an update to the already-active one (token rotation): either way
        # config.json must reflect it now, not after the next `stacks switch`.
        mirror_active_to_config(reg)
    note = ("Saved and set active (first stack); config.json now points at it." if first
            else "Updated the active stack; config.json refreshed." if reg.get("active") == a.name
            else "Saved without touching other stacks. Run `stacks switch %s` to activate it." % a.name)
    jout({
        "saved": a.name, "updated": existed, "fqdn": fqdn, "active": reg["active"],
        "activatedNow": first, "storePath": STACKS_PATH, "note": note,
    })


def cmd_stacks_switch(a):
    """Make a saved stack active: mirror it into config.json, then validate the connection."""
    reg = load_stacks()
    if a.name not in reg.get("stacks", {}):
        avail = ", ".join(reg.get("stacks", {})) or "(none saved yet)"
        die("No stack named '%s'. Saved stacks: %s" % (a.name, avail))
    reg["active"] = a.name
    save_stacks(reg)
    mirror_active_to_config(reg)
    result = {"switchedTo": a.name}
    warn = _env_override_warning()
    if warn:
        result["warning"] = warn
    result.update(diagnose_connection())
    jout(result)


def cmd_stacks_rm(a):
    reg = load_stacks()
    if a.name not in reg.get("stacks", {}):
        die("No stack named '%s'." % a.name)
    was_active = reg.get("active") == a.name
    del reg["stacks"][a.name]
    if was_active:
        reg["active"] = next(iter(reg["stacks"]), None)
    save_stacks(reg)
    note = "Removed '%s'." % a.name
    if was_active:
        note += (" It was active; run `stacks switch %s` to point config.json at another stack." % reg["active"]
                 if reg.get("active") else " No stacks remain active - config.json is unchanged.")
    jout({"removed": a.name, "wasActive": was_active, "active": reg.get("active"),
                      "storePath": STACKS_PATH, "note": note})


def cmd_check(a):
    fqdn, api_token, action_token = load_config()
    probes = [
        ("System metrics", "GET", "/CMDB/v2/system/metrics/data", None, "assetCount"),
        ("Field metadata", "GET", "/CMDB/v2/data/metadata/asset", None, "metadata"),
        ("Data query", "POST", "/CMDB/v2/data/cmdb", {"table": "asset", "query": [[{"searchFieldName": "Asset_Name", "operator": "exists", "type": "String", "value": None}]], "paging": {"page": 0, "recordsPerPage": 1}}, "totalRecords"),
        ("Connectors", "GET", "/CMDB/v2/connector", None, "connectors"),
    ]
    # Four independent probes; none informs the next, so they go out together.
    results = parallel([(lambda m=method, e=ep, b=body: call(m, e, b, retries=0)) for _, method, ep, body, _ in probes])
    caps = []
    for (label, _, _, _, marker), out in zip(probes, results):
        if isinstance(out, Exception):
            # classify_connect_error anchors on the "HTTP <code>:" prefix. The old substring scan
            # searched the whole string, so a 500 whose JSON body carried an error sub-code like
            # "code":403001 was misreported as FORBIDDEN -- blaming the token for a server fault.
            state, code = classify_connect_error(str(out))
            st = {"auth_error": "UNAUTHORIZED", "forbidden": "FORBIDDEN"}.get(state) or \
                 ("NOT_FOUND" if code == 404 else "HTTP_ERROR" if state == "http_error" else "UNREACHABLE")
            caps.append({"area": label, "status": st} if code is None
                        else {"area": label, "status": st, "httpStatus": code})
        else:
            # All four probes return a dict at the top level (see api-reference.md), so this is a key
            # check, not a json.dumps()-and-substring-search over a payload that can be ~1MB.
            ok = isinstance(out, dict) and marker in out
            caps.append({"area": label, "status": "OK" if ok else "UNKNOWN"})
    jout({"fqdn": fqdn, "actionTokenConfigured": bool(action_token),
                      "tlsVerified": not insecure_tls(), "capabilities": caps})


# --- Self-update: keep an installed skill current with the public release -----------------------
#
# The skill ships as a zip extracted into ~/.claude/skills/meridiancs, so an install has no git
# remote to pull from and -- until this landed -- no way to know it was stale: the version lives in
# the release tag and does not survive into an extracted tree. So every packaged install now carries
# a generated VERSION.json (see make-package.py), SKILL.md runs `selfupdate` once per session, and an
# outdated install replaces itself from the release asset.
#
# Five rules hold this together. Four of them are the rule this file keeps re-learning in a new
# costume -- an absence must never read as an all-clear:
#
#   1. An unreachable check is `unknown`, never `current`. GitHub down, a corporate proxy, or the
#      anonymous 60/hr API budget must not be reported as "you are up to date", because the whole
#      point of the feature is that the user stops thinking about their version.
#   2. A working tree is never overwritten. ~/.claude/skills/meridiancs is a symlink to a checkout on
#      a maintainer's machine, so an updater that trusted its own path would delete the repo it was
#      built from. Four independent markers (a .git entry, CLAUDE.md, a symlinked install dir, a
#      missing or unusable version stamp) each veto an apply on their own.
#   3. Nothing is fetched from a host outside github.com. The repo is a pinned constant, the download
#      URL is host-validated, and there is deliberately NO "update from this URL" environment
#      variable -- that would be remote code execution for anything that can set an env var.
#   4. The Meridian bearer token never goes near this path, the same way _post_webhook keeps it away
#      from a chat webhook: no call(), no Authorization header, no pooled connection. The insecure_tls
#      escape hatch does NOT apply either -- that is a posture for one customer's stack certificate,
#      not a licence to fetch executable code over an unverified connection.
#   5. The swap is recoverable. The new tree is staged, smoke-tested by running its own --help, and
#      only then moved into place; the old tree is kept until that succeeds, and restored if it fails.

# The PUBLIC distribution repo, "owner/name". EMPTY MEANS THE FEATURE IS INERT: check_update returns
# `disabled` without touching the network, which is the correct behaviour while the public repo does
# not exist yet. It has to be filled in BEFORE the release that first ships this file, because an
# install can only self-update if the copy the user ALREADY HAS knows where to look -- shipping it
# empty means every user needs a second manual upgrade later.
UPDATE_REPO = "CyderesInc/MeridianCS"
INSTALL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL_REAL = os.path.realpath(INSTALL_DIR)
VERSION_PATH = os.path.join(INSTALL_REAL, "VERSION.json")
VERSION_SCHEMA = 1          # the stamp make-package.py writes; keys are additive-optional, as in snapshots
UPDATE_CHECK_CACHE_SCHEMA = 1   # separate from the stamp's: they version different files and must be
                                # free to move independently
UPDATE_CHECK_PATH = os.path.join(CFG_DIR, ".updatecheck")
UPDATE_CHECK_INTERVAL = 24 * 3600      # a launch-time check that hits the network every session is a
UPDATE_RETRY_INTERVAL = 3600           # tax on every question; a FAILED check retries far sooner, so
#                                        one outage cannot hide a release for a whole day
UPDATE_ASSET_SUFFIX = ".skill.zip"
UPDATE_TIMEOUT = 10
UPDATE_MAX_JSON_BYTES = 4 * 1024 * 1024
UPDATE_MAX_ASSET_BYTES = 64 * 1024 * 1024      # the package is <1MB; this is a runaway-download stop
UPDATE_MAX_REDIRECTS = 5    # a release download is one hop (github.com -> *.githubusercontent.com)
# Staging and held-copy folder prefixes, beside the install. Short on purpose: every staged file sits
# at <parent>/<prefix+8 random>/unpacked/meridiancs/<path>, and on Windows without long paths any path
# over 259 chars cannot be opened. ".meridiancs-update-" made staging 37 chars deeper than the install;
# this is 23. ".mcsp" + 8 is 13 chars against "meridiancs"' 10, so a held copy is barely deeper.
UPDATE_STAGING_PREFIX = ".mcsu"
UPDATE_HELD_PREFIX = ".mcsp"
WINDOWS_MAX_PATH = 259      # MAX_PATH (260) less the terminating NUL
UPDATE_MAX_UNPACKED_BYTES = 256 * 1024 * 1024  # ... and this is the zip-bomb stop
UPDATE_MAX_MEMBERS = 2000
UPDATE_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
UPDATE_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
# Members every package must contain. A zip missing any of these is not this skill, whatever the
# release says, and installing it would leave the user with no working skill at all.
UPDATE_REQUIRED_MEMBERS = ("SKILL.md", "scripts/meridian.py", "VERSION.json")
UPDATE_UA = "meridiancs-skill-selfupdate"


def _github_host(url, api=False):
    """The lowercase hostname of `url` if it is an https github.com host, else None.

    One place decides what "github.com" means, because two slightly different host checks in two
    functions is how one of them ends up permissive.
    """
    parts = urllib.parse.urlsplit(url or "")
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        return None
    ok = (host == "github.com" or host.endswith(".github.com")
          or host.endswith(".githubusercontent.com"))
    return host if ok else None


def _update_open(req):
    """urlopen for the self-update path, with EVERY redirect hop held to _github_host.

    _gh_get and _download_asset checked the URL they were handed and then let urlopen follow
    redirects wherever they pointed -- another host, or plain http://, since urllib follows both. A
    release download is always redirected (github.com -> a *.githubusercontent.com storage host), so
    the check that mattered was the one on the hop nobody looked at. Now each hop must pass the same
    rule as the first URL, and the chain is capped at UPDATE_MAX_REDIRECTS; the final URL is checked
    again after the fact, so a redirect path this handler somehow did not see still cannot land.
    Signed releases make a malicious package fail verification anyway; this keeps the bytes being
    verified coming only from where they claim to.
    """
    import urllib.request

    class _HeldRedirect(urllib.request.HTTPRedirectHandler):
        max_redirections = UPDATE_MAX_REDIRECTS

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not _github_host(newurl):
                raise ValueError("refusing a redirect to %r - self-update only follows https "
                                 "github.com hosts" % newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    resp = urllib.request.build_opener(_HeldRedirect).open(req, timeout=UPDATE_TIMEOUT)
    if not _github_host(resp.geturl()):
        resp.close()
        raise ValueError("self-update fetch ended at %r, which is not an https github.com host"
                         % resp.geturl())
    return resp


def update_repo():
    """The repo to check, or None if self-update is not configured.

    MERIDIAN_UPDATE_REPO overrides the constant so this is testable against a scratch repo, but it is
    validated to an owner/name pair: it selects a repo ON github.com and cannot introduce a host.
    """
    repo = (os.environ.get("MERIDIAN_UPDATE_REPO") or UPDATE_REPO or "").strip()
    return repo if repo and UPDATE_REPO_RE.match(repo) else None


def autoupdate_disabled():
    """Why self-update is switched off, or None. Env var, or "autoupdate": false in config.json.

    Read straight off disk rather than through load_config(), which die()s when no stack is
    configured -- the update check has to work before a user has connected to anything. utf-8-sig for
    the usual reason: an install upgraded from <=v2.1 can still have a BOM'd config on disk.
    """
    if os.environ.get("MERIDIAN_NO_AUTOUPDATE", "").strip() not in ("", "0", "false", "False"):
        return "MERIDIAN_NO_AUTOUPDATE is set"
    try:
        with open(CFG_PATH, encoding="utf-8-sig") as f:
            if json.load(f).get("autoupdate") is False:
                return "autoupdate is false in config.json"
    except Exception:  # noqa - a missing or unreadable config is not an opt-out
        pass
    return None


def parse_version(s):
    """"2.16.1" or "v2.16.1" -> (2, 16, 1). Anything else -> None, never a guess."""
    m = UPDATE_VERSION_RE.match((s or "").strip() if isinstance(s, str) else "")
    return tuple(int(g) for g in m.groups()) if m else None


def installed_version():
    """This install's VERSION.json as a dict, or None. Never invents a version for an unstamped tree."""
    try:
        with open(VERSION_PATH, encoding="utf-8-sig") as f:
            rec = json.load(f)
    except Exception:  # noqa
        return None
    return rec if isinstance(rec, dict) else None


def install_markers():
    """Reasons this install must NOT be overwritten. Any single one is disqualifying.

    Deliberately redundant. The one that must never fail is a maintainer's
    ~/.claude/skills/meridiancs symlinked to a git checkout, where an apply would delete a working
    tree holding unpushed commits -- so the symlink, the .git entry, the packaging-excluded CLAUDE.md
    and the absent version stamp are all checked, and any one of them alone is enough.

    The symlink test is `islink` and nothing more. It used to also flag realpath != abspath, which
    looks like a stronger version of the same question and is not: on Windows a TEMP or profile path
    can arrive in 8.3 short form (C:/Users/FIRSTN~1/...) that realpath expands, so a perfectly
    ordinary install was reported as "a link to" its own resolved path and never updated again. It
    failed safe and silently, which is the worst shape for a guard -- the feature would simply not
    work for those users and the reason would read as intentional.
    """
    real, reasons = INSTALL_REAL, []
    if os.path.islink(INSTALL_DIR):
        reasons.append("the install directory is a link to %s" % real)
    if os.path.exists(os.path.join(real, ".git")):
        reasons.append("it is a git checkout (.git is present)")
    if os.path.exists(os.path.join(real, "CLAUDE.md")):
        # CLAUDE.md is in make-package.py's EXCLUDE, so NO package contains it. Its presence means a
        # working tree or a hand-assembled copy -- either way not something a release may land on.
        reasons.append("CLAUDE.md is present, so this is a working tree rather than a package")
    rec = installed_version()
    if rec is None:
        reasons.append("there is no VERSION.json, so this copy's provenance is unknown")
    elif not parse_version(rec.get("version")):
        reasons.append("VERSION.json carries no usable version (%r)" % rec.get("version"))
    return reasons


def _gh_get(url, accept="application/vnd.github+json", max_bytes=UPDATE_MAX_JSON_BYTES):
    """GET a github.com URL over verified TLS and return the bytes.

    Independent of call() on purpose (rule 4 at the top of this section): no Authorization header, no
    pooled connection, and the platform's default VERIFYING ssl context -- never _ssl_context(),
    which honours this stack's insecure_tls opt-out. A self-signed Meridian appliance is a reason to
    skip verification for that stack's API; it is not a reason to accept an unverified certificate
    while downloading code that is about to run. urllib.request is imported here rather than at module
    scope for the same startup-cost reason as _post_webhook.
    """
    import urllib.request
    host = _github_host(url)
    if not host:
        raise ValueError("refusing to fetch %r - self-update only talks to https github.com hosts" % url)
    req = urllib.request.Request(url, headers={
        "Accept": accept,
        # GitHub rejects an anonymous API request with no User-Agent, and a named one makes this
        # traffic identifiable in a customer's proxy log rather than looking like a stray script.
        "User-Agent": UPDATE_UA,
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with _update_open(req) as resp:
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("response exceeded %d bytes" % max_bytes)
    return data


def latest_release(repo):
    """The newest release's version and package asset.

    /releases/latest is GitHub's newest non-draft, non-prerelease release, so tagging a prerelease
    cannot push it to every install by accident.
    """
    rel = json.loads(_gh_get("https://api.github.com/repos/%s/releases/latest" % repo).decode("utf-8"))
    tag = rel.get("tag_name") or ""
    ver = parse_version(tag)
    if not ver:
        raise ValueError("release tag %r is not vX.Y.Z, so it cannot be compared" % tag)
    assets = [a for a in (rel.get("assets") or [])
              if str(a.get("name") or "").endswith(UPDATE_ASSET_SUFFIX)]
    if not assets:
        raise ValueError("release %s carries no %s asset" % (tag, UPDATE_ASSET_SUFFIX))
    if len(assets) > 1:
        # One package per release. Two means the release was assembled by hand, and nothing here can
        # say which is real -- picking either would be a guess about what code to install.
        raise ValueError("release %s carries %d %s assets; expected exactly one"
                         % (tag, len(assets), UPDATE_ASSET_SUFFIX))
    a = assets[0]
    return {"version": "%d.%d.%d" % ver, "tag": tag, "assetName": a.get("name"),
            "assetUrl": a.get("browser_download_url"), "assetSize": a.get("size")}


def _read_updatecheck():
    try:
        with open(UPDATE_CHECK_PATH, encoding="utf-8-sig") as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) and rec.get("schema") == UPDATE_CHECK_CACHE_SCHEMA else None
    except Exception:  # noqa
        return None


def _write_updatecheck(rec):
    try:
        # _private_write for its atomicity, not its permissions: this file holds no secret, but a
        # half-written cache read back as JSON garbage would silently disable the check.
        _private_write(UPDATE_CHECK_PATH, rec)
    except Exception:  # noqa - an uncacheable check is slow, not broken
        pass


# What's new: CHANGELOG.md ships in every package, so the release an install just moved to always
# carries its own notes. Users never open the skill folder, so the notes only reach anyone if the
# skill says them -- once, in the first session on a new version. `.lastseen` records the last
# version announced; it is separate from `.updatecheck` because every updater, old ones included,
# rewrites that file with the NEW version straight after an apply, erasing the one fact needed here.
WHATS_NEW_MAX_VERSIONS = 3     # an install that skipped ten releases gets the newest three, and a count
WHATS_NEW_MAX_ITEMS = 4        # per version; the full list is one "what's new?" away in CHANGELOG.md
WHATS_NEW_ITEM_CHARS = 240
_CHANGELOG_HEAD_RE = re.compile(r"^##\s+\[?v?(\d+\.\d+\.\d+)\]?")


def _changelog_path():
    return os.path.join(INSTALL_REAL, "CHANGELOG.md")


def _lastseen_path():
    return os.path.join(CFG_DIR, ".lastseen")


def parse_changelog(text):
    """CHANGELOG.md -> [(version, [item, ...]), ...] in file order (newest first by convention).

    An item is one top-level "- " bullet with its indented continuation lines folded in, whitespace
    collapsed and capped. Anything else -- prose, sub-bullets' own markers, blank lines -- is layout.
    """
    out, items, cur = [], None, None
    for line in (text or "").splitlines():
        m = _CHANGELOG_HEAD_RE.match(line)
        if m:
            items = []
            out.append((m.group(1), items))
            cur = None
            continue
        if items is None:
            continue
        if line.startswith("## "):          # a heading that is not a version ends the section
            items, cur = None, None
            continue
        if line.startswith("- "):
            cur = [line[2:].strip()]
            items.append(cur)
        elif cur is not None and line.startswith((" ", "\t")) and line.strip():
            cur.append(line.strip())
        elif not line.strip():
            cur = None
    return [(v, [_cap(" ".join(parts)) for parts in its]) for v, its in out]


def _cap(s, n=WHATS_NEW_ITEM_CHARS):
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + "\u2026"


def whats_new(prev, current, path=None):
    """Changelog entries newer than `prev`, up to and including `current`, newest first.

    `prev` None means "some earlier version, unknown": only `current`'s own entry is returned rather
    than guessing how far back to go. A version with no entry is simply absent -- never an empty
    "nothing changed", which would be a claim the file does not make.
    """
    cv, pv = parse_version(current), parse_version(prev) if prev else None
    if not cv:
        return []
    try:
        with open(path or _changelog_path(), encoding="utf-8-sig") as f:
            entries = parse_changelog(f.read())
    except Exception:  # noqa - no changelog is no announcement, not a failure
        return []
    picked = []
    for v, items in entries:
        vv = parse_version(v)
        if not vv or vv > cv or (pv and vv <= pv) or (not pv and vv != cv):
            continue
        rec = {"version": v, "items": items[:WHATS_NEW_MAX_ITEMS]}
        if len(items) > WHATS_NEW_MAX_ITEMS:
            rec["moreItems"] = len(items) - WHATS_NEW_MAX_ITEMS
        picked.append(rec)
    picked.sort(key=lambda r: parse_version(r["version"]), reverse=True)
    return picked[:WHATS_NEW_MAX_VERSIONS]


def _read_lastseen():
    try:
        with open(_lastseen_path(), encoding="utf-8-sig") as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) else None
    except Exception:  # noqa
        return None


def _write_lastseen(version):
    try:
        _private_write(_lastseen_path(), {"schema": 1, "version": version})
    except Exception:  # noqa - failing to record means announcing again next session, not breaking
        pass


def announce_whats_new(res, prior_cache):
    """Attach `whatsNew` to a check result on the first session after this install changed version.

    `prior_cache` is `.updatecheck` as it was BEFORE this session's check rewrote it. Three cases:
    `.lastseen` exists -> announce everything newer than it. No `.lastseen` but a prior cache -> the
    skill ran here before this feature existed, so it was updated: announce the range from the
    cache's version if that is older, else just this version's entry (an older updater rewrote the
    cache with the new version, so how far back is unknown). Neither -> a fresh install, which has
    nothing to be told is new. A copy with no comparable version (a working tree) never announces.
    """
    local = res.get("installedVersion")
    lv = parse_version(local)
    if not lv or res.get("state") == "dev":
        return res
    seen = _read_lastseen()
    announce, prev = False, None
    if seen is not None:
        sv = parse_version(seen.get("version"))
        announce, prev = (not sv or sv < lv), (seen.get("version") if sv and sv < lv else None)
    elif prior_cache:
        pv = parse_version(prior_cache.get("installedVersion"))
        announce, prev = True, (prior_cache.get("installedVersion") if pv and pv < lv else None)
    if seen is None or parse_version(seen.get("version")) != lv:
        _write_lastseen(local)
    if announce:
        notes = whats_new(prev, local)
        if notes:
            res = dict(res, whatsNew=notes, updatedFrom=prev,
                       changelog=_changelog_path())
    return res


def check_update(force=False, now=None):
    """Is a newer release available? Returns a state dict and never raises.

    States: `disabled` (not configured, or opted out), `dev` (must not be overwritten), `current`,
    `outdated`, `ahead` (installed newer than released -- a maintainer build, never downgraded), and
    `unknown` (the check itself failed). `unknown` is the load-bearing one: it exists so a network
    failure cannot masquerade as `current`. SKILL.md treats it as "say nothing", never "up to date".
    """
    now = time.time() if now is None else now
    rec = installed_version() or {}
    local = rec.get("version")
    base = {"installedVersion": local, "installedCommit": rec.get("commit"), "installDir": INSTALL_REAL}
    off = autoupdate_disabled()
    if off:
        return dict(base, state="disabled", reason=off, checkedVia="local",
                    message="Self-update is switched off (%s)." % off)
    repo = update_repo()
    if not repo:
        return dict(base, state="disabled", reason="no update repo configured", checkedVia="local",
                    message="Self-update is not configured for this build.")
    base["repo"] = repo
    markers = install_markers()
    if markers:
        # Reported rather than silently skipped: a maintainer running from a checkout should be able
        # to see why it never updates itself, and an unstamped user install is a real support signal.
        return dict(base, state="dev", reasons=markers, checkedVia="local",
                    message="This copy is not an updatable package: %s." % markers[0])

    cached = _read_updatecheck()
    if cached and not force and cached.get("repo") == repo and cached.get("installedVersion") == local:
        age = now - (cached.get("checkedAt") or 0)
        ttl = UPDATE_RETRY_INTERVAL if cached.get("state") == "unknown" else UPDATE_CHECK_INTERVAL
        if 0 <= age < ttl:
            # `base` goes over the cached record, not under it. The cache is one file per user,
            # keyed on repo + version, so a second install of the same version on this machine (a
            # branded and a public copy, or a test extraction) would otherwise report the FIRST
            # install's commit and folder as its own until --force. What the cache knows is the feed:
            # the latest release. Which commit and folder this copy is are read from disk every time.
            out = dict(cached, **base, checkedVia="cache", cacheAgeSeconds=int(age))
            out.pop("schema", None)
            out.pop("checkedAt", None)
            return out

    try:
        rel = latest_release(repo)
    except Exception as e:  # noqa - ANY failure here is `unknown`; never `current`
        out = dict(base, state="unknown", checkedVia="network", detail=str(e)[:300],
                   message="Couldn't check for a newer version; continuing on %s."
                           % (local or "an unknown version"))
        _write_updatecheck(dict(out, schema=UPDATE_CHECK_CACHE_SCHEMA, checkedAt=now))
        return out

    lv, iv = parse_version(rel["version"]), parse_version(local)
    state = "outdated" if lv > iv else "ahead" if lv < iv else "current"
    msgs = {"outdated": "Version %s is available; this install is %s." % (rel["version"], local),
            "current": "Up to date on %s." % local,
            "ahead": "This install (%s) is newer than the latest release (%s); leaving it alone."
                     % (local, rel["version"])}
    out = dict(base, state=state, latestVersion=rel["version"], latestTag=rel["tag"],
               assetUrl=rel["assetUrl"], assetName=rel["assetName"], assetSize=rel["assetSize"],
               checkedVia="network", message=msgs[state])
    _write_updatecheck(dict(out, schema=UPDATE_CHECK_CACHE_SCHEMA, checkedAt=now))
    return out


def _validate_package(zf, want_version):
    """Refuse a zip that is not a meridiancs package for `want_version`; return its stamp.

    Every member is checked before ANYTHING is written: a path escaping the prefix (zip slip), an
    absolute or drive-qualified path, a member count or unpacked size that says zip bomb, a missing
    required file, or a VERSION.json disagreeing with the release tag. That last check matters most --
    it is what stops a mislabelled release from installing itself and then reporting `current`
    forever, since the stamp is the only thing the next check reads.
    """
    prefix = "meridiancs/"
    names, total = [], 0
    infos = zf.infolist()
    if len(infos) > UPDATE_MAX_MEMBERS:
        raise ValueError("package has %d members; refusing anything over %d"
                         % (len(infos), UPDATE_MAX_MEMBERS))
    for info in infos:
        name = info.filename
        if name.endswith("/"):
            continue
        # Normalise the separator FIRST: a member written with backslashes sidesteps a forward-slash
        # check and then lands as a nested path on Windows, which is how zip-slip guards get bypassed.
        norm = name.replace("\\", "/")
        if not norm.startswith(prefix):
            raise ValueError("member %r is outside %s" % (name, prefix))
        rel = norm[len(prefix):]
        if not rel or rel.startswith("/") or ":" in rel or ".." in rel.split("/"):
            raise ValueError("member %r is not a safe relative path" % name)
        total += info.file_size
        if total > UPDATE_MAX_UNPACKED_BYTES:
            raise ValueError("package unpacks to more than %d bytes" % UPDATE_MAX_UNPACKED_BYTES)
        names.append(rel)
    missing = [r for r in UPDATE_REQUIRED_MEMBERS if r not in names]
    if missing:
        raise ValueError("package is missing %s, so it is not a meridiancs skill" % ", ".join(missing))
    try:
        stamp = json.loads(zf.read(prefix + "VERSION.json").decode("utf-8-sig"))
    except Exception as e:  # noqa
        raise ValueError("package VERSION.json is unreadable (%s)" % e)
    got = stamp.get("version") if isinstance(stamp, dict) else None
    if parse_version(got) is None or parse_version(got) != parse_version(want_version):
        raise ValueError("release says %s but the package is stamped %r; refusing the mismatch"
                         % (want_version, got))
    return stamp


def _download_asset(url, dest):
    """Stream the release asset to `dest`, stopping at UPDATE_MAX_ASSET_BYTES. Returns bytes written."""
    import urllib.request
    if not _github_host(url):
        raise ValueError("release asset URL %r is not an https github.com host; refusing to download"
                         % url)
    req = urllib.request.Request(url, headers={"Accept": "application/octet-stream",
                                               "User-Agent": UPDATE_UA})
    got = 0
    with _update_open(req) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            got += len(chunk)
            if got > UPDATE_MAX_ASSET_BYTES:
                raise ValueError("asset exceeded %d bytes" % UPDATE_MAX_ASSET_BYTES)
            f.write(chunk)
    return got


def _smoke_test(staged):
    """Run the staged copy's own --help. Returns None if it works, else why it doesn't.

    Cheap and load-bearing: it imports the new meridian.py and builds its entire argument parser in a
    separate interpreter, so a truncated download or a syntax error is caught while the old tree is
    still in place. Without it, the failure mode is a user left with no working skill and no way to
    ask this one to fix it.
    """
    import subprocess          # lazy, like the report path's: nothing else in this file needs it
    entry = os.path.join(staged, "scripts", "meridian.py")
    if not os.path.exists(entry):
        return "the staged copy has no scripts/meridian.py"
    try:
        r = subprocess.run([sys.executable, entry, "--help"], capture_output=True, text=True,
                           timeout=120)
    except Exception as e:  # noqa
        return "could not run the staged copy (%s)" % e
    if r.returncode != 0:
        return "the staged copy failed `--help` (exit %s): %s" % (r.returncode, (r.stderr or "")[:200])
    return None


def _long_paths_enabled():
    """True unless this is Windows with the 260-char path limit still in force.

    Python itself is long-path aware on Windows; the OS setting is what decides. Unreadable is
    treated as off, because the cost of guessing wrong the other way is a failed extraction.
    """
    if os.name != "nt":
        return True
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
            return winreg.QueryValueEx(k, "LongPathsEnabled")[0] == 1
    except OSError:
        return False


def _too_long_for_windows(zf, staged_root):
    """The deepest path this update would write that Windows cannot open, or None.

    Checked before a byte is extracted. Found verifying v2.26.0: from an install whose folder was
    already deep, the staged `assets/fonts/SpaceGrotesk-Bold.ttf` came to 263 chars and extraction
    died with a bare "No such file or directory". It failed safe -- the install was untouched -- but
    the reason named a file that plainly exists in the package, and the next session would retry and
    fail identically. Both where the file is staged and where it finally lands are checked: a
    package whose installed paths do not fit would leave a skill Windows cannot read.
    """
    if _long_paths_enabled():
        return None
    worst = None
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if name.endswith("/") or not name.startswith("meridiancs/"):
            continue
        rel = name[len("meridiancs/"):].split("/")
        for p in (os.path.join(staged_root, "meridiancs", *rel), os.path.join(INSTALL_REAL, *rel)):
            if len(p) > WINDOWS_MAX_PATH and (worst is None or len(p) > len(worst)):
                worst = p
    return worst


def _extract_package(zf, staged_root):
    """Extract validated members under `staged_root`. Containment is re-checked per member."""
    import shutil
    root = os.path.realpath(staged_root)
    for info in zf.infolist():
        if info.filename.endswith("/"):
            continue
        parts = [p for p in info.filename.replace("\\", "/").split("/") if p not in ("", ".")]
        # normpath, not realpath: it collapses any surviving ".." so the containment check is
        # about the path, and it does not depend on how the platform resolves a path that does
        # not exist yet. Every directory under root is created here, so there is no symlink to
        # resolve. Belt and braces over _validate_package -- this is the one place a mistake
        # writes to disk.
        target = os.path.normpath(os.path.join(root, *parts))
        if not target.startswith(root + os.sep):
            raise ValueError("member %r would write outside the staging directory" % info.filename)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)


# Both are os.replace; named so the tests can make the directory rename fail the way Windows does
# when a process is parked in the install dir, which no POSIX runner can reproduce for real.
_rename_dir = os.replace
_move_entry = os.replace


def _swap_contents(live, staged, backup):
    """Replace `live`'s entries with `staged`'s, keeping `live` itself -- the fallback for when the
    directory cannot be renamed. All or nothing: any failure moves every entry back and re-raises.

    Windows refuses to rename a directory that is ANY process's working directory (WinError 32), and
    the one process most likely to be sitting in the install dir is the assistant's own shell, after
    a `cd` there to run `python scripts/meridian.py`. It does not refuse renaming the entries INSIDE
    such a directory, so moving the contents is the swap that still works. Measured before this
    existed: an install with a process parked in it failed every update with `applied: false`, and
    SKILL.md is silent on that result -- so it retried and failed every session, and no release,
    security patches included, ever reached that machine. Every earlier end-to-end check of the
    apply path had run from outside the directory, which is why nobody saw it.
    """
    moved_out, moved_in = [], []
    try:
        for name in sorted(os.listdir(live)):
            _move_entry(os.path.join(live, name), os.path.join(backup, name))
            moved_out.append(name)
        for name in sorted(os.listdir(staged)):
            _move_entry(os.path.join(staged, name), os.path.join(live, name))
            moved_in.append(name)
    except Exception as e:
        stuck = []
        for name in reversed(moved_in):
            try:
                _move_entry(os.path.join(live, name), os.path.join(staged, name))
            except Exception:  # noqa
                stuck.append(name)
        for name in reversed(moved_out):
            try:
                _move_entry(os.path.join(backup, name), os.path.join(live, name))
            except Exception:  # noqa
                stuck.append(name)
        if stuck:
            # The one failure that cannot be undone here: say exactly where the old files are, and
            # never delete that directory -- it may hold the only copy of the working skill.
            raise RuntimeError("%s; restoring the previous install was incomplete (%s) -- its files "
                               "are in %s" % (e, ", ".join(stuck), backup))
        raise


def apply_update(rel):
    """Install `rel` (a check_update result) over this install. Returns a result dict; may raise.

    Staged, verified, then swapped -- never written into the live tree file by file. A half-updated
    skill is the worst outcome available here, so the sequence is: download to a temp dir BESIDE the
    install (same filesystem, so the final move is a rename rather than a copy), validate the zip
    before extracting a byte, extract, smoke-test, move the live tree aside, move the new one in, and
    only then delete the old. If the move-in fails, the old tree goes straight back.
    """
    import shutil, tempfile, zipfile
    markers = install_markers()
    if markers:
        raise ValueError("refusing to update a copy that is not a package: %s" % "; ".join(markers))
    want = rel.get("latestVersion")
    if not rel.get("assetUrl") or not parse_version(want):
        raise ValueError("no comparable release asset to install")
    parent = os.path.dirname(INSTALL_REAL)
    # Temp dir BESIDE the install, not in the system temp dir: os.replace cannot rename across
    # filesystems, and ~/.claude/skills is routinely on a different volume from /tmp.
    tmp = tempfile.mkdtemp(prefix=UPDATE_STAGING_PREFIX, dir=parent)
    backup = None
    try:
        pkg = os.path.join(tmp, "package.zip")
        size = _download_asset(rel["assetUrl"], pkg)
        staged_root = os.path.join(tmp, "unpacked")
        os.makedirs(staged_root)
        with zipfile.ZipFile(pkg) as zf:
            stamp = _validate_package(zf, want)
            deep = _too_long_for_windows(zf, staged_root)
            if deep:
                raise ValueError(
                    "this install's folder is too deep for Windows to update it: %r is %d characters, "
                    "over the %d-character limit. Turn on Windows long paths (LongPathsEnabled), or "
                    "move the skill to a shorter folder such as ~/.claude/skills/meridiancs."
                    % (deep, len(deep), WINDOWS_MAX_PATH + 1))
            _extract_package(zf, staged_root)
        staged = os.path.join(staged_root, "meridiancs")
        why = _smoke_test(staged)
        if why:
            raise ValueError("downloaded package rejected: %s" % why)

        backup = INSTALL_REAL + ".previous"
        if os.path.exists(backup):
            shutil.rmtree(backup, ignore_errors=True)
        try:
            _rename_dir(INSTALL_REAL, backup)
            swap = "directory"
        except OSError:
            # Nothing has moved yet (a failed rename is atomic), so falling back is safe. The
            # contents backup is a fresh directory rather than `.previous`, which may be a
            # half-deleted leftover, and it is NOT under `tmp`: the finally below deletes `tmp`, and
            # after an incomplete restore this directory is the only copy of the old skill.
            backup = None
            held = tempfile.mkdtemp(prefix=UPDATE_HELD_PREFIX, dir=parent)
            try:
                _swap_contents(INSTALL_REAL, staged, held)
            except Exception:
                if not os.listdir(held):   # restored in full: nothing of the old skill is in it
                    os.rmdir(held)
                raise
            shutil.rmtree(held, ignore_errors=True)
            swap = "contents"
        if swap == "directory":
            try:
                os.replace(staged, INSTALL_REAL)
            except Exception:
                os.replace(backup, INSTALL_REAL)   # put the working skill back before re-raising
                backup = None
                raise
            shutil.rmtree(backup, ignore_errors=True)
            backup = None
        _write_updatecheck({"schema": UPDATE_CHECK_CACHE_SCHEMA, "checkedAt": time.time(),
                            "repo": rel.get("repo"), "installedVersion": want, "latestVersion": want,
                            "state": "current", "installDir": INSTALL_REAL,
                            "message": "Up to date on %s." % want})
        _write_lastseen(want)
        notes = whats_new(rel.get("installedVersion"), want)   # the NEW CHANGELOG.md is on disk now
        return {"applied": True, "fromVersion": rel.get("installedVersion"), "toVersion": want,
                **({"whatsNew": notes, "changelog": _changelog_path()} if notes else {}),
                "commit": (stamp or {}).get("commit"), "installDir": INSTALL_REAL,
                "assetBytes": size, "swap": swap,
                # The skill's instructions were loaded into this session BEFORE the swap, so the
                # scripts on disk are now newer than the SKILL.md the model is following. Saying so is
                # the difference between an update and an unexplained change in behaviour.
                "note": "Updated to %s. The scripts are live now; the new SKILL.md instructions take "
                        "effect in a new session." % want}
    finally:
        if backup and os.path.exists(backup) and not os.path.exists(INSTALL_REAL):
            os.replace(backup, INSTALL_REAL)   # last resort: never leave the skill missing
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_selfupdate(a):
    """`selfupdate` reports; `selfupdate --apply` installs. Neither ever fails the session.

    Exit stays 0 for every state, `unknown` included: this runs at launch, and a non-zero exit from a
    housekeeping check reads as "the skill is broken" to whoever is looking at the output.
    """
    apply_it = bool(getattr(a, "apply", False))
    force = bool(getattr(a, "force", False))
    prior = _read_updatecheck()          # before check_update rewrites it -- see announce_whats_new
    res = check_update(force=force or apply_it)
    if not apply_it:
        jout(announce_whats_new(res, prior)); return
    if res.get("state") in ("dev", "disabled", "unknown"):
        # --force overrides the "you already have it" check, NOT these. A dev tree must not be
        # overwritten however hard the caller insists, and there is nothing to install when the
        # check never resolved a release -- reporting "no asset" for either would name the wrong
        # cause, which for `dev` is the difference between a guard working and a bug.
        jout(dict(res, applied=False,
                              reason="cannot install while state=%s" % res.get("state")))
        return
    if res.get("state") != "outdated" and not force:
        jout(dict(res, applied=False,
                              reason="nothing to install (state=%s)" % res.get("state")))
        return
    if not res.get("assetUrl"):
        jout(dict(res, applied=False,
                              reason="no release asset available to install"))
        return
    try:
        jout(apply_update(res))
    except Exception as e:  # noqa - a failed update must leave a working skill, and say why
        jout(dict(res, applied=False, error=str(e)[:400],
                              message="Update to %s failed; still running %s."
                                      % (res.get("latestVersion"), res.get("installedVersion"))))


# --where and --select read identically on every verb that takes them. Shared so the copies cannot
# drift, and written out in full because `<verb> --help` is the cheap route to a flag: ~150 tokens of
# tool output against ~7,000 for reading references/scripts.md to find the same thing.
_WHERE_HELP = ("\"Field op Type value\" filter, repeatable and ANDed "
               "(e.g. --where \"Count_KEV >= Integer 1\"). Relative dates (-30d, +90d, today) "
               "resolve locally; the windowed Datetime operators are refused on purpose")
_SELECT_HELP = ("comma/space-separated columns to keep. Prefer it on any row-returning call -- "
                "measured, it is ~a third off the payload, and --format csv --out is three orders "
                "of magnitude off it")


def _add_output_flags(s):
    """--format csv writes the rows to a spreadsheet; the JSON envelope with the caveats still prints."""
    s.add_argument("--format", choices=["json", "csv"], default="json",
                   help="csv writes the rows to --out and prints the JSON envelope alone; past a few "
                        "hundred rows this is the difference between ~87 tokens and tens of thousands")
    s.add_argument("--out", help="write CSV here instead of stdout; must end .csv")
    return s


def build_parser():
    """The CLI's argparse parser. Its own function so a test can parse every command the docs show
    without running one: a documented flag that no longer exists costs the model an error and a retry."""
    p = argparse.ArgumentParser(description="Meridian API v2 CLI (cross-platform).")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("api", help="raw API call, for an endpoint no other verb covers")
    s.add_argument("endpoint", help="path, e.g. CMDB/v2/data/cmdb -- omit the leading slash under Git "
                                    "Bash, which rewrites it into a Windows path")
    s.add_argument("-X", "--method", default="GET", help="HTTP method (default GET)")
    s.add_argument("--body", help="request body as a JSON string")
    s.add_argument("--body-file", help="read the JSON body from this file (avoids shell quoting)")
    s.add_argument("--allow-write", action="store_true",
                   help="permit a call that can change the stack (PUT/PATCH/DELETE, or a POST that is "
                        "not a read query) -- only after the user confirms that change")
    s.set_defaults(func=cmd_api)

    s = sub.add_parser("refresh-fields", help="re-read this stack's real field names into the cache")
    s.add_argument("--search", help="find a cached field by name fragment")
    s.set_defaults(func=cmd_refresh_fields)

    s = sub.add_parser("digest", help="one-command periodic posture review, for scheduled delivery")
    s.add_argument("--table", default="asset"); s.add_argument("--by", help="breakdown field (default Risk_Level)")
    s.add_argument("--field", default="Risk_Score", help="field to rank the top lists by")
    s.add_argument("--top", type=int, default=5, help="rows in each top list (default 5)")
    s.add_argument("--snapshot", action="store_true",
                   help="also append these counts to this stack's local history (no extra API calls); "
                        "implies --refresh, since a history row must be measured, not re-served")
    s.add_argument("--refresh", action="store_true", help="ignore cached aggregates and recompute")
    s.set_defaults(func=cmd_digest)

    s = sub.add_parser("snapshot", help="append today's counts to this stack's local history, for trends")
    s.add_argument("--table", default="asset"); s.add_argument("--by", help="breakdown field (default Risk_Level)")
    s.add_argument("--field", default="Risk_Score", help="field the top lists rank by")
    s.add_argument("--top", type=int, default=5, help="rows in each top list (default 5)")
    s.add_argument("--no-metrics", dest="metrics", action="store_false",
                   help="skip the defined metrics (they are captured by default, one call each)")
    s.add_argument("--entities", metavar="SCOPE",
                   help="also capture per-entity scores, e.g. top500:asset:Risk_Score or "
                        "label:<SmartLabel>:Risk_Score. Bounded; there is no unbounded scope.")
    s.add_argument("--allow-large-scope", dest="allow_large_scope", action="store_true",
                   help="accept an --entities scope above %d (up to %d)"
                        % (ENTITY_SCOPE_DEFAULT_MAX, LIST_MAX_RECORDS))
    s.add_argument("--with-names", dest="with_names", action="store_true",
                   help="store raw customer identifiers instead of salted hashes (the file then holds "
                        "customer data)")
    s.set_defaults(func=cmd_snapshot)

    s = sub.add_parser("trend", help="compare snapshots from this stack's local history")
    s.add_argument("--since", help="earliest stack date to compare from, YYYY-MM-DD")
    s.add_argument("--metric", help="compare one named metric")
    s.add_argument("--table", help="only compare breakdowns/rankings for this table")
    s.add_argument("--by", help="only compare the breakdown by this field")
    # The question-to-metric loop: supply a definition alongside --metric and an untracked metric is
    # registered (after validation) and answered with today's value as a baseline.
    s.add_argument("--derive-where", action="append", dest="derive_where",
                   help="define an untracked --metric so it is tracked from the next snapshot on")
    s.add_argument("--derive-smart-label", dest="derive_smart_label",
                   help="define an untracked --metric from one of the stack's SmartLabels")
    s.add_argument("--derive-label", dest="derive_label", help="human label for the derived metric")
    s.add_argument("--derive-table", dest="derive_table", default="asset",
                   help="table the derived metric counts over (default asset)")
    s.add_argument("--name-entities", dest="name_entities", action="store_true",
                   help="look the moved entities' names up live; names are never written to history")
    _add_output_flags(s).set_defaults(func=cmd_trend)

    sn = sub.add_parser("snapshots", help="inspect or prune this stack's local snapshot history")
    sns = sn.add_subparsers(dest="snapshots_cmd", required=True)
    sns.add_parser("list", help="count, oldest, newest and size of the history").set_defaults(func=cmd_snapshots)
    sp = sns.add_parser("prune", help="keep the newest N records and report what was dropped")
    sp.add_argument("--keep", type=int, default=SNAPSHOT_MAX_RECORDS,
                    help="newest records to keep (default %d)" % SNAPSHOT_MAX_RECORDS)
    sp.set_defaults(func=cmd_snapshots)

    mx = sub.add_parser("metrics", help="named counts captured on every snapshot, for trends")
    mxs = mx.add_subparsers(dest="metrics_cmd", required=True)
    mxs.add_parser("list", help="tracked metrics, and whether each still resolves").set_defaults(func=cmd_metrics)
    ma = mxs.add_parser("add", help="define a metric (validated now, refused if it cannot resolve)")
    ma.add_argument("--name", required=True, help="short key this metric is captured and trended under")
    ma.add_argument("--label", help="human-readable name for reports and charts")
    ma.add_argument("--table", default="asset", help="asset (default) or user")
    ma.add_argument("--where", action="append", help=_WHERE_HELP)
    ma.add_argument("--smart-label", dest="smart_label", help="track one of the stack's own SmartLabels")
    ma.add_argument("--derived", action="store_true",
                    help="mark as derived from a question, and report today's value as a baseline")
    ma.set_defaults(func=cmd_metrics)
    mr = mxs.add_parser("rm", help="stop measuring a metric (captured history is kept)")
    mr.add_argument("name"); mr.set_defaults(func=cmd_metrics)

    # alerts -- MVP, deliberately NOT routed from SKILL.md yet. Reachable by explicit CLI use only, so
    # merging it cannot change what a natural-language question does (design/trends.md rule 1).
    al = sub.add_parser("alerts", help="threshold rules evaluated against this stack's local history")
    als = al.add_subparsers(dest="alerts_cmd", required=True)
    als.add_parser("list", help="configured rules, and whether each can be evaluated").set_defaults(func=cmd_alerts)
    aa = als.add_parser("add", help="define a rule (validated now, refused if it could never fire)")
    aa.add_argument("--name", required=True)
    aa.add_argument("--if", dest="condition", required=True, choices=ALERT_CONDITIONS,
                    help="coverage-regressed (a connector entered the failing set) | above | below")
    aa.add_argument("--metric", help="the tracked metric to threshold (above/below)")
    aa.add_argument("--value", type=float, help="numeric threshold (above/below)")
    aa.add_argument("--include-degraded", dest="include_degraded", action="store_true",
                    help="coverage-regressed also fires when a connector enters the DEGRADED set, not "
                         "only the failing one (noisier)")
    aa.set_defaults(func=cmd_alerts)
    ar = als.add_parser("rm", help="remove a rule")
    ar.add_argument("name"); ar.set_defaults(func=cmd_alerts)
    ae = als.add_parser("eval", help="evaluate every rule against the stored history (no API calls)")
    ae.add_argument("--window", choices=("daily", "full"), default="daily",
                    help="daily (default) compares the two most recent snapshots -- what changed since "
                         "yesterday; full compares the oldest with the newest, so an old regression "
                         "keeps firing. Ignored when --since is given.")
    ae.add_argument("--since", help="earliest stack date to compare from, YYYY-MM-DD (overrides --window)")
    ae.add_argument("--format", choices=("markdown", "json"), default="markdown")
    ae.set_defaults(func=cmd_alerts)
    an = als.add_parser("notify", help="send the current verdict to Slack/Teams/email, but only if a "
                                       "rule's verdict changed since the last notify (or --force)")
    an.add_argument("--to", help="comma-separated targets to attempt: slack,teams,email (default: "
                                 "all three, skipping any without their env vars configured)")
    an.add_argument("--force", action="store_true", help="send even if nothing changed since last time")
    an.add_argument("--window", choices=("daily", "full"), default="daily")
    an.add_argument("--since", help="earliest stack date to compare from, YYYY-MM-DD (overrides --window)")
    an.set_defaults(func=cmd_alerts)

    s = sub.add_parser("labels", help="the stack's own SmartLabels and what each one means")
    s.add_argument("--search", help="a business term to resolve to a SmartLabel")
    s.add_argument("--table", choices=["asset", "user"])
    s.add_argument("--refresh", action="store_true",
                   help="refetch the labels instead of using the cache (they define new ones over time)")
    s.set_defaults(func=cmd_labels)

    s = sub.add_parser("top", help="top-N by a numeric field (riskiest / highest / most-vulnerable)")
    s.add_argument("--table", default="asset", help="asset (default) or user")
    s.add_argument("--field", default="Risk_Score", help="numeric field to rank by (default Risk_Score)")
    s.add_argument("--top", type=int, default=5, help="rows to return (default 5)")
    s.add_argument("--select", help=_SELECT_HELP)
    s.add_argument("--where", action="append", help=_WHERE_HELP)
    _add_output_flags(s).set_defaults(func=cmd_top)

    s = sub.add_parser("list", help="every record matching a filter (show me all X)")
    s.add_argument("--table", default="asset", help="asset (default) or user")
    s.add_argument("--where", action="append", help=_WHERE_HELP)
    s.add_argument("--select", help=_SELECT_HELP)
    s.add_argument("--limit", type=int, default=50, help="rows to return (default 50); --all overrides it")
    s.add_argument("--all", action="store_true", help="return every matching record, up to %d" % LIST_MAX_RECORDS)
    s.add_argument("--count-only", action="store_true",
                   help="return the match count alone, no rows -- for \"how many X\"")
    _add_output_flags(s).set_defaults(func=cmd_list)

    s = sub.add_parser("summary", help="group-by breakdown, or whole-stack totals (posture)")
    s.add_argument("--table", default="asset", help="asset (default) or user")
    s.add_argument("--by", help="field to break down by. The result states its own reach -- complete, "
                                "accountedRecords, unaccountedRecords, groupsCapped, "
                                "recordsWithoutField (matches with no value, in no group) -- and "
                                "that has "
                                "to reach the answer; a partial breakdown read as whole is a wrong one")
    s.add_argument("--where", action="append", help=_WHERE_HELP)
    s.add_argument("--metrics", action="store_true",
                   help="stack totals and license instead of a breakdown")
    s.add_argument("--refresh", action="store_true", help="ignore the cached breakdown and recompute")
    _add_output_flags(s).set_defaults(func=cmd_summary)

    s = sub.add_parser("profile",
                       help="full risk and blast-radius profile for one named user or asset, "
                            "with findings and recommendations")
    s.add_argument("--name", required=True,
                   help="exact Owner_Name/Asset_Name, or a human/partial name; several matches "
                        "return a risk-ranked candidate list rather than a guess")
    s.add_argument("--type", default="user", choices=["user", "asset"], help="subject type (default user)")
    s.add_argument("--ascii", action="store_true", help="render a terminal blast-radius view instead of JSON")
    s.add_argument("--vuln-detail", action="store_true",
                   help="include the per-CVE array (asset profiles). Off by default: it was 68%% of "
                        "the payload and no renderer or rule reads it -- the counts, maxCvss and the "
                        "named notFixable/highEpss lists are always present. Use it to enumerate "
                        "specific CVEs, not to get the findings.")
    s.add_argument("--linked-detail", action="store_true",
                   help="include every fetched linked asset (user profiles), not just the top %d by "
                        "risk. Findings/recommendations always reflect the full fetched list either "
                        "way; this only affects how much of `linkedAssets` is serialized."
                        % PROFILE_LINKED_ASSETS_SHOW)
    s.set_defaults(func=cmd_profile)

    s = sub.add_parser("compare", help="two users or two assets side by side")
    s.add_argument("--name1", required=True, help="first subject (same name matching as profile)")
    s.add_argument("--name2", required=True, help="second subject")
    s.add_argument("--type", default="user", choices=["user", "asset"], help="subject type (default user)")
    s.set_defaults(func=cmd_compare)

    s = sub.add_parser("connect", help="session preflight: resolve credentials and report one state")
    s.add_argument("--with-connectors", action="store_true",
        help="append the connector/data-coverage summary (session launch)")
    s.add_argument("--coverage-full", dest="coverage_full", action="store_true",
        help="keep every connector row in full instead of rolling the ones SKILL.md does not print "
             "into delivering[]; `connectors` gives the same thing")
    s.add_argument("--refresh", action="store_true", help="ignore the cached coverage block and refetch")
    s.set_defaults(func=cmd_connect)

    s = sub.add_parser("asof", help="when the LDG was last rebuilt (last completed merger run); one "
                                    "call, the check before reusing earlier answers")
    s.set_defaults(func=cmd_asof)

    s = sub.add_parser("connectors", help="which connectors are enabled, succeeding, and ingesting")
    s.add_argument("--max-failures", type=int, default=12,
                   help="hard failures to list (default 12); the rest are counted, never dropped")
    s.add_argument("--max-other", type=int, default=15,
                   help="unconnected data sources to list (default 15); the rest are counted")
    s.add_argument("--full", action="store_true",
                   help="keep per-connector notes[] instead of the deduped warningGroups[] "
                        "(same facts, ~2x the payload)")
    s.add_argument("--max-warnings", type=int, default=12,
                   help="distinct warning causes to quote (default 12); the rest are counted")
    s.add_argument("--max-detail", type=int, default=10,
                   help="delivering connectors keeping status/timestamp (default 10); failing rows "
                        "are always detailed and do not count against this")
    s.add_argument("--refresh", action="store_true", help="ignore the cached coverage block and refetch")
    s.set_defaults(func=cmd_connectors)

    s = sub.add_parser("hr", help="which HR systems (Dayforce, BambooHR, ADP, Workday, UKG...) feed "
                                  "this stack, exact user-record counts, and the --where to scope by them")
    s.add_argument("--refresh", action="store_true", help="ignore the cached connector health and refetch")
    s.set_defaults(func=cmd_hr)

    s = sub.add_parser("check", help="what this token can reach, and rate-limit headroom "
                                     "(run after a 403, or before a big investigation)")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("selfupdate", help="is a newer release of this skill available, and "
                                          "install it")
    s.add_argument("--check", action="store_true",
                   help="report only (the default; accepted so the launch command reads clearly)")
    s.add_argument("--apply", action="store_true",
                   help="install the newer release over this one, then report the new version")
    s.add_argument("--force", action="store_true",
                   help="ignore the once-a-day check cache; with --apply, reinstall the latest "
                        "release even if this install already matches it")
    s.set_defaults(func=cmd_selfupdate)

    # Multi-stack management: keep several stacks' credentials and switch the active one.
    sk = sub.add_parser("stacks", help="manage multiple Meridian stacks and switch between them")
    sksub = sk.add_subparsers(dest="stacks_cmd", required=True)
    sksub.add_parser("list", help="list saved stacks (active marked; tokens redacted)").set_defaults(func=cmd_stacks_list)
    sa = sksub.add_parser("add", help="add/update one stack without touching the others")
    sa.add_argument("--name", required=True); sa.add_argument("--fqdn", required=True)
    sa.add_argument("--token", help="API token; '-' reads it from stdin (keeps it out of shell "
                    "history and process listings); omit to use MERIDIAN_API_TOKEN")
    sa.add_argument("--action-token", help="separate token for /data/ldg writes, if this stack uses "
                    "one; '-' reads it from stdin (after the API token's line if --token - too)")
    sa.add_argument("--insecure-tls", dest="insecure_tls", action="store_true",
                    help="this stack presents a self-signed/internal certificate; stored per stack "
                         "and carried by `stacks switch`. Reverting to verified TLS is rm + re-add.")
    sa.set_defaults(func=cmd_stacks_add)
    sw = sksub.add_parser("switch", help="make a saved stack active and validate it")
    sw.add_argument("name"); sw.set_defaults(func=cmd_stacks_switch)
    sr = sksub.add_parser("rm", help="remove a saved stack")
    sr.add_argument("name"); sr.set_defaults(func=cmd_stacks_rm)

    s = sub.add_parser("report", help="branded PDF from any verb's JSON -- the shareable deliverable")
    s.add_argument("--input", nargs="+",
        help="JSON from any verb; omit to read stdin. Several files render one document with "
             "several subjects -- profiles only (e.g. a user and each of its linked assets).")
    s.add_argument("--out", required=True, help="output path; must end .pdf or .html (.html with --html)")
    s.add_argument("--title", help="document title (defaults to one derived from the input)")
    s.add_argument("--date", help="date shown on the cover (defaults to today)")
    s.add_argument("--html", action="store_true",
                   help="write branded HTML instead of PDF (also the fallback when no Chromium is found)")
    s.set_defaults(func=cmd_report)
    return p


def main():
    args = build_parser().parse_args()
    # Checked before dispatch, so a bad path fails before any API call or render is spent on it.
    out = getattr(args, "out", None)
    if out:
        if args.func is cmd_report:
            suffixes = (".html", ".htm") if args.html else (".pdf", ".html", ".htm")
        else:
            suffixes = (".csv",)
        problem = out_path_problem(out, suffixes)
        if problem:
            die(problem, 2)
        warning = out_in_skill_dir(out)
        if warning:
            print(warning, file=sys.stderr)
    try:
        args.func(args)
    except Exception as e:  # noqa
        die(str(e), 1)


if __name__ == "__main__":
    main()
