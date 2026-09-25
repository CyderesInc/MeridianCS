#!/usr/bin/env python3
"""
Automated smoke test for the Meridian connection preflight (SKILL.md section 1).

Covers the *deterministic* parts of onboarding — the bits that don't need a live stack:

  1. Error classification  : HTTP status / network error -> connect `state`
                             (401->auth_error, 403->forbidden, other HTTP->http_error, none->unreachable)
  2. not_configured (E2E)  : with no MERIDIAN_* env vars and an empty HOME, `meridian.py connect`
                             must return state=not_configured and list both missing fields,
                             making NO network call.
  3. connector rollup      : the SKILL.md section 1.5 data-coverage aggregation, driven off a fixture
                             instead of a live stack - health verdicts, the service-name fallback
                             when a run is filed under another profile, failure grouping, and the
                             promise that no credential field survives extraction.
  4. profile insights      : the governance recommendation reads the tool's name out of the findings
                             instead of naming one vendor, and degrades to a generic phrase when the
                             data doesn't say - so a report never points at the wrong console.
  5. profile shape         : an investigation answer arrives complete in one call - `findings` and
                             `recommendations` in the JSON (they used to exist only inside the
                             rendered PDF, so asking "what should we do" cost ~4s of headless
                             Chrome), and a human-looking name resolves in one round trip.

These map to evals 14 (not_configured) and the state logic behind 15/16/http_error in evals.json.
Runs fully offline; safe for CI. Exit code 0 = all passed, 1 = a failure.

Optional live check (needs a reachable stack + valid config or MERIDIAN_* env):
    python evals/test_connect.py --live
It asserts a real `connect` returns state=connected with numeric asset/user counts, never leaks more
than the last 4 chars of the token, and that `connectors` returns a coherent coverage summary with no
credential fields in it.

Usage:
    python evals/test_connect.py            # offline deterministic tests
    python evals/test_connect.py --live     # also hit the configured stack
"""
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MERIDIAN_PY = os.path.join(os.path.dirname(HERE), "scripts", "meridian.py")

_passed = 0
_failed = 0


def derived_tree():
    """True when the suite is running inside make-public.py's OUTPUT rather than the source repo.

    Four tests here assert properties OF the derivation -- that this tree is branded [29], that
    the .public.md sources exist and are in sync [30], that deriving produces a clean audit [32],
    and that publishing a derivation commits before it builds [36]. None of them can hold in the
    derived tree itself, which has no brand assets, no variants and no design/ record. The public
    repository runs this same suite, so they skip there instead of failing assertions that
    describe work already done correctly (17 of them before [36] existed).

    Joint signal, for the same reason check-docs-pii.py uses one: an absent design/ on its own,
    or an absent variant on its own, is a source-repo defect that has to stay loud. Only the
    combination means "derived".
    """
    root = os.path.dirname(HERE)
    return (not os.path.isdir(os.path.join(root, "design"))
            and not os.path.exists(os.path.join(root, "references", "api-reference.public.md")))


def check(name, got, want):
    global _passed, _failed
    if got == want:
        _passed += 1
        print("  PASS  %s" % name)
    else:
        _failed += 1
        print("  FAIL  %s\n          got:  %r\n          want: %r" % (name, got, want))


def load_meridian():
    spec = importlib.util.spec_from_file_location("meridian", MERIDIAN_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_classify(m):
    print("[1] error classification (offline)")
    cases = [
        ("HTTP 401: {\"code\":401001}",                          ("auth_error", 401)),
        ("HTTP 403: Forbidden",                                  ("forbidden", 403)),
        ("HTTP 500: Internal Server Error",                      ("http_error", 500)),
        ("HTTP 404: Not Found",                                  ("http_error", 404)),
        ("<urlopen error [Errno 11001] getaddrinfo failed>",     ("unreachable", None)),
        ("<urlopen error [Errno -2] Name or service not known>", ("unreachable", None)),
        ("",                                                     ("unreachable", None)),
    ]
    for err, want in cases:
        check("classify(%r)" % (err[:32] + ("..." if len(err) > 32 else "")),
              m.classify_connect_error(err), want)


def test_lazy_imports():
    """Four stdlib modules are deferred to their call sites; assert they stay deferred.

    One process runs per verb, so import cost is paid on every single invocation. Measured with
    `python -X importtime` on this file: concurrent.futures 25.4ms, hashlib 16.0ms, difflib 8.0ms,
    secrets 4.0ms -- ~53ms of module import, for modules most verbs never touch. End to end that is
    31ms off the total import time (118.9 -> 87.8) and 25-40ms off every verb's wall clock.

    This is guarded because the regression is invisible: adding `import hashlib` to the top of the
    file breaks nothing, passes every other test, and silently gives the time back. Checked in a
    subprocess -- by the time the suite runs in-process, its own imports have polluted sys.modules.

    Deliberately NOT deferred, so don't "finish the job": `http.client`/`ssl`/`socket`, because
    `_TRANSPORT_ERRS` is a module-level tuple built from `http.client.HTTPException` and deferring it
    means restructuring the one HTTP chokepoint; and `csv`/`datetime`, whose 0.9ms combined is below
    noise. `shutil` cannot be deferred at all -- argparse imports it itself for the terminal width.
    """
    print("[3f] deferred imports stay deferred (offline)")
    DEFERRED = ("concurrent.futures", "hashlib", "difflib", "secrets")
    probe = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('m', %r)\n"
        "m = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(m)\n"
        "print(','.join(n for n in %r if n in sys.modules))\n" % (MERIDIAN_PY, DEFERRED)
    )
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60)
    check("the probe ran", proc.returncode, 0)
    eager = [x for x in proc.stdout.strip().split(",") if x]
    check("no deferred module is imported at module level", eager, [])

    # ...and each is genuinely reachable where it is needed, or the deferral broke a code path.
    # A NameError from a missed call-site import would only fire on these paths, which is exactly
    # the shape of bug a "modules aren't imported" check alone would bless.
    m = load_meridian()
    check("hashlib reachable: cache key", isinstance(m._rescache_key("k", a=1), str), True)
    check("hashlib reachable: entity hashing", len(m.hash_entity("salt", "name")), 16)
    check("hashlib reachable: salt id", len(m.salt_id("salt")), 8)
    check("concurrent.futures reachable: parallel()", m.parallel([lambda: 1, lambda: 2]), [1, 2])

    real_fm = dict(m._FIELD_MAP)
    m._FIELD_MAP["asset"] = {"Asset_Type": "String", "Asset_Name": "String"}
    try:
        # field_problem() is the difflib caller: it must still produce the did-you-mean.
        msg = m.field_problem("asset", "Asset_Typo")
        check("difflib reachable: did-you-mean is produced", "Did you mean" in (msg or ""), True)
        check("...naming the near match", "Asset_Type" in (msg or ""), True)
    finally:
        m._FIELD_MAP.clear()
        m._FIELD_MAP.update(real_fm)


def test_not_configured():
    print("[2] not_configured end-to-end (offline, no network call)")
    with tempfile.TemporaryDirectory() as tmp:
        env = {k: v for k, v in os.environ.items() if not k.startswith("MERIDIAN_")}
        # Point HOME/USERPROFILE at an empty dir so no config.json is found on any OS.
        env["HOME"] = tmp
        env["USERPROFILE"] = tmp
        env.pop("HOMEDRIVE", None)
        env.pop("HOMEPATH", None)
        proc = subprocess.run([sys.executable, MERIDIAN_PY, "connect"],
                              capture_output=True, text=True, env=env, timeout=30)
        try:
            out = json.loads(proc.stdout)
        except (ValueError, json.JSONDecodeError):
            check("connect returns JSON", False, True)
            print("          stdout: %r\n          stderr: %r" % (proc.stdout, proc.stderr))
            return
        check("state == not_configured", out.get("state"), "not_configured")
        check("missing == [fqdn, api_token]", sorted(out.get("missing", [])), ["api_token", "fqdn"])
        check("no counts reported", "assetCount" in out, False)


# A stack in miniature, shaped like the real payloads: two AWS-ish services that pass, an Intune
# profile whose services fail with one identical message, a Datadog profile whose run only warned, a
# cert profile that has never run, and a disabled AD profile whose data is still in the stack. The
# credential fields (host / password / field_metadata) are here on purpose - extraction must drop them.
# So are the fields NOBODY HAS SEEN YET. Platform security is migrating connector secrets to Vault,
# and from 2026-10-05 this endpoint returns a vault path in place of the encrypted string. The
# protection is an allow-list -- summarize_connectors builds a new dict from named keys -- so an
# unanticipated field is dropped by construction rather than by matching a pattern. Pinning that
# PROPERTY matters more than pinning the field names that happen to exist today: without it the
# suite would keep describing the pre-Vault world and still pass, and a later loosening of the
# extraction to a deny-list would go unnoticed exactly when the response shape was changing.
FIXTURE_PROFILES = {"connectorProfiles": [
    {"display_name": "Amazon Web Services (AWS)", "bridge_name": "aws", "profile_name": "Prod",
     "group": "Cloud Infrastructure", "host": "10.0.0.9", "password": "gAAAAABsecret",
     "field_metadata": {"password": "encrypt;required"},
     "vault_path": "secret/data/meridian/aws/prod",
     "secret_ref": "kv/meridian/aws#password",
     "proxy": {"host": "proxy.internal.invalid", "port": 8080},
     "config": {"role_arn": "role-placeholder-value", "region": "us-east-1"},
     "services_list": [
         {"service": "aws_ec2", "display_name": "AWS EC2", "status": "OK", "activity": True},
         {"service": "aws_s3", "display_name": "AWS S3", "status": "OK", "activity": True}]},
    {"display_name": "Microsoft Intune", "bridge_name": "intune", "profile_name": "Default profile",
     "group": "Asset Management",
     "services_list": [
         {"service": "intune_device", "display_name": "Intune Device", "status": "FAIL",
          "activity": True, "message": "401 Client Error: Unauthorized"},
         {"service": "intune_user", "display_name": "Intune User", "status": "FAIL",
          "activity": True, "message": "401 Client Error: Unauthorized"}]},
    {"display_name": "Datadog Enterprise", "bridge_name": "datadog", "profile_name": "Lucidum",
     "group": "Monitoring",
     "services_list": [
         {"service": "datadog_host", "display_name": "Datadog Host", "status": "OK", "activity": True},
         {"service": "datadog_user", "display_name": "Datadog User", "status": "OK", "activity": False}]},
    {"display_name": "Domain Certificate", "bridge_name": "cert_info", "profile_name": "Default profile",
     "group": "Certificates",
     "services_list": [{"service": "cert_info", "display_name": "Cert Info", "status": "OK", "activity": True}]},
    {"display_name": "Microsoft Active Directory (AD)", "bridge_name": "ad_ldap", "profile_name": "Lucidum",
     "group": "Identity Access Management", "username": "LDAP\\svc",
     "services_list": [{"service": "ad_user", "display_name": "AD User", "status": "FAIL", "activity": False}]},
]}

FIXTURE_RUNS = {"content": [
    # aws_ec2 ran under 'Default profile' while the configured profile is 'Prod' -> service fallback
    {"bridge_name": "aws_ec2", "platform": "aws", "profile": "Default profile", "status": "Success",
     "output_records": 6100, "_utc": "2026-07-27T14:00:00.000+00:00", "_time": 1785164000},
    {"bridge_name": "aws_s3", "platform": "aws", "profile": "Prod", "status": "Success",
     "output_records": 700, "_utc": "2026-07-27T14:01:00.000+00:00", "_time": 1785164060},
    # an older run for the same key must lose to the newer one above
    {"bridge_name": "aws_s3", "platform": "aws", "profile": "Prod", "status": "FAILED",
     "output_records": 1, "_utc": "2026-07-20T14:01:00.000+00:00", "_time": 1784500000},
    {"bridge_name": "datadog_host", "platform": "api", "profile": "Lucidum", "status": "Warning",
     "output_records": 600, "_utc": "2026-07-27T14:02:00.000+00:00", "_time": 1785164120},
    # ingesting with its profile switched off - real records, nothing refreshing them
    {"bridge_name": "ad_user", "platform": "api", "profile": "Default profile", "status": "Success",
     "output_records": 6230, "_utc": "2026-07-27T14:03:00.000+00:00", "_time": 1785164180},
    # no profile configured for this one at all
    {"bridge_name": "okta_user", "platform": "api", "profile": "Default profile", "status": "Error",
     "output_records": 5607, "_utc": "2026-07-27T14:04:00.000+00:00", "_time": 1785164240,
     "event_messages": "Okta | rate limited"},
    # neither of these brings data in: outbound action, and the internal merger
    {"bridge_name": "webhook", "platform": "action", "profile": "Recertification", "status": "SUCCESS",
     "output_records": 353, "_utc": "2026-07-27T15:00:00.000+00:00", "_time": 1785166000},
    {"bridge_name": "Lucidum Asset Merger", "platform": "ML-ENGINE", "profile": None, "status": "Warning",
     "output_records": 34201, "_utc": "2026-07-27T15:01:00.000+00:00", "_time": 1785166060},
]}


def test_connector_rollup(m):
    print("[3] connector / data-coverage rollup (offline, fixture-driven)")
    calls = []

    def fake_call(method, endpoint, body=None, retries=1):
        calls.append(endpoint)
        if "connector/profile" in endpoint:
            return FIXTURE_PROFILES
        if "metrics/connector" in endpoint:
            return FIXTURE_RUNS
        raise AssertionError("unexpected endpoint %r" % endpoint)

    real_call = m.call
    m.call = fake_call
    try:
        out = m.summarize_connectors()
        brief_calls = len(calls)
        # The --full shape as well, because the leak assertions below are worthless without it.
        # `brief` (the default) reshapes rows through _brief_connectors, which rebuilds them from
        # named keys -- so a field leaking out of summarize_connectors itself is invisible in the
        # brief output while sitting in plain view of anyone running `connectors --full`. Proven,
        # not assumed: adding a passthrough key to the profile extraction left every one of these
        # checks passing until this second call existed.
        full = m.summarize_connectors(brief=False)
    finally:
        m.call = real_call

    check("both endpoints read, once each", brief_calls, 2)
    check("whole run history in one call", any("size=2000" in c for c in calls), True)
    check("profiles fetched", out["fetched"]["profiles"], "ok")
    check("ingestion fetched", out["fetched"]["ingestion"], "ok")

    s = out["summary"]
    # AD's only service is disabled, so its profile is not an enabled connector.
    check("enabled connectors (disabled profile excluded)", s["connectorsEnabled"], 4)
    check("enabled services (disabled service excluded)", s["servicesEnabled"], 6)
    check("services passing", s["servicesPassing"], 4)
    check("services failing", s["servicesFailing"], 2)
    check("health tally ok/degraded/failing/idle",
          [s["healthy"], s["degraded"], s["failing"], s["idle"]], [1, 1, 1, 1])
    # 6100 + 700 + 600 + 6230 + 5607; the action and merger rows are counted, not ingested.
    check("recordsLastRun counts each run once", s["recordsLastRun"], 19237)
    check("ingesting sources", s["ingestingSources"], 5)
    check("outbound actions counted separately", s["outboundActions"], 1)
    check("pipeline runs counted separately", s["pipelineRuns"], 1)
    check("last ingest is the newest run", s["lastIngestUtc"], "2026-07-27T14:04:00.000+00:00")

    rows = {(c["connector"], c["profile"]): c for c in out["connectors"]}
    aws = rows[("Amazon Web Services (AWS)", "Prod")]
    check("AWS healthy", aws["health"], "ok")
    check("AWS records (newest run per service)", aws["lastIngest"]["records"], 6800)
    check("AWS run matched by service name only", aws.get("ingestProfileInferred"), True)
    intune = rows[("Microsoft Intune", "Default profile")]
    check("Intune failing", intune["health"], "failing")
    check("Intune has no ingest block", "lastIngest" in intune, False)
    dd = rows[("Datadog Enterprise", "Lucidum")]
    check("Datadog degraded by a warning run", dd["health"], "degraded")
    check("Datadog counts only its enabled service", dd["servicesEnabled"], 1)
    cert = rows[("Domain Certificate", "Default profile")]
    check("cert connector is idle (green, never ran)", cert["health"], "idle")
    check("worst connector sorts first", out["connectors"][0]["health"], "failing")

    fails = out["failures"]
    check("one grouped failure per distinct message", len(fails), 2)
    check("grouped failure lists both services", sorted(fails[0]["services"]), ["Intune Device", "Intune User"])
    check("grouped failure counts services", fails[0]["serviceCount"], 2)
    check("connection-test failures rank first", fails[0]["kind"], "connection-test")
    check("failed ingestion reported too", fails[1]["kind"], "ingestion")
    check("nothing truncated at defaults", [out["failuresTruncated"], out["otherSourcesTruncated"]], [0, 0])

    other = {o["source"]: o for o in out["otherSources"]}
    check("disabled-profile source still listed", other["Microsoft Active Directory (AD)"]["records"], 6230)
    check("...and flagged as configured", other["Microsoft Active Directory (AD)"]["configured"], True)
    check("unconfigured source gets a readable name", "Okta User" in other, True)
    check("...and flagged as unconfigured", other["Okta User"]["configured"], False)
    check("Okta run health from its Error status", other["Okta User"]["health"], "fail")
    check("connector-covered services aren't repeated here",
          [k for k in other if k.startswith("Amazon") or k.startswith("Datadog")], [])

    # Extraction must keep every credential-ish field out of the summary.
    blob = json.dumps(out) + json.dumps(full)
    for leak in ("password", "gAAAAAB", "field_metadata", "10.0.0.9", "LDAP", "username"):
        check("no %r in output" % leak, leak in blob, False)
    # The post-Vault shape, and the non-secret configuration that a secret migration does not
    # remove. None of these are named by the allow-list, so none may appear whatever they are
    # called upstream.
    for future in ("vault_path", "secret/data", "secret_ref", "kv/meridian",
                   "proxy.internal.invalid", "role_arn", "role-placeholder-value"):
        check("no %r in output" % future, future in blob, False)

    check("pretty name: acronym + word", m._pretty_name("sepm_computers"), "SEPM Computers")
    check("pretty name: short non-acronym word", m._pretty_name("pan_vpn_log"), "PAN VPN Log")
    check("pretty name: 4-letter acronym", m._pretty_name("infoblox_dhcp"), "Infoblox DHCP")
    check("pretty name: ordinary words", m._pretty_name("okta_user"), "Okta User")
    check("profile object reduced to its name",
          m._profile_name({"profile_name": "Lucidum OCI", "config": {"password": "x"}}), "Lucidum OCI")
    check("run status casing/compound handling",
          [m._run_health(x) for x in ("Success", "SUCCESS", "Warning", "Warning&Error", "Error", "FAILED", None)],
          ["ok", "ok", "warn", "fail", "fail", "fail", "unknown"])

    # A scoped token that can read one endpoint but not the other must degrade, never raise - and an
    # unreadable run history must not be reported as "nothing has ingested".
    def half_blocked(method, endpoint, body=None, retries=1):
        if "connector/profile" in endpoint:
            return FIXTURE_PROFILES
        raise RuntimeError('HTTP 403: {"code":403001,"message":"Forbidden"}')

    m.call = half_blocked
    try:
        part = m.summarize_connectors()
    finally:
        m.call = real_call
    check("blocked half reported, not raised", part["fetched"]["profiles"], "ok")
    check("...with the error on the other half", "403" in part["fetched"]["ingestion"], True)
    check("connectors still enumerated", part["summary"]["connectorsEnabled"], 4)
    check("no connector called idle without run data", part["summary"]["idle"], 0)
    check("connection tests still judged", part["summary"]["failing"], 1)

    m.call = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("HTTP 403: Forbidden"))
    try:
        none = m.summarize_connectors()
    finally:
        m.call = real_call
    check("both halves blocked still returns a summary", none["summary"]["connectorsEnabled"], 0)
    check("...and says why for each half",
          ["403" in none["fetched"]["profiles"], "403" in none["fetched"]["ingestion"]], [True, True])


def test_connector_warning_messages(m):
    """A `degraded` connector used to say THAT its last run wasn't clean but never WHY - the per-
    service `event_messages` were read, mangled through _short() into a raw loguru dump, and then
    discarded entirely once several services rolled up into one connector verdict. `connectors[].
    lastIngest.notes` now carries the actual cause, in the reader's terms rather than a log line."""
    print("[3c] connector warning/failure messages, cleaned and surfaced (offline)")
    loguru_list = [{"WARNING": "2026-08-26 04:29:19.400 | WARNING  | loguru._logger:warning:1979 - "
                                "no local data template and save options, or invalid json file format\n"},
                   {"WARNING": "2026-08-26 04:29:19.401 | WARNING  | loguru._logger:warning:1979 - "
                                "no local data template and save options, or invalid json file format\n"}]
    check("loguru framing stripped, identical repeats deduped to one", m._friendly_event_message(loguru_list),
          "no local data template and save options, or invalid json file format")

    distinct = [{"WARNING": "2026-08-26 04:22:00.000 | WARNING | m:f:1 - first distinct problem\n"},
                {"WARNING": "2026-08-26 04:22:01.000 | WARNING | m:f:2 - second distinct problem\n"},
                {"WARNING": "2026-08-26 04:22:02.000 | WARNING | m:f:3 - third distinct problem\n"}]
    check("capped to `limit` distinct messages, joined",
          m._friendly_event_message(distinct, limit=2),
          "first distinct problem; second distinct problem")

    # A collapsed traceback truncated from the front is all frames and no cause. Measured on a live
    # stack, 22 connectors shared this exact message and every visible character of it was botocore
    # frame noise; the AccessDeniedException naming an actual AWS permission gap fell off the end.
    tb = ("2026-08-19 04:21:59.880 | WARNING | loguru._logger:warning:1896 - Traceback (most recent "
          'call last): File "aws_org.py", line 73, in tmp.app.data.aws_org.get_account File '
          '"/usr/local/lib/python3.10/site-packages/botocore/paginate.py", line 272, in __iter__ '
          "response = self._make_request(current_kwargs) File "
          '"/usr/local/lib/python3.10/site-packages/botocore/client.py", line 1078, in _make_api_call '
          "raise error_class(parsed_response, operation_name) "
          "botocore.errorfactory.AccessDeniedException: An error occurred (AccessDeniedException) "
          "when calling the ListAccounts operation: You don't have permissions to access this resource.")
    check("a traceback reduces to the exception it raised, not its call frames",
          m._friendly_event_message([{"WARNING": tb}]),
          "botocore.errorfactory.AccessDeniedException: An error occurred (AccessDeniedException) when "
          "calling the ListAccounts operation: You don't have permissions to access this resource.")
    check("...so it survives the 200-char cap intact",
          "permissions to access this resource" in m._friendly_event_message([{"WARNING": tb}]), True)
    check("the last exception wins, not a summary prefix",
          m._friendly_event_message([{"WARNING": "run-adhoc error: KeyError:'x' Traceback (most "
                                                 'recent call last): File "a.py", line 1, in f '
                                                 "IndexError: list index out of range"}]),
          "IndexError: list index out of range")
    check("two paths failing the same way collapse to one message",
          m._friendly_event_message([{"WARNING": 'Traceback (most recent call last): File "a.py", '
                                                 "line 1, in f ValueError: bad input"},
                                     {"WARNING": 'Traceback (most recent call last): File "b.py", '
                                                 "line 9, in g ValueError: bad input"}]),
          "ValueError: bad input")
    check("a traceback with no recognisable exception keeps its tail, not its frames",
          m._traceback_cause('Traceback (most recent call last): File "a.py", line 1, in f '
                             "something odd happened downstream"),
          "Traceback (most recent call last): something odd happened downstream")
    check("a message with no traceback is untouched",
          m._traceback_cause("MAC_Address does not exist in dataframe"),
          "MAC_Address does not exist in dataframe")

    check("plain string (no loguru framing) passes through unchanged",
          m._friendly_event_message("Okta | rate limited"), "Okta | rate limited")
    check("empty list -> None", m._friendly_event_message([]), None)
    check("None -> None", m._friendly_event_message(None), None)

    def run(service, status, msg, t):
        return {"service": service, "status": status, "records": 1, "utc": "2026-08-26T00:00:00Z",
                "_time": t, "message": msg}

    clean = m._last_ingest([run("svc_a", "Success", None, 1), run("svc_b", "Success", None, 2)])
    check("no notes when every service is clean", "notes" in clean, False)

    warned = m._last_ingest([run("svc_a", "Success", None, 1),
                              run("svc_b", "Warning", "quota nearly exhausted", 2)])
    check("a warn-tier run gets a note", len(warned.get("notes", [])), 1)
    check("...naming the message", warned["notes"][0]["message"], "quota nearly exhausted")
    check("...and the service", warned["notes"][0]["services"], ["svc_b"])
    check("...tagged as a warning, not a failure", warned["notes"][0]["severity"], "warn")

    shared = m._last_ingest([run("svc_a", "Warning", "same problem", 1),
                              run("svc_b", "Warning", "same problem", 2),
                              run("svc_c", "Warning", "different problem", 3)])
    check("identical messages across services collapse into one note", len(shared["notes"]), 2)
    check("...listing both affected services", sorted(shared["notes"][0]["services"]), ["svc_a", "svc_b"])
    check("...biggest group first", shared["notes"][0]["message"], "same problem")

    mixed = m._last_ingest([run("svc_a", "Warning", "just a warning", 1),
                             run("svc_b", "Error", "an actual failure", 2)])
    check("fail-tier note ranks ahead of a warn-tier one", mixed["notes"][0]["severity"], "fail")
    check("...even though it's the smaller group", mixed["notes"][0]["message"], "an actual failure")
    check("overall verdict is the worse of the two", mixed["health"], "fail")


def test_connector_brief_shape(m):
    """`connectors` is the one call SKILL.md makes mandatory every session, so its payload is pure
    overhead on whatever the user actually asked. Measured on a live 58-connector stack it was 75,288
    chars (~20,900 tokens) of which 50,277 was the connector block -- while 50 of the 58 connectors
    were ingesting *and* warning, drawing 143 note instances from just 67 distinct messages. One
    message appeared on 22 separate connectors; 8,892 of 18,023 message chars were byte-identical
    repeats.

    Brief mode reshapes that message-first. What this guards is the set of things it must NOT cost:

      * no connector row may be dropped -- `_snapshot_coverage` asserts len(failingNames) == failing,
        so a missing row makes a trend report movement in coverage that never happened;
      * a row that warned must stay marked as having warned even when its cause loses the cap, or a
        truncated detail reads as a clean run (§1.5 depends on a blank warning cell meaning clean);
      * both §1.5 tables have to stay populated -- failing rows must not spend the delivering-row
        detail budget, which is exactly the bug the first draft shipped with;
      * `--full` has to still produce the per-row notes[] the shape replaced.
    """
    print("[3d] connectors brief shape (offline)")

    # 24 connectors: 2 fail their connection test, 20 ingest fine but share only 3 distinct warnings
    # (so the message table dedupes hard), and 2 are clean. Enough to trip both caps.
    profiles, runs = [], []
    for i in range(24):
        fails = i < 2
        profiles.append({"display_name": "Conn%02d" % i, "bridge_name": "b%02d" % i,
                         "profile_name": "P%02d" % i, "group": "Group%d" % (i % 3),
                         "services_list": [{"service": "s%02d" % i, "display_name": "S%02d" % i,
                                            "status": "FAIL" if fails else "OK", "activity": True,
                                            "message": "401 Unauthorized" if fails else None}]})
        if fails:
            continue
        clean = i >= 22
        runs.append({"bridge_name": "s%02d" % i, "platform": "api", "profile": "P%02d" % i,
                     "status": "Success" if clean else "Warning",
                     # Descending records, so the detail window is a known prefix.
                     "output_records": 10000 - i * 100,
                     "_utc": "2026-08-26T00:00:00.000+00:00", "_time": 1790000000 + i,
                     "event_messages": None if clean else "shared cause %d" % (i % 3)})

    def fake_call(method, endpoint, body=None, retries=1):
        if "connector/profile" in endpoint:
            return {"connectorProfiles": profiles}
        if "metrics/connector" in endpoint:
            return {"content": runs}
        raise AssertionError("unexpected endpoint %r" % endpoint)

    real_call = m.call
    m.call = fake_call
    try:
        brief = m.summarize_connectors(brief=True)
        full = m.summarize_connectors(brief=False)
    finally:
        m.call = real_call

    check("brief is labelled as such", brief["shape"], "brief")
    check("full carries no shape marker", "shape" in full, False)

    # --- nothing is dropped -------------------------------------------------------------------
    check("brief keeps every connector row", len(brief["connectors"]), len(full["connectors"]))
    check("...all 24 of them", len(brief["connectors"]), 24)
    check("summary is untouched", brief["summary"], full["summary"])
    check("failures are untouched", brief["failures"], full["failures"])

    # --- the coverage invariant the trend layer rests on --------------------------------------
    cov_b = m._snapshot_coverage(brief, [])
    cov_f = m._snapshot_coverage(full, [])
    check("coverage derived from brief matches coverage from full", cov_b, cov_f)
    check("len(failingNames) == failing still holds",
          len(cov_b["failingNames"]), cov_b["failing"])

    # --- messages are deduped, not repeated ---------------------------------------------------
    groups = brief["warningGroups"]
    check("3 distinct causes across 20 warning connectors", len(groups), 3)
    check("...biggest group first", groups[0]["connectorCount"] >= groups[-1]["connectorCount"], True)
    check("...naming the connectors it affects", len(groups[0]["connectors"]), groups[0]["connectorCount"])
    check("group identity is the qualified one", " (" in groups[0]["connectors"][0], True)
    check("no per-row notes survive in brief",
          any("notes" in (c.get("lastIngest") or {}) for c in brief["connectors"]), False)
    check("--full still carries per-row notes",
          any("notes" in (c.get("lastIngest") or {}) for c in full["connectors"]), True)
    check("brief is materially smaller",
          len(json.dumps(brief)) < len(json.dumps(full)) * 0.8, True)

    # --- a truncated cause must not read as a clean run ---------------------------------------
    # Re-brief a fresh copy of the full result with the caps squeezed down, so the truncation paths
    # run against known inputs rather than whatever the fixture happens to produce at the defaults.
    tight = m._brief_connectors(json.loads(json.dumps(full)), max_warnings=1, max_detail=3)
    check("groups capped to 1", len(tight["warningGroups"]), 1)
    check("...and the drop is counted, not silent", tight["warningGroupsTruncated"], 2)
    check("every warning connector is still marked warned",
          len([c for c in tight["connectors"] if c.get("warned")]), 20)
    # 20 warning connectors over 3 equal-ish causes; the one surviving group's members are detailed,
    # every other warning instance is counted as undetailed. The two must sum to 20.
    check("undetailed instances account for the rest",
          tight["warningsUndetailed"] + tight["warningGroups"][0]["connectorCount"], 20)
    check("a squeezed detail window still yields that many delivering rows",
          len([c for c in tight["connectors"]
               if (c.get("lastIngest") or {}).get("utc")
               and (c["connector"], c["profile"]) not in
               {(f["connector"], f["profile"]) for f in tight["failures"]}]), 3)

    # --- both §1.5 tables stay populated ------------------------------------------------------
    failed_idents = {(f["connector"], f["profile"]) for f in brief["failures"]}
    detailed_delivering = [c for c in brief["connectors"]
                           if (c.get("lastIngest") or {}).get("utc")
                           and (c["connector"], c["profile"]) not in failed_idents]
    check("the delivering table gets its full detail window", len(detailed_delivering), 10)
    check("a failing row never spends that budget",
          all((c["connector"], c["profile"]) not in failed_idents for c in detailed_delivering), True)
    rolled = [c for c in brief["connectors"]
              if (c.get("lastIngest") or {}) and "utc" not in (c.get("lastIngest") or {})]
    check("rolled-up rows keep their record count", all("records" in c["lastIngest"] for c in rolled), True)
    check("...and drop the timestamp/status", all("status" not in c["lastIngest"] for c in rolled), True)
    check("group is dropped from brief rows", any("group" in c for c in brief["connectors"]), False)


def test_preflight_coverage(m):
    """`_preflight_coverage` -- the rollup `connect --with-connectors` applies on top of brief.

    Brief already cut the payload 71% by inverting it message-first. What it could not cut is the
    row count: SKILL.md §1.5 renders roughly 6-8 delivering rows plus a rollup line and 6-8 needing
    attention, so on a 58-connector stack the model is handed 58 full rows to print about 18 of
    them. Measured live, 8,377 of the block's 12,933 chars described connectors that reach the
    answer only as "+ 40 more delivering data: ...".

    This aligns the payload with what §1.5 already prints rather than changing what it prints, so
    the contract needs no edit -- which is the whole design, and what this guards. Every row §1.5
    draws has to arrive unchanged:

      * a row in failures[], or failing outright, is the needs-attention list and is never rolled;
      * a delivering row still carrying lastIngest.status is one of the detail-window rows the
        delivering table draws with its Records and Last-ingest columns, and is never rolled;
      * a rolled row keeps its name, profile and record count, because §1.5 requires the rollup line
        to name connectors and says a bare count is not actionable;
      * a rolled row that warned stays marked warned -- the same rule brief keeps, and the one
        mistake this shape could cause that brief could not: §1.5 reads a blank warning cell as
        "the run was actually clean";
      * summary passes through verbatim, and failures/warningGroups losslessly: each cause is quoted
        once, by `id`, and the rows and failures it covers point at it with `warningIds`. Rebuilding
        the original groups and failure messages from those ids has to give back exactly the input,
        since warningGroups is how §1.5 rule 2 answers "what was the warning". Measured on the local
        51-connector stack, the repetition this removes was 84 mentions of 51 names plus 7 of 10
        failure messages, and the block went 18,810 -> 15,693 chars;

    And the invariant one layer down: this is presentation, applied at cmd_connect's boundary only.
    digest/snapshot call summarize_connectors() in-process, so _snapshot_coverage must see exactly
    what it saw before -- asserted here by deriving coverage from the input after the rollup ran,
    which also catches the shape mutating its argument in place.
    """
    print("[3g] preflight coverage rollup (offline)")

    # Same population shape as the brief fixture: 2 fail their connection test, 20 ingest with a
    # shared cause, 2 are clean -- enough that the detail window closes and rows actually roll up.
    profiles, runs = [], []
    for i in range(24):
        fails = i < 2
        profiles.append({"display_name": "Conn%02d" % i, "bridge_name": "b%02d" % i,
                         "profile_name": "P%02d" % i, "group": "Group%d" % (i % 3),
                         "services_list": [{"service": "s%02d" % i, "display_name": "S%02d" % i,
                                            "status": "FAIL" if fails else "OK", "activity": True,
                                            "message": "401 Unauthorized" if fails else None}]})
        if fails:
            continue
        clean = i >= 22
        runs.append({"bridge_name": "s%02d" % i, "platform": "api", "profile": "P%02d" % i,
                     "status": "Success" if clean else "Warning",
                     "output_records": 10000 - i * 100,
                     "_utc": "2026-08-26T00:00:00.000+00:00", "_time": 1790000000 + i,
                     "event_messages": None if clean else "shared cause %d" % (i % 3)})

    def fake_call(method, endpoint, body=None, retries=1):
        if "connector/profile" in endpoint:
            return {"connectorProfiles": profiles}
        if "metrics/connector" in endpoint:
            return {"content": runs}
        raise AssertionError("unexpected endpoint %r" % endpoint)

    real_call = m.call
    m.call = fake_call
    try:
        brief = m.summarize_connectors(brief=True)
    finally:
        m.call = real_call

    baseline = json.loads(json.dumps(brief))          # what cmd_connect --coverage-full would print
    pre = m._preflight_coverage(brief)

    check("preflight is labelled as such", pre["shape"], "preflight")
    check("...and says how many rows moved", pre["deliveringRolledUp"], len(pre["delivering"]))
    check("...in words as well as a key name", "delivering[]" in (pre.get("deliveringNote") or ""), True)
    check("rows did move (the fixture actually exercises it)", pre["deliveringRolledUp"] > 0, True)

    # --- no connector is lost -----------------------------------------------------------------
    # The failure this guards is not a smaller payload, it is a connector that silently stops
    # existing: §1.5's headline is a count of what is delivering, and a dropped row makes it wrong
    # in the reassuring direction.
    named = ([m._coverage_ident(r) for r in pre["connectors"]]
             + [m._coverage_ident(r) for r in pre["delivering"]])
    check("every connector row is still represented",
          sorted(named), sorted(m._coverage_ident(r) for r in baseline["connectors"]))
    check("...exactly once each", len(named), len(set(named)))
    check("...and the tally still matches the rows", len(named), baseline["summary"]["connectorsEnabled"])

    # --- the two §1.5 tables keep every row they draw -----------------------------------------
    failed_idents = {m._coverage_ident(f) for f in pre["failures"]}
    check("no failing row was rolled up",
          any(m._coverage_ident(r) in failed_idents or r.get("warned") == "fail"
              for r in pre["delivering"]), False)
    check("...and all of them are still kept in full",
          failed_idents <= {m._coverage_ident(r) for r in pre["connectors"]}, True)
    check("no detail-window row was rolled up",
          all((r.get("lastIngest") or {}).get("status") is None for r in pre["delivering"]), True)
    detailed = [r for r in pre["connectors"]
                if (r.get("lastIngest") or {}).get("utc") and m._coverage_ident(r) not in failed_idents]
    check("the delivering table still gets its full detail window", len(detailed), 10)

    # --- an elided row is never a clean one ---------------------------------------------------
    warned_before = {m._coverage_ident(r) for r in baseline["connectors"] if r.get("warned")}
    warned_after = ({m._coverage_ident(r) for r in pre["connectors"] if r.get("warned")}
                    | {m._coverage_ident(r) for r in pre["delivering"] if r.get("warned")})
    check("every warned row is still marked warned", warned_after, warned_before)
    check("a rolled row keeps its record count",
          all("records" in r for r in pre["delivering"]), True)
    check("...and its profile, so the ident survives",
          all(r.get("profile") for r in pre["delivering"]), True)

    # --- the blocks §1.5 reads for causes and headline numbers pass through untouched ----------
    check("summary is verbatim", pre["summary"], baseline["summary"])
    check("the input is not mutated", brief, baseline)
    # This fixture's idents are short ("Conn02 (P02)"), which makes a `warningIds` reference dearer
    # than the name it replaces -- so it is exactly the block the index has to leave alone.
    check("a block the warning index would grow is left verbatim",
          (pre["warningGroups"], pre["failures"], "warningIdsNote" in pre),
          (baseline["warningGroups"], baseline["failures"], False))

    # --- the trend layer must not be able to tell this happened -------------------------------
    # _snapshot_coverage runs off summarize_connectors() in-process, never off this verb's stdout.
    # Deriving it from `brief` *after* the rollup ran is the assertion: if _preflight_coverage
    # mutated its argument, coverage would move here and a trend would report connectors entering
    # and leaving the failing set that never moved.
    cov_after = m._snapshot_coverage(brief, [])
    cov_baseline = m._snapshot_coverage(baseline, [])
    check("snapshot coverage is unchanged by the rollup", cov_after, cov_baseline)
    check("len(failingNames) == failing still holds",
          len(cov_after["failingNames"]), cov_after["failing"])

    check("preflight is materially smaller",
          len(json.dumps(pre)) < len(json.dumps(baseline)) * 0.85, True)

    # --- degenerate inputs ---------------------------------------------------------------------
    # A scoped token returns {"unavailable": ...} and a stack with nothing configured returns no
    # rows; neither may become an exception on the one call SKILL.md makes mandatory.
    check("an unavailable block passes through", m._preflight_coverage({"unavailable": "403"}),
          {"unavailable": "403"})
    check("an empty row list passes through unmarked",
          "shape" in m._preflight_coverage({"connectors": [], "failures": []}), False)
    check("a non-dict passes through", m._preflight_coverage(None), None)
    # Nothing to roll up means nothing to explain: no marker, so a reader is never told rows moved
    # when none did.
    small = m._preflight_coverage({"connectors": baseline["connectors"][:2], "failures": []})
    check("...and so does a population smaller than the detail window", "delivering" in small, False)

    # --- the warning index: live-shaped idents, a failure dedupe, an elided cause -------------
    # Hand-built to the live stack's proportions: idents ~50 chars (measured average 46), one cause
    # spanning most connectors, and an ingestion failure -- which is both a failures[] entry and a
    # fail-severity group with the byte-identical message, the duplication measured on 7 of 10 live
    # failures. Rows carry no lastIngest, so every one but the failing row rolls into delivering[].
    long_msg = ("AccessDeniedException: User is not authorized to perform organizations:ListAccounts "
                "because no identity-based policy allows the action on this resource")
    idents = [("Amazon Web Services (AWS)", "AWS_Account_%02d_Prod_Connector" % i) for i in range(9)]
    hand = {
        "summary": {"connectorsEnabled": 10},
        "connectors": [{"connector": c, "profile": p, "health": "degraded",
                        "warned": "fail" if i == 0 else "warn"} for i, (c, p) in enumerate(idents)]
                      + [{"connector": "Okta", "profile": "Prod", "health": "ok"}],
        "failures": [{"connector": idents[0][0], "profile": idents[0][1], "kind": "ingestion",
                      "status": "Error", "message": long_msg},
                     {"connector": "Intune", "profile": "Corp", "kind": "connection-test",
                      "status": "FAIL", "message": "401 Unauthorized"}],
        # idents[8] warned, but its cause is the one the cap dropped.
        "warningGroups": [{"severity": "fail", "message": long_msg,
                           "connectors": ["%s (%s)" % idents[0]], "connectorCount": 1},
                          {"severity": "warn", "message": "Rate limited, retried",
                           "connectors": ["%s (%s)" % x for x in idents[:8]], "connectorCount": 8}],
        "warningGroupsTruncated": 1, "warningsUndetailed": 1,
    }
    hand_copy = json.loads(json.dumps(hand))
    h = m._preflight_coverage(hand)
    check("a live-shaped block is indexed", "warningIdsNote" in h, True)
    check("every warning group has an id and no member list",
          all(g.get("id") and "connectors" not in g for g in h["warningGroups"]), True)
    rebuilt, fails = _rebuild_warnings(m, h)
    check("warningGroups rebuild exactly from warningIds", rebuilt, _norm_groups(hand_copy["warningGroups"]))
    check("failures rebuild exactly", fails, hand_copy["failures"])
    rows = {m._coverage_ident(r): r for r in h["connectors"] + h.get("delivering", [])}
    first, elided = "%s (%s)" % idents[0], "%s (%s)" % idents[8]
    check("a row in two groups points at both", rows[first].get("warningIds"), ["w1", "w2"])
    check("a failure repeating a group's message points at it instead",
          (h["failures"][0].get("warningIds"), "message" in h["failures"][0]), (["w1"], False))
    check("a failure with its own message keeps it",
          (h["failures"][1].get("message"), "warningIds" in h["failures"][1]), ("401 Unauthorized", False))
    # The one mistake this shape could add: an elided cause read as a clean run.
    check("an elided cause keeps warned and gets no warningIds",
          (rows[elided].get("warned"), "warningIds" in rows[elided]), ("warn", False))
    check("a row with no cause carries no warningIds", "warningIds" in rows["Okta (Prod)"], False)
    check("the hand-built input is not mutated", hand, hand_copy)
    # A group naming a connector no row carries cannot be rebuilt from rows, so indexing it would
    # silently drop that membership. The whole block has to pass through instead.
    orphan = json.loads(json.dumps(hand_copy))
    orphan["warningGroups"][1]["connectors"].append("Gone (x)")
    o = m._preflight_coverage(orphan)
    check("an unmatched group member leaves the block verbatim",
          (o.get("warningGroups"), o.get("failures"), "warningIdsNote" in o),
          (orphan["warningGroups"], orphan["failures"], False))


def _norm_groups(groups):
    """Groups with member order ignored: the preflight splits rows across two lists, so the order
    names came back in carries no meaning -- membership is what has to survive."""
    return [dict(g, connectors=sorted(g["connectors"])) for g in groups]


def _rebuild_warnings(m, pre):
    """Undo _index_warning_groups: (groups with member lists, failures with messages)."""
    groups = {g["id"]: dict({k: v for k, v in g.items() if k != "id"}, connectors=[])
              for g in pre["warningGroups"]}
    for r in pre["connectors"] + pre.get("delivering", []):
        for gid in r.get("warningIds") or []:
            groups[gid]["connectors"].append(m._coverage_ident(r))
    fails = []
    for f in pre["failures"]:
        f = dict(f)
        if "warningIds" in f:
            f["message"] = groups[f.pop("warningIds")[0]]["message"]
        fails.append(f)
    return _norm_groups(list(groups.values())), fails


def test_result_cache(m):
    """The aggregate cache, and the one thing it must never do: feed a snapshot.

    Why it exists: the API has no aggregation endpoint and no field projection, so an aggregate is paid
    for in downloaded records. Measured live -- one 100-record /data/cmdb page is 5.87 MB, the connector
    run history is 24.8 MB x 4 pages, `summary --by Risk_Level` is 11 calls and ~47 MB for 85 tokens of
    output, and `--by sourcetype` is 33 calls, which cannot fit a hard 60/min budget and so spent 92.6s
    waiting on the pacer. All of it describes a daily-cadence fact.

    The hazard, and the reason this test is the interesting one: `take_snapshot` and `digest --snapshot`
    write a history row that `trend` later compares. Serving that row from cache would stamp a value
    that was never measured today with today's date -- inventing a flat segment, which per
    design/trends.md is the most convincing wrong answer this tool can produce. Both paths must refetch.
    """
    print("[3e] aggregate result cache (offline)")
    import tempfile
    real = (m.CFG_DIR, m.call, m.load_config, m.check_fields, m.field_type)
    tmp = tempfile.mkdtemp(prefix="rescache-")
    m.CFG_DIR = tmp
    m.load_config = lambda: ("s.example", "tok", None)
    m.check_fields = lambda *a, **k: None
    m.field_type = lambda t, f: "String"
    os.environ.pop("MERIDIAN_NO_CACHE", None)   # this test is the one that exercises cached reads

    calls = []

    def fake_call(method, endpoint, body=None, retries=1):
        calls.append(endpoint)
        paging = (body or {}).get("paging") or {}
        if paging.get("recordsPerPage") == 1:
            return {"totalRecords": 120, "data": []}
        return {"totalRecords": 120,
                "data": [{"Risk_Level": "1-low"} for _ in range(100)]}

    m.call = fake_call
    # Every breakdown here passes a rebuild stamp. Since design/data-currency.md the stamp is part of
    # the key and NO stamp means NO cache, so a call without one would make each "is a miss" check
    # below pass trivially -- and leave the 0600 check stat()-ing a file that was never written.
    stamp = "2026-09-24T10:12:12Z"

    def sb(*args, **kw):
        kw.setdefault("rebuilt", stamp)
        return m.summarize_by(*args, **kw)

    try:
        # --- a second identical breakdown costs nothing ---------------------------------------
        first = sb("asset", "Risk_Level")
        n_first = len(calls)
        second = sb("asset", "Risk_Level")
        check("a repeated breakdown issues no further calls", len(calls), n_first)
        check("...and the first one actually cost calls", n_first > 1, True)
        check("cached groups match the computed ones", second["groups"], first["groups"])
        check("a fresh result carries no cache stamp", "fromCache" in first, False)
        check("a served result is stamped", second.get("fromCache"), True)
        check("...with an age, so staleness is statable", isinstance(second.get("cacheAgeSeconds"), int), True)

        # --- the cache key discriminates ------------------------------------------------------
        calls.clear()
        sb("asset", "Risk_Level", where=["OS == String Windows"])
        check("a different --where is a different entry", len(calls) > 0, True)
        calls.clear()
        sb("user", "Risk_Level")
        check("a different table is a different entry", len(calls) > 0, True)

        # --- the LDG rebuild is part of the key ------------------------------------------------
        # A count cached just before a merge must not be served after it: the TTL alone would do
        # exactly that for up to RESCACHE_TTL.
        calls.clear()
        sb("asset", "Risk_Level", rebuilt="2026-09-24T14:05:00Z")
        check("a later rebuild is a miss, not a pre-merge hit", len(calls) > 0, True)
        calls.clear()
        back = sb("asset", "Risk_Level")
        check("...and the original rebuild's entry still hits", (len(calls), back.get("fromCache")), (0, True))

        # --- no stamp, no cache: read AND write ---------------------------------------------------
        calls.clear()
        unstamped = m.summarize_by("asset", "Risk_Level", rebuilt=None)
        check("an unknown rebuild never reads the cache", (len(calls) > 0, "fromCache" in unstamped), (True, False))
        before = set((m._rescache_read().get("entries") or {}))
        m.summarize_by("asset", "Risk_Level", where=["OS == String Solaris"], rebuilt=None)
        check("...and never writes to it", set((m._rescache_read().get("entries") or {})), before)

        # --- refresh bypasses -----------------------------------------------------------------
        calls.clear()
        again = sb("asset", "Risk_Level", refresh=True)
        check("--refresh recomputes", len(calls) > 0, True)
        check("...and its result is unstamped", "fromCache" in again, False)

        # --- the kill switch ------------------------------------------------------------------
        os.environ["MERIDIAN_NO_CACHE"] = "1"
        calls.clear()
        sb("asset", "Risk_Level")
        check("MERIDIAN_NO_CACHE=1 disables reads", len(calls) > 0, True)
        os.environ.pop("MERIDIAN_NO_CACHE", None)

        # --- TTL expiry -----------------------------------------------------------------------
        # Age the stored entry past its TTL rather than sleeping.
        data = m._rescache_read()
        for rec in (data.get("entries") or {}).values():
            rec["at"] = time.time() - (m.RESCACHE_TTL + 60)
        m._private_write(m._rescache_path(), data)
        calls.clear()
        sb("asset", "Risk_Level")
        check("an entry past its TTL is a miss", len(calls) > 0, True)

        # --- a future timestamp is a miss, not an un-ageable hit ------------------------------
        data = m._rescache_read()
        for rec in (data.get("entries") or {}).values():
            rec["at"] = time.time() + 86400
        m._private_write(m._rescache_path(), data)
        calls.clear()
        sb("asset", "Risk_Level")
        check("a backwards clock is a miss, not a hit with an unstatable age", len(calls) > 0, True)

        # --- an unverifiable breakdown is not cached ------------------------------------------
        # `complete` absent means the coverage check could not run; caching that would pin an
        # unanswered question in place for the whole TTL.
        m.drop_rescache()
        broken = dict(sb("asset", "Risk_Level"))
        broken.pop("complete", None)
        check("a breakdown without a completeness verdict is refused by the cache",
              m.rescache_get("summary_by", m.RESCACHE_TTL,
                             table="asset", by="NeverComputed", where=[])[0], None)

        # --- THE ONE THAT MATTERS: a snapshot must never be served from cache -----------------
        m.drop_rescache()
        seen = {"digest": 0}
        real_bd = m.build_digest

        def counting_digest(table="asset", by=None, field="Risk_Score", top=5, refresh=False):
            seen["digest"] += 1
            seen["last_refresh"] = refresh
            return {"generated": "digest", "stack": "s.example", "table": table,
                    "metrics": {"metrics": {"assetCount": 1}}, "headline": {}}

        m.build_digest = counting_digest
        try:
            class A:
                table, by, field, top, snapshot = "asset", None, "Risk_Score", 5, True
            import contextlib, io
            buf = io.StringIO()
            real_ts = m.take_snapshot
            m.take_snapshot = lambda **k: {"written": True}
            try:
                with contextlib.redirect_stdout(buf):
                    m.cmd_digest(A())
            finally:
                m.take_snapshot = real_ts
            check("digest --snapshot computes with refresh=True", seen["last_refresh"], True)

            class B:
                table, by, field, top, snapshot = "asset", None, "Risk_Score", 5, False
            with contextlib.redirect_stdout(io.StringIO()):
                m.cmd_digest(B())
            check("a plain digest may serve from cache", seen["last_refresh"], False)
        finally:
            m.build_digest = real_bd

        # The standalone `snapshot` verb goes through take_snapshot, not cmd_digest, so it needs its
        # own guard -- it computes the digest itself when none is handed in.
        seen["last_refresh"] = None
        m.build_digest = counting_digest
        real_metrics, real_load = m.measure_metrics, m.load_metrics
        m.measure_metrics, m.load_metrics = (lambda mm: {}), (lambda: {})
        try:
            m.take_snapshot(table="asset", by="Risk_Level", with_metrics=False)
        except Exception:
            pass   # storage side-effects are not what this asserts
        finally:
            m.measure_metrics, m.load_metrics = real_metrics, real_load
            m.build_digest = real_bd
        check("take_snapshot computes with refresh=True too", seen["last_refresh"], True)

        # build_digest itself must forward refresh to both cached building blocks.
        got = {}
        real_sc, real_sb, real_tn, real_sm = (m.summarize_connectors, m.summarize_by,
                                             m.top_n, m.stack_metrics)
        def note_connectors(*a, **k):
            got["connectors"] = k.get("refresh")
            return {"summary": {}}

        def note_summary(t, b, w=None, **k):
            got["summary"] = k.get("refresh")
            return {}

        m.stack_metrics = lambda: {"metrics": {}}
        m.summarize_connectors = note_connectors
        m.summarize_by = note_summary
        m.top_n = lambda *a, **k: {}
        try:
            real_bd("asset", "Risk_Level", "Risk_Score", 5, refresh=True)
            check("build_digest(refresh=True) refreshes the coverage block", got.get("connectors"), True)
            check("...and the breakdown", got.get("summary"), True)
        finally:
            m.summarize_connectors, m.summarize_by = real_sc, real_sb
            m.top_n, m.stack_metrics = real_tn, real_sm

        # --- the file itself -------------------------------------------------------------------
        # Not a token file, but not public either: the cached coverage block carries connector
        # warning messages, and those were measured to contain customer hostnames. So it goes through
        # _private_write for the same 0600-and-atomic treatment as config.json, and a read-modify-write
        # racing a scheduled digest must not leave a temp file or half-written JSON behind.
        m.drop_rescache()
        sb("asset", "Risk_Level")
        check("no temp file survives a cache write",
              [f for f in os.listdir(tmp) if f.endswith(".tmp")], [])
        if os.name == "posix":
            import stat
            mode = stat.S_IMODE(os.stat(m._rescache_path()).st_mode)
            check("rescache is owner-only (0600)", oct(mode), oct(0o600))
        check("a corrupt cache file is a miss, not a crash", (lambda: (
            open(m._rescache_path(), "w", encoding="utf-8").write("{not json"),
            sb("asset", "Risk_Level").get("groups") is not None)[1])(), True)
        m.drop_rescache()
        check("a wrong-schema file is ignored", (lambda: (
            open(m._rescache_path(), "w", encoding="utf-8").write('{"schema": 99, "entries": {}}'),
            m._rescache_read())[1])(), {})

        # --- a partial connector fetch is never cached ----------------------------------------
        m.drop_rescache()
        real_fp, real_fr = m._fetch_connector_profiles, m._fetch_connector_runs
        m._fetch_connector_profiles = lambda: (_ for _ in ()).throw(RuntimeError("HTTP 403: nope"))
        m._fetch_connector_runs = lambda: ({}, [], [], False)
        try:
            partial = m.summarize_connectors()
            check("a half-read coverage summary still answers", partial["fetched"]["ingestion"], "ok")
            check("...and is NOT cached, so one 403 can't hide the stack for an hour",
                  m.rescache_get("connectors", m.RESCACHE_CONNECTOR_TTL,
                                 max_failures=12, max_other=15)[0], None)
        finally:
            m._fetch_connector_profiles, m._fetch_connector_runs = real_fp, real_fr
    finally:
        m.CFG_DIR, m.call, m.load_config, m.check_fields, m.field_type = real
        os.environ["MERIDIAN_NO_CACHE"] = "1"


def test_connector_runs_pagination(m):
    """`/CMDB/v2/system/metrics/connector?size=2000` is a page SIZE, not a history depth, and the
    endpoint returns runs unsorted. A stack with more than one page of history used to be read from
    page 0 only - on a real stack that page happened to hold only the 3 oldest days of an 8-day
    history, so a connector's `lastIngest` read up to 5 days stale and a persistent nightly failure
    looked like an old, resolved one. This drives summarize_connectors() through a 2-page and an
    over-cap history to prove every page is read (up to CONNECTOR_RUNS_MAX_PAGES) and merged, and
    that exceeding the cap is reported rather than silently truncated."""
    print("[3a] connector run history pagination (offline, fixture-driven)")
    profiles = {"connectorProfiles": [
        {"display_name": "Amazon Web Services (AWS)", "bridge_name": "aws", "profile_name": "Prod",
         "group": "Cloud Infrastructure",
         "services_list": [{"service": "aws_s3", "display_name": "AWS S3", "status": "OK", "activity": True}]},
    ]}
    # Page 0 holds only the older, failing run; the newer, successful one is on page 1. Unsorted, so
    # a reader that stops at page 0 sees only the stale failure.
    page0 = {"totalPages": 2, "content": [
        {"bridge_name": "aws_s3", "platform": "aws", "profile": "Prod", "status": "Error",
         "output_records": 0, "_utc": "2026-08-19T04:22:00.000+00:00", "_time": 1787113320},
    ]}
    page1 = {"totalPages": 2, "content": [
        {"bridge_name": "aws_s3", "platform": "aws", "profile": "Prod", "status": "Success",
         "output_records": 700, "_utc": "2026-08-26T05:09:00.000+00:00", "_time": 1787720940},
    ]}
    calls = []

    def fake_call(method, endpoint, body=None, retries=1):
        calls.append(endpoint)
        if "connector/profile" in endpoint:
            return profiles
        if "metrics/connector" in endpoint:
            return page1 if "page=1" in endpoint else page0
        raise AssertionError("unexpected endpoint %r" % endpoint)

    real_call = m.call
    m.call = fake_call
    try:
        out = m.summarize_connectors()
    finally:
        m.call = real_call

    metrics_calls = [c for c in calls if "metrics/connector" in c]
    check("both pages fetched", len(metrics_calls), 2)
    check("page 0 requested", any("page=0" in c for c in metrics_calls), True)
    check("page 1 requested", any("page=1" in c for c in metrics_calls), True)
    check("ingestion still reports ok", out["fetched"]["ingestion"], "ok")
    check("not flagged truncated under the cap", out["summary"]["runHistoryTruncated"], False)

    aws = out["connectors"][0]
    check("newest run (page 1) wins, not page 0's stale one", aws["lastIngest"]["utc"],
          "2026-08-26T05:09:00.000+00:00")
    check("newest run's status carried through", aws["lastIngest"]["status"], "Success")
    check("summary lastIngestUtc reflects the later page", out["summary"]["lastIngestUtc"],
          "2026-08-26T05:09:00.000+00:00")

    # A history deeper than the page cap must fetch only up to the cap and say the read may be
    # incomplete, rather than either hanging on an unbounded fetch or silently under-reading.
    many_calls = []

    def fake_call_many(method, endpoint, body=None, retries=1):
        many_calls.append(endpoint)
        if "connector/profile" in endpoint:
            return profiles
        if "metrics/connector" in endpoint:
            return {"totalPages": m.CONNECTOR_RUNS_MAX_PAGES + 5, "content": []}
        raise AssertionError("unexpected endpoint %r" % endpoint)

    m.call = fake_call_many
    try:
        capped = m.summarize_connectors()
    finally:
        m.call = real_call

    metrics_calls_many = [c for c in many_calls if "metrics/connector" in c]
    check("pagination stops at the cap, not totalPages", len(metrics_calls_many), m.CONNECTOR_RUNS_MAX_PAGES)
    check("over-cap history flagged, not silently truncated", capped["summary"]["runHistoryTruncated"], True)
    check("...and named in the message", "exceeds" in capped["summary"]["message"], True)


def test_api_guard(m):
    """/CMDB/v2/connector/profile returns connector credentials despite the docs; only `connectors`
    (which strips them through an in-process allow-list) may read it. That rule used to live only
    in SKILL.md prose, so `api CMDB/v2/connector/profile` printed service accounts raw."""
    print("[3b] raw api verb refuses the credential endpoint (offline)")
    import contextlib, io
    calls = []
    real_call = m.call
    m.call = lambda method, endpoint, body=None, retries=1: calls.append(endpoint) or {}

    def run(endpoint):
        class A:
            method, body, body_file = "GET", None, None
        a = A(); a.endpoint = endpoint
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(buf):
                m.cmd_api(a)
            return None
        except SystemExit:
            return buf.getvalue()

    try:
        out = run("CMDB/v2/connector/profile")
        check("credential endpoint is refused", bool(out), True)
        check("...pointing at `connectors`", "connectors" in (out or ""), True)
        check("...with the leading slash too", bool(run("/CMDB/v2/connector/profile")), True)
        check("...a query string doesn't slip past", bool(run("/CMDB/v2/connector/profile?size=2000")), True)
        check("...nor a trailing slash", bool(run("CMDB/v2/connector/profile/")), True)
        # The bypass that shipped: the guard compared the RAW path, but the API percent-decodes, so
        # `profil%65` was served while reading as a different endpoint here. Every spelling the
        # server would honour has to be refused, or the guard only stops the honest caller.
        check("...nor a percent-encoded character", bool(run("CMDB/v2/connector/profil%65")), True)
        check("...nor one at the start of the segment", bool(run("CMDB/v2/connector/%70rofile")), True)
        check("...nor a double-encoded one", bool(run("CMDB/v2/connector/profil%2565")), True)
        check("...nor an encoded separator", bool(run("CMDB/v2/connector%2Fprofile")), True)
        check("...nor an empty segment", bool(run("CMDB/v2/connector//profile")), True)
        check("...nor a dot segment", bool(run("CMDB/v2/connector/./profile")), True)
        check("...nor a case variant", bool(run("CMDB/v2/Connector/Profile")), True)
        check("...nor a backslash spelling", bool(run("CMDB\\v2\\connector\\profile")), True)
        calls.clear()
        check("the connector CATALOG is still reachable", run("CMDB/v2/connector"), None)
        check("...and went to the wire", calls, ["CMDB/v2/connector"])

        # Non-GET gets no automatic retry: `api` reaches arbitrary endpoints (including the
        # Action-token /data/ldg path), and a mutating POST retried blind after a mid-response
        # timeout would run the action twice.
        retries_seen = []
        m.call = lambda method, endpoint, body=None, retries=1: retries_seen.append(retries) or {}

        def run_method(method):
            class A:
                body, body_file, endpoint = None, None, "CMDB/v2/data/cmdb"
            a = A(); a.method = method
            with contextlib.redirect_stdout(io.StringIO()):
                m.cmd_api(a)

        run_method("GET"); run_method("POST")
        check("GET keeps its retry; POST gets none", retries_seen, [1, 0])
    finally:
        m.call = real_call


def test_insights(m):
    print("[4] profile insights: no hardcoded governance vendor (offline)")
    demo = ["SailPoint System allows any user to request access on behalf of any other user",
            "SailPoint user does not have an active lifecycle state: current state - NOT_SET"]
    check("names the tool the findings name", m._finding_source(demo), "SailPoint")
    check("another vendor reads through", m._finding_source(["Saviynt role assignment is not certified"]), "Saviynt")
    fb = "your access-governance tool"
    check("two vendors disagree -> generic", m._finding_source(demo[:1] + ["Okta user has no group"]), fb)
    check("prose findings -> generic", m._finding_source(["User has excessive privileges"]), fb)
    check("lowercase lead -> generic", m._finding_source(["sailpoint lifecycle state NOT_SET"]), fb)
    check("empty value -> generic", m._finding_source(demo[:1] + [""]), fb)
    check("non-string value -> generic", m._finding_source([42, {"a": 1}]), fb)

    def recs_for(nc, oscillating=False):
        return m._derive_insights({"type": "user", "risk": {}, "threats": {}, "identity": {"emails": []},
                                   "linkedAssets": [], "stability": {"oscillating": oscillating},
                                   "posture": {"nonCompliance": nc, "mfa": []}})[1]

    r = recs_for(demo)
    check("recommendation points at the real tool",
          any("access certification in SailPoint" in x for x in r), True)
    check("...and drops the fixed category list",
          any("separation-of-duties" in x for x in r), False)
    r = recs_for(["User has excessive privileges", "Account has never been certified"])
    check("generic recommendation still reads as a sentence",
          any("certification in your access-governance tool and resolve the 2" in x for x in r), True)
    check("no vendor name survives anywhere in the advice",
          [x for x in recs_for(["User has excessive privileges"], oscillating=True) if "SailPoint" in x], [])


def test_profile_shape(m):
    """An investigation answer must arrive complete in one call: the recommendations used to exist
    only inside the rendered PDF, so a question like "what are their risks and what should we do"
    paid ~4s of headless Chrome for text the JSON could carry. Guard that they stay in the JSON."""
    print("[5] profile carries its own findings + recommendations (offline)")
    user = {"Owner_Name": "TESTUSER", "displayName": "Test User", "Risk_Score": 900,
            "Risk_Level": "3-high", "Asset_Name": ["ASSET1"], "Count_No_MFA": 1,
            "Threat_List": ["Leaked Password x3", "[Critical] data movement"],
            "Is_MFA_Configured": [{"Source": "okta_user", "Status": "No"}],
            "Non_Compliance": ["Saviynt role assignment is not certified"], "Owner_Email": ["t@corp.example"]}
    asset = {"Asset_Name": "ASSET1", "Risk_Score": 500, "Risk_Level": "3-high", "OS": "Ubuntu 16.04.7",
             "Is_Encrypted": "0", "Count_KEV": 4, "High_Risk_User": ["TESTUSER", "OTHER"]}
    seen = []

    def fake_call(method, endpoint, body=None, retries=1):
        seen.append(endpoint)
        if "/change" in endpoint:
            return [{"field": "Owner_Department", "oldValue": "A", "newValue": "B"}] * 3
        table = (body or {}).get("table")
        if table == "asset":
            return {"totalRecords": 1, "data": [asset]}
        return {"totalRecords": 1, "data": [user]}

    class A:
        name, type, ascii, json = "TESTUSER", "user", False, True

    real_call = m.call
    m.call = fake_call
    try:
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            m.cmd_profile(A())
        out = json.loads(buf.getvalue())
    finally:
        m.call = real_call

    check("profile JSON has findings", bool(out.get("findings")), True)
    check("profile JSON has recommendations", bool(out.get("recommendations")), True)
    check("findings are severity-ordered (instability first)",
          "unstable" in out["findings"][0], True)
    check("account-takeover path is called out",
          any("account-takeover" in f for f in out["findings"]), True)
    check("EOL linked asset surfaces", any("end-of-life" in f for f in out["findings"]), True)
    check("KEV patching is recommended", any("known-exploited" in r for r in out["recommendations"]), True)
    check("governance rec names the tool from the data",
          any("Saviynt" in r for r in out["recommendations"]), True)
    check("no PDF/report work needed for any of it", any("report" in s for s in seen), False)

    # An asset profile carries them too, and an exact key still costs one resolve call.
    class B:
        name, type, ascii, json = "ASSET1", "asset", False, True

    m.call = fake_call
    try:
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            m.cmd_profile(B())
        aout = json.loads(buf.getvalue())
    finally:
        m.call = real_call
    check("asset profile has findings", bool(aout.get("findings")), True)
    check("asset profile has recommendations", bool(aout.get("recommendations")), True)

    # A human-looking name fires exact + fuzzy together; a key-shaped one doesn't waste a call.
    m.call = fake_call
    try:
        seen.clear()
        m.resolve("ALLCAPSKEY", "user", "Owner_Name", ["Owner_Name", "displayName"])
        key_calls = len(seen)
        seen.clear()
        m.resolve("Jane Doe", "user", "Owner_Name", ["Owner_Name", "displayName"])
        human_calls = len(seen)
    finally:
        m.call = real_call
    check("exact key resolves in one call", key_calls, 1)
    check("human name resolves in one round trip (two concurrent calls)", human_calls, 2)

    # The change-log id is server-provided data landing in a URL. Unencoded, an `&` truncated the
    # id (the server answered with someone ELSE's change history) and a non-ASCII name raised
    # inside http.client -- silently costing the stability signal for exactly the unusual names.
    tricky = dict(user, Owner_Name="Ana & Béa #1")

    def tricky_call(method, endpoint, body=None, retries=1):
        seen.append(endpoint)
        if "/change" in endpoint:
            return []
        if (body or {}).get("table") == "asset":
            return {"totalRecords": 1, "data": [asset]}
        return {"totalRecords": 1, "data": [tricky]}

    class C:
        name, type, ascii, json = "Ana & Béa #1", "user", False, True

    m.call = tricky_call
    try:
        seen.clear()
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            m.cmd_profile(C())
    finally:
        m.call = real_call
    ch = [s for s in seen if "/change" in s]
    check("change-log id is percent-encoded",
          bool(ch and ch[0].endswith("id=Ana%20%26%20B%C3%A9a%20%231")), True)

    # --- the rendered cells, not just the payload -------------------------------------------------
    # Every other assertion here reads the JSON, which is why three formatting bugs shipped in the
    # PDF unnoticed: `cloud` (a list) printed as `['oci_instance']`, `encrypted` (a float) as `0.0`,
    # and an absent `encrypted` as the literal string `None`. A null KEV count rendered as an empty
    # stat card. The last two matter beyond tidiness -- "None" and a blank card are unreadable as
    # "not reported", and the one thing absence must never render as is a clean zero.
    _, cards, body = m._profile_html({
        "type": "asset",
        "identity": {"assetName": "HOST1", "hostName": ["HOST1"], "ip": ["10.0.0.1"],
                     "os": "Ubuntu 20.04.6", "cloud": ["oci_instance", "aws_ec2"],
                     "encrypted": None, "publicIps": []},
        "risk": {"score": 72.0, "level": "3-high", "factors": []},
        "vulnerabilities": {"kevCount": None, "critical": 1.0},
        "associatedUsers": {}})
    check("a list-valued cell is joined, not repr'd", "['oci_instance'" in body, False)
    check("...and shows every value", "oci_instance, aws_ec2" in body, True)
    check("unreported encryption is a dash, never the string None", "<td>None</td>" in body, False)
    check("...and never reads as encrypted", "<td>Yes</td>" in body, False)
    check("a null metric renders as not-reported", "<div class='n'>&mdash;</div>" in cards, True)
    check("...and never as a clean zero", "<div class='n'>0</div>" in cards, False)
    check("...leaving no blank card, which reads as broken", "<div class='n'></div>" in cards, False)

    # 0.0 must still be a visible "No" -- the finding and the cell have to agree.
    _, _, unenc = m._profile_html({
        "type": "asset",
        "identity": {"assetName": "HOST2", "hostName": ["HOST2"], "ip": [], "os": "Ubuntu 20.04.6",
                     "cloud": [], "encrypted": 0.0, "publicIps": []},
        "risk": {"score": 1.0, "level": "1-low", "factors": []},
        "vulnerabilities": {}, "associatedUsers": {}})
    check("an unencrypted asset says No in the table", ">No</b>" in unenc, True)
    check("...and the finding agrees with the cell", "not encrypted at rest" in unenc, True)
    check("...and 0.0 is never printed raw", "<td>0.0</td>" in unenc, False)

    # The helper is display-only; the payload the rules read must be untouched, or normalising the
    # cell would silently delete the "not encrypted" finding.
    for raw, want in ((0.0, ">No</b>"), (0, ">No</b>"), ("0", ">No</b>"), (1.0, "Yes"), (1, "Yes"),
                      (None, "&mdash;"), ("", "&mdash;"), ("null", "&mdash;"), ("True", "True")):
        check("encrypted %r renders as %s" % (raw, want), want in m._encrypted_cell(raw), True)

    # The user branch renders the same field in its linked-assets table and had the same defect
    # in its Yes case, so both branches go through the one helper now.
    _, _, ub = m._profile_html({
        "type": "user", "identity": {"ownerName": "U1", "displayName": "U1", "emails": []},
        "risk": {"score": 10.0, "level": "1-low", "factors": []}, "posture": {}, "threats": {},
        "linkedAssets": [{"asset": "A1", "level": "1-low", "os": "Win", "ip": [], "kev": None,
                          "encrypted": 1.0, "otherHighRiskUsers": []}]})
    check("an encrypted linked asset says Yes, not 1.0", ("<td>Yes</td>" in ub, "1.0" in ub),
          (True, False))


def test_compare(m):
    """`compare` runs both profiles in-process and overlapped. It used to shell out to
    `meridian.py profile` once per name, serially: two interpreter startups, two fresh TLS
    handshakes (the keep-alive pool is per-process), four sequential round-trip waves."""
    print("[5b] compare runs in-process (offline)")
    import contextlib, io
    user_a = {"Owner_Name": "USER.A", "displayName": "User A", "Risk_Score": 900, "Risk_Level": "3-high"}
    user_b = {"Owner_Name": "USER.B", "displayName": "User B", "Risk_Score": 100, "Risk_Level": "1-low"}

    def fake_call(method, endpoint, body=None, retries=1):
        if "/change" in endpoint:
            return []
        s = json.dumps(body or {})
        u = user_a if "USER.A" in s else (user_b if "USER.B" in s else None)
        return {"totalRecords": 1, "data": [u]} if u else {"totalRecords": 0, "data": []}

    class A:
        name1, name2, type = "USER.A", "USER.B", "user"

    real_call = m.call
    m.call = fake_call
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            m.cmd_compare(A())
        out = json.loads(buf.getvalue())
    finally:
        m.call = real_call
    check("both profiles resolve", (out["profileA"]["identity"]["ownerName"],
                                    out["profileB"]["identity"]["ownerName"]), ("USER.A", "USER.B"))
    check("...each carrying its own findings", ("findings" in out["profileA"], "findings" in out["profileB"]),
          (True, True))
    check("...under the compared names", (out["a"], out["b"]), ("USER.A", "USER.B"))


def test_tls_posture(m):
    """Verification must stay ON unless explicitly disabled. This was CERT_NONE for as long as the
    PowerShell helpers existed, so a regression here is a plausible accident rather than a theory --
    and it would silently ship a client that hands a bearer token to any interposing certificate."""
    print("[6] TLS posture (offline)")
    import ssl
    keep = os.environ.pop("MERIDIAN_INSECURE_TLS", None)
    cfg_keep = m.CFG_PATH
    m.CFG_PATH = os.path.join(tempfile.gettempdir(), "meridian-tls-test-absent.json")
    try:
        m._SSL_CTX.clear()
        check("insecure_tls() defaults to False", m.insecure_tls(), False)
        ctx = m._ssl_context()
        check("default context verifies certificates", ctx.verify_mode, ssl.CERT_REQUIRED)
        check("default context checks hostname", ctx.check_hostname, True)

        for val in ("1", "true", "YES", "on"):
            os.environ["MERIDIAN_INSECURE_TLS"] = val
            check("env %r opts out" % val, m.insecure_tls(), True)
        os.environ["MERIDIAN_INSECURE_TLS"] = "0"
        check("env '0' does NOT opt out", m.insecure_tls(), False)
        del os.environ["MERIDIAN_INSECURE_TLS"]

        # An opt-out saved in config must be honoured, and must be reported so it can't go unnoticed.
        with open(m.CFG_PATH, "w") as f:
            json.dump({"fqdn": "x.example", "api_token": "abcd1234", "insecure_tls": True}, f)
        m._SSL_CTX.clear()
        check("config insecure_tls honoured", m.insecure_tls(), True)
        ctx = m._ssl_context()
        check("opted-out context skips verification", ctx.verify_mode, ssl.CERT_NONE)
        check("contexts are cached, not rebuilt", m._ssl_context() is ctx, True)
    finally:
        if os.path.exists(m.CFG_PATH):
            os.remove(m.CFG_PATH)
        m.CFG_PATH = cfg_keep
        os.environ.pop("MERIDIAN_INSECURE_TLS", None)
        if keep is not None:
            os.environ["MERIDIAN_INSECURE_TLS"] = keep
        m._SSL_CTX.clear()


def test_summary_completeness(m):
    """`summary --by` discovers which values exist by sampling, then counts each exactly. A category
    too rare to appear in the sample used to vanish from the breakdown with nothing said -- a posture
    answer that looked complete and wasn't. Guard the arithmetic that now detects it."""
    print("[7] summary breakdown reports its own completeness (offline)")
    import contextlib, io

    def run(by, dtype, records, counts, total, covered=None):
        """Drive cmd_summary against a fixture. `counts` maps value -> exact count; `covered` is what
        the List-field coverage probe (one OR of every discovered value) should report."""
        def fake_call(method, endpoint, body=None, retries=1):
            paging = (body or {}).get("paging", {})
            groups = (body or {}).get("query") or []
            q = json.dumps(groups)
            if paging.get("recordsPerPage") == 1:
                # The coverage probe ORs every value into ONE inner array; a per-value count has one.
                if groups and len(groups[-1]) > 1:
                    return {"totalRecords": total if covered is None else covered, "data": []}
                for v, c in counts.items():
                    if '"%s"' % v in q:
                        return {"totalRecords": c, "data": []}
                return {"totalRecords": 0, "data": []}
            page = paging.get("page", 0)                    # a discovery page
            chunk = records[page * 100:(page + 1) * 100]
            return {"totalRecords": total, "data": chunk}

        class A:
            table, by_, where, metrics = "asset", by, None, False
        A.by = by
        # load_config is patched too: check_fields reaches it through load_field_map/_fields_path,
        # and on a machine with no ~/.meridian (CI) that die()s out of the whole suite. Every dev
        # machine had credentials, so the dependency stayed invisible until the first CI run.
        real_call, real_ft, real_lc, real_cfg = m.call, m.field_type, m.load_config, m.CFG_DIR
        m.call, m.field_type = fake_call, lambda t, f: dtype
        m.load_config, m.CFG_DIR = lambda: ("s.example", "tok", None), tempfile.mkdtemp()
        m._FIELD_MAP.clear()
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                m.cmd_summary(A())
            return json.loads(buf.getvalue())
        finally:
            m.call, m.field_type = real_call, real_ft
            m.load_config, m.CFG_DIR = real_lc, real_cfg
            m._FIELD_MAP.clear()

    # Every value present in the sample, and the counts partition the records -> complete.
    recs = [{"Risk_Level": "1-low"}] * 60 + [{"Risk_Level": "3-high"}] * 40
    out = run("Risk_Level", "String", recs, {"1-low": 600, "3-high": 400}, 1000)
    check("complete breakdown says so", out.get("complete"), True)
    check("accounted equals total", out.get("accountedRecords"), 1000)
    check("no gap note when complete", "note" in out, False)
    check("groups sorted by count, largest first", [g["value"] for g in out["groups"]], ["1-low", "3-high"])

    # A third category exists in the data but never appears in the sample -> must be reported.
    out = run("Risk_Level", "String", recs, {"1-low": 600, "3-high": 300}, 1000)
    check("incomplete breakdown flagged", out.get("complete"), False)
    check("unaccounted records quantified", out.get("unaccountedRecords"), 100)
    check("note explains the shortfall", "did not appear in the" in (out.get("note") or ""), True)
    check("sample size reported", out.get("sampledForValues"), 100)

    # List fields: sum-vs-total proves nothing because one record holds several values, so coverage is
    # measured with a single OR query instead. Without it a breakdown could omit 37% of the inventory
    # in silence -- measured on a live stack before this existed.
    recs = [{"sourcetype": ["aws", "okta"]}] * 50
    out = run("sourcetype", "List", recs, {"aws": 900, "okta": 800}, 1000, covered=1000)
    check("List values still counted", sorted(g["value"] for g in out["groups"]), ["aws", "okta"])
    check("List coverage is measured", out.get("coveredRecords"), 1000)
    check("full coverage -> complete", out.get("complete"), True)
    check("overlap explained", "one record can hold several values" in (out.get("note") or ""), True)
    check("...and confirms every record is grouped",
          "falls in at least one group" in (out.get("note") or ""), True)

    # Same field, but a whole value the sample never reached: the shortfall must be exact and stated.
    out = run("sourcetype", "List", recs, {"aws": 900, "okta": 800}, 1000, covered=830)
    check("List shortfall flagged", out.get("complete"), False)
    check("List shortfall quantified", out.get("unaccountedRecords"), 170)
    check("...and blamed on sample reach", "never reached them" in (out.get("note") or ""), True)

    # High cardinality: count the biggest values the sample saw rather than returning nothing at all.
    many = [{"OS": "os%02d" % i} for i in range(50)] * 2
    out = run("OS", "String", many, {("os%02d" % i): (50 - i) for i in range(50)}, 1000, covered=900)
    check("high cardinality still returns groups", len(out["groups"]), m.SUMMARY_MAX_GROUPS)
    check("...reports how many distinct values it saw", out.get("distinctValuesSeen"), 50)
    check("...flags the group cap", out.get("groupsCapped"), m.SUMMARY_MAX_GROUPS)
    check("...is never called complete", out.get("complete"), False)
    check("...and quantifies what's unplaced", out.get("unaccountedRecords"), 100)

    # A dotted `by` reaches into a per-source Embed_List: a fetched record stores
    # rec["Details"] = [{"OS": ...}, ...], never a literal top-level "Details.OS" key. Before the nested
    # path existed, `rec.get(by)` always missed, so this came back with distinctValuesSeen: 0 no matter
    # what the fixture held. This also must NOT take the sum-vs-total branch even though the leaf type
    # here is "String", not "List" -- one record's array can carry the field on more than one entry.
    recs = [{"Details": [{"OS": "Linux"}]}] * 60 + [{"Details": [{"OS": "Windows"}]}] * 40
    out = run("Details.OS", "String", recs, {"Linux": 600, "Windows": 400}, 1000, covered=1000)
    check("nested path is flagged", out.get("nested"), True)
    check("values are found inside the embedded list", set(g["value"] for g in out["groups"]),
          {"Linux", "Windows"})
    check("nested field uses the coverage query, not sum-vs-total", "accountedRecords" in out, False)
    check("full coverage -> complete", out.get("coveredRecords"), 1000)
    check("...and is reported complete", out.get("complete"), True)

    # Same nested field, but the OR-coverage probe finds a real shortfall -- the completeness math must
    # still work once routed through the coverage branch instead of being silently skipped.
    out = run("Details.OS", "String", recs, {"Linux": 600, "Windows": 400}, 1000, covered=750)
    check("nested shortfall flagged", out.get("complete"), False)
    check("nested shortfall quantified", out.get("unaccountedRecords"), 250)

    # A single record's embedded array can carry the field on MORE THAN ONE entry at once (e.g. a GKE
    # node reporting several OS-shaped values from different sub-records) -- both must be counted, the
    # same way a genuine List field's multiple values are, not just the first entry seen.
    recs = [{"Details": [{"OS": "Linux"}, {"OS": "COS 125"}]}] * 100
    out = run("Details.OS", "String", recs, {"Linux": 1000, "COS 125": 1000}, 1000, covered=1000)
    check("both values from one record's array are discovered",
          set(g["value"] for g in out["groups"]), {"Linux", "COS 125"})

    # A record whose parent key is missing, null, or a bare dict (not a list) must not raise -- absence
    # of the embedded list is unknown, not a crash.
    recs = [{"Details": [{"OS": "Linux"}]}] * 50 + [{"Details": None}] * 25 + \
           [{}] * 15 + [{"Details": {"OS": "Windows"}}] * 10
    out = run("Details.OS", "String", recs, {"Linux": 500, "Windows": 100}, 1000, covered=600)
    check("missing/null/dict-shaped parent entries don't crash the sampler",
          set(g["value"] for g in out["groups"]), {"Linux", "Windows"})


def test_transport(m):
    """call() speaks http.client over a pooled connection now. Two properties are worth pinning: a
    redirect must never carry the Authorization header to another host, and the pool must never hand
    back a socket opened for a different stack or a different TLS posture."""
    print("[8] transport: redirect safety and pool keying (offline)")
    real = (m.load_config, m.pace, m._ssl_context, m._round_trip)
    m.load_config = lambda: ("stack.example", "tok", None)
    m.pace = lambda: None
    m._ssl_context = lambda: None
    try:
        # Off-host redirect: must be refused, and the second request must never be issued.
        seen = []

        def off_host(fqdn, key, ctx, method, endpoint, data, headers):
            seen.append(endpoint)
            return 302, "", "https://evil.example/steal"

        m._round_trip = off_host
        try:
            m.call("GET", "/CMDB/v2/x", retries=0)
            check("off-host redirect raises", False, True)
        except RuntimeError as e:
            check("off-host redirect refused", "another host" in str(e), True)
            check("...and names the host", "evil.example" in str(e), True)
        check("token never replayed off-host", [p for p in seen if "steal" in p], [])

        # Same-host redirect: followed, because urlopen used to do it transparently.
        hops = []

        def same_host(fqdn, key, ctx, method, endpoint, data, headers):
            hops.append(endpoint)
            if endpoint == "/CMDB/v2/x":
                return 301, "", "/CMDB/v2/y"
            return 200, '{"ok": true}', None

        m._round_trip = same_host
        check("same-host redirect followed", m.call("GET", "/CMDB/v2/x", retries=0), {"ok": True})
        check("...to the redirected path", hops, ["/CMDB/v2/x", "/CMDB/v2/y"])

        # A non-2xx keeps the string shape classify_connect_error() parses.
        m._round_trip = lambda *a, **k: (403, '{"code":403001}', None)
        try:
            m.call("GET", "/CMDB/v2/x", retries=0)
            check("403 raises", False, True)
        except RuntimeError as e:
            check("403 error text still parses as HTTP 403", m.classify_connect_error(str(e)),
                  ("forbidden", 403))
    finally:
        m.load_config, m.pace, m._ssl_context, m._round_trip = real

    # Pool must key on (fqdn, posture), not hand back any idle socket it happens to hold.
    m._POOL.clear()

    class Dummy:
        closed = False

        def close(self):
            self.closed = True

    d = Dummy()
    m._pool_put(("a.example", False), d)
    got = m._pool_get(("b.example", False), "b.example", None)
    check("pool won't reuse another stack's connection", got is d, False)
    check("...and closes the one it discarded", d.closed, True)
    m._POOL.clear()
    d2 = Dummy()
    m._pool_put(("a.example", False), d2)
    check("pool reuses a matching connection", m._pool_get(("a.example", False), "a.example", None) is d2, True)
    m._POOL.clear()
    d3 = Dummy()
    m._pool_put(("a.example", False), d3)
    check("pool won't reuse across a TLS-posture change",
          m._pool_get(("a.example", True), "a.example", None) is d3, False)
    m._POOL.clear()


def test_pace(m):
    """The 60/min budget is enforced across PROCESSES, not just threads. Two concurrent callers
    used to read the same stamps and each append only its own -- the second write clobbered the
    first, double-spending the hard budget -- and a reader catching a half-written file hit the
    ValueError fallback and reset the stamps entirely."""
    print("[8b] rate pacer: cross-process locking (offline)")
    import threading
    tmp = tempfile.mkdtemp()
    real = (m.CFG_DIR, m.RL_PATH)
    m.CFG_DIR = tmp
    m.RL_PATH = os.path.join(tmp, ".ratelimit")
    try:
        # Mutual exclusion with SEPARATE lock objects on the same path -- what two processes hold.
        counter = os.path.join(tmp, "counter")
        with open(counter, "w") as f:
            f.write("0")

        def bump(n):
            for _ in range(n):
                with m._FileLock(m.RL_PATH + ".lock"):
                    with open(counter) as f:
                        v = int(f.read())
                    with open(counter, "w") as f:
                        f.write(str(v + 1))

        ts = [threading.Thread(target=bump, args=(25,)) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        with open(counter) as f:
            check("separate lock handles exclude each other", f.read(), "100")

        # pace() end to end: every call leaves exactly one live stamp, no resets, no clobbers.
        for _ in range(5):
            m.pace()
        with open(m.RL_PATH) as f:
            stamps = [s for s in f.read().split() if s.strip()]
        check("every paced call left its stamp", len(stamps), 5)

        # A clock that ran ahead and was corrected (NTP step, resumed VM, dual boot) leaves stamps
        # the sliding window can never expire. The retry loop then computed a wait, slept, and found
        # the same stamps -- forever, with nothing printed. Every verb simply hung.
        future = int(time.time() * 1000) + 3600 * 1000
        with open(m.RL_PATH, "w") as f:
            f.write("\n".join(str(future + i) for i in range(m.RL_LIMIT + 5)))
        t0 = time.time()
        m.pace()
        check("a future-dated budget doesn't hang the CLI", time.time() - t0 < 2.0, True)
        with open(m.RL_PATH) as f:
            left = [int(x) for x in f.read().split() if x.strip()]
        check("...the future stamps are dropped, not counted", len(left), 1)
        check("...and the slot taken is now, not the future", left[0] <= int(time.time() * 1000), True)

        # The lock must degrade rather than spin when it can't be taken. Bounded by LOCK_MAX_WAIT.
        real_wait = m.LOCK_MAX_WAIT
        m.LOCK_MAX_WAIT = 0.05
        try:
            outer = m._FileLock(m.RL_PATH + ".lock")
            with outer:
                t0 = time.time()
                with m._FileLock(m.RL_PATH + ".lock") as inner:
                    pass
                check("an untakeable lock gives up instead of spinning", time.time() - t0 < 3.0, True)
                # Both platforms: a blocking POSIX flock had no ceiling at all (one wedged process
                # hangs every verb), and nesting two locks on one path deadlocked the thread outright
                # -- which is how CI caught this, since only Windows was non-blocking before.
                check("...and reports it didn't hold the lock", inner.held, False)
        finally:
            m.LOCK_MAX_WAIT = real_wait
    finally:
        m.CFG_DIR, m.RL_PATH = real


def test_check_classification(m):
    """`check` classifies a probe's failure from the status line via classify_connect_error. The
    old substring scan over the WHOLE error string turned a 500 whose JSON body carried an error
    sub-code like "code":403001 into FORBIDDEN -- blaming the token for a server fault."""
    print("[8c] check: probe failures classified by status line (offline)")
    import contextlib, io
    errors = {
        "/system/metrics/data": RuntimeError('HTTP 500: {"code":403001,"message":"boom"}'),
        "/metadata/asset": RuntimeError("HTTP 403: Forbidden"),
        "/data/cmdb": RuntimeError("HTTP 401: {}"),
        "/connector": RuntimeError("<urlopen error [Errno 11001] getaddrinfo failed>"),
    }

    def fake_call(method, endpoint, body=None, retries=1):
        for frag, e in errors.items():
            if frag in endpoint:
                raise e
        raise AssertionError("unexpected probe: %s" % endpoint)

    real = (m.call, m.load_config)
    m.call, m.load_config = fake_call, lambda: ("s.example", "tok", None)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            m.cmd_check(type("A", (), {})())
        out = json.loads(buf.getvalue())
    finally:
        m.call, m.load_config = real
    st = {c["area"]: c["status"] for c in out["capabilities"]}
    check("a 500 with a 403-ish body is HTTP_ERROR, not FORBIDDEN", st["System metrics"], "HTTP_ERROR")
    check("a real 403 is FORBIDDEN", st["Field metadata"], "FORBIDDEN")
    check("a 401 is UNAUTHORIZED", st["Data query"], "UNAUTHORIZED")
    check("no HTTP status at all is UNREACHABLE", st["Connectors"], "UNREACHABLE")
    check("...and the HTTP code is carried when known",
          [c.get("httpStatus") for c in out["capabilities"]], [500, 403, 401, None])


def test_field_metadata(m):
    """Field metadata is what tells a typo apart from a real zero, and a List field apart from a
    String one. It used to be populated only by an explicit `refresh-fields`, defaulting every field
    to "String" when absent -- which made `summary --by` query a multi-valued field with `==`."""
    print("[9] field metadata: typing and name validation (offline)")
    import contextlib, io
    META = {"metadata": [{"fieldName": "Risk_Score", "dataType": "Float"},
                         {"fieldName": "sourcetype", "dataType": "List"},
                         {"fieldName": "Count_KEV", "dataType": "Integer"},
                         {"fieldName": "Risk_Level", "dataType": "String"}]}
    tmp = tempfile.mkdtemp()
    real = (m.CFG_DIR, m.call, m.load_config)
    fetches = []

    def fake_call(method, endpoint, body=None, retries=1):
        if "/metadata/" in endpoint:
            fetches.append(endpoint)
            return META
        raise AssertionError("unexpected call to %s" % endpoint)

    m.CFG_DIR, m.call, m.load_config = tmp, fake_call, lambda: ("s.example", "tok", None)
    m._FIELD_MAP.clear()
    try:
        # No cache on disk: types must come from a fetch, not from a "String" default.
        check("cold cache still types a List field", m.field_type("asset", "sourcetype"), "List")
        check("...and a Float field", m.field_type("asset", "Risk_Score"), "Float")
        check("...and an Integer field", m.field_type("asset", "Count_KEV"), "Integer")
        check("metadata fetched exactly once", len(fetches), 1)
        check("...and written to the cache",
              any(f.startswith("fields.") for f in os.listdir(tmp)), True)
        m._FIELD_MAP.clear()
        check("second process reads the cache, no refetch",
              (m.field_type("asset", "sourcetype"), len(fetches)), ("List", 1))

        # A cold fill under a fan-out happens once. build_digest runs summarize_by and top_n
        # concurrently; unlocked, both missed the memo and both fetched the same ~1MB metadata,
        # each burning a slot of the 60/min budget on exactly the clean-install path.
        import time as _time
        m._FIELD_MAP.clear()
        m.CFG_DIR = tempfile.mkdtemp()   # no disk cache either, so every thread starts truly cold
        fetches.clear()

        def slow_call(method, endpoint, body=None, retries=1):
            _time.sleep(0.05)   # widen the race window the lock must close
            return fake_call(method, endpoint, body, retries)

        m.call = slow_call
        rs = m.parallel([(lambda: m.field_type("asset", "sourcetype")) for _ in range(4)])
        check("concurrent cold loads fetch metadata once", (set(rs), len(fetches)), ({"List"}, 1))
        m.call = fake_call

        # Name validation: a typo must raise, not sail through into a query returning 0.
        def err(fn, *a):
            try:
                fn(*a)
                return None
            except SystemExit:
                return "exited"

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            check("unknown field is rejected", err(m.check_fields, "asset", "Rsk_Score"), "exited")
        check("...naming the closest match", "Risk_Score" in buf.getvalue(), True)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            check("case-only miss is rejected", err(m.check_fields, "asset", "risk_score"), "exited")
        check("...and says case-sensitive", "case-sensitive" in buf.getvalue(), True)
        check("a valid field passes", m.check_fields("asset", "Risk_Score", "sourcetype"), None)

        # Metadata unreachable (scoped token): degrade, never invent a validation failure.
        m._FIELD_MAP.clear()
        m.CFG_DIR = os.path.join(tmp, "empty")

        def deny(method, endpoint, body=None, retries=1):
            raise RuntimeError("HTTP 403: Forbidden")

        m.call = deny
        check("no metadata -> no false rejection", m.check_fields("asset", "Whatever"), None)
        check("no metadata -> type falls back to String", m.field_type("asset", "Whatever"), "String")
    finally:
        m.CFG_DIR, m.call, m.load_config = real
        m._FIELD_MAP.clear()


def test_top_ladder(m):
    """`top` on an unbounded field (epoch timestamps, byte counts) used to die with a TypeError:
    every ladder rung filled -- including the top one -- so nothing bracketed the threshold from
    above and the refinement computed float(None). The fix climbs x10 until a probe under-fills,
    and past even that it answers with the best filling threshold instead of crashing."""
    print("[9b] top threshold ladder: unbounded fields (offline)")
    tmp = tempfile.mkdtemp()
    real = (m.CFG_DIR, m.call, m.load_config)
    m.CFG_DIR = tmp
    m.load_config = lambda: ("s.example", "tok", None)
    META = {"metadata": [{"fieldName": "Last_Seen", "dataType": "Float"},
                         {"fieldName": "Ingest_Bytes", "dataType": "Float"}]}

    def make_call(count_at):
        def fake_call(method, endpoint, body=None, retries=1):
            if "/metadata/" in endpoint:
                return META
            clause = body["query"][0][0]
            paging = body.get("paging", {})
            if paging.get("recordsPerPage") == 1:
                return {"totalRecords": count_at(clause["value"])}
            page = paging.get("page", 0)
            return {"data": [{"Asset_Name": "A%d" % (page * 100 + i), clause["searchFieldName"]: 1.6e9,
                              "Risk_Level": "1-low", "IP_Address": "10.0.0.1", "OS": "linux"}
                             for i in range(100)]}
        return fake_call

    try:
        m._FIELD_MAP.clear()
        # 600 records all valued ~1.6e9: every ladder rung fills, so no rung brackets from above.
        m.call = make_call(lambda t: 600 if t <= 1.6e9 else 0)
        out = m.top_n("asset", "Last_Seen", 10)
        check("unbounded field returns a ranking instead of crashing", len(out["top"]), 10)
        check("...with a climbed, bracketed threshold", out["matchedAtThreshold"] >= 1e9, True)
        check("...and the exact tail count", out["totalInTail"], 600)
        check("...untruncated", out.get("truncated"), None)

        # Values outrunning even the x10 climb: refinement is skipped, the verb still answers.
        m.call = make_call(lambda t: 600)
        out = m.top_n("asset", "Ingest_Bytes", 10)
        check("a field beyond the climb's reach still answers", len(out["top"]), 10)
        check("...with the full tail intact", out["totalInTail"], 600)
    finally:
        m.CFG_DIR, m.call, m.load_config = real
        m._FIELD_MAP.clear()


def test_stacks_registry(m):
    """`stacks add` on an existing name is the token-rotation path, and it used to replace the
    registry entry wholesale -- dropping entity_salt, whose loss is unrecoverable: the next entity
    snapshot regenerates one and every earlier record reads as appeared-and-disappeared."""
    print("[9c] stacks registry: update preserves entity_salt (offline)")
    import contextlib, io
    tmp = tempfile.mkdtemp()
    real = (m.CFG_DIR, m.CFG_PATH, m.STACKS_PATH, m._CFG_CACHE)
    m.CFG_DIR = tmp
    m.CFG_PATH = os.path.join(tmp, "config.json")
    m.STACKS_PATH = os.path.join(tmp, "stacks.json")

    def run(args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            m.cmd_stacks_add(args)
        return json.loads(buf.getvalue())

    class A:
        name, fqdn, token, action_token = "demo", "demo.example.com", "tok-original", None

    try:
        out = run(A())
        check("first stack activates", out["activatedNow"], True)
        # Simulate what accumulates on a working install: the salt an entity snapshot minted,
        # plus a saved action token.
        reg = m.load_stacks()
        reg["stacks"]["demo"]["entity_salt"] = "abc123"
        reg["stacks"]["demo"]["action_token"] = "act-1"
        m.save_stacks(reg)
        m.mirror_active_to_config(reg)

        class B:
            name, fqdn, token, action_token = "demo", "demo.example.com", "tok-rotated", None

        out = run(B())
        reg = m.load_stacks()
        check("token rotation lands", reg["stacks"]["demo"]["api_token"], "tok-rotated")
        check("...preserving entity_salt", reg["stacks"]["demo"].get("entity_salt"), "abc123")
        check("...and the saved action token", reg["stacks"]["demo"].get("action_token"), "act-1")
        with open(m.CFG_PATH, encoding="utf-8-sig") as f:
            cfg = json.load(f)
        check("updating the active stack refreshes config.json", cfg.get("api_token"), "tok-rotated")
        check("...carrying the salt through the mirror", cfg.get("entity_salt"), "abc123")
        check("...and the note says so", "config.json refreshed" in out["note"], True)

        # Token files are written atomically (a truncated stacks.json loses EVERY stack's
        # credentials) and owner-only on POSIX (the default umask made them world-readable).
        check("no temp file survives a registry write",
              [f for f in os.listdir(tmp) if f.endswith(".tmp")], [])
        if os.name == "posix":
            import stat
            for fname in ("stacks.json", "config.json"):
                mode = stat.S_IMODE(os.stat(os.path.join(tmp, fname)).st_mode)
                check("%s is owner-only (0600)" % fname, oct(mode), oct(0o600))

        # Token channels that stay out of shell history and process listings.
        class C:
            name, fqdn, token, action_token = "demo", "demo.example.com", "-", None
        real_stdin = sys.stdin
        sys.stdin = io.StringIO("tok-stdin\n")
        try:
            run(C())
        finally:
            sys.stdin = real_stdin
        check("--token - reads from stdin", m.load_stacks()["stacks"]["demo"]["api_token"], "tok-stdin")

        class D:
            name, fqdn, token, action_token = "demo", "demo.example.com", None, None
        os.environ["MERIDIAN_API_TOKEN"] = "tok-env"
        try:
            run(D())
        finally:
            del os.environ["MERIDIAN_API_TOKEN"]
        check("omitted --token falls back to the env var",
              m.load_stacks()["stacks"]["demo"]["api_token"], "tok-env")

        # TLS posture is per stack and survives the mirror; it used to evaporate on any switch.
        class E:
            name, fqdn, token, action_token, insecure_tls = "lab", "lab.example.com", "tok-lab", None, True
        run(E())
        reg = m.load_stacks()
        check("--insecure-tls is stored on the stack", reg["stacks"]["lab"].get("insecure_tls"), True)
        reg["active"] = "lab"
        m.save_stacks(reg); m.mirror_active_to_config(reg)
        with open(m.CFG_PATH, encoding="utf-8-sig") as f:
            check("...and carried into config.json by the mirror", json.load(f).get("insecure_tls"), True)
        reg["active"] = "demo"
        m.save_stacks(reg); m.mirror_active_to_config(reg)
        with open(m.CFG_PATH, encoding="utf-8-sig") as f:
            check("...but never onto a verified stack", "insecure_tls" in json.load(f), False)

        # An upgraded install (config.json, no stacks.json) migrates salt and TLS posture into the
        # registry -- the migration must carry everything the mirror carries, or the first
        # `stacks add` strips them and the salt loss is unrecoverable.
        tmp2 = tempfile.mkdtemp()
        m.CFG_DIR, m.CFG_PATH, m.STACKS_PATH = tmp2, os.path.join(tmp2, "config.json"), os.path.join(tmp2, "stacks.json")
        m._private_write(m.CFG_PATH, {"fqdn": "old.example.com", "api_token": "tok-old",
                                      "entity_salt": "salt-old", "insecure_tls": True})
        reg = m.load_stacks()
        entry = reg["stacks"][next(iter(reg["stacks"]))]
        check("migration carries entity_salt", entry.get("entity_salt"), "salt-old")
        check("...and the TLS posture", entry.get("insecure_tls"), True)

        # config.json is documented as hand-editable (call()'s own TLS error says to set
        # insecure_tls there), but the mirror rewrote it from scratch -- so rotating a token with
        # `stacks add`, which now re-mirrors, silently dropped a hand-set posture and the next call
        # failed cert verification with no clue why.
        tmp3 = tempfile.mkdtemp()
        m.CFG_DIR, m.CFG_PATH, m.STACKS_PATH = tmp3, os.path.join(tmp3, "config.json"), os.path.join(tmp3, "stacks.json")

        class F:
            name, fqdn, token, action_token = "prod", "prod.example.com", "tok-1", None
        run(F())
        m._private_write(m.CFG_PATH, {"fqdn": "prod.example.com", "api_token": "tok-1",
                                      "insecure_tls": True, "custom_key": "kept"})

        class G:
            name, fqdn, token, action_token = "prod", "prod.example.com", "tok-2", None
        run(G())
        with open(m.CFG_PATH, encoding="utf-8-sig") as f:
            cfg = json.load(f)
        check("a token rotation keeps a hand-set insecure_tls", cfg.get("insecure_tls"), True)
        check("...and any other hand-added key", cfg.get("custom_key"), "kept")
        check("...while the new token lands", cfg.get("api_token"), "tok-2")
        check("...and the posture is promoted into the registry",
              m.load_stacks()["stacks"]["prod"].get("insecure_tls"), True)

        # ...but a switch to a DIFFERENT stack must not inherit the previous one's settings.
        class H:
            name, fqdn, token, action_token = "other", "other.example.com", "tok-3", None
        run(H())
        reg = m.load_stacks(); reg["active"] = "other"
        m.save_stacks(reg); m.mirror_active_to_config(reg)
        with open(m.CFG_PATH, encoding="utf-8-sig") as f:
            cfg = json.load(f)
        check("switching stacks does NOT inherit the other's TLS opt-out", "insecure_tls" in cfg, False)
        check("...nor its unrelated keys", "custom_key" in cfg, False)

        # Concurrent writers must not collide on one temp path (a scheduled snapshot's entity_salt
        # rewrite racing an interactive stacks switch produced invalid JSON, read as "not configured").
        import threading as _th
        errs = []
        def writer(tag):
            try:
                for _ in range(15):
                    m._private_write(m.CFG_PATH, {"fqdn": "x.example", "api_token": tag, "pad": tag * 40})
            except Exception as e:
                errs.append(e)
        ts = [_th.Thread(target=writer, args=("a",)), _th.Thread(target=writer, args=("b",))]
        for t in ts: t.start()
        for t in ts: t.join()
        with open(m.CFG_PATH, encoding="utf-8-sig") as f:
            survived = json.load(f)        # must parse: never a half-written blend of both writers
        check("concurrent credential writes never corrupt the file", survived.get("api_token") in ("a", "b"), True)
        check("...and leave no temp files behind", [f for f in os.listdir(tmp3) if f.endswith(".tmp")], [])
        check("...with no write errors", errs, [])
    finally:
        (m.CFG_DIR, m.CFG_PATH, m.STACKS_PATH, m._CFG_CACHE) = real


def test_clause_parsing(m):
    """`--where` shape is checked locally. Omitting the type used to put the value in the type slot,
    which the API rejected with `Invalid operator: >=` -- blaming the operator, which was fine -- after
    spending a round trip to say so."""
    print("[10] --where clause validation (offline)")
    import contextlib, io

    def fails(clause):
        buf = io.StringIO()
        try:
            with contextlib.redirect_stderr(buf):
                m.parse_clause(clause)
            return None
        except SystemExit:
            return buf.getvalue()

    check("well-formed clause parses", m.parse_clause("Risk_Score >= Float 500"),
          {"searchFieldName": "Risk_Score", "operator": ">=", "type": "Float", "value": 500.0})
    check("Integer clause coerces to int", m.parse_clause("Count_KEV >= Integer 1")["value"], 1)
    check("valueless operator is fine", m.parse_clause("Owner_Name exists String")["value"], None)
    out = fails("Risk_Score >= 500")
    check("missing type is caught locally", bool(out), True)
    check("...and explains the value landed in the type slot", "type slot" in (out or ""), True)
    check("...and shows the corrected form", 'Risk_Score >= Float 500' in (out or ""), True)
    check("too-few tokens is caught", bool(fails("Risk_Score")), True)
    check("bogus type is caught", bool(fails("Risk_Score >= Flot 500")), True)
    check("non-numeric numeric value is caught", bool(fails("Risk_Score >= Float high")), True)
    check("clause_fields extracts names for validation",
          m.clause_fields(["Risk_Score >= Float 500", "OS match String Windows"]),
          ["Risk_Score", "OS"])

    # Multi-word operators. The parser used to do a fixed `split(None, 3)`, so every operator that
    # occupies more than one word pushed the type out of its slot and was rejected as a bad type. That
    # took out the DSL's ONLY negation (`not match` / `not in` -- there is no NOT wrapper), the four
    # windowed Datetime operators, and the three List `length` ones. Consequences, in order of how
    # quietly they failed: `metrics add --where` could never save a time-windowed count, so nothing in
    # the trend layer could track "new this week"; SKILL.md's own sample prompt about certificates
    # expiring in 30 days had no expressible form; and references/recipes.md documented a clause that
    # errored. Each arity is checked here so a future maxsplit "simplification" fails loudly.
    check("two-word negation parses", m.parse_clause("OS not match String Windows"),
          {"searchFieldName": "OS", "operator": "not match", "type": "String", "value": "Windows"})
    check("...and `not in`", m.parse_clause("sourcetype not in List aws")["operator"], "not in")
    check("List length operator parses",
          m.parse_clause("IP_Address length gt Integer 2")["operator"], "length gt")
    check("...and still coerces its numeric value",
          m.parse_clause("IP_Address length gt Integer 2")["value"], 2)
    # The operator table -- not the token position -- decides where the type slot starts, so a value
    # that happens to contain operator words cannot hijack the parse.
    check("a value containing operator words is not mistaken for one",
          m.parse_clause("sourcetype in List not match"),
          {"searchFieldName": "sourcetype", "operator": "in", "type": "List", "value": "not match"})
    check("a multi-word String value survives intact",
          m.parse_clause("OS match String Windows Server 2012")["value"], "Windows Server 2012")

    # The windowed Datetime operators are parsed and then REFUSED. Measured against a live stack, the
    # `past` spellings return HTTP 400 and the rest return HTTP 200 having ignored the window: the same
    # 52 records for 1, 30, 90 and 3650 days, where the true 90-day count via an absolute range is 51.
    # A number that is plausible, slightly wrong, and answers a question nobody asked is the exact
    # failure this suite exists to catch -- and as a saved metric it would record it weekly forever.
    for op in ("within past", "not within past", "within", "not within",
               "within future", "not within future"):
        out = fails("Expired_Datetime %s Datetime 90, days" % op)
        check("windowed operator %r is refused, not sent" % op, bool(out), True)
        check("...naming the working alternative", "Datetime" in (out or "") and "90d" in (out or ""),
              True)
    out = fails("Expired_Datetime within future Datetime 90, days")
    check("...and saying why, not just no", "ignoring the window" in (out or ""), True)
    # No surrounding quotes in these needles: `die` JSON-encodes the message, so the example's own
    # double quotes arrive backslash-escaped.
    check("...pointing a future window at an upper bound",
          "Expired_Datetime <= Datetime +90d" in out, True)
    check("...and a past window at a lower bound",
          "First_Discovered_Datetime >= Datetime -90d" in
          (fails("First_Discovered_Datetime within past Datetime 7, days") or ""), True)
    # A negation inverts the comparison, not the sign: "not within a trailing 90 days" means OLDER than
    # that, so `< -90d`. Suggesting `>= -90d` would hand back the complement of the intended set -- and
    # this is the case that matters most, since `not within Datetime 30, days` reported 0 stale assets
    # where `< -30d` reports 60.
    for op, want in (("not within past", "< Datetime -90d"), ("not within", "< Datetime -90d"),
                     ("within future", "<= Datetime +90d"), ("not within future", "> Datetime +90d"),
                     ("within past", ">= Datetime -90d"), ("within", ">= Datetime -90d")):
        check("%r is redirected to %r" % (op, want),
              want in (fails("Last_Discovered_Datetime %s Datetime 90, days" % op) or ""), True)

    # Relative Datetime values, resolved client-side. This is the supported route to a time window now
    # that the windowed operators are refused, so SKILL.md's advertised "certificates expiring in the
    # next 30 days" has an expressible form. Verified live: `>= -0d` + `<= +90d` returns 51, matching a
    # hand-built absolute range exactly, against the windowed operator's 52-for-any-window.
    now = datetime.datetime(2026, 8, 17, 14, 30, 0)
    check("a future relative date resolves to an end-of-day bound",
          m.resolve_relative_date("+90d", "<=", now), ("2026-11-15 23:59:59", None))
    check("a past relative date resolves to a start-of-day bound",
          m.resolve_relative_date("-30d", ">=", now), ("2026-07-18 00:00:00", None))
    check("weeks resolve too", m.resolve_relative_date("+2w", "<=", now)[0], "2026-08-31 23:59:59")
    check("`today` is accepted as a spelling of the current day",
          m.resolve_relative_date("today", ">=", now)[0], "2026-08-17 00:00:00")
    # A bare date lands at an unspecified point inside the day -- measured, `>= 2026-08-17` matched 51
    # records where `>= 2026-08-17 00:00:00` matched 52. The explicit time is what makes it exact.
    check("...always with an explicit time component",
          all(len(m.resolve_relative_date(v, o, now)[0]) == len("2026-08-17 00:00:00")
              for v, o in (("+1d", "<="), ("-1d", ">="), ("today", "<"))), True)
    check("an absolute value passes through untouched",
          m.resolve_relative_date("2026-01-01 00:00:00", ">="), (None, None))
    check("a non-date value passes through untouched",
          m.resolve_relative_date("Windows", "match"), (None, None))
    check("a relative date is refused on an equality operator",
          m.resolve_relative_date("+90d", "==")[0], None)
    check("...explaining that a whole day has no meaning there",
          "whole-day window" in (m.resolve_relative_date("+90d", "==")[1] or ""), True)
    check("a full clause resolves its relative value",
          m.parse_clause("Expired_Datetime <= Datetime +0d")["value"].endswith("23:59:59"), True)
    check("...and keeps the operator and type intact",
          [m.parse_clause("Expired_Datetime <= Datetime +0d")[k] for k in ("operator", "type")],
          ["<=", "Datetime"])
    check("a relative value on a non-Datetime type is left alone",
          m.parse_clause("Owner_Name match String +90d")["value"], "+90d")
    check("an equality operator with a relative date fails the clause",
          bool(fails("Expired_Datetime == Datetime +90d")), True)

    # An unknown operator is now its own message. The old parser reported whatever landed in the type
    # slot instead, which for a misspelled operator pointed at the wrong token entirely.
    out = fails("Risk_Score >>> Float 500")
    check("an unknown operator is caught", bool(out), True)
    check("...naming the operator, not the type slot", "'>>>' is not an operator" in (out or ""), True)
    check("...and listing the multi-word ones it could have meant",
          "not within past" in (out or ""), True)
    check("clause_fields still reads the field off a multi-word clause",
          m.clause_fields(["First_Time_Seen not within past Datetime 30, days"]), ["First_Time_Seen"])
    check("and_query nests a multi-word clause one-per-group",
          m.and_query(["OS not match String Windows", "Risk_Score >= Float 500"]),
          [[{"searchFieldName": "OS", "operator": "not match", "type": "String", "value": "Windows"}],
           [{"searchFieldName": "Risk_Score", "operator": ">=", "type": "Float", "value": 500.0}]])


def test_smartlabels(m):
    """SmartLabels are the customer's own vocabulary, and Meridian ships an llmBusinessValue with each.
    Two things must hold: the declared type is translated to a DSL type, and the table is resolved from
    field metadata rather than from `field_collection`, whose values are stack-specific names."""
    print("[11] SmartLabels (offline)")
    import contextlib, io
    RAW = [
        {"friendly_name": "Crown Jewels", "field_name": "Crown_Jewels", "field_type": "Boolean",
         "field_collection": "SOME_STACK_SPECIFIC_NAME", "llmBusinessValue": "Assets the business cannot lose."},
        {"friendly_name": "PCI Scope", "field_name": "PCI_Scope", "field_type": "Str",
         "field_collection": "another_name", "llmBusinessValue": "In scope for PCI DSS."},
        {"friendly_name": "Joiner Risk", "field_name": "Joiner_Risk", "field_type": "Integer",
         "field_collection": "whatever", "llmBusinessValue": "Recent joiners with broad access."},
        {"friendly_name": "Orphaned", "field_name": "Not_A_Real_Field", "field_type": "Str",
         "field_collection": "x", "llmBusinessValue": "Label whose field no longer exists."},
    ]
    tmp = tempfile.mkdtemp()
    real = (m.CFG_DIR, m.call, m.load_config, m._LABELS, dict(m._FIELD_MAP))
    m.CFG_DIR, m.load_config = tmp, lambda: ("s.example", "tok", None)
    m._LABELS = None
    m._FIELD_MAP.clear()
    # Crown_Jewels/PCI_Scope on asset, Joiner_Risk on user -- collection names deliberately unhelpful.
    m._FIELD_MAP["asset"] = {"Crown_Jewels": "Binary", "PCI_Scope": "String"}
    m._FIELD_MAP["user"] = {"Joiner_Risk": "Integer"}
    m.call = lambda method, ep, body=None, retries=1: RAW if "smartlabel" in ep else {}
    try:
        labels = m.load_labels()
        by = {l["name"]: l for l in labels}
        check("declared Boolean maps to the DSL's Binary", by["Crown Jewels"]["type"], "Binary")
        check("declared Str maps to the DSL's String", by["PCI Scope"]["type"], "String")
        check("Integer passes through", by["Joiner Risk"]["type"], "Integer")
        check("asset label resolved to the asset table", by["Crown Jewels"]["table"], "asset")
        check("user label resolved to the user table", by["Joiner Risk"]["table"], "user")
        check("business-value text is carried", by["Crown Jewels"]["purpose"],
              "Assets the business cannot lose.")
        check("label with no matching field is not queryable", by["Orphaned"]["queryable"], False)
        check("...and its table is unknown, not guessed", by["Orphaned"]["table"], None)
        check("cache written for the next process",
              any(f.startswith("labels.") for f in os.listdir(tmp)), True)

        check("exact business term resolves", [l["field"] for l in m.find_label("Crown Jewels")], ["Crown_Jewels"])
        check("substring resolves", [l["field"] for l in m.find_label("jewel")], ["Crown_Jewels"])
        check("near miss still resolves", [l["field"] for l in m.find_label("Crown Jewls")], ["Crown_Jewels"])
        check("unqueryable label never offered", [l["field"] for l in m.find_label("Orphaned")], [])
        check("nonsense term resolves to nothing", m.find_label("zzzz nothing"), [])

        # --refresh drops the cache so a label defined after the first run becomes visible. Nothing used
        # to invalidate this file, so it never did.
        RAW.append({"friendly_name": "New Label", "field_name": "PCI_Scope", "field_type": "Str",
                    "field_collection": "x", "llmBusinessValue": "Defined after the cache was written."})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            class A: search, table, refresh = None, None, None
            m.cmd_labels(A())
        check("a label defined after the cache is NOT seen without --refresh",
              json.loads(buf.getvalue())["labelsDefined"], 4)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            class A: search, table, refresh = None, None, True
            m.cmd_labels(A())
        check("--refresh picks it up", json.loads(buf.getvalue())["labelsDefined"], 5)
        check("refresh-fields clears the labels cache too", m.drop_labels_cache(), True)
        check("...and is honest when there was nothing to clear", m.drop_labels_cache(), False)

        # Field metadata unreachable: every label resolves to no table, which is indistinguishable from
        # "they never defined that label". Persisting it would make one 403 hide every SmartLabel forever.
        m._LABELS = None
        m._FIELD_MAP.clear()
        m.CFG_DIR = os.path.join(tmp, "nometa")
        m.call = lambda method, ep, body=None, retries=1: (
            RAW if "smartlabel" in ep else (_ for _ in ()).throw(RuntimeError("HTTP 403: Forbidden")))
        labels = m.load_labels()
        check("labels still returned when metadata is unreachable", len(labels), 5)
        check("...marked provisional", m._LABELS_PROVISIONAL, True)
        check("...and NOT written to disk",
              os.path.isdir(m.CFG_DIR) and any(f.startswith("labels.") for f in os.listdir(m.CFG_DIR)), False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            class A: search, table, refresh = "Crown Jewels", None, None
            m.cmd_labels(A())
        out = json.loads(buf.getvalue())
        check("an unresolvable lookup is reported, not answered 'no match'",
              out.get("fieldMetadataUnavailable"), True)
        check("...and never claims the term isn't one of their labels",
              "simply not be one of them" in (out.get("note") or ""), False)

        # --- purpose text: full on a search, absent on a bare listing ---------------------------
        # The purpose blurbs run 180-300 characters each. Truncating them to 110 rather than dropping
        # them barely helped: measured on the demo stack, 151 truncated blurbs were 16,947 of the
        # payload's 42,895 characters -- 40%, ~4,200 tokens -- on a call whose question is "what
        # vocabulary exists here", which the name and field already answer.
        m._LABELS, m._LABELS_PROVISIONAL = None, False
        m.CFG_DIR = os.path.join(tmp, "purpose")
        m.call = lambda method, ep, body=None, retries=1: RAW if "smartlabel" in ep else {}
        m._FIELD_MAP["asset"] = {"Crown_Jewels": "Binary", "PCI_Scope": "String"}
        m._FIELD_MAP["user"] = {"Joiner_Risk": "Integer"}

        def labels_out(search=None, table=None):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                class A: pass
                a = A(); a.search, a.table, a.refresh = search, table, None
                m.cmd_labels(a)
            return json.loads(buf.getvalue())

        found = labels_out(search="Crown Jewels")
        check("a search keeps the purpose text in full", found["labels"][0].get("purpose"),
              "Assets the business cannot lose.")
        check("...and does not announce an omission", "purposeOmitted" in found, False)

        # A small listing is still detailed -- the threshold is about swamping an answer, not secrecy.
        few = labels_out()
        check("a listing under the detail threshold keeps purpose",
              any("purpose" in r for r in few["labels"]), True)

        # Past the threshold the prose goes, and its absence is stated rather than implied.
        RAW.extend([{"friendly_name": "Bulk %d" % i, "field_name": "PCI_Scope", "field_type": "Str",
                     "llmBusinessValue": "x" * 250} for i in range(20)])
        m._LABELS = None
        m.CFG_DIR = os.path.join(tmp, "purpose2")
        many = labels_out()
        check("a large listing drops purpose entirely, not to a truncated stub",
              any("purpose" in r for r in many["labels"]), False)
        check("...and says the text exists so its absence isn't read as 'no description'",
              "--search" in (many.get("purposeOmitted") or ""), True)
        check("...while name/field/table/type -- the queryable part -- all survive",
              sorted(many["labels"][0].keys()), ["field", "name", "table", "type"])
        # Narrowing by table is a search-shaped act but not a search: still a listing.
        check("a table filter past the threshold also drops purpose",
              any("purpose" in r for r in labels_out(table="asset")["labels"]), False)

        # A scoped token can't read SmartLabels at all: degrade, don't fail the session. Needs a directory
        # with no cache in it -- the assertions above wrote one, and reading that back is correct.
        m._LABELS, m._LABELS_PROVISIONAL = None, False
        m.CFG_DIR = os.path.join(tmp, "nocache")
        m.call = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("HTTP 403: Forbidden"))
        check("403 on the endpoint degrades to empty", m.load_labels(), [])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            class A: search, table, refresh = None, None, None
            m.cmd_labels(A())
        out = json.loads(buf.getvalue())
        check("...and says why rather than looking empty", "full User Generated token" in (out.get("note") or ""), True)
    finally:
        m._LABELS_PROVISIONAL = False
        m.CFG_DIR, m.call, m.load_config, m._LABELS, fm = real
        m._FIELD_MAP.clear(); m._FIELD_MAP.update(fm)


def test_csv_export(m):
    """CSV carries the rows; the envelope must still carry the caveats. A spreadsheet cannot hold
    "37% of records are unaccounted for", and dropping it silently would undo the point of measuring."""
    print("[12] CSV export (offline)")
    import contextlib, io
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "out.csv")
    payload = {"table": "asset", "totalRecords": 1201, "shown": 2, "truncated": True,
               "note": "Showing 2 of 1201 matching records.",
               "rows": [{"Asset_Name": "A1", "IP_Address": ["10.0.0.1", "10.0.0.2"], "Risk_Score": 900},
                        {"Asset_Name": "A2", "IP_Address": None, "Risk_Score": None}]}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m.emit(dict(payload), "rows", "csv", path)
    env = json.loads(buf.getvalue())
    check("envelope keeps the truncation caveat", env.get("note"), payload["note"])
    check("envelope keeps the totals", (env.get("totalRecords"), env.get("shown")), (1201, 2))
    check("envelope reports rows written", env.get("rowsWritten"), 2)
    check("rows are not duplicated into the envelope", "rows" in env, False)
    raw = open(path, "rb").read()
    check("file starts with a BOM so Excel reads UTF-8", raw[:3], b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    lines = [l for l in text.splitlines() if l.strip()]
    check("header row present", lines[0], "Asset_Name,IP_Address,Risk_Score")
    check("multi-value field flattened, not Python-repr'd", "10.0.0.1; 10.0.0.2" in lines[1], True)
    check("...and no list syntax leaks into a cell", "['" in text, False)
    check("None becomes empty, not the string None", lines[2], "A2,,")
    # JSON mode must be untouched by any of this.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m.emit(dict(payload), "rows", None, None)
    check("json mode still emits the whole payload", json.loads(buf.getvalue()).get("rows") is not None, True)

    # Formula neutralization: cell values originate in the customer's environment (asset and owner
    # names -- transitively, whatever feeds their connectors), and this file is tuned to be
    # double-clicked straight into Excel.
    check("a leading = is neutralized", m._csv_cell('=WEBSERVICE("http://evil")'), "'=WEBSERVICE(\"http://evil\")")
    check("...and DDE via +", m._csv_cell("+cmd|' /C calc'!A0"), "'+cmd|' /C calc'!A0")
    check("...and @", m._csv_cell("@SUM(1)"), "'@SUM(1)")
    check("...and a list whose first item is a formula", m._csv_cell(["=1+1", "b"]), "'=1+1; b")
    check("negative numbers stay numbers, not text", m._csv_cell(-5), -5)
    check("an ordinary name is untouched", m._csv_cell("web-01"), "web-01")


def test_list(m):
    """The default `list` (one page) reads count and rows from the same page-0 fetch -- the
    separate recordsPerPage:1 count call was pure duplication. Multi-page and --all keep the
    count-first shape on purpose: the cheap count lets every data page go out in one concurrent
    wave, where merging would serialize page 0 ahead of the rest."""
    print("[12b] list call budget (offline)")
    import contextlib, io
    tmp = tempfile.mkdtemp()
    real = (m.CFG_DIR, m.call, m.load_config)
    m.CFG_DIR, m.load_config = tmp, lambda: ("s.example", "tok", None)
    calls = []
    TOTAL = 120

    def fake_call(method, endpoint, body=None, retries=1):
        if "/metadata/" in endpoint:
            return {"metadata": [{"fieldName": "Asset_Name", "dataType": "String"},
                                 {"fieldName": "Risk_Score", "dataType": "Float"}]}
        calls.append(body["paging"])
        page, rpp = body["paging"]["page"], body["paging"]["recordsPerPage"]
        start = page * rpp
        n = max(0, min(TOTAL - start, rpp))
        return {"totalRecords": TOTAL,
                "data": [{"Asset_Name": "A%d" % (start + i), "Risk_Score": 5, "Extra_Field": "x"}
                         for i in range(n)]}

    def run(**kw):
        class A: pass
        a = A()
        a.table, a.where, a.select = "asset", None, None
        a.count_only, a.all, a.limit = False, False, 50
        a.format, a.out = None, None
        for k, v in kw.items():
            setattr(a, k, v)
        calls.clear()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            m.cmd_list(a)
        return json.loads(buf.getvalue())

    try:
        m._FIELD_MAP.clear()
        m.call = fake_call
        out = run()
        check("default list costs ONE call", (out["apiCalls"], len(calls)), (1, 1))
        check("...rows honour the limit", out["shown"], 50)
        check("...the total comes from the same page", out["totalRecords"], 120)
        check("...truncation is still declared", out["truncated"], True)
        check("...and rows are projected to the selected fields", "Extra_Field" in out["rows"][0], False)

        out = run(limit=250)
        check("a multi-page list keeps count-first", (out["apiCalls"], len(calls)), (3, 3))  # count + 2 pages
        check("...returning everything", (out["shown"], out["truncated"]), (120, False))

        out = run(count_only=True)
        check("count-only costs one call", len(calls), 1)
        check("...returning the total", out["totalRecords"], 120)
    finally:
        m.CFG_DIR, m.call, m.load_config = real
        m._FIELD_MAP.clear()


def test_digest(m):
    """A digest runs unattended, so a failed section must be named rather than quietly absent -- and its
    report tables must render row VALUES. _simple_table takes positional cells; handing it dicts renders
    the header names as data, which is exactly what happened the first time."""
    print("[13] digest (offline)")
    import contextlib, io
    real = (m.stack_metrics, m.summarize_connectors, m.summarize_by, m.top_n, m.load_config)
    m.load_config = lambda: ("s.example", "tok", None)
    m.stack_metrics = lambda: {"metrics": {"assetCount": 34229, "userCount": 10305,
                                           "avg30DaysAssetCount": 34164, "avg30DaysUserCount": 10067}}
    m.summarize_connectors = lambda *a, **k: {"summary": {"connectorsEnabled": 19, "healthy": 2,
                                              "degraded": 9, "failing": 8, "idle": 0,
                                              "recordsLastRun": 93583},
                                              "failures": [{"service": "intune", "serviceCount": 2,
                                                            "message": "auth failed <script>"}]}
    m.summarize_by = lambda t, b, w=None, **k: {"table": t, "by": b, "total": 34229, "complete": True,
                                                "groups": [{"value": "1-low", "count": 16739}]}
    m.top_n = lambda t, f, n, w=None, s=None, **k: {"table": t, "field": f,
                                                    "top": [{"Owner_Name": "U1", "Risk_Score": 1500}]}
    try:
        class A: table, by, field, top = "asset", None, "Risk_Score", 5
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            m.cmd_digest(A())
        d = json.loads(buf.getvalue())
        check("digest is tagged for the report renderer", d.get("generated"), "digest")
        check("all sections assembled",
              sorted(k for k in ("metrics", "connectors", "breakdown", "topUsers", "topAssets") if k in d),
              ["breakdown", "connectors", "metrics", "topAssets", "topUsers"])
        check("headline carries the 30-day averages", d["headline"]["assets30DayAvg"], 34164)
        check("headline surfaces failing connectors", d["headline"]["connectorsFailing"], 8)

        sub, stats, body = m._digest_html(d)
        check("report subtitle names the stack", "s.example" in sub, True)
        check("trend arrow computed against the average", "▲ 34,229" in body, True)
        check("percent delta shown", "+0.2%" in body, True)
        check("header names are NOT rendered as cell values", "<td>Measure</td>" in body, False)
        check("real coverage numbers rendered", "<td>93,583</td>" in body, True)
        check("untrusted connector message is escaped", "&lt;script&gt;" in body, True)
        check("...and not left raw", "<script>" in body, False)

        # One section failing must be reported, not silently dropped from a scheduled report.
        m.summarize_by = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("HTTP 403: Forbidden"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            m.cmd_digest(A())
        d2 = json.loads(buf.getvalue())
        check("failed section is named", [s["section"] for s in d2.get("sectionsUnavailable", [])], ["breakdown"])
        check("...and omitted rather than faked", "breakdown" in d2, False)
        check("other sections still present", "topUsers" in d2, True)
        _, _, body2 = m._digest_html(d2)
        check("the PDF says a section was unavailable", "Sections unavailable" in body2, True)
    finally:
        m.stack_metrics, m.summarize_connectors, m.summarize_by, m.top_n, m.load_config = real


def test_multi_profile_report(m):
    """`report --input a.json b.json ...` renders one document with several subjects -- an identity
    plus each of its linked assets. The rules: nothing is aggregated across subjects, non-profile
    input is refused by name rather than rendered into something that looks deliberate, and a
    single-profile report must come out exactly as it did before this existed."""
    print("[19b] multi-subject profile reports (offline)")
    import contextlib, io, json as _json, os, tempfile

    user = {"type": "user",
            "identity": {"ownerName": "U1", "displayName": "Ada Lovelace", "title": "HR Director",
                         "department": "Human Resources", "emails": ["a@corp.example"]},
            "risk": {"score": 54.0, "level": "3-high", "ranking": 98.0, "factors": ["Threats Detected"]},
            "posture": {"mfa": [{"source": "okta_user", "configured": "no"}], "nonCompliance": []},
            "threats": {"leakedCredentialCount": 15, "dlpBehavioral": []},
            "linkedAssets": [{"asset": "HOST1", "level": "3-high", "os": "Ubuntu 16.04.7",
                              "ip": ["10.0.0.1"], "kev": 8, "encrypted": 0.0,
                              "otherHighRiskUsers": []}]}

    def asset(name, **kw):
        d = {"type": "asset",
             "identity": dict({"assetName": name, "hostName": [name], "ip": ["10.0.0.1"],
                               "os": "Ubuntu 16.04.7", "cloud": ["oci_instance"], "encrypted": 0.0,
                               "publicIps": []}, **kw.pop("ident", {})),
             "risk": {"score": 72.0, "level": "3-high", "ranking": 98.0, "factors": ["Not Encrypted"],
                      "dataClass": "Confidential"},
             "vulnerabilities": {"kevCount": 8, "critical": 2.0, "high": 2.0, "maxCvss": 9.8},
             "associatedUsers": {"highRiskUsers": []}}
        d.update(kw)
        return d

    subtitle, statcards, body = m._multi_profile_html([user, asset("HOST1"), asset("HOST2")])
    check("every subject is rendered",
          (body.count("<section class='subj"), body.count("class='subject'")), (3, 3))
    check("the identity is marked as such", "class='subj identity'" in body, True)
    check("each subject is named", ("Ada Lovelace" in body, "HOST1" in body, "HOST2" in body),
          (True, True, True))
    check("each subject keeps its own findings", body.count("Key findings"), 3)
    check("...and its own recommendations", body.count("Recommended actions"), 3)
    check("...and its own stat row", body.count("class='stats'"), 3)
    check("subtitle counts the subjects", subtitle, "3 subjects: 1 identity, 2 linked assets")
    check("a contents line lists them", "In this report" in body, True)
    # A "total KEVs" across an identity and its own linked assets counts the same finding twice.
    check("nothing is aggregated into a top-level stat row", statcards, "")

    # Layout hooks: asset bodies flow in two columns, an identity body does not (its linked-assets
    # table is 7 columns wide). Both are CSS-only concerns, but the classes have to be emitted.
    check("asset bodies opt into two columns", body.count("class='pbody cols'"), 2)
    check("...and the identity body does not", body.count("class='pbody'"), 1)
    check("the graph is grouped with its label so the pair can float", "class='graphbox'" in body, True)
    check("the asset table is grouped so the pair can span both columns",
          body.count("class='assetbox'"), 2)

    # Pluralisation, and the singular case.
    sub1, _, _ = m._multi_profile_html([user, asset("HOST1")])
    check("one asset is singular", sub1, "2 subjects: 1 identity, 1 linked asset")
    sub0, _, _ = m._multi_profile_html([asset("HOST1"), asset("HOST2")])
    check("assets alone still count honestly", sub0, "2 subjects: 0 identity, 2 linked assets")

    # An uninformative "None." section is dropped when stacked, but a real one survives: an asset's
    # "Associated high-risk users: None recorded." means no lateral path, which is a finding.
    check("empty label+None pairs are dropped", "Linked source identities" in body, False)
    check("...but 'None recorded' survives, it means no lateral path",
          "None recorded." in body, True)
    _, _, solo = m._profile_html(user)
    check("a single-subject report still shows the empty section",
          "Linked source identities" in solo, True)

    # A missing score is "not reported", never a number and never 0.
    _, name, desc = m._profile_subject({"type": "asset", "identity": {"assetName": "H"},
                                        "risk": {"score": None, "level": None}})
    check("an unscored subject says not reported", "Risk not reported" in desc, True)
    check("...and its level is unknown, not blank", "(unknown)" in desc, True)

    # --- the CLI ----------------------------------------------------------------------------------
    tmp = tempfile.mkdtemp()
    paths = []
    for i, d in enumerate([user, asset("HOST1"), asset("HOST2")]):
        p = os.path.join(tmp, "p%d.json" % i)
        with open(p, "w", encoding="utf-8") as f:
            _json.dump(d, f)
        paths.append(p)
    notprofile = os.path.join(tmp, "top.json")
    with open(notprofile, "w", encoding="utf-8") as f:
        _json.dump({"table": "asset", "field": "Risk_Score", "top": [{"Asset_Name": "X"}]}, f)

    class R:
        input, out, title, date, html = paths, os.path.join(tmp, "multi.html"), "T", "2026-08-21", True

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m.cmd_report(R())
    written = _json.loads(buf.getvalue())["written"]
    html = open(written, encoding="utf-8").read()
    check("the CLI renders every subject", html.count("class='subject'"), 3)
    check("...with the requested title", "<title>T</title>" in html, True)
    check("...and names the stack it came from", "stack <code>" in html, True)

    # Mixing shapes is refused, and the message names the offending file -- rendering a `top` beside
    # a profile would produce a document that looks deliberate.
    class BAD(R):
        input = [paths[0], notprofile]
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):     # die() writes to stderr, not stdout
            m.cmd_report(BAD())
        rc = 0
    except SystemExit as e:
        rc = e.code
    check("a non-profile input is refused", rc, 2)
    payload = _json.loads(err.getvalue())
    check("...naming the file", "top.json" in payload["error"], True)
    check("...and saying what multiple inputs are for", "profile" in payload["error"], True)

    # The single-input and stdin paths must be untouched by all of the above.
    class ONE(R):
        input, out = [paths[1]], os.path.join(tmp, "one.html")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m.cmd_report(ONE())
    one = open(_json.loads(buf.getvalue())["written"], encoding="utf-8").read()
    check("a single profile emits no multi-subject markup",
          ("class='subj" in one, "class='pbody" in one, "In this report" in one),
          (False, False, False))
    check("...and still renders its own findings", "Key findings" in one, True)


def _snap_digest():
    """A digest payload shaped like the real one, for reducing to a snapshot record."""
    return {
        "generated": "digest", "stack": "s.example", "table": "asset", "rankedBy": "Risk_Score",
        "topN": 2,
        "metrics": {"metrics": {"assetCount": 34229, "userCount": 10305, "date": "2026-08-05",
                                "avg30DaysAssetCount": 34164, "avg30DaysUserCount": 10067}},
        "connectors": {"fetched": {"profiles": "ok", "ingestion": "ok"},
                       "summary": {"connectorsEnabled": 4, "healthy": 1, "degraded": 1, "failing": 2,
                                   "idle": 0, "lastIngestUtc": "2026-08-05T14:58:00Z"},
                       "connectors": [{"connector": "crowdstrike", "health": "ok"},
                                      {"connector": "tenable", "health": "degraded"},
                                      {"connector": "intune", "health": "failing"},
                                      {"connector": "azure", "health": "failing"}]},
        "breakdown": {"table": "asset", "by": "Risk_Level", "fieldType": "String", "total": 34229,
                      "sampledForValues": 800, "distinctValuesSeen": 3, "complete": True,
                      "accountedRecords": 34229,
                      "groups": [{"value": "1-low", "count": 29899, "percent": 87.4},
                                 {"value": "2-medium", "count": 4210, "percent": 12.3},
                                 {"value": "3-high", "count": 120, "percent": 0.4}]},
        "topUsers": {"table": "user", "field": "Risk_Score", "matchedAtThreshold": 900,
                     "totalInTail": 512, "apiCalls": 9,
                     "top": [{"Owner_Name": "Dana Whitfield", "Risk_Score": 1500},
                             {"Owner_Name": "Priya Raman", "Risk_Score": 1400}]},
        "topAssets": {"table": "asset", "field": "Risk_Score", "matchedAtThreshold": 1000,
                      "totalInTail": 2600, "truncated": True, "apiCalls": 21,
                      "note": "Tail spans 2600 records; only the first 2000 were read.",
                      "top": [{"Asset_Name": "PROD-DB-04", "IP_Address": ["10.4.1.9"],
                               "Risk_Score": 4120}]},
    }


def test_report_cleanup(m):
    """The print-to-PDF temp HTML carries the same customer PII as the PDF. A double browser
    failure (e.g. two timeouts) used to propagate before the os.remove, orphaning it in the
    working tree -- where no gitignore rule covered it -- and killing the verb instead of
    degrading to the documented HTML fallback."""
    print("[13b] report: PII temp-file cleanup on browser failure (offline)")
    import contextlib, io
    tmp = tempfile.mkdtemp()
    inp = os.path.join(tmp, "in.json")
    with open(inp, "w", encoding="utf-8") as f:
        json.dump({"table": "asset", "totalRecords": 1, "shown": 1,
                   "rows": [{"Asset_Name": "SENSITIVE-HOST-01"}]}, f)
    out = os.path.join(tmp, "r.pdf")

    class A:
        title, date, html = None, None, False
    a = A(); a.input, a.out = inp, out

    real = (m._find_browser, m._print_to_pdf, m.load_config)
    m.load_config = lambda: ("s.example", "tok", None)
    m._find_browser = lambda: "fake-chrome"

    def hang(*args):
        raise RuntimeError("browser hung twice")  # what a double TimeoutExpired surfaces as

    m._print_to_pdf = hang
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            m.cmd_report(a)
        res = json.loads(buf.getvalue())
        check("a twice-failed browser still answers (HTML fallback)", res.get("format"), "html")
        check("...saying the conversion failed", "failed" in (res.get("note") or ""), True)
        check("...and the PII temp file is gone", os.path.exists(out + ".tmp.html"), False)
        check("...leaving only the declared fallback next to the input",
              sorted(os.listdir(tmp)), ["in.json", "r.pdf.html"])
    finally:
        (m._find_browser, m._print_to_pdf, m.load_config) = real


def test_snapshots(m):
    """Local snapshot storage (design/trends.md phase 1). Nothing here is reachable from a
    natural-language question yet -- SKILL.md gains no trend routing until phase 5 -- but the storage
    guarantees are the ones a trend built on top will inherit, so they are pinned now.

    The load-bearing one: a snapshot's `coverage` is mandatory and null-when-unreadable, because a
    vulnerability count that fell when its scanner broke is not an improvement. The number is real;
    only the interpretation is wrong, and nothing about the output looks partial."""
    print("[14] snapshot storage (offline)")
    import contextlib, io
    tmp = tempfile.mkdtemp()
    real_cfg_dir, real_load_config, real_build = m.CFG_DIR, m.load_config, m.build_digest
    m.CFG_DIR = tmp
    m.load_config = lambda: ("demo.example.com", "tok", None)
    try:
        # --- round trip, and the schema is honoured rather than assumed -------------------------
        path = m._snapshots_path()
        check("history file is per-stack and JSONL", os.path.basename(path),
              "snapshots.demo.example.com.jsonl")
        rec = m.snapshot_record(_snap_digest())
        m.append_snapshot(rec)
        recs, skipped = m.load_snapshots(path)
        check("one appended record reads back", (len(recs), len(skipped)), (1, 0))
        check("record carries schema 1", recs[0].get("schema"), 1)
        check("one line per snapshot", len(open(path, encoding="utf-8-sig").read().strip().splitlines()), 1)
        m.append_snapshot(rec)
        recs, _ = m.load_snapshots(path)
        check("appending never rewrites history", len(recs), 2)

        # --- an unrecognised schema is skipped WITH A REASON, never coerced ---------------------
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"schema": 99, "totals": {"assets": 1}}) + "\n")
            f.write("{not json at all\n")
            f.write(json.dumps({"schema": 1, "stackDate": "2026-08-06", "totals": {"assets": 5}}) + "\n")
        recs, skipped = m.load_snapshots(path)
        check("forward-schema line is not parsed as history", len(recs), 3)
        check("...it is skipped, with the line numbered", [s["line"] for s in skipped], [3, 4])
        check("...and the reason names the schema it saw", "99" in skipped[0]["reason"], True)
        check("...and says which schema this build reads", "schema 1" in skipped[0]["reason"], True)
        check("unparsable line is skipped, not fatal", skipped[1]["reason"], "line is not valid JSON")
        check("later valid records still load", recs[-1]["totals"]["assets"], 5)

        # --- per-stack isolation: two FQDNs never read each other's history ---------------------
        m.load_config = lambda: ("other.example.com", "tok", None)
        other = m._snapshots_path()
        check("a second stack gets its own file", other != path, True)
        orecs, _ = m.load_snapshots(other)
        check("...and starts empty rather than inheriting", len(orecs), 0)
        m.append_snapshot(m.snapshot_record(_snap_digest()))
        check("...writing it leaves the first stack's history alone", len(m.load_snapshots(path)[0]), 3)
        m.load_config = lambda: ("demo.example.com", "tok", None)

        # --- the record shape (design/trends.md §3) ---------------------------------------------
        check("stackDate comes from the API's own date", rec["stackDate"], "2026-08-05")
        check("takenAt is a UTC local-clock stamp", rec["takenAt"].endswith("Z") and len(rec["takenAt"]) == 20, True)
        check("totals captured", (rec["totals"]["assets"], rec["totals"]["users"]), (34229, 10305))
        check("...including the stack-side 30-day averages", rec["totals"]["assets30DayAvg"], 34164)
        check("coverage tally captured", (rec["coverage"]["connectorsEnabled"], rec["coverage"]["failing"]), (4, 2))
        check("failing connectors named, for the coverage check a trend needs",
              rec["coverage"]["failingNames"], ["azure", "intune"])
        check("degraded named separately rather than mislabelled failing",
              rec["coverage"]["degradedNames"], ["tenable"])
        check("last ingest recorded", rec["coverage"]["lastIngestAt"], "2026-08-05T14:58:00Z")
        b = rec["breakdowns"][0]
        check("breakdown groups become a {value: count} map", b["groups"],
              {"1-low": 29899, "2-medium": 4210, "3-high": 120})
        check("...with the completeness verdict carried through", (b["complete"], b["accountedRecords"]),
              (True, 34229))
        check("rankings trimmed to counts", (rec["rankings"][0]["totalInTail"], rec["rankings"][0]["top"]),
              (512, 2))
        check("...and a truncated tail stays flagged", rec["rankings"][1]["truncated"], True)

        # --- no customer identifier reaches disk (§5/§7: PII policy, not a size trim) -----------
        blob = open(path, encoding="utf-8-sig").read()
        for leak in ("Dana Whitfield", "Priya Raman", "PROD-DB-04", "10.4.1.9",
                     "Owner_Name", "Asset_Name", "IP_Address"):
            check("no %r in the written history" % leak, leak in blob, False)

        # --- coverage is mandatory: null + a stated reason, never a silent zero -----------------
        d = _snap_digest()
        d["connectors"]["fetched"]["profiles"] = "HTTP 403: Forbidden"
        r2 = m.snapshot_record(d)
        check("unreadable connector health records coverage as null", r2["coverage"], None)
        check("...with the reason kept, not the misleading 0-failing tally",
              [s["section"] for s in r2["sectionsUnavailable"]], ["coverage"])
        check("...naming the HTTP status", "403" in r2["sectionsUnavailable"][0]["error"], True)
        check("the coverage key is present, never omitted", "coverage" in r2, True)

        d = _snap_digest()
        del d["connectors"]
        d["sectionsUnavailable"] = [{"section": "connectors", "error": "HTTP 403: Forbidden"}]
        r3 = m.snapshot_record(d)
        check("a failed connectors section also yields coverage null", r3["coverage"], None)
        check("...and the original failure is preserved",
              [s["section"] for s in r3["sectionsUnavailable"]], ["connectors"])

        d = _snap_digest()
        d["connectors"]["fetched"]["ingestion"] = "HTTP 500: boom"
        r4 = m.snapshot_record(d)
        check("health readable but run history not is flagged, not assumed",
              r4["coverage"]["ingestionUnreadable"], True)

        # --- an incomplete breakdown is inherited, not laundered --------------------------------
        d = _snap_digest()
        d["breakdown"].update({"complete": False, "accountedRecords": 21549,
                               "unaccountedRecords": 12680, "note": "37% unaccounted"})
        r5 = m.snapshot_record(d)
        check("incomplete breakdown stays incomplete in history",
              (r5["breakdowns"][0]["complete"], r5["breakdowns"][0]["unaccountedRecords"]), (False, 12680))

        # --- a high-cardinality breakdown warns with its measured cost --------------------------
        d = _snap_digest()
        d["breakdown"].update({"by": "OS", "distinctValuesSeen": 51, "groupsCapped": 40})
        r6 = m.snapshot_record(d)
        w = r6["warnings"][0]
        check("expensive breakdown is flagged", w["breakdown"], "OS")
        check("...with the measured call cost, not a vague caution", w["estimatedCalls"], 49)
        check("...and points at the one-call alternative", "metric" in w["warning"], True)
        check("a 3-group breakdown warns about nothing", "warnings" in rec, False)

        # ...and the *next* run warns before spending the calls, from what the last one measured.
        # Cardinality can't be known in advance -- field metadata carries no values -- so the previous
        # measurement is the only honest pre-check, and it costs no API call.
        m.build_digest = lambda *a, **k: _snap_digest()
        m.append_snapshot(r6)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(err):
                m.take_snapshot(table="asset", by="OS")
        check("a repeat of an expensive breakdown warns BEFORE running", "49 of the 60 calls" in err.getvalue(), True)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(err):
                m.take_snapshot(table="asset", by="Risk_Level")
        check("...and a cheap one says nothing", err.getvalue(), "")

        # --- entry points ----------------------------------------------------------------------
        before = len(m.load_snapshots(path)[0])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with contextlib.redirect_stderr(io.StringIO()):
                m.cmd_snapshot(argparse.Namespace(table="asset", by=None, field="Risk_Score", top=2))
        info = json.loads(buf.getvalue())
        check("`snapshot` reports where it wrote", os.path.basename(info["path"]),
              "snapshots.demo.example.com.jsonl")
        check("...and adds exactly one record", len(m.load_snapshots(path)[0]) - before, 1)
        check("...and states that coverage was recorded", info["coverageRecorded"], True)
        check("...and names what it captured", info["breakdownsCaptured"], ["Risk_Level"])
        check("...and names the lines it could not read", [s["line"] for s in info["historySkipped"]], [3, 4])

        # `digest --snapshot` must append from the payload it already has: zero extra API calls, and
        # one JSON document on stdout so `digest --snapshot | report` still parses.
        real_call = m.call
        m.call = lambda *a, **k: (_ for _ in ()).throw(AssertionError("snapshot made an API call"))
        try:
            before = len(m.load_snapshots(path)[0])
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                m.cmd_digest(argparse.Namespace(table="asset", by=None, field="Risk_Score", top=2,
                                                snapshot=True))
            out = json.loads(buf.getvalue())   # one document, not two concatenated
            check("digest --snapshot costs no extra API call", out["snapshot"]["written"], True)
            check("...adds exactly one line", len(m.load_snapshots(path)[0]) - before, 1)
            check("...and the digest payload still comes through intact",
                  out["metrics"]["metrics"]["assetCount"], 34229)
        finally:
            m.call = real_call

        # Plain `digest` must not write history.
        before = len(m.load_snapshots(path)[0])
        with contextlib.redirect_stdout(io.StringIO()):
            m.cmd_digest(argparse.Namespace(table="asset", by=None, field="Risk_Score", top=2))
        check("plain digest writes no snapshot", len(m.load_snapshots(path)[0]) - before, 0)
    finally:
        m.CFG_DIR, m.load_config, m.build_digest = real_cfg_dir, real_load_config, real_build


def _snap(date, assets=34229, users=10305, failing=("intune",), degraded=("tenable",),
          groups=None, coverage=True, complete=True, metrics=None, threshold=1000, tail=512,
          taken=None):
    """A phase-1-shaped snapshot record, for driving trend comparisons offline."""
    rec = {"schema": 1, "takenAt": taken or (date + "T23:00:00Z"), "stackDate": date,
           "fqdn": "s.example",
           "totals": {"assets": assets, "users": users,
                      "assets30DayAvg": assets - 65, "users30DayAvg": users - 238},
           "breakdowns": [{"table": "asset", "by": "Risk_Level", "fieldType": "String",
                           "total": assets, "complete": complete, "distinctValuesSeen": 3,
                           "groups": groups if groups is not None
                           else {"1-low": 29899, "2-medium": 4210, "3-high": 120}}],
           "rankings": [{"table": "asset", "field": "Risk_Score", "top": 5,
                         "matchedAtThreshold": threshold, "totalInTail": tail, "truncated": False}]}
    if not complete:
        rec["breakdowns"][0]["unaccountedRecords"] = 12680
    rec["coverage"] = ({"connectorsEnabled": 19, "healthy": 2, "degraded": len(degraded),
                        "failing": len(failing), "idle": 0,
                        "failingNames": sorted(failing), "degradedNames": sorted(degraded),
                        "lastIngestAt": date + "T14:00:00Z"} if coverage else None)
    if metrics is not None:
        rec["metrics"] = metrics
    return rec


def test_coverage_identity(m):
    """Connector name lists carry one entry per counted row, keyed on (connector, profile).

    Every assertion here was checked to FAIL against the pre-fix code, which keyed both lists on the
    display name alone via a set comprehension. On a stack with two degraded profiles of the same
    integration that reported `degraded: 8` beside seven names -- indistinguishable from a connector
    that went unnamed -- and made one of a same-named pair recovering invisible to a set diff.
    """
    print("[26] connector identity in coverage (the count/name invariant)")

    def cov(rows, tally):
        conn = {"fetched": {"profiles": "ok", "ingestion": "ok"},
                "summary": dict({"connectorsEnabled": len(rows), "idle": 0,
                                 "lastIngestUtc": "2026-08-24T16:00:00Z"}, **tally),
                "connectors": rows}
        return m._snapshot_coverage(conn, [])

    # Two distinct profiles of one integration, both degraded -- the real demo-stack shape.
    rows = [{"connector": "CrowdStrike Falcon", "profile": "demo", "health": "degraded"},
            {"connector": "CrowdStrike Falcon", "profile": "Lucidum NFR", "health": "degraded"},
            {"connector": "Slack", "profile": "Lucidum", "health": "failing"}]
    c = cov(rows, {"healthy": 0, "degraded": 2, "failing": 1})
    check("same-named profiles are not collapsed", len(c["degradedNames"]), 2)
    check("...so the count and the name list agree", len(c["degradedNames"]), c["degraded"])
    check("...and each names its own profile", c["degradedNames"],
          ["CrowdStrike Falcon (Lucidum NFR)", "CrowdStrike Falcon (demo)"])
    check("failing names qualified the same way", c["failingNames"], ["Slack (Lucidum)"])
    check("the scheme is stamped into the record", c["coverageIdentity"], "connector+profile")

    # A nameless row used to be filtered out while still being counted.
    c2 = cov([{"profile": "orphan", "health": "failing"}], {"healthy": 0, "degraded": 0, "failing": 1})
    check("a nameless row is named unknown, not dropped", c2["failingNames"],
          ["(unnamed connector: orphan)"])
    check("...keeping the invariant", len(c2["failingNames"]), c2["failing"])

    # The invariant itself, over every health mix -- this is the guard, not the examples above.
    mixed = [{"connector": "A", "profile": "p1", "health": "failing"},
             {"connector": "A", "profile": "p2", "health": "failing"},
             {"connector": "A", "profile": "p3", "health": "degraded"},
             {"connector": "B", "profile": "p1", "health": "degraded"},
             {"connector": "B", "profile": "p1", "health": "ok"}]
    c3 = cov(mixed, {"healthy": 1, "degraded": 2, "failing": 2})
    check("len(failingNames) == failing, by construction", len(c3["failingNames"]), c3["failing"])
    check("len(degradedNames) == degraded, by construction", len(c3["degradedNames"]), c3["degraded"])

    # --- the migration boundary: a scheme change must not manufacture total churn --------------
    old = _snap("2026-08-23", failing=("intune",), degraded=("tenable",))
    new = _snap("2026-08-24", failing=("Microsoft Intune (Default profile)",),
                degraded=("Tenable (Lucidum)",))
    new["coverage"]["coverageIdentity"] = "connector+profile"
    v, blocked = m._coverage_verdict(old, new)
    check("a scheme change does not block the trend", blocked, False)
    check("...counts stay comparable", v["comparable"], True)
    check("...names do not", v["namesComparable"], False)
    check("...no connector is claimed to have entered the failing set",
          "failingAdded" in v or "failingResolved" in v, False)
    check("...and the window is flagged as changed rather than quietly clean",
          v["coverageChanged"], True)
    check("...the reason says the counts survive", "counts below still can be"
          in v["namesIncomparableReason"], True)

    # Same scheme at both ends still diffs normally -- the refusal is narrow.
    n2 = _snap("2026-08-25", failing=("Microsoft Intune (Default profile)", "Slack (Lucidum)"),
               degraded=("Tenable (Lucidum)",))
    n2["coverage"]["coverageIdentity"] = "connector+profile"
    v2, _ = m._coverage_verdict(new, n2)
    check("matching schemes still diff", v2.get("namesComparable", True), True)
    check("...and still name what entered", v2["failingAdded"], ["Slack (Lucidum)"])

    # --- connector-regression must go unevaluable, never clear, across the boundary -------------
    rule = {"name": "connector-regression", "condition": "coverage-regressed"}
    ev = m.evaluate_alerts({"coverage": v, "window": {"from": "2026-08-23"}}, new, [rule])
    names = lambda k: [r["rule"] for r in ev[k]]
    check("the rule is unevaluable across a scheme change", names("unevaluable"),
          ["connector-regression"])
    check("...not clear", "connector-regression" in names("clear"), False)
    check("...and says so in words", "did NOT pass" in ev["unevaluable"][0]["note"], True)


def test_trend(m):
    """The `trend` verb (design/trends.md phase 2). Still dormant -- SKILL.md gains no trend routing
    until phase 5 -- but every refusal here is load-bearing.

    A vulnerability count that fell because its scanner connector broke is not an improvement. The
    number is real; only the interpretation is wrong, and nothing about a percentage looks partial. So
    the tests below assert on what the verb REFUSES to say as much as on what it computes."""
    print("[15] trend comparison (offline)")
    import contextlib, io

    # --- fewer than two comparable snapshots: never a 0% flat line --------------------------------
    t = m.compute_trend([])
    check("empty history is insufficient, not flat", t.get("insufficientHistory"), True)
    check("...with no computed changes at all", t["changes"], [])
    check("...and no zero delta anywhere in the payload", "0%" in json.dumps(t), False)
    check("...and it says there is nothing to backfill from", "backfill" in t["note"], True)
    t = m.compute_trend([_snap("2026-08-05")])
    check("one snapshot is insufficient", (t.get("insufficientHistory"), t["distinctStackDates"]), (True, 1))

    # Two snapshots on ONE stack date are one point on the stack's timeline, not two: both read the
    # same daily ingest, so differencing them would manufacture the flattest possible wrong answer.
    t = m.compute_trend([_snap("2026-08-05", taken="2026-08-05T09:00:00Z"),
                         _snap("2026-08-05", taken="2026-08-05T21:00:00Z")])
    check("two snapshots on one stack date is not a trend", t.get("insufficientHistory"), True)
    check("...and says so in terms of distinct stack dates", t["distinctStackDates"], 1)
    check("...producing no change rows to misread", t["changes"], [])

    # --- a normal, comparable two-endpoint trend --------------------------------------------------
    pair = [_snap("2026-07-01", assets=33000, users=10000, tail=400),
            _snap("2026-08-05", assets=34229, users=10305, tail=512)]
    t = m.compute_trend(pair)
    check("endpoints chosen by stack date", (t["from"]["stackDate"], t["to"]["stackDate"]),
          ("2026-07-01", "2026-08-05"))
    check("stable coverage is comparable and unflagged",
          (t["coverage"]["comparable"], t["coverage"]["coverageChanged"]), (True, False))
    rows = {r["name"]: r for r in t["changes"]}
    check("absolute change on totals", (rows["assets"]["from"], rows["assets"]["to"],
                                        rows["assets"]["change"]), (33000, 34229, 1229))
    check("percentage change on totals", rows["assets"]["percentChange"], 3.7)
    check("users compared too", rows["users"]["change"], 305)
    check("the stack's own 30-day average is carried as its own row",
          "assets (stack 30-day average)" in rows, True)
    check("breakdown categories compared", rows["3-high"]["change"], 0)
    check("...and a real 0 change is allowed when both ends were measured",
          rows["3-high"]["percentChange"], 0.0)
    check("ranking tails compared at a matching threshold",
          (rows["asset Risk_Score at or above 1000"]["change"],
           rows["asset Risk_Score at or above 1000"]["atOrAbove"]), (112, 1000))
    check("no notTracked on a fully captured trend", "notTracked" in t, False)

    # --- coverage CHANGED: computed, but flagged, and never attributed ----------------------------
    t = m.compute_trend([_snap("2026-07-01", failing=("intune",)),
                         _snap("2026-08-05", failing=("intune", "tenable-vuln"), degraded=())])
    cov = t["coverage"]
    check("a changed failing-connector set is flagged", cov["coverageChanged"], True)
    check("...naming what started failing", cov["failingAdded"], ["tenable-vuln"])
    check("...and what stopped being degraded", cov["degradedResolved"], ["tenable"])
    check("...with a note telling the reader to judge before believing a percentage",
          "rather than a change in the environment" in cov["note"], True)
    check("...and the trend is still computed, since the change is KNOWN",
          len(t["changes"]) > 0, True)
    check("...but no metric-to-connector attribution is invented",
          "not something this API exposes" in cov["note"], True)
    t2 = m.compute_trend([_snap("2026-07-01", failing=("intune", "azure")),
                          _snap("2026-08-05", failing=("azure", "intune"))])
    check("the same failing set in a different order is not a change",
          t2["coverage"]["coverageChanged"], False)

    # --- coverage UNREADABLE: unverifiable, not computed ------------------------------------------
    for label, pair in (("the later", [_snap("2026-07-01"), _snap("2026-08-05", coverage=False)]),
                        ("the earlier", [_snap("2026-07-01", coverage=False), _snap("2026-08-05")]),
                        ("both", [_snap("2026-07-01", coverage=False),
                                  _snap("2026-08-05", coverage=False)])):
        t = m.compute_trend(pair)
        check("null coverage at %s endpoint is unverifiable" % label, t.get("unverifiable"), True)
        check("...so nothing is computed", t["changes"], [])
        check("...and no percentage is printed at all", "percentChange" in json.dumps(t), False)
    check("...and the reason explains why, not just that", "stopped reporting" in
          m.compute_trend([_snap("2026-07-01"), _snap("2026-08-05", coverage=False)])["coverage"]["reason"], True)

    # A snapshot with no stackDate cannot be placed on the stack's timeline at all.
    bad = _snap("2026-08-05"); bad["stackDate"] = None
    t = m.compute_trend([_snap("2026-07-01"), bad])
    check("a snapshot with no stackDate is named unusable", len(t["snapshotsUnusable"]), 1)
    check("...and does not silently become an endpoint", t.get("insufficientHistory"), True)

    # --- an incomplete breakdown is inherited and stated ------------------------------------------
    t = m.compute_trend([_snap("2026-07-01", complete=False), _snap("2026-08-05")])
    row = next(r for r in t["changes"] if r["name"] == "1-low")
    check("an incomplete endpoint marks every category it produced", row["incompleteAt"], ["from"])
    check("...and says the movement inherits it", "inherits that incompleteness" in row["note"], True)
    t = m.compute_trend([_snap("2026-07-01", complete=False), _snap("2026-08-05", complete=False)])
    check("both ends incomplete is reported as both",
          next(r for r in t["changes"] if r["name"] == "1-low")["incompleteAt"], ["from", "to"])

    # A category present at one end only: unknown there, NOT zero. The discovery sample may simply
    # never have reached it, so differencing against an assumed 0 would invent the whole movement.
    t = m.compute_trend([_snap("2026-07-01", groups={"1-low": 29899}),
                         _snap("2026-08-05", groups={"1-low": 29899, "3-high": 120})])
    row = next(r for r in t["changes"] if r["name"] == "3-high")
    check("a category missing at one end yields no change", (row["change"], row["percentChange"]),
          (None, None))
    check("...and says unknown rather than zero", "unknown, not zero" in row["note"], True)

    # --- notTracked: never 0, 0%, "no change" or a flat line --------------------------------------
    t = m.compute_trend([_snap("2026-07-01"), _snap("2026-08-05")], metric="kev-exposed")
    check("an untracked metric is reported as such", t["notTracked"][0]["metric"], "kev-exposed")
    check("...naming the absence of a backfill", "keeps no history" in t["notTracked"][0]["reason"], True)
    check("...and producing no metric row", [r for r in t["changes"] if r["kind"] == "metric"], [])
    check("...and never the words 'no change'", "no change" in json.dumps(t).lower(), False)
    check("...with the rule stated in the envelope", "unanswerable" in t["notTrackedNote"], True)
    t = m.compute_trend([_snap("2026-07-01"), _snap("2026-08-05")], by="OS")
    check("an untracked breakdown field is notTracked too", t["notTracked"][0]["breakdown"], "OS")
    check("...and no breakdown rows are invented for it",
          [r for r in t["changes"] if r["kind"] == "breakdown"], [])

    # Phase 3 will populate `metrics`; the comparison path is already correct for it.
    mt = lambda c, ok=True: [{"name": "kev-exposed", "label": "KEV-exposed", "count": c, "ok": ok}]
    t = m.compute_trend([_snap("2026-07-01", metrics=mt(400)), _snap("2026-08-05", metrics=mt(458))],
                        metric="kev-exposed")
    row = next(r for r in t["changes"] if r["kind"] == "metric")
    check("a captured metric is compared", (row["change"], row["percentChange"]), (58, 14.5))
    check("...and carries its human label", row["label"], "KEV-exposed")
    check("...with nothing left in notTracked", "notTracked" in t, False)
    # A metric that stopped resolving is unavailable, never 0 -- a zero reads as good news.
    t = m.compute_trend([_snap("2026-07-01", metrics=mt(400)),
                         _snap("2026-08-05", metrics=[{"name": "kev-exposed", "count": None,
                                                       "ok": False, "error": "no such field"}])],
                        metric="kev-exposed")
    check("a metric that stopped resolving is unavailable", t["notTracked"][0]["unavailableAt"], ["to"])
    check("...with the reason it gave", "no such field" in t["notTracked"][0]["reason"], True)
    check("...and is never counted as zero", [r for r in t["changes"] if r["kind"] == "metric"], [])

    # --- no percentage invented from a zero baseline ----------------------------------------------
    t = m.compute_trend([_snap("2026-07-01", groups={"1-low": 100, "3-high": 0}),
                         _snap("2026-08-05", groups={"1-low": 100, "3-high": 47})])
    row = next(r for r in t["changes"] if r["name"] == "3-high")
    check("up from zero has an absolute change", row["change"], 47)
    check("...but no percentage, since 0% would say the opposite", row["percentChange"], None)
    check("...and says why", "zero" in row["percentNote"], True)

    # --- a ranking measured at two different thresholds is not comparable ------------------------
    t = m.compute_trend([_snap("2026-07-01", threshold=300, tail=900),
                         _snap("2026-08-05", threshold=1000, tail=512)])
    check("differing tail thresholds are not differenced",
          [r for r in t["changes"] if r["kind"] == "ranking"], [])
    check("...and the reason names both thresholds",
          all(s in t["caveats"][-1]["note"] for s in (">=300", ">=1000")), True)

    # --- --since, and history that cannot be read -------------------------------------------------
    three = [_snap("2026-06-01", assets=30000), _snap("2026-07-01", assets=33000),
             _snap("2026-08-05", assets=34229)]
    t = m.compute_trend(three, since="2026-07-01")
    check("--since picks the earliest endpoint at or after it", t["from"]["stackDate"], "2026-07-01")
    check("...and the most recent as the other end", t["to"]["stackDate"], "2026-08-05")
    t = m.compute_trend(three, since="2026-09-01")
    check("a --since past every snapshot is insufficient, not empty-but-fine",
          t.get("insufficientHistory"), True)
    check("...and says there are none at or after it", "at or after 2026-09-01" in t["note"], True)
    t = m.compute_trend(three, skipped=[{"line": 4, "reason": "line is not valid JSON"}])
    check("unreadable history lines stay visible in a trend", t["historySkipped"][0]["line"], 4)

    # --- CSV export keeps the envelope ------------------------------------------------------------
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "trend.csv")
    real = m.load_snapshots, m.load_config
    m.load_config = lambda: ("s.example", "tok", None)
    m.load_snapshots = lambda *a, **k: ([_snap("2026-07-01", assets=33000, failing=("intune",)),
                                         _snap("2026-08-05", failing=("intune", "vuln-scanner"))], [])
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            m.cmd_trend(argparse.Namespace(since=None, metric=None, table=None, by=None,
                                           format="csv", out=path))
        env = json.loads(buf.getvalue())
        check("CSV mode still prints the coverage flag a spreadsheet cannot hold",
              env["coverage"]["coverageChanged"], True)
        check("...and names the connector that started failing", env["coverage"]["failingAdded"],
              ["vuln-scanner"])
        check("...and the rows went to the file, not the envelope", "changes" in env, False)
        check("...reporting how many", env["rowsWritten"] > 0, True)
        text = open(path, encoding="utf-8-sig").read()
        check("the CSV carries the delta columns", text.splitlines()[0].startswith("kind,name,from,to,change,percentChange"), True)
        check("...and a data row", "assets" in text, True)
        # JSON mode: one document, and the stack is named.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            m.cmd_trend(argparse.Namespace(since=None, metric=None, table=None, by=None,
                                           format=None, out=None))
        d = json.loads(buf.getvalue())
        check("json mode names the stack", d["stack"], "s.example")
        check("...and keeps the rows", len(d["changes"]) > 0, True)
    finally:
        m.load_snapshots, m.load_config = real


ASSET_FIELDS = {"Risk_Score": "Float", "Has_KEV": "Binary", "Internet_Exposed": "Binary",
                "Crown_Jewels": "Binary", "OS": "String", "Risk_Level": "String"}
USER_FIELDS = {"Risk_Score": "Float", "Owner_Name": "String", "Joiner_Risk": "Integer"}


def test_trend_report(m):
    """Trend charts (v2.10.0): `trend` output carries a per-date `series`, and `report` renders it
    as branded line charts. The whole feature hangs on one rule inherited from the trend layer: a
    date with no captured value BREAKS the line -- a flat segment (or a zero) across a gap is the
    most convincing possible wrong answer a picture can give."""
    print("[15b] trend series + branded trend report (offline)")
    import contextlib, io

    M = lambda count, ok=True: [{"name": "kev", "label": "KEV-exposed assets", "count": count, "ok": ok}]

    # --- series shape: absence is null, never 0 ----------------------------------------------------
    recs = [_snap("2026-07-01", assets=33000, metrics=M(400)),
            _snap("2026-07-15", assets=33600),                      # metric not captured that day
            _snap("2026-08-05", assets=34229, metrics=M(458))]
    t = m.compute_trend(recs)
    s = t.get("series") or {}
    check("series carries the compared dates", s.get("dates"), ["2026-07-01", "2026-07-15", "2026-08-05"])
    check("totals series is per date", s["totals"]["assets"], [33000, 33600, 34229])
    kev = next(x for x in s["metrics"] if x["name"] == "kev")
    check("an uncaptured metric is null in the series, never 0", kev["values"], [400, None, 458])

    # An absent group is UNKNOWN, with no completeness exception. This shipped once charting it as 0
    # when `complete: true`, which was wrong twice: for a List field `complete` means "covered >=
    # total" (a record can carry several values), so it proves nothing about a value the sample never
    # discovered -- and _trend_breakdowns never made that exception for any type, so the chart said 0
    # while the delta table on the same page said "unknown, not zero".
    for ftype in ("String", "List"):
        recs = [_snap("2026-07-01", groups={"1-low": 100, "3-high": 5}, complete=True),
                _snap("2026-08-05", groups={"1-low": 120}, complete=True)]
        for r in recs:
            r["breakdowns"][0]["fieldType"] = ftype
        s = m.compute_trend(recs)["series"]
        check("absent group is unknown, not 0 (%s field)" % ftype,
              s["breakdowns"][0]["groups"]["3-high"], [5, None])
        # ...and the chart must not contradict the delta table rendered beside it.
        row = next((r for r in m.compute_trend(recs)["changes"] if r.get("name") == "3-high"), None)
        check("...matching what the delta table says (%s)" % ftype,
              "not zero" in ((row or {}).get("note") or ""), True)
    recs[1]["breakdowns"][0]["complete"] = False
    s = m.compute_trend(recs)["series"]
    check("an incomplete date is still named", s["breakdowns"][0].get("incompleteDates"), ["2026-08-05"])

    # Refusals carry no series at all: a chart over unverifiable coverage is the
    # percentage-with-a-caveat mistake in picture form.
    check("insufficient history carries no series", "series" in m.compute_trend([_snap("2026-08-05")]), False)
    t = m.compute_trend([_snap("2026-07-01", coverage=False), _snap("2026-08-05")])
    check("unverifiable coverage carries no series", (t.get("unverifiable"), "series" in t), (True, False))

    # --- the chart: a gap breaks the line ----------------------------------------------------------
    svg = m._line_chart_svg(["d1", "d2", "d3", "d4", "d5"],
                            [("kev", [10, 11, None, 12, 14], "#000000")])
    check("a null splits the line into two segments", svg.count("<polyline"), 2)
    check("...with a dot for every real point", svg.count("<circle"), 4)
    svg = m._line_chart_svg(["d1", "d2", "d3"], [("kev", [10, None, 12], "#000000")])
    check("isolated points render as dots, not lines", (svg.count("<polyline"), svg.count("<circle")), (0, 2))
    check("nothing plottable renders nothing", m._line_chart_svg(["d1"], [("kev", [10], "#000")]), "")

    # The x-axis is spaced by DATE. Index spacing drew Jul 1, Jul 2, Aug 5 as three evenly-spaced
    # points joined by an ordinary line -- a five-week hole in the cadence rendered as one routine
    # step, with the slope wrong. A null at a captured date is only half of "absence".
    offs = m._date_offsets(["2026-07-01", "2026-07-02", "2026-08-05"])
    check("a cadence gap is spaced by elapsed time", [round(o, 3) for o in offs], [0.0, 0.029, 1.0])
    check("an even cadence stays even",
          [round(o, 3) for o in m._date_offsets(["2026-07-01", "2026-07-08", "2026-07-15"])], [0.0, 0.5, 1.0])
    check("unparseable dates fall back to index spacing",
          [round(o, 3) for o in m._date_offsets(["x", "y", "z"])], [0.0, 0.5, 1.0])

    # The flat-line guard has to be tested AFTER padding: at large magnitudes a +/-1.0 nudge is
    # below the ULP, so lo == hi survived and y() divided by zero.
    check("a huge flat series doesn't divide by zero",
          bool(m._line_chart_svg(["d1", "d2"], [("x", [1e16, 1e16], "#000")])), True)
    check("...nor a flat zero series",
          bool(m._line_chart_svg(["d1", "d2"], [("x", [0, 0], "#000")])), True)

    # Dates when connectors were failing are marked ON the chart: _coverage_verdict only examines
    # the two endpoints, so a dip on an unexamined middle date otherwise reads as a real change.
    recs = [_snap("2026-07-01", assets=34000),
            _snap("2026-07-08", assets=26000, failing=("intune", "aws", "okta"), degraded=()),
            _snap("2026-08-05", assets=34200)]
    s = m.compute_trend(recs)["series"]
    flags = s.get("coverageFlags") or []
    check("a mid-window coverage drop is flagged", [f["date"] for f in flags], ["2026-07-08"])
    check("...naming what was wrong", "3 connector(s) failing" in flags[0]["reason"], True)
    # Measured against the endpoints, not perfection: a stack permanently running one failing
    # connector must not flag every date, or the mark stops meaning anything.
    steady = m.compute_trend([_snap("2026-07-01"), _snap("2026-07-08"), _snap("2026-08-05")])["series"]
    check("...but a steady baseline of failures is not flagged", "coverageFlags" in steady, False)
    block = m._chart_block("Inventory totals", s["dates"], sorted(s["totals"].items()), coverage_flags=flags)
    check("...marked on the chart itself", "<rect" in block, True)
    check("...and explained beneath it", "may be the data sources" in block, True)
    # Unreadable coverage is flagged too -- not silently treated as healthy.
    s2 = m.compute_trend([_snap("2026-07-01"), _snap("2026-07-08", coverage=False, failing=(), degraded=()),
                          _snap("2026-08-05")])["series"]
    check("unreadable coverage is flagged, not assumed healthy",
          [f["date"] for f in (s2.get("coverageFlags") or [])], ["2026-07-08"])

    # n() feeds _simple_table, which does not escape its cells.
    check("the delta table's value formatter escapes", m._trend_html.__globals__["_esc"]("<b>"), "&lt;b&gt;")

    # --- end to end: branded HTML from a real trend payload ----------------------------------------
    tmp = tempfile.mkdtemp()
    recs = [_snap("2026-07-01", assets=33000, metrics=M(400),
                  groups={"1-low": 100, "<script>alert(1)</script>": 5}),
            _snap("2026-07-15", assets=33600),
            _snap("2026-08-05", assets=34229, metrics=M(458),
                  groups={"1-low": 120, "<script>alert(1)</script>": 9})]
    payload = m.compute_trend(recs)
    payload["stack"] = "s.example"
    inp = os.path.join(tmp, "trend.json")
    with open(inp, "w", encoding="utf-8") as f:
        json.dump(payload, f)

    class A:
        title, date, html = None, None, True
    a = A(); a.input, a.out = inp, os.path.join(tmp, "trend.html")

    real_lc = m.load_config
    m.load_config = lambda: ("s.example", "tok", None)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            m.cmd_report(a)
        check("report answers in html", json.loads(buf.getvalue()).get("format"), "html")
        with open(a.out, encoding="utf-8") as f:
            html = f.read()
        check("the page has line charts", "<svg" in html and "<polyline" in html, True)
        check("...the gap honesty note", "unknown, never zero" in html, True)
        check("...the delta table", "Changes, 2026-07-01" in html, True)
        check("...and customer-originated values are escaped", "<script>alert(1)</script>" in html, False)
        check("   (but present, escaped)", "&lt;script&gt;" in html, True)

        # A refusal renders AS the refusal -- a branded page saying why, never an empty chart.
        with open(inp, "w", encoding="utf-8") as f:
            json.dump(m.compute_trend([_snap("2026-08-05")]), f)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            m.cmd_report(a)
        with open(a.out, encoding="utf-8") as f:
            html = f.read()
        check("a refusal renders as the refusal", "Why there is no trend" in html, True)
        check("...with no chart to misread", "<polyline" in html, False)
    finally:
        m.load_config = real_lc

    # --- CSV mode strips the series (the envelope's caveats must stay readable) --------------------
    real = (m.CFG_DIR, m.load_config)
    m.CFG_DIR, m.load_config = tempfile.mkdtemp(), lambda: ("s.example", "tok", None)
    try:
        m.append_snapshot(_snap("2026-07-01", metrics=M(400)))
        m.append_snapshot(_snap("2026-08-05", metrics=M(458)))

        class T:
            since = metric = table = by = None
            derive_label = derive_table = derive_where = derive_smart_label = None
            name_entities, out = False, None

        t1 = T(); t1.format = None
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            m.cmd_trend(t1)
        check("json mode carries the series", "series" in json.loads(buf.getvalue()), True)

        t2 = T(); t2.format = "csv"
        obuf, ebuf = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(obuf), contextlib.redirect_stderr(ebuf):
            m.cmd_trend(t2)
        check("csv mode drops it from the envelope", "series" in json.loads(ebuf.getvalue()), False)
        check("...but keeps the caveat keys", "coverage" in json.loads(ebuf.getvalue()), True)
    finally:
        m.CFG_DIR, m.load_config = real


def test_metrics(m):
    """Option B, declarative metrics (design/trends.md phase 3). Still dormant until phase 5.

    The load-bearing requirement: validate at `metrics add` time, not at snapshot time. A metric that
    silently counts 0 every week, unattended, is the exact wrong-answer class this codebase keeps
    fixing -- and worse, because a zero reads as good news so nobody investigates it."""
    print("[16] user-defined metrics (offline)")
    import contextlib, io
    tmp = tempfile.mkdtemp()
    real = (m.CFG_DIR, m.load_config, m.call, m._FIELD_MAP, m._LABELS)
    m.CFG_DIR = tmp
    m.load_config = lambda: ("s.example", "tok", None)
    m._FIELD_MAP = {"asset": dict(ASSET_FIELDS), "user": dict(USER_FIELDS)}
    m._LABELS = [{"name": "Crown Jewels", "field": "Crown_Jewels", "table": "asset",
                  "type": "Binary", "purpose": "Assets the business cannot lose.", "queryable": True}]
    bodies = []

    def fake_call(method, endpoint, body=None, retries=1):
        bodies.append(body)
        return {"totalRecords": 458, "data": []}
    m.call = fake_call

    def add(**kw):
        kw.setdefault("label", None); kw.setdefault("table", "asset")
        kw.setdefault("where", None); kw.setdefault("smart_label", None)
        return m.add_metric(kw.pop("name"), kw.pop("label"), kw.pop("table"), kw.pop("where"),
                            kw.pop("smart_label"), **kw)

    try:
        # --- validation happens at ADD time, and refuses -----------------------------------------
        rec, problem = add(name="kev", where=["Hs_KEV == Binary true"])
        check("a misspelled field is refused at add time", rec, None)
        check("...naming the closest match", "'Has_KEV'" in problem, True)
        check("...and nothing was written", m.load_metrics(), [])
        rec, problem = add(name="kev", where=["has_kev == Binary true"])
        check("a case-only miss is called out as such", "case-sensitive" in problem, True)
        rec, problem = add(name="kev", where=["Risk_Score >= 500"])
        check("a malformed clause is refused too", rec, None)
        check("...explaining the type slot", "type slot" in problem, True)
        rec, problem = add(name="kev")
        check("a metric with no definition at all is refused", "neither" in problem, True)
        rec, problem = add(name="kev", where=["Has_KEV == Binary true"], smart_label="Crown Jewels")
        check("both a where and a smart-label is refused", "not both" in problem, True)
        rec, problem = add(name="bad name!", where=["Has_KEV == Binary true"])
        check("an unusable metric name is refused", "not usable" in problem, True)
        check("still nothing written after six refusals", m.load_metrics(), [])

        # --- a valid metric saves, and costs exactly one call to measure --------------------------
        rec, problem = add(name="kev-exposed", label="KEV-exposed assets",
                           where=["Has_KEV == Binary true", "Internet_Exposed == Binary true"])
        check("a valid metric is saved", (problem, rec["name"]), (None, "kev-exposed"))
        check("...with its two clauses", len(rec["where"]), 2)
        check("...and reads back from disk", [x["name"] for x in m.load_metrics()], ["kev-exposed"])
        check("...into a per-stack file", os.path.basename(m._metrics_path()), "metrics.s.example.json")
        doc = json.load(open(m._metrics_path(), encoding="utf-8-sig"))
        check("...carrying a schema", doc["schema"], 1)
        bodies.clear()
        got = m.measure_metric(rec)
        check("measuring costs exactly one API call", len(bodies), 1)
        check("...on the cheapest possible page, never paging for a count",
              bodies[0]["paging"], {"page": 0, "recordsPerPage": 1})
        check("...ANDing the clauses as separate inner arrays", len(bodies[0]["query"]), 2)
        check("...and reads totalRecords", (got["count"], got["ok"]), (458, True))
        check("...keeping the human label", got["label"], "KEV-exposed assets")

        # --- a SmartLabel metric resolves through find_label() -----------------------------------
        rec, problem = add(name="crown-jewels", label="Crown jewels", smart_label="crown jewels")
        check("a SmartLabel metric saves on a fuzzy term", problem, None)
        bodies.clear()
        got = m.measure_metric(rec)
        check("...resolving to the label's own field", bodies[0]["query"][0][0]["searchFieldName"],
              "Crown_Jewels")
        check("...with the label's declared DSL type", bodies[0]["query"][0][0]["type"], "Binary")
        check("...and its table, not the stored one", bodies[0]["table"], "asset")
        check("...measured ok", got["ok"], True)
        # The operator depends on the label's type, and the wrong one is silently, plausibly wrong.
        # Measured live: `== null` on a String label returned 32,597 of 34,270 records (it matches where
        # the label is ABSENT); `exists` returned the 1,673 it applies to.
        m._LABELS = m._LABELS + [{"name": "All Vulns", "field": "All_Vulns_SmartLabel", "table": "asset",
                                  "type": "String", "purpose": "", "queryable": True}]
        m._FIELD_MAP["asset"]["All_Vulns_SmartLabel"] = "String"
        rec, problem = add(name="all-vulns", smart_label="All Vulns")
        check("a non-Binary SmartLabel saves", problem, None)
        bodies.clear()
        m.measure_metric(rec)
        check("...and is counted with `exists`, never `== null`",
              bodies[0]["query"][0][0]["operator"], "exists")
        check("...which is what a Binary label does NOT use",
              m.metric_query({"name": "x", "smartLabel": "Crown Jewels", "table": "asset"})[0]
              ["query"][0][0]["operator"], "==")
        m.save_metrics([x for x in m.load_metrics() if x["name"] != "all-vulns"])
        rec, problem = add(name="ghost", smart_label="No Such Label")
        check("a SmartLabel that doesn't exist is refused at add time", rec, None)
        check("...saying it may have been renamed", "renamed or removed" in problem, True)

        # --- re-validation on READ: a vanished field is unavailable, never 0 ----------------------
        kev = next(x for x in m.load_metrics() if x["name"] == "kev-exposed")
        m._FIELD_MAP = {"asset": {k: v for k, v in ASSET_FIELDS.items() if k != "Has_KEV"},
                        "user": dict(USER_FIELDS)}
        bodies.clear()
        got = m.measure_metric(kev)
        check("a metric whose field vanished is not resolved", got["ok"], False)
        check("...is NEVER counted as zero", got["count"], None)
        check("...costs no API call", len(bodies), 0)
        check("...and says which field went missing", "'Has_KEV'" in got["error"], True)
        # An API failure is the same disposition: unavailable, not zero.
        m._FIELD_MAP = {"asset": dict(ASSET_FIELDS), "user": dict(USER_FIELDS)}
        m.call = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("HTTP 403: Forbidden"))
        got = m.measure_metric(kev)
        check("a metric whose query is forbidden is unavailable", (got["ok"], got["count"]), (False, None))
        check("...with the status kept", "403" in got["error"], True)
        m.call = fake_call

        # --- `metrics list` names what no longer resolves ----------------------------------------
        m._FIELD_MAP = {"asset": {k: v for k, v in ASSET_FIELDS.items() if k != "Has_KEV"},
                        "user": dict(USER_FIELDS)}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            m.cmd_metrics(argparse.Namespace(metrics_cmd="list"))
        lst = json.loads(buf.getvalue())
        check("list reports the tracked count against the cap", (lst["tracked"], lst["cap"]), (2, 20))
        check("...and names what stopped resolving", lst["notResolving"], ["kev-exposed"])
        check("...promising unavailable rather than 0", "rather than as 0" in lst["note"], True)
        check("...while a still-good metric is marked resolving",
              next(r for r in lst["metrics"] if r["name"] == "crown-jewels")["resolves"], True)
        m._FIELD_MAP = {"asset": dict(ASSET_FIELDS), "user": dict(USER_FIELDS)}

        # --- the 20-metric cap refuses; it never evicts ------------------------------------------
        for i in range(18):
            _, problem = add(name="filler%d" % i, where=["Risk_Score >= Float %d" % (i + 1)])
            check("filler %d saved" % i, problem, None) if problem else None
        check("at the cap, exactly 20 are tracked", len(m.load_metrics()), 20)
        rec, problem = add(name="twenty-first", where=["Risk_Score >= Float 999"])
        check("the 21st metric is refused", rec, None)
        check("...saying the cap is reached", "cap is reached" in problem, True)
        check("...promising nothing was evicted", "Nothing was evicted" in problem, True)
        check("...listing what is tracked so a human can choose", "kev-exposed" in problem, True)
        check("...and the 20 on disk are untouched", len(m.load_metrics()), 20)
        # Replacing an existing name is not blocked by the cap -- it is not a 21st metric.
        rec, problem = add(name="kev-exposed", label="Renamed", where=["Has_KEV == Binary true"])
        check("redefining an existing metric works at the cap", problem, None)
        check("...without growing the list", len(m.load_metrics()), 20)
        check("...and takes the new label",
              next(x for x in m.load_metrics() if x["name"] == "kev-exposed")["label"], "Renamed")

        # --- rm keeps captured history ------------------------------------------------------------
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            m.cmd_metrics(argparse.Namespace(metrics_cmd="rm", name="filler0"))
        rm = json.loads(buf.getvalue())
        check("rm removes one", (rm["removed"], rm["tracked"]), ("filler0", 19))
        check("...and says history is kept", "History already captured" in rm["note"], True)

        # --- snapshots capture metrics at one call each -------------------------------------------
        for x in list(m.load_metrics()):
            if x["name"] not in ("kev-exposed", "crown-jewels"):
                m.save_metrics([y for y in m.load_metrics() if y["name"] != x["name"]])
        check("trimmed back to two metrics", len(m.load_metrics()), 2)
        real_build = m.build_digest
        m.build_digest = lambda *a, **k: _snap_digest()
        try:
            bodies.clear()
            with contextlib.redirect_stderr(io.StringIO()):
                info = m.take_snapshot()
            check("a snapshot captures both metrics", info["metricsCaptured"], 2)
            check("...at one API call each", len(bodies), 2)
            recs, _ = m.load_snapshots()
            check("...writing them into the record", len(recs[-1]["metrics"]), 2)
            check("...with counts", recs[-1]["metrics"][0]["count"], 458)
            # A broken metric is named in the snapshot report AND the record, never silently 0.
            m._FIELD_MAP = {"asset": {k: v for k, v in ASSET_FIELDS.items() if k != "Has_KEV"},
                            "user": dict(USER_FIELDS)}
            with contextlib.redirect_stderr(io.StringIO()):
                info = m.take_snapshot()
            check("a broken metric is reported by the snapshot",
                  [u["metric"] for u in info["metricsUnresolved"]], ["kev-exposed"])
            check("...and only the good one counts as captured", info["metricsCaptured"], 1)
            recs, _ = m.load_snapshots()
            bad = next(x for x in recs[-1]["metrics"] if x["name"] == "kev-exposed")
            check("...recorded ok: false in history, not count: 0", (bad["ok"], bad["count"]),
                  (False, None))
            check("...and named in sectionsUnavailable",
                  [s["section"] for s in recs[-1]["sectionsUnavailable"]], ["metrics"])
            m._FIELD_MAP = {"asset": dict(ASSET_FIELDS), "user": dict(USER_FIELDS)}
            # --no-metrics skips them entirely.
            bodies.clear()
            with contextlib.redirect_stderr(io.StringIO()):
                info = m.take_snapshot(with_metrics=False)
            check("--no-metrics captures none", (info["metricCalls"], len(bodies)), (0, 0))
            check("...and writes no metrics key", "metrics" in m.load_snapshots()[0][-1], False)
        finally:
            m.build_digest = real_build

        # --- the question-to-metric loop -----------------------------------------------------------
        # An untracked metric: notTracked, a baseline, a registration, and NEVER a zero delta.
        m.save_metrics([])
        out = {"notTracked": [{"metric": "kev-exposed", "reason": "not captured in either snapshot"}],
               "changes": [{"kind": "total", "name": "assets", "from": 1, "to": 2,
                            "change": 1, "percentChange": 100.0}]}
        m.resolve_not_tracked(out, "kev-exposed", label="KEV-exposed assets", table="asset",
                              where=["Has_KEV == Binary true"])
        nt = out["notTracked"][0]
        check("an untracked metric registers itself", nt["registered"]["name"], "kev-exposed")
        check("...reporting the implicit config write", "wrote to the user's config" in nt["registeredNote"], True)
        check("...and is now on disk", [x["name"] for x in m.load_metrics()], ["kev-exposed"])
        check("...answered with today's value as a baseline", nt["baseline"]["count"], 458)
        check("...labelled a baseline, not a trend", "not a trend" in nt["baselineNote"], True)
        check("...and no metric delta was invented",
              [r for r in out["changes"] if r["kind"] == "metric"], [])
        check("...nor a zero anywhere in the entry", ("0%" in json.dumps(nt)) or (nt.get("change") == 0), False)

        # A derived metric that fails validation is NOT written -- an unanswerable question must not
        # leave a permanently broken metric behind.
        m.save_metrics([])
        out = {"notTracked": [{"metric": "typo-metric", "reason": "not captured"}]}
        m.resolve_not_tracked(out, "typo-metric", where=["Hs_KEV == Binary true"])
        nt = out["notTracked"][0]
        check("an invalid derived metric is refused", nt["registered"], False)
        check("...naming the closest field", "'Has_KEV'" in nt["registrationRefused"], True)
        check("...and leaves nothing broken on disk", m.load_metrics(), [])
        check("...and offers no baseline it cannot measure", "baseline" in nt, False)

        # A metric defined but not yet snapshotted: baseline available, no registration needed.
        add(name="already-defined", where=["Has_KEV == Binary true"])
        out = {"notTracked": [{"metric": "already-defined", "reason": "not captured"}]}
        m.resolve_not_tracked(out, "already-defined")
        check("a defined-but-unsnapshotted metric still gets a baseline",
              out["notTracked"][0]["baseline"]["count"], 458)
        check("...and is not re-registered", "registered" in out["notTracked"][0], False)

        # At the cap, auto-registration is REFUSED rather than evicting an existing metric.
        m.save_metrics([{"name": "m%d" % i, "label": "m%d" % i, "table": "asset",
                         "where": ["Risk_Score >= Float %d" % (i + 1)]} for i in range(20)])
        out = {"notTracked": [{"metric": "one-too-many", "reason": "not captured"}]}
        m.resolve_not_tracked(out, "one-too-many", where=["Has_KEV == Binary true"])
        nt = out["notTracked"][0]
        check("at the cap, auto-registration is refused", nt["registered"], False)
        check("...saying the cap is reached", "cap is reached" in nt["registrationRefused"], True)
        check("...rather than evicting one", len(m.load_metrics()), 20)
        check("...and listing the tracked names so a human can pick",
              "m0" in nt["registrationRefused"], True)

        # The loop must fire even when history is ALSO insufficient -- which is the common case, not an
        # edge one. Someone asks for a trend early, when there is little history and the metric was never
        # defined; answering only "come back later" would leave the list just as empty next month.
        m.save_metrics([])
        t = m.compute_trend([_snap("2026-08-05")], metric="kev-exposed")
        check("insufficient history still reports the requested metric as untracked",
              t["notTracked"][0]["metric"], "kev-exposed")
        m.resolve_not_tracked(t, "kev-exposed", where=["Has_KEV == Binary true"])
        check("...so it still registers", t["notTracked"][0]["registered"]["name"], "kev-exposed")
        check("...and still returns a baseline", t["notTracked"][0]["baseline"]["count"], 458)
        check("...while insufficientHistory still stands", t["insufficientHistory"], True)
        check("...with no changes fabricated", t["changes"], [])
        # A metric that IS in the history is not re-reported as untracked on that path.
        t = m.compute_trend([_snap("2026-08-05", metrics=[{"name": "kev-exposed", "count": 5, "ok": True}])],
                            metric="kev-exposed")
        check("a captured metric is not called untracked just because history is short",
              "notTracked" in t, False)

        # A metric that resolved-and-failed is a different report; the loop must not overwrite it.
        out = {"notTracked": [{"metric": "kev-exposed", "unavailableAt": ["to"],
                               "reason": "did not resolve"}]}
        m.resolve_not_tracked(out, "kev-exposed", where=["Has_KEV == Binary true"])
        check("an unavailable metric is not treated as untracked",
              "baseline" in out["notTracked"][0], False)

        # --- end to end through the CLI -----------------------------------------------------------
        m.save_metrics([])
        real_snaps = m.load_snapshots
        m.load_snapshots = lambda *a, **k: ([_snap("2026-07-01", assets=33000), _snap("2026-08-05")], [])
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                m.cmd_trend(argparse.Namespace(
                    since=None, metric="kev-exposed", table=None, by=None, format=None, out=None,
                    derive_where=["Has_KEV == Binary true"], derive_smart_label=None,
                    derive_label="KEV-exposed assets", derive_table="asset"))
            d = json.loads(buf.getvalue())
            nt = d["notTracked"][0]
            check("trend --metric with a definition registers it", nt["registered"]["name"], "kev-exposed")
            check("...and answers with a baseline", nt["baseline"]["count"], 458)
            check("...while still computing the trends it CAN", len(d["changes"]) > 0, True)
            check("...and emitting no metric row", [r for r in d["changes"] if r["kind"] == "metric"], [])
            check("...with the never-render-as-zero rule in the envelope",
                  "unanswerable" in d["notTrackedNote"], True)
        finally:
            m.load_snapshots = real_snaps
    finally:
        m.CFG_DIR, m.load_config, m.call, m._FIELD_MAP, m._LABELS = real


CUSTOMER_ASSETS = [{"Asset_Name": "PROD-DB-04", "Risk_Score": 4120},
                   {"Asset_Name": "dana-whitfield-mbp", "Risk_Score": 900},
                   {"Asset_Name": "vpn-gw-lon-02", "Risk_Score": 700}]


def test_entities(m):
    """Option C, scoped entity deltas (design/trends.md phase 4). Still dormant until phase 5.

    Two things carry this phase. Scope is bounded, because the full inventory is ~343 pages and ~13
    minutes -- about 6x the rate budget. And identity is a salted hash, because a delta only needs
    "same entity across time" and there is no reason to put customer names on disk to get it."""
    print("[17] scoped entity deltas (offline)")
    import contextlib, io
    tmp = tempfile.mkdtemp()
    real = (m.CFG_DIR, m.load_config, m.top_n, m._LABELS, m._FIELD_MAP, m.build_digest)
    m.CFG_DIR = tmp
    m.load_config = lambda: ("s.example", "tok", None)
    m._FIELD_MAP = {"asset": dict(ASSET_FIELDS), "user": dict(USER_FIELDS)}
    m._LABELS = [{"name": "Crown Jewels", "field": "Crown_Jewels", "table": "asset",
                  "type": "Binary", "purpose": "", "queryable": True},
                 {"name": "All Vulns", "field": "All_Vulns_SmartLabel", "table": "asset",
                  "type": "String", "purpose": "", "queryable": True}]
    m._FIELD_MAP["asset"]["All_Vulns_SmartLabel"] = "String"
    m.build_digest = lambda *a, **k: _snap_digest()
    calls = []

    def fake_top(table, field, n, where=None, select=None):
        calls.append({"table": table, "field": field, "n": n, "where": where, "select": select})
        cols = (select or "").replace(",", " ").split()
        return {"table": table, "field": field, "matchedAtThreshold": 100, "totalInTail": 3,
                "top": [{c: r.get(c) for c in cols} for r in CUSTOMER_ASSETS]}
    m.top_n = fake_top
    SALT_A, SALT_B = "a" * 64, "b" * 64

    try:
        # --- scope is mandatory and bounded ------------------------------------------------------
        spec, problem = m.parse_entity_scope("top500:asset:Risk_Score")
        check("the default scope parses", (spec["kind"], spec["n"], spec["table"]), ("top", 500, "asset"))
        check("...and carries its measured cost", (spec["pages"], spec["estimatedSeconds"]), (5, 11.5))
        spec, problem = m.parse_entity_scope("all")
        check("there is no unbounded scope", spec, None)
        check("...and the refusal states the cost of one", "13 minutes" in problem, True)
        spec, problem = m.parse_entity_scope("top2000:asset:Risk_Score")
        check("a scope above the default is refused", spec, None)
        check("...stating its cost against the default", "20 pages" in problem, True)
        check("...and naming the flag that accepts it", "--allow-large-scope" in problem, True)
        spec, problem = m.parse_entity_scope("top2000:asset:Risk_Score", allow_large=True)
        check("...and allowed behind the flag", spec["n"], 2000)
        spec, problem = m.parse_entity_scope("top9000:asset:Risk_Score", allow_large=True)
        check("above the 5000 ceiling it is refused even with the flag", spec, None)
        check("...naming the ceiling", "5000 ceiling" in problem, True)
        spec, problem = m.parse_entity_scope("label:Crown Jewels:Risk_Score")
        check("a SmartLabel scope resolves through find_label", spec["labelField"], "Crown_Jewels")
        check("...taking the label's own table", spec["table"], "asset")
        check("...bounded by the default, so a small label isn't refused for costing nothing",
              spec["n"], 500)
        check("...and raised to the existing ceiling only behind the flag",
              m.parse_entity_scope("label:Crown Jewels:Risk_Score", allow_large=True)[0]["n"], 5000)
        spec, problem = m.parse_entity_scope("label:No Such Thing:Risk_Score")
        check("an unknown label scope is refused", spec, None)
        check("...pointing at labels --search", "labels --search" in problem, True)

        # --- identity: stable per salt, different across salts, and no names on disk --------------
        spec, _ = m.parse_entity_scope("top500:asset:Risk_Score")
        e1 = m.capture_entities(spec, SALT_A)
        e2 = m.capture_entities(spec, SALT_A)
        check("ids are stable across snapshots for one salt", e1["scores"], e2["scores"])
        check("...and are 16-char hashes, not names",
              all(len(k) == 16 and k.isalnum() for k in e1["scores"]), True)
        e3 = m.capture_entities(spec, SALT_B)
        check("...and differ under another salt", set(e1["scores"]) & set(e3["scores"]), set())
        check("the saltId identifies which salt produced them", e1["saltId"] != e3["saltId"], True)
        check("...and is not the salt itself", SALT_A[:8] in json.dumps(e1), False)
        check("the cutoff score is recorded, since a top-N scope is a window", e1["cutoffScore"], 700.0)
        check("only the identity field and the score are fetched", calls[-1]["select"],
              "Asset_Name,Risk_Score")

        # A SmartLabel scope uses `exists` for a String label, `== true` for Binary -- the same rule a
        # metric follows, because `== null` matches where the label is ABSENT.
        spec, _ = m.parse_entity_scope("label:All Vulns:Risk_Score")
        m.capture_entities(spec, SALT_A)
        check("a String-label scope filters with exists", calls[-1]["where"],
              ["All_Vulns_SmartLabel exists String"])
        spec, _ = m.parse_entity_scope("label:Crown Jewels:Risk_Score")
        m.capture_entities(spec, SALT_A)
        check("...and a Binary-label scope with == true", calls[-1]["where"],
              ["Crown_Jewels == Binary true"])

        # --- THE grep: no customer identifier reaches disk without --with-names ------------------
        spec, _ = m.parse_entity_scope("top500:asset:Risk_Score")
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(io.StringIO()):
                m.take_snapshot(entities="top500:asset:Risk_Score", salt=SALT_A)
        blob = open(m._snapshots_path(), encoding="utf-8-sig").read()
        for leak in ("PROD-DB-04", "dana-whitfield-mbp", "vpn-gw-lon-02", "Asset_Name", "Owner_Name"):
            check("no %r in the written history" % leak, leak in blob, False)
        check("...and the salt itself is never written", SALT_A in blob, False)
        check("...while the saltId is", m.salt_id(SALT_A) in blob, True)
        recs, _ = m.load_snapshots()
        check("the scope is recorded", recs[-1]["entities"]["scope"], "top500:asset:Risk_Score")
        check("...with three hashed entities", recs[-1]["entities"]["count"], 3)
        check("...marked as not storing names", recs[-1]["entities"]["namesStored"], False)

        # --with-names is an explicit opt-in, warns once, and says so in the record.
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(err):
                info = m.take_snapshot(entities="top500:asset:Risk_Score", with_names=True, salt=SALT_A)
        check("--with-names warns about customer data", "customer data" in err.getvalue(), True)
        check("...naming the permissions.deny protection", "permissions.deny" in err.getvalue(), True)
        check("...and reports that it warned", info["entitiesCaptured"]["privacyWarningShown"], True)
        check("...storing names as asked", info["entitiesCaptured"]["namesStored"], True)
        blob = open(m._snapshots_path(), encoding="utf-8-sig").read()
        check("...so identifiers ARE on disk now", "PROD-DB-04" in blob, True)
        recs, _ = m.load_snapshots()
        check("...with the record saying what it holds", "permissions.deny" in recs[-1]["entities"]["privacyNote"], True)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(err):
                info = m.take_snapshot(entities="top500:asset:Risk_Score", with_names=True, salt=SALT_A)
        check("the privacy warning is made once, not every run", "customer data" in err.getvalue(), False)
        check("...and not re-reported", "privacyWarningShown" in info["entitiesCaptured"], False)

        # A refused scope is named, not silently skipped.
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(io.StringIO()):
                info = m.take_snapshot(entities="top9000:asset:Risk_Score", salt=SALT_A)
        check("a refused scope is reported", "5000 ceiling" in info["entitiesUnavailable"], True)
        check("...and no entities key is written", "entities" in m.load_snapshots()[0][-1], False)

        # --- the diff ----------------------------------------------------------------------------
        def ents(scores, scope="top500:asset:Risk_Score", salt=SALT_A, names=False, cutoff=None):
            return {"scope": scope, "table": "asset", "field": "Risk_Score", "count": len(scores),
                    "saltId": m.salt_id(salt), "namesStored": names, "requested": 500,
                    "matchedAtThreshold": 100, "cutoffScore": cutoff if cutoff is not None
                    else (min(scores.values()) if scores else None), "scores": scores}

        a = _snap("2026-07-01"); a["entities"] = ents({"aa": 100.0, "bb": 200.0, "cc": 300.0})
        b = _snap("2026-08-05"); b["entities"] = ents({"bb": 250.0, "cc": 150.0, "dd": 400.0})
        t = m.compute_trend([a, b])
        d = t["entities"]
        check("a comparable scope is diffed", d["comparable"], True)
        check("appeared", d["appeared"], ["dd"])
        check("disappeared", d["disappeared"], ["aa"])
        check("worsened, by how much", [(x["id"], x["change"]) for x in d["worsened"]], [("bb", 50.0)])
        check("improved", [(x["id"], x["change"]) for x in d["improved"]], [("cc", -150.0)])
        check("counts are exact", (d["counts"]["appeared"], d["counts"]["worsened"]), (1, 1))
        check("the score direction is stated, not assumed", "higher" in d["scoreDirection"], True)
        check("...and hashes are flagged as nameable live", "--name-entities" in d["namingNote"], True)
        check("a moving cutoff is called out", d["cutoffMoved"], True)
        check("...explaining disappeared != left the inventory",
              "not the same as leaving the inventory" in d["note"], True)

        # A mismatched saltId REFUSES rather than reporting 100% churn.
        b2 = _snap("2026-08-05"); b2["entities"] = ents({"zz": 100.0}, salt=SALT_B)
        d = m.compute_trend([a, b2])["entities"]
        check("a mismatched salt refuses to diff", (d["comparable"], d["saltMismatch"]), (False, True))
        check("...naming both salt ids", m.salt_id(SALT_B) in d["reason"], True)
        check("...and saying why it would be wrong", "did not happen" in d["reason"], True)
        check("...producing no appeared/disappeared at all", "appeared" in d, False)

        # A different scope, or a different identity kind, is not comparable either.
        b3 = _snap("2026-08-05"); b3["entities"] = ents({"bb": 250.0}, scope="top1000:asset:Risk_Score")
        d = m.compute_trend([a, b3])["entities"]
        check("a different scope is not compared", d["comparable"], False)
        check("...saying the scopes share no cutoff", "share no cutoff" in d["reason"], True)
        b4 = _snap("2026-08-05"); b4["entities"] = ents({"PROD-DB-04": 1.0}, names=True)
        d = m.compute_trend([a, b4])["entities"]
        check("names against hashes is not compared", d["comparable"], False)
        check("...saying they are not the same kind of thing", "same kind of thing" in d["reason"], True)

        # Captured at one end only.
        c = _snap("2026-08-05")
        d = m.compute_trend([a, c])["entities"]
        check("a scope at one end only is not compared", d["comparable"], False)
        check("...naming which end", "the later snapshot" in d["reason"], True)
        check("...and saying it cannot be backfilled", "no way to backfill" in d["reason"], True)

        # Lists are sampled; counts stay exact, and the truncation is stated.
        big_a = _snap("2026-07-01"); big_a["entities"] = ents({"x%d" % i: 1.0 for i in range(60)})
        big_b = _snap("2026-08-05"); big_b["entities"] = ents({"y%d" % i: 1.0 for i in range(60)})
        d = m.compute_trend([big_a, big_b])["entities"]
        check("counts are exact past the sample cap", d["counts"]["appeared"], 60)
        check("...the list is capped", len(d["appeared"]), 25)
        check("...and the truncation is stated", d["listsTruncated"]["appeared"], 60)

        # --- naming the movers happens live, and is never persisted ------------------------------
        a5 = _snap("2026-07-01")
        a5["entities"] = ents({m.hash_entity(SALT_A, "PROD-DB-04"): 100.0,
                               m.hash_entity(SALT_A, "gone-forever"): 50.0})
        b5 = _snap("2026-08-05")
        b5["entities"] = ents({m.hash_entity(SALT_A, "PROD-DB-04"): 400.0,
                               m.hash_entity(SALT_A, "vpn-gw-lon-02"): 60.0})
        t = m.compute_trend([a5, b5])
        real_salt = m.entity_salt
        m.entity_salt = lambda: SALT_A
        try:
            m.name_entities(t["entities"])
        finally:
            m.entity_salt = real_salt
        named = t["entities"]["names"]
        check("a still-present mover is named from the live query",
              named["worsened"][m.hash_entity(SALT_A, "PROD-DB-04")], "PROD-DB-04")
        check("...and the count that could not be named is stated",
              "could not be named" in t["entities"]["namesNote"], True)
        check("...saying the names were not stored", "not stored" in t["entities"]["namesNote"], True)
        blob = open(m._snapshots_path(), encoding="utf-8-sig").read()
        check("naming wrote nothing to the history file", "PROD-DB-04" in blob.split("privacyNote")[0], False)

        # --- the salt survives a stacks switch ---------------------------------------------------
        reg = {"active": "one", "stacks": {
            "one": {"fqdn": "one.example", "api_token": "t1", "entity_salt": SALT_A},
            "two": {"fqdn": "two.example", "api_token": "t2", "entity_salt": SALT_B}}}
        real_cfg_path = m.CFG_PATH
        m.CFG_PATH = os.path.join(tmp, "cfg.json")
        try:
            m.mirror_active_to_config(reg)
            check("switching a stack carries its salt into config.json",
                  json.load(open(m.CFG_PATH, encoding="utf-8-sig"))["entity_salt"], SALT_A)
            reg["active"] = "two"
            m.mirror_active_to_config(reg)
            check("...and each stack keeps its own",
                  json.load(open(m.CFG_PATH, encoding="utf-8-sig"))["entity_salt"], SALT_B)
        finally:
            m.CFG_PATH = real_cfg_path
    finally:
        (m.CFG_DIR, m.load_config, m.top_n, m._LABELS, m._FIELD_MAP, m.build_digest) = real


SKILL_MD = os.path.join(os.path.dirname(HERE), "SKILL.md")
# Agent Skills cap `description` at 1024 characters. Over it, the skill does not merely route worse --
# it FAILS TO IMPORT, so the whole skill is silently absent from the user's Claude. Nothing in the repo
# caught that: it is frontmatter, not code, and a merge to main is a live deploy. This grew past the cap
# one capability blurb at a time and shipped broken.
SKILL_DESCRIPTION_MAX = 1024
SKILL_NAME_MAX = 64


def _skill_frontmatter():
    s = open(SKILL_MD, encoding="utf-8").read()
    assert s.startswith("---"), "SKILL.md must open with YAML frontmatter"
    fm = s.split("---")[1]
    out = {}
    mm = re.search(r"^description: >-\n((?:  .*\n)+)", fm, re.M)
    out["description"] = " ".join(l.strip() for l in mm.group(1).strip().splitlines()) if mm else ""
    nn = re.search(r"^name:\s*(.+)$", fm, re.M)
    out["name"] = nn.group(1).strip() if nn else ""
    return out


def test_alerts(m):
    """Alert rules (MVP, dormant). The three-verdict model is the entire point of this feature.

    A two-state alerting layer reports a rule it could not test as "clear", and alerting is the layer
    people stop watching precisely because it is supposed to watch for them. So the first assertion here
    is the one that matters: a run where nothing could be evaluated must NOT exit 0."""
    print("[22] alert rules (offline)")
    tmp = tempfile.mkdtemp()
    real = (m.CFG_DIR, m.load_config, m.load_metrics)
    m.CFG_DIR = tmp
    m.load_config = lambda: ("s.example", "tok", None)
    # Rules validate against the tracked metric list, so stub it rather than the whole metrics file.
    m.load_metrics = lambda: [{"name": "kev-exposed", "table": "asset"},
                              {"name": "kev-critical", "table": "asset"}]
    try:
        def mt(name, count=None, ok=True, error=None, label=None):
            r = {"name": name, "ok": ok, "label": label or name}
            if count is not None:
                r["count"] = count
            if error:
                r["error"] = error
            return r

        RULE_COV = {"name": "conn", "condition": "coverage-regressed"}
        RULE_KEV = {"name": "kev", "condition": "above", "metric": "kev-exposed", "value": 1500}

        # --- THE load-bearing test -------------------------------------------------------------------
        # One snapshot: no comparison is possible, so a coverage rule cannot be answered. If this ever
        # returns exit 0, the feature is actively lying to a scheduler and everything else here is moot.
        one = m.compute_trend([_snap("2026-08-05", metrics=[mt("kev-exposed", 1201)])])
        v = m.evaluate_alerts(one, _snap("2026-08-05", metrics=[mt("kev-exposed", 1201)]), [RULE_COV])
        check("a single snapshot cannot answer a coverage rule", len(v["unevaluable"]), 1)
        check("...and it is NOT reported as clear", len(v["clear"]), 0)
        check("...and the run does not exit 0", v["exitCode"] != 0, True)
        check("...it exits with the unevaluable bit", v["exitCode"], m.ALERT_EXIT_UNEVALUABLE)
        check("...saying the rule did not pass",
              "did NOT pass" in v["unevaluable"][0]["note"], True)

        # An empty rule set is the whole-run version of the same failure: nothing was checked, which is
        # not an all-clear. It must not exit 0 and must not render a green headline.
        v = m.evaluate_alerts(one, _snap("2026-08-05"), [])
        check("no rules configured is not an all-clear", v.get("nothingChecked"), True)
        check("...and does not exit 0", v["exitCode"] != 0, True)
        check("...and the headline does not say all clear",
              "all clear" in m.render_alerts(v).lower(), False)
        check("...it says nothing was checked", "nothing checked" in m.render_alerts(v).lower(), True)

        # --- coverage-regressed ----------------------------------------------------------------------
        a = _snap("2026-08-05", failing=("intune",), metrics=[mt("kev-exposed", 1201)])
        b = _snap("2026-08-12", failing=("intune", "checkpoint"), metrics=[mt("kev-exposed", 1198)])
        t = m.compute_trend([a, b])
        v = m.evaluate_alerts(t, b, [RULE_COV])
        check("a connector entering the failing set fires", len(v["firing"]), 1)
        check("...naming it", "checkpoint" in v["firing"][0]["failingAdded"], True)
        check("...and exits with the firing bit", v["exitCode"], m.ALERT_EXIT_FIRING)
        check("...and says what it means for the answers",
              "stale" in v["firing"][0]["message"], True)
        # A connector RESOLVING is not a regression.
        t2 = m.compute_trend([b, _snap("2026-08-19", failing=("intune",),
                                       metrics=[mt("kev-exposed", 1150)])])
        v = m.evaluate_alerts(t2, _snap("2026-08-19", metrics=[mt("kev-exposed", 1150)]), [RULE_COV])
        check("a connector recovering does not fire", len(v["firing"]), 0)
        check("...and is genuinely clear, not unevaluable", len(v["clear"]), 1)
        # degraded is opt-in, because connector churn is normal on a real stack.
        c = _snap("2026-08-12", failing=("intune",), degraded=("tenable", "okta"),
                  metrics=[mt("kev-exposed", 1198)])
        t3 = m.compute_trend([a, c])
        check("a new DEGRADED connector does not fire by default",
              len(m.evaluate_alerts(t3, c, [RULE_COV])["firing"]), 0)
        check("...but does with includeDegraded",
              len(m.evaluate_alerts(t3, c, [dict(RULE_COV, includeDegraded=True)])["firing"]), 1)
        # --- the degraded picture rides on EVERY coverage-regressed row ------------------------------
        # Found by replaying this verb day-by-day over real snapshot history: 55 connectors entered the
        # degraded set in one window and the row said "no connector entered the failing set ... and none
        # are failing now". The verdict was right; the row read as an all-clear while a third of the
        # fleet had just degraded. Informational only -- these assertions check the verdict does NOT move.
        many = tuple("conn%02d" % i for i in range(8))
        d0 = _snap("2026-08-05", failing=("intune",), degraded=(), metrics=[mt("kev-exposed", 1201)])
        d1 = _snap("2026-08-06", failing=("intune",), degraded=many, metrics=[mt("kev-exposed", 1198)])
        td = m.compute_trend([d0, d1])
        v = m.evaluate_alerts(td, d1, [RULE_COV])
        check("a fleet-wide degrade does not change the verdict", len(v["clear"]), 1)
        check("...and still does not fire", len(v["firing"]), 0)
        row = v["clear"][0]
        check("...but the row names what entered the degraded set",
              row.get("degradedEntered"), sorted(many))
        check("...and carries the standing degraded count", row.get("stillDegradedCount"), len(many))
        check("...and says so in the message", "DEGRADED" in row["message"], True)
        check("...saying it did not affect the verdict",
              "did not affect the verdict" in row["message"], True)
        # The remainder is DISCLOSED, never silently dropped -- a shortened list that does not say it is
        # shortened misstates the size of the event.
        check("...capping the names with the remainder stated", "and 3 more" in row["message"], True)

        # A rule that opted in already carries `degradedAdded` as the evidence that FIRED it; the same
        # names must not appear twice under two keys meaning two different things.
        v = m.evaluate_alerts(td, d1, [dict(RULE_COV, includeDegraded=True)])
        check("includeDegraded fires on the same movement", len(v["firing"]), 1)
        check("...without duplicating the names", v["firing"][0].get("degradedEntered"), None)
        check("...and the firing row carries the standing count too",
              v["firing"][0].get("stillDegradedCount"), len(many))

        # Connectors LEAVING the degraded set is reported as well -- the mirror window of a flap.
        td2 = m.compute_trend([d1, _snap("2026-08-07", failing=("intune",), degraded=(),
                                         metrics=[mt("kev-exposed", 1195)])])
        row = m.evaluate_alerts(td2, _snap("2026-08-07", failing=("intune",), degraded=()),
                                [RULE_COV])["clear"][0]
        check("connectors leaving the degraded set are reported",
              row.get("degradedLeft"), sorted(many))

        # ...and an UNRECORDED degraded set is unknown, never zero. A snapshot that carries no degraded
        # figures is not saying none are degraded, and "0 degraded in total" would be the reassuring
        # wrong answer this whole verb exists to refuse.
        blind = _snap("2026-08-06", failing=("intune",), degraded=())
        blind["coverage"].pop("degraded")
        blind["coverage"].pop("degradedNames")
        row = m.evaluate_alerts(m.compute_trend([d0, blind]), blind, [RULE_COV])["clear"][0]
        check("an unrecorded degraded set is flagged unknown", row.get("stillDegradedUnknown"), True)
        check("...and is never reported as zero", row.get("stillDegradedCount"), None)
        check("...and the message refuses to claim none are degraded",
              "not a statement that none are degraded" in row["message"], True)

        # Unreadable coverage blocks the trend entirely, so the rule is unevaluable -- not clear.
        t4 = m.compute_trend([_snap("2026-08-05", coverage=False), b])
        v = m.evaluate_alerts(t4, b, [RULE_COV])
        check("unreadable coverage makes the rule unevaluable", len(v["unevaluable"]), 1)
        check("...never clear", len(v["clear"]), 0)

        # --- the window: day-over-day by default -----------------------------------------------------
        # The trend layer compares EARLIEST to latest. As an alert window that means a baseline receding
        # a day further into the past on every run, so a one-off regression fires forever and the rule
        # stops describing now.
        ds = ["2026-08-05", "2026-08-06", "2026-08-12", "2026-08-13"]
        # `since` is the SECOND-to-last date, not the last: compute_trend selects dates at or after it, so
        # the window has to open on the earlier of the two endpoints to contain both.
        check("daily window opens on the second-to-last date", m.alert_window_since(ds), "2026-08-12")
        check("full window compares from the beginning", m.alert_window_since(ds, "full"), None)
        check("an explicit --since overrides the default",
              m.alert_window_since(ds, "daily", "2026-08-06"), "2026-08-06")
        check("...and overrides full too", m.alert_window_since(ds, "full", "2026-08-06"), "2026-08-06")
        check("one date cannot form a daily window", m.alert_window_since(["2026-08-05"]), None)
        check("no dates at all is not an error", m.alert_window_since([]), None)

        # The window's real span is stated, because "the last two snapshots" is day-over-day only if a
        # snapshot was taken both days. A weekend asleep makes the same two records three days apart, and
        # a 3-day movement read as a 1-day one is quiet wrongness.
        far = _snap("2026-08-12", failing=("intune",), metrics=[mt("kev-exposed", 1198)])
        v = m.evaluate_alerts(m.compute_trend([_snap("2026-08-05", failing=("intune",),
                                                     metrics=[mt("kev-exposed", 1201)]), far]),
                              far, [RULE_KEV])
        check("a non-consecutive window reports its true span", v["window"]["days"], 7)
        check("...and flags that it is not consecutive", v["window"]["consecutive"], False)
        check("...saying no snapshot was taken between", "no snapshot" in v["window"]["note"], True)
        check("...and the rendered header names the gap",
              "7 days — no snapshot in between" in m.render_alerts(v), True)
        adj = _snap("2026-08-06", failing=("intune",), metrics=[mt("kev-exposed", 1198)])
        v = m.evaluate_alerts(m.compute_trend([_snap("2026-08-05", failing=("intune",),
                                                    metrics=[mt("kev-exposed", 1201)]), adj]),
                              adj, [RULE_KEV])
        check("a genuine day-over-day window says so", v["window"]["days"], 1)
        check("...and is not flagged as a gap", "consecutive" in v["window"], False)
        check("...rendering as day over day", "(day over day)" in m.render_alerts(v), True)
        # A multi-date window must NOT claim "no snapshot in between" -- there are intermediate ones.
        wide = m.compute_trend([_snap("2026-08-05", failing=("intune",), metrics=[mt("kev-exposed", 1201)]),
                                _snap("2026-08-08", failing=("intune",), metrics=[mt("kev-exposed", 1200)]),
                                far])
        v = m.evaluate_alerts(wide, far, [RULE_KEV])
        check("a multi-snapshot window does not claim nothing is in between",
              v["window"].get("consecutive"), None)
        check("...and renders the snapshot count instead",
              "3 snapshots" in m.render_alerts(v), True)

        # --- `clear` must never be readable as "healthy" ---------------------------------------------
        # Day-over-day makes this load-bearing: a connector failing for a fortnight produces NO CHANGE, so
        # the rule is legitimately clear. If clear were reported bare, "nothing failed since yesterday"
        # would read as "no connectors are failing" -- the same reassuring wrong answer, relocated from
        # the unevaluable case to the clear one.
        s1 = _snap("2026-08-12", failing=("intune", "checkpoint"), metrics=[mt("kev-exposed", 1198)])
        s2 = _snap("2026-08-13", failing=("intune", "checkpoint"), metrics=[mt("kev-exposed", 1197)])
        v = m.evaluate_alerts(m.compute_trend([s1, s2]), s2, [RULE_COV])
        check("a long-standing failure produces no change", len(v["firing"]), 0)
        check("...so the rule is clear", len(v["clear"]), 1)
        check("...but clear carries the standing failures",
              v["clear"][0]["stillFailing"], ["checkpoint", "intune"])
        check("...and says STILL failing in words",
              "STILL" in v["clear"][0]["message"], True)
        check("...naming them", "checkpoint" in v["clear"][0]["message"], True)
        check("...and explaining the rule watches for change",
              "watches for change" in v["clear"][0]["message"], True)
        check("...so the rendered row cannot be read as healthy",
              "STILL" in m.render_alerts(v), True)
        # Genuinely nothing failing -- the only case allowed to say so.
        h1 = _snap("2026-08-12", failing=(), metrics=[mt("kev-exposed", 1198)])
        h2 = _snap("2026-08-13", failing=(), metrics=[mt("kev-exposed", 1197)])
        v = m.evaluate_alerts(m.compute_trend([h1, h2]), h2, [RULE_COV])
        check("with nothing failing, clear may say so",
              "none are failing now" in v["clear"][0]["message"], True)
        check("...and lists no standing failures", v["clear"][0]["stillFailing"], [])
        # A firing verdict carries the standing total too: "1 newly failed" and "9 failing" are different
        # facts and the second is the one that sizes the problem.
        s3 = _snap("2026-08-13", failing=("intune", "checkpoint", "okta"),
                   metrics=[mt("kev-exposed", 1197)])
        v = m.evaluate_alerts(m.compute_trend([s1, s3]), s3, [RULE_COV])
        check("a firing verdict states the standing total too",
              "3 connectors are failing in total" in v["firing"][0]["message"], True)
        # If the standing count is unreadable, clear must not assert that none are failing.
        v = m.evaluate_alerts(m.compute_trend([h1, h2]), {"stackDate": "2026-08-13"}, [RULE_COV])
        # Check the AFFIRMATIVE phrasing, not the bare substring: "none are failing" also occurs inside
        # the disclaimer ("...not a statement that none are failing"), so matching it alone proves nothing.
        check("an unreadable standing count is not reported as none failing",
              "and none are failing now" in v["clear"][0]["message"], False)
        check("...saying so explicitly", "not a statement" in v["clear"][0]["message"], True)

        # --- above / below ---------------------------------------------------------------------------
        # These read the LATEST snapshot, so they answer from the first snapshot on -- history is only
        # needed for comparisons. Making a known present value unevaluable would be wrong.
        v = m.evaluate_alerts(one, _snap("2026-08-05", metrics=[mt("kev-exposed", 1600)]), [RULE_KEV])
        check("an absolute threshold works with only one snapshot", len(v["firing"]), 1)
        check("...reporting the observed value", v["firing"][0]["observed"], 1600)
        v = m.evaluate_alerts(t, b, [RULE_KEV])
        check("under the threshold is clear", len(v["clear"]), 1)
        check("...and a clear row still carries a message", bool(v["clear"][0].get("message")), True)
        check("below fires when under",
              len(m.evaluate_alerts(t, b, [{"name": "floor", "condition": "below",
                                            "metric": "kev-exposed", "value": 1500}])["firing"]), 1)

        # A metric that stopped resolving is UNKNOWN, never 0. A zero would clear an `above` rule and
        # fire a `below` one -- both confidently wrong, and the `above` case reads as good news.
        broke = _snap("2026-08-12", metrics=[mt("kev-exposed", ok=False, error="HTTP 403")])
        v = m.evaluate_alerts(m.compute_trend([a, broke]), broke, [RULE_KEV])
        check("a metric that stopped resolving is unevaluable", len(v["unevaluable"]), 1)
        check("...not treated as zero", len(v["clear"]) + len(v["firing"]), 0)
        check("...and the reason names it as unknown rather than zero",
              "unknown rather than zero" in v["unevaluable"][0]["reason"], True)
        v = m.evaluate_alerts(m.compute_trend([a, broke]), broke,
                              [{"name": "floor", "condition": "below",
                                "metric": "kev-exposed", "value": 1500}])
        check("...and a `below` rule does not fire on the same absence", len(v["firing"]), 0)

        # A metric never captured at all is unevaluable, for the same reason: no backfill exists.
        nom = _snap("2026-08-12", metrics=[mt("kev-exposed", 1198)])
        v = m.evaluate_alerts(m.compute_trend([a, nom]), nom,
                             [{"name": "crit", "condition": "above",
                               "metric": "kev-critical", "value": 25}])
        check("a metric absent from the snapshot is unevaluable", len(v["unevaluable"]), 1)
        check("...and says there is no backfill",
              "backfill" in v["unevaluable"][0]["reason"], True)

        # --- exit-code matrix ------------------------------------------------------------------------
        both = m.evaluate_alerts(m.compute_trend([a, b]), broke, [RULE_COV, RULE_KEV])
        check("firing + unevaluable ORs both bits", both["exitCode"],
              m.ALERT_EXIT_FIRING | m.ALERT_EXIT_UNEVALUABLE)
        check("...which is 12, distinct from die()'s 1 and 2", both["exitCode"], 12)
        allclear = m.evaluate_alerts(m.compute_trend([b, _snap("2026-08-19", failing=("intune",),
                                                              metrics=[mt("kev-exposed", 1100)])]),
                                    _snap("2026-08-19", metrics=[mt("kev-exposed", 1100)]),
                                    [RULE_COV, RULE_KEV])
        check("everything evaluated and nothing firing exits 0", allclear["exitCode"], 0)
        check("...and only then does the headline say all clear",
              "all clear" in m.render_alerts(allclear).lower(), True)

        # --- rule validation ------------------------------------------------------------------------
        check("a rule on an untracked metric is refused at add time",
              m.add_alert("x", "above", metric="nope", value=1)[1] is not None, True)
        check("...explaining it could never fire",
              "never fire" in (m.add_alert("x", "above", metric="nope", value=1)[1] or ""), True)
        check("above with no threshold is refused",
              m.add_alert("x", "above", metric="kev-exposed")[1] is not None, True)
        check("an unknown condition is refused",
              m.add_alert("x", "sideways", metric="kev-exposed", value=1)[1] is not None, True)
        rec, problem = m.add_alert("conn", "coverage-regressed")
        check("a valid rule saves", (problem, rec["condition"]), (None, "coverage-regressed"))
        check("...and round-trips through the file", [r["name"] for r in m.load_alerts()], ["conn"])
        # A rule stored against a metric that is later deleted must report, not silently never fire.
        m.save_alerts([{"name": "orphan", "condition": "above", "metric": "deleted-metric", "value": 5}])
        v = m.evaluate_alerts(m.compute_trend([a, b]), b, m.load_alerts())
        check("a rule orphaned by a deleted metric is unevaluable", len(v["unevaluable"]), 1)
        check("...never clear", len(v["clear"]), 0)

        # An unreadable rules file is NOT "no rules configured" -- the operator would go define rules
        # that already exist while the real fault went unmentioned.
        with open(m._alerts_path(), "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        raised = ""
        try:
            m.load_alerts()
        except RuntimeError as e:
            raised = str(e)
        check("a corrupt rules file raises rather than reporting zero rules", bool(raised), True)
        check("...saying no rule was evaluated", "No rule was evaluated" in raised, True)
        os.remove(m._alerts_path())
        check("...while a genuinely absent file is simply no rules", m.load_alerts(), [])

        # `alerts eval` (everything above this point) must never touch the state file -- only
        # `alerts notify` (below) reads and writes it, so evaluation semantics can't be changed by
        # whether delivery happens to be configured.
        check("the alert state file path is reserved",
              m._alertstate_path().endswith("alertstate.s.example.json"), True)
        check("...and `eval` alone does not write it", os.path.exists(m._alertstate_path()), False)
    finally:
        (m.CFG_DIR, m.load_config, m.load_metrics) = real


def test_alerts_notify(m):
    """Alert delivery (Slack/Teams/email), still dormant/CLI-only.

    The load-bearing property here is the same shape as the MVP's: on-change delivery must not become
    suppression. `render_alerts_*` always lists every current rule; `update_alertstate` only decides
    whether that message is worth sending. And the transport calls must stay structurally independent
    of `call()` -- no Authorization header, no Meridian token anywhere near a third-party webhook."""
    print("[23] alert delivery: Slack/Teams/email (offline)")
    tmp = tempfile.mkdtemp()
    real = (m.CFG_DIR, m.load_config)
    m.CFG_DIR = tmp
    m.load_config = lambda: ("s.example", "tok", None)

    def mkv(firing=(), clear=(), unevaluable=(), stack_date="2026-08-19"):
        return {"summary": {"rules": len(firing) + len(clear) + len(unevaluable),
                            "firing": len(firing), "clear": len(clear), "unevaluable": len(unevaluable)},
               "firing": [{"rule": n, "condition": "above", "message": "%s is high" % n} for n in firing],
               "clear": [{"rule": n, "condition": "above", "message": "%s is fine" % n} for n in clear],
               "unevaluable": [{"rule": n, "condition": "above", "reason": "no data"} for n in unevaluable],
               "exitCode": (4 if firing else 0) | (8 if unevaluable else 0),
               "stack": "s.example", "stackDate": stack_date}

    try:
        # --- change detection -----------------------------------------------------------------------
        v1 = mkv(firing=["kev"], clear=["conn"])
        state1, changes1 = m.update_alertstate(None, v1, "2026-08-17")
        check("a first-ever run treats every current rule as a change", len(changes1), 2)
        check("...recording where each came from", {c["from"] for c in changes1}, {None})
        check("...and where each landed",
              {c["rule"]: c["to"] for c in changes1}, {"kev": "firing", "conn": "clear"})
        check("...and the state records both rules' verdicts",
              {n: r["lastVerdict"] for n, r in state1["rules"].items()}, {"kev": "firing", "conn": "clear"})

        # Same verdicts again: no change.
        v2 = mkv(firing=["kev"], clear=["conn"])
        state2, changes2 = m.update_alertstate(state1, v2, "2026-08-18")
        check("an unchanged verdict is not a change", changes2, [])
        check("...but lastSeen still advances", state2["rules"]["kev"]["lastSeen"], "2026-08-18")
        check("...while firstSeen does not, since the verdict didn't change",
              state2["rules"]["kev"]["firstSeen"], "2026-08-17")

        # A rule resolving (firing -> clear) IS a change, and so is one going unevaluable.
        v3 = mkv(clear=["kev", "conn"])
        _, changes3 = m.update_alertstate(state2, v3, "2026-08-19")
        check("a rule resolving is a change", {c["rule"] for c in changes3}, {"kev"})
        check("...reporting the transition", changes3[0], {"rule": "kev", "from": "firing", "to": "clear"})
        v4 = mkv(unevaluable=["kev"], clear=["conn"])
        _, changes4 = m.update_alertstate(state2, v4, "2026-08-19")
        check("a rule going unevaluable is a change", {c["rule"] for c in changes4}, {"kev"})

        # A rule removed entirely (renamed or `alerts rm`) is also surfaced, not silently dropped.
        v5 = mkv(clear=["conn"])
        _, changes5 = m.update_alertstate(state2, v5, "2026-08-19")
        check("a removed rule is reported, not silently dropped",
              [c for c in changes5 if c["rule"] == "kev" and c["to"] is None], changes5)

        # --- state file round trip -------------------------------------------------------------------
        check("no state file yet", m.load_alertstate(), None)
        m.save_alertstate(state1)
        check("it round-trips", m.load_alertstate()["rules"]["kev"]["lastVerdict"], "firing")
        with open(m._alertstate_path(), "w", encoding="utf-8") as f:
            f.write("{ not json")
        raised = ""
        try:
            m.load_alertstate()
        except RuntimeError as e:
            raised = str(e)
        check("a corrupt state file raises rather than silently resetting", bool(raised), True)
        os.remove(m._alertstate_path())

        # --- renderers: escaping and shape -----------------------------------------------------------
        # Rule/message text can carry customer environment data (a connector name), so the HTML target
        # must escape it -- this file has already shipped the unescaped-customer-data bug once.
        nasty = mkv(firing=["<script>alert(1)</script> & Co"])
        subject, text, html = m.render_alerts_email(nasty)
        check("email HTML escapes a hostile rule name", "<script>" in html, False)
        check("...but the plain-text body is untouched (it isn't rendered as markup)",
              "<script>" in text, True)
        check("the subject names the stack", "s.example" in subject, True)
        slack_payload = m.render_alerts_slack(nasty)
        check("slack payload is a single text field", set(slack_payload), {"text"})
        # The Workflows template posts each entry of `attachments` as a card; a body without them is
        # accepted with HTTP 202 and posts nothing, which is what the old {title, text} shape did.
        teams_payload = m.render_alerts_teams(nasty)
        att = (teams_payload.get("attachments") or [{}])[0]
        card = att.get("content") or {}
        check("teams payload is a message with one Adaptive Card attachment",
              (teams_payload.get("type"), len(teams_payload.get("attachments") or []),
               att.get("contentType"), card.get("type")),
              ("message", 1, "application/vnd.microsoft.card.adaptive", "AdaptiveCard"))
        card_text = " ".join(b.get("text", "") for b in card.get("body") or [])
        check("...whose card carries the rule", "&amp; Co" in card_text or "& Co" in card_text, True)
        check("...with a top-level text fallback for a flow that reads that field",
              "& Co" in teams_payload.get("text", ""), True)
        linky = m.render_alerts_teams(mkv(firing=["[Reset your password](https://evil.example/x)"]))
        check("a markdown link in customer text does not form a link",
              "](" in json.dumps(linky["attachments"][0]["content"]), False)
        check("...and the payload fits Teams' 28 KB message limit", len(json.dumps(teams_payload)) < 28000, True)

        # --- env-var-only config resolution -----------------------------------------------------------
        real_env = dict(os.environ)
        for k in list(os.environ):
            if k.startswith("MERIDIAN_ALERT_"):
                del os.environ[k]
        try:
            check("no env vars means nothing configured",
                  m.notify_configs(), {"slack": None, "teams": None, "email": None})
            os.environ["MERIDIAN_ALERT_SLACK_WEBHOOK"] = "https://hooks.example/slack"
            check("slack resolves from its one env var",
                  m._notify_target_config("slack"), {"webhook": "https://hooks.example/slack"})
            os.environ["MERIDIAN_ALERT_EMAIL_SMTP_HOST"] = "smtp.example"
            check("email needs BOTH host and to", m._notify_target_config("email"), None)
            os.environ["MERIDIAN_ALERT_EMAIL_TO"] = "soc@example.com"
            cfg = m._notify_target_config("email")
            check("email resolves once both are set", cfg["host"], "smtp.example")
            check("...defaulting from to the first recipient", cfg["from"], "soc@example.com")
            check("...defaulting the port to 587", cfg["port"], 587)
            check("...defaulting starttls to on", cfg["starttls"], True)
            os.environ["MERIDIAN_ALERT_EMAIL_STARTTLS"] = "0"
            check("...unless explicitly disabled",
                  m._notify_target_config("email")["starttls"], False)
        finally:
            for k in list(os.environ):
                if k.startswith("MERIDIAN_ALERT_"):
                    del os.environ[k]
            os.environ.update(real_env)

        # --- transport: structurally independent of call() --------------------------------------------
        import urllib.request
        real_urlopen = urllib.request.urlopen
        captured = {}

        class FakeResp:
            status = 200
            def read(self): return b"ok"
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            captured["body"] = req.data
            return FakeResp()

        urllib.request.urlopen = fake_urlopen
        try:
            status, body = m._post_webhook("https://hooks.example/slack", {"text": "hi"})
            check("webhook post reaches the given url", captured["url"], "https://hooks.example/slack")
            check("...as JSON", json.loads(captured["body"]), {"text": "hi"})
            check("...with no Authorization header (never the Meridian token)",
                  "Authorization" in captured["headers"], False)
            check("...and returns the response", (status, body), (200, "ok"))
        finally:
            urllib.request.urlopen = real_urlopen

        class FakeSMTP:
            sent = []
            def __init__(self, host, port, timeout=None):
                self.host, self.port = host, port
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def starttls(self, context=None): FakeSMTP.sent.append("starttls")
            def login(self, user, password): FakeSMTP.sent.append(("login", user, password))
            def send_message(self, msg): FakeSMTP.sent.append(("sent", str(msg["To"]), str(msg["Subject"])))

        m._SMTP_CLIENT = FakeSMTP
        try:
            m._send_email({"host": "smtp.example", "port": 587, "from": "a@example.com",
                          "to": ["b@example.com"], "user": None, "password": None, "starttls": True},
                         "subj", "text body", "<p>html body</p>")
            check("email goes out over the injected SMTP client",
                  ("sent", "b@example.com", "subj") in FakeSMTP.sent, True)
            check("...using starttls by default", "starttls" in FakeSMTP.sent, True)
            check("...and skips login when no user is configured",
                  any(isinstance(x, tuple) and x[0] == "login" for x in FakeSMTP.sent), False)
        finally:
            m._SMTP_CLIENT = None

        # --- deliver_alerts: the on-change gate, end to end --------------------------------------------
        sent_log = []

        def fake_post(url, payload, timeout=10):
            sent_log.append((url, payload))
            return 200, "ok"

        real_post = m._post_webhook
        m._post_webhook = fake_post
        os.environ["MERIDIAN_ALERT_SLACK_WEBHOOK"] = "https://hooks.example/slack"
        try:
            v1 = mkv(firing=["kev"], stack_date="2026-08-17")
            r1 = m.deliver_alerts(v1, targets=("slack", "teams", "email"), stack_date="2026-08-17")
            check("first run sends (nothing to compare against)", len(sent_log), 1)
            check("...and reports what's configured vs skipped",
                  (r1["configured"]["slack"], "teams" in r1["skipped"], "email" in r1["skipped"]),
                  (True, True, True))
            sent_log.clear()

            v2 = mkv(firing=["kev"], stack_date="2026-08-18")
            r2 = m.deliver_alerts(v2, targets=("slack",), stack_date="2026-08-18")
            check("an unchanged verdict does not re-send", len(sent_log), 0)
            check("...and says why", "note" in r2 and "nothing sent" in r2["note"], True)

            m.deliver_alerts(v2, targets=("slack",), force=True, stack_date="2026-08-18")
            check("--force resends regardless", len(sent_log), 1)
            sent_log.clear()

            v3 = mkv(clear=["kev"], stack_date="2026-08-19")
            m.deliver_alerts(v3, targets=("slack",), stack_date="2026-08-19")
            check("a resolved rule triggers a real send, not just --force", len(sent_log), 1)
        finally:
            m._post_webhook = real_post
            del os.environ["MERIDIAN_ALERT_SLACK_WEBHOOK"]

        # One target failing must not cancel or hide the others.
        def raising_post(url, payload, timeout=10):
            if "slack" in url:
                raise RuntimeError("HTTP 500: boom")
            return 200, "ok"

        m._post_webhook = raising_post
        os.environ["MERIDIAN_ALERT_SLACK_WEBHOOK"] = "https://hooks.example/slack"
        os.environ["MERIDIAN_ALERT_TEAMS_WEBHOOK"] = "https://hooks.example/teams"
        try:
            v4 = mkv(firing=["new-rule"], stack_date="2026-08-20")
            r4 = m.deliver_alerts(v4, targets=("slack", "teams"), stack_date="2026-08-20")
            check("a broken target reports its own error", "boom" in r4["sent"]["slack"]["error"], True)
            check("...without cancelling a working target", r4["sent"]["teams"]["status"], 200)
        finally:
            m._post_webhook = real_post
            del os.environ["MERIDIAN_ALERT_SLACK_WEBHOOK"]
            del os.environ["MERIDIAN_ALERT_TEAMS_WEBHOOK"]
    finally:
        (m.CFG_DIR, m.load_config) = real


def test_vuln_detail(m):
    """Per-CVE severity/exploitability in an asset profile (`_vuln_summary` + the asset rules in
    `_derive_insights`).

    KEV membership alone missed a whole class of dangerous host: measured on a live stack, a node
    carrying a CVSS 10.0, 21 unfixable CVEs and 31 in the top 10% of EPSS rendered as a single KEV
    line. The rules below read detail that is already in the fetched record, so none of this costs
    an extra call -- and every count is taken from the FULL list even though the named lists beside
    it are capped, because a cap that silently shrinks a finding is the same class of bug as a
    truncated result set reported as complete.
    """
    print("[24] asset vulnerability detail (offline)")

    # --- _to_float: unknown must stay distinct from zero ------------------------------------------
    check("a parseable score becomes a float", m._to_float("8.8"), 8.8)
    check("a missing score is None, not 0", m._to_float(None), None)
    check("...and so is a garbage score", m._to_float("n/a"), None)
    # The whole reason _to_float exists next to _to_int: an unscored CVE must not sort as harmless.
    check("_to_int would have fabricated a 0 here", m._to_int(None), 0)

    # --- _is_private_ip: under-claim exposure, never invent it -------------------------------------
    for ip in ("10.1.2.3", "172.16.0.1", "172.31.255.254", "192.168.1.1", "127.0.0.1",
               "169.254.1.1", "100.64.0.1", "0.0.0.0", "239.1.1.1"):
        check("%s is not public" % ip, m._is_private_ip(ip), True)
    for ip in ("3.165.255.191", "8.8.8.8", "172.32.0.1", "192.169.0.1", "100.128.0.1"):
        check("%s is public" % ip, m._is_private_ip(ip), False)
    # Anything unrecognisable errs toward "not public" so exposure is never asserted from a guess.
    for junk in ("", None, "not-an-ip", "2001:db8::1", "1.2.3", "1.2.3.4.5", "999.1.1.1"):
        check("unparseable %r is not claimed public" % junk, m._is_private_ip(junk), True)

    # --- _is_top_risk_tier: the word, never a bare digit ------------------------------------------
    check("3-high is the top tier", m._is_top_risk_tier("3-high"), True)
    check("1-low is not", m._is_top_risk_tier("1-low"), False)
    check("2-medium is not", m._is_top_risk_tier("2-medium"), False)
    check("blank is not", m._is_top_risk_tier(None), False)
    # The regression this replaced: `"3" in level` fired on any label merely containing a 3, and
    # said nothing about where that tier sat in the scale (3 of 3 is the top, 3 of 5 is the middle).
    check("a label containing a 3 but no tier word is not the top tier",
          m._is_top_risk_tier("tier-3-of-5-moderate"), False)
    check("critical counts as the top tier", m._is_top_risk_tier("4-critical"), True)

    # --- _vuln_summary: counts from the full list, lists capped, cap disclosed ---------------------
    many = [{"CVE": "CVE-2024-%04d" % i, "Score": 5.0 + (i % 5), "epss_percentile": 0.95,
             "Is_Fixable": 0.0, "Is_KEV": 0.0, "lucidum_vuln_risk": float(i),
             "Name": "x" * 400} for i in range(40)]
    v = m._vuln_summary({"Vuln_List": many, "Count_KEV": 0,
                         "Count_Critical_Severity_Vuln": 2, "Count_High_Severity_Vuln": 38},
                        with_detail=True)
    check("detailTotal is the true count", v["detailTotal"], 40)
    check("detail is capped", len(v["detail"]), m.PROFILE_VULN_DETAIL_MAX)
    check("...and the cap is disclosed", v["detailTruncated"], True)
    check("notFixableCount is the FULL count, not the capped list", v["notFixableCount"], 40)
    check("...while the named list is capped", len(v["notFixable"]), m.PROFILE_VULN_CVES_MAX)
    check("highEpssCount is the full count too", v["highEpssCount"], 40)
    check("detail is ranked worst-first", v["detail"][0]["cve"], "CVE-2024-0039")
    # `Name` runs 300+ characters per CVE on a real stack; 25 of them would dominate the payload.
    check("a 400-char CVE description is truncated",
          (len(v["detail"][0]["name"]) < 250, v["detail"][0]["name"].endswith("...")), (True, True))
    check("maxCvss is the highest score seen", v["maxCvss"], 9.0)
    check("scoredCount counts what actually carried a CVSS", v["scoredCount"], 40)

    # --- the per-CVE array is OFF by default, and that is a context-cost decision -----------------
    # Measured at v2.14.0: `detail` was 8,563 of an asset profile's 12,569 characters and nothing read
    # it -- not _derive_insights, not _profile_html, not the ASCII view, not `compare`. What the rules
    # actually consume is the counts and the named lists, which are always present. The assertions
    # below are the contract that keeps it that way: turning detail off must not change a finding.
    off = m._vuln_summary({"Vuln_List": many, "Count_KEV": 0,
                           "Count_Critical_Severity_Vuln": 2, "Count_High_Severity_Vuln": 38})
    check("detail is omitted by default", "detail" in off, False)
    check("...and so is its cap flag, which would be meaningless", "detailTruncated" in off, False)
    check("...but detailTotal survives, so 'no detail' != 'no CVEs'", off["detailTotal"], 40)
    check("...and the omission is announced, not silent", "--vuln-detail" in off["detailNote"], True)
    for k in ("maxCvss", "scoredCount", "notFixableCount", "notFixable",
              "highEpssCount", "highEpss", "kevCount", "critical", "high"):
        check("%s is present without --vuln-detail" % k, k in off, True)
    check("every rule input is identical with and without detail",
          {k: off[k] for k in off if not k.startswith("detail")},
          {k: v[k] for k in v if not k.startswith("detail")})
    check("...so the findings are byte-identical either way",
          m._derive_insights({"type": "asset", "identity": {}, "risk": {}, "vulnerabilities": off}),
          m._derive_insights({"type": "asset", "identity": {}, "risk": {}, "vulnerabilities": v}))
    # A host with no CVEs at all gets no note -- there is nothing being withheld.
    check("no note when there is no detail to withhold",
          "detailNote" in m._vuln_summary({"Vuln_List": []}), False)

    # An empty Vuln_List is not a zero-risk host -- it is a host with no per-CVE detail.
    empty = m._vuln_summary({"Vuln_List": None, "Count_KEV": 4}, with_detail=True)
    check("no Vuln_List leaves maxCvss unknown, not 0", empty["maxCvss"], None)
    check("...and scoredCount 0", empty["scoredCount"], 0)
    check("...and nothing is claimed unfixable", empty["notFixableCount"], 0)
    check("...and the cap flag is honest", empty["detailTruncated"], False)

    # A CVE with no parseable score must not sort ahead of one that has a score.
    mixed = m._vuln_summary(with_detail=True, x={"Vuln_List": [
        {"CVE": "CVE-A", "Score": None, "epss_percentile": None, "lucidum_vuln_risk": None},
        {"CVE": "CVE-B", "Score": 4.0, "epss_percentile": 0.1, "lucidum_vuln_risk": 10.0}]})
    check("an unscored CVE sorts after a scored one", [d["cve"] for d in mixed["detail"]],
          ["CVE-B", "CVE-A"])

    # --- the asset rules ---------------------------------------------------------------------------
    def insights(vulns=None, ident=None, risk=None):
        return m._derive_insights({
            "type": "asset",
            "identity": dict({"os": "Ubuntu 22.04", "encrypted": 1, "publicIps": []}, **(ident or {})),
            "risk": dict({"level": "1-low", "factors": []}, **(risk or {})),
            "vulnerabilities": vulns or {},
            "associatedUsers": {}})

    f, r = insights({"critical": 18, "high": 112, "maxCvss": 10.0, "scoredCount": 140,
                     "notFixableCount": 21, "notFixable": ["CVE-1", "CVE-2"],
                     "highEpssCount": 31, "highEpss": ["CVE-3"]})
    check("severity counts surface", any("18 critical, 112 high" in x for x in f), True)
    check("...with the max CVSS", any("max CVSS 10.0" in x for x in f), True)
    check("the TRUE unfixable count is stated, not the capped list length",
          any("21 vulnerabilities have no available fix" in x for x in f), True)
    check("...and the capped list is marked as partial", any("CVE-1, CVE-2)" in x for x in f), True)
    check("the true EPSS count is stated", any("31 vulnerabilities sit in the top 10%" in x for x in f), True)
    check("an unfixable CVE gets an upgrade recommendation",
          any("no patch exists" in x for x in r), True)

    # crit/high present but nothing scored: the max is UNKNOWN and must say so, not vanish.
    f, _ = insights({"critical": 0, "high": 3, "maxCvss": None, "scoredCount": 0})
    check("an unreported CVSS is named as unreported", any("no CVSS reported" in x for x in f), True)
    check("...and no CVSS figure is invented", any("max CVSS" in x for x in f), False)

    # Singular grammar -- these strings are read by people (SKILL.md section 4).
    f, _ = insights({"critical": 0, "high": 1, "maxCvss": 8.8, "scoredCount": 1,
                     "notFixableCount": 1, "notFixable": ["CVE-9"],
                     "highEpssCount": 1, "highEpss": ["CVE-9"]})
    check("one vulnerability reads singular", any("1 high severity vulnerability" in x for x in f), True)
    check("...as does the unfixable line", any("1 vulnerability has no available fix" in x for x in f), True)
    check("...and the EPSS line", any("1 vulnerability sits in the top" in x for x in f), True)

    # Public exposure only counts when there is something exploitable to reach.
    f, r = insights({"critical": 1, "high": 0}, ident={"publicIps": ["3.4.5.6"]})
    check("public IP + unpatched criticals is called out",
          any("routable public address (3.4.5.6)" in x for x in f), True)
    check("...and gets a firewall recommendation", any("restrict or firewall" in x for x in r), True)
    f, _ = insights({}, ident={"publicIps": ["3.4.5.6"]})
    check("a public IP with no known vulns is not an exposure finding",
          any("routable public address" in x for x in f), False)

    # Data classification and Meridian's own risk drivers.
    f, _ = insights({}, risk={"dataClass": "Confidential"})
    check("confidential data raises a data-exposure finding",
          any("Confidential-classified data" in x for x in f), True)
    f, _ = insights({}, risk={"factors": ["Threats Detected"]})
    check("a threat driver defers to the endpoint console",
          any("endpoint console" in x for x in f), True)

    # The tier line orients the reader, so it leads -- and quotes Risk_STD only when present.
    f, _ = insights({"kevCount": 3}, risk={"level": "3-high", "ranking": 97.5})
    check("the risk tier leads the findings", f[0], "In the top risk tier (3-high, ranked 97.5 of 100).")
    f, _ = insights({"kevCount": 3}, risk={"level": "3-high", "ranking": None})
    check("...omitting a ranking the stack did not report", f[0], "In the top risk tier (3-high).")
    f, _ = insights({}, risk={"level": "1-low"})
    check("a low-risk clean host still gets an explicit all-clear",
          any("No critical exposure signals" in x for x in f), True)


def test_linked_assets_cap(m):
    """A user profile's `linkedAssets` is unbounded -- measured live, one identity with 30 linked
    assets was 46% of a 13,040-char profile. Same shape of fix as `_vuln_summary` above: counts and
    findings come from the FULL fetched list, only the serialized array is capped, and the cap is
    disclosed rather than silent. Also covers a pre-existing gap found alongside it: the fetch itself
    is capped at PROFILE_LINKED_ASSETS_FETCH_CAP with no signal when a busier identity exceeds it.
    """
    print("[38] user profile linked-asset cap (offline)")

    def asset(i, encrypted="1"):
        return {"Asset_Name": "ASSET%02d" % i, "Risk_Score": float(i), "Risk_Level": "3-high" if i >= 41 else "1-low",
                "OS": "Ubuntu 22.04", "IP_Address": ["10.0.0.%d" % i], "Count_KEV": 0,
                "Is_Encrypted": encrypted, "Data_Classification": "Internal", "High_Risk_User": []}

    # 55 assets are linked; the fetch itself only returns 50 (its own page-size ceiling) -- and the
    # lowest-risk one of those 50 is the only unencrypted asset, so it lands OUTSIDE the top 10 shown
    # by default. If insights were derived from the truncated display list instead of the full fetch,
    # this asset's "unencrypted" finding would silently disappear.
    fetched_assets = [asset(i, encrypted=("0" if i == 1 else "1")) for i in range(1, 51)]
    user = {"Owner_Name": "SVC", "displayName": "Service Account", "Risk_Score": 50, "Risk_Level": "3-high",
            "Asset_Name": ["ASSET%02d" % i for i in range(1, 56)]}

    def fake_call(method, endpoint, body=None, retries=1):
        if "/change" in endpoint:
            return []
        if (body or {}).get("table") == "asset":
            return {"totalRecords": 55, "data": fetched_assets}
        return {"totalRecords": 1, "data": [user]}

    real_call = m.call
    m.call = fake_call
    try:
        out = m.build_profile("SVC", "user")
        detailed = m.build_profile("SVC", "user", linked_detail=True)
    finally:
        m.call = real_call

    check("linkedAssetsTotal is the true count, not what was fetched", out["linkedAssetsTotal"], 55)
    check("linkedAssetsShown is capped to the default", out["linkedAssetsShown"], m.PROFILE_LINKED_ASSETS_SHOW)
    check("the cap is disclosed", out["linkedAssetsTruncated"], True)
    check("the serialized array matches the disclosed count", len(out["linkedAssets"]), m.PROFILE_LINKED_ASSETS_SHOW)
    check("shown assets are sorted worst-first by risk",
          [a["risk"] for a in out["linkedAssets"]], sorted((float(i) for i in range(41, 51)), reverse=True))
    check("the lowest-risk (unencrypted) asset is excluded from the capped view",
          "ASSET01" in [a["asset"] for a in out["linkedAssets"]], False)
    # The regression guard: the one unencrypted asset never makes the top-10 display, so this finding
    # only exists if _derive_insights ran on the full 50, not the capped 10.
    check("...but its finding survives anyway, because insights ran on the full fetched list",
          any("1 linked asset(s) are unencrypted" in f for f in out["findings"]), True)
    check("note names the escape hatch", "--linked-detail" in out["note"], True)
    check("...and both counts", ("55" in out["note"], "50" in out["note"]), (True, True))

    check("--linked-detail shows every fetched asset, not just the top 10", detailed["linkedAssetsShown"], 50)
    check("...still disclosing the fetch-level gap this is NOT a fix for", detailed["linkedAssetsTruncated"], True)
    check("...naming the page-size ceiling, not the display cap", "page-size ceiling" in detailed["note"], True)
    check("...since --linked-detail is already in effect, it isn't suggested again",
          "--linked-detail" in detailed["note"], False)

    # Small case: nothing to cap, nothing to disclose.
    def fake_call_small(method, endpoint, body=None, retries=1):
        if "/change" in endpoint:
            return []
        if (body or {}).get("table") == "asset":
            return {"totalRecords": 2, "data": [asset(1), asset(2)]}
        return {"totalRecords": 1, "data": [dict(user, Asset_Name=["ASSET01", "ASSET02"])]}

    m.call = fake_call_small
    try:
        small = m.build_profile("SVC", "user")
    finally:
        m.call = real_call
    check("under the cap: nothing is truncated", small["linkedAssetsTruncated"], False)
    check("...and there is nothing to say about it", "note" in small, False)
    check("...the total matches what's shown", (small["linkedAssetsTotal"], small["linkedAssetsShown"]), (2, 2))

    # Zero linked assets: `assets` is empty before any asset-table call is made at all.
    def fake_call_zero(method, endpoint, body=None, retries=1):
        if "/change" in endpoint:
            return []
        return {"totalRecords": 1, "data": [dict(user, Asset_Name=[])]}

    m.call = fake_call_zero
    try:
        zero = m.build_profile("SVC", "user")
    finally:
        m.call = real_call
    check("no linked assets is zero, not an error", (zero["linkedAssetsTotal"], zero["linkedAssetsShown"]), (0, 0))
    check("...and not flagged as truncated", zero["linkedAssetsTruncated"], False)

    # --- the rendered cells: the stat card and ASCII header must show the TRUE total ----------------
    # Reading len(linkedAssets) directly would have silently regressed the "Linked assets" stat card
    # (and the ASCII header) to the capped count the moment the cap above shipped.
    shown_assets = [{"asset": "A%d" % i, "level": "1-low", "os": "Win", "risk": 50 - i, "ip": [],
                     "kev": None, "encrypted": 1.0, "otherHighRiskUsers": []} for i in range(3)]
    profile_dict = {"type": "user", "identity": {"ownerName": "U1", "displayName": "U1", "emails": []},
                    "risk": {"score": 10.0, "level": "1-low", "factors": []}, "posture": {}, "threats": {},
                    "stability": {"oscillating": False}, "linkedAssets": shown_assets,
                    "linkedAssetsTotal": 30, "linkedAssetsShown": 3, "linkedAssetsTruncated": True,
                    "note": "Showing the 3 highest-risk of 30 fetched linked assets."}
    _, cards, body = m._profile_html(profile_dict)
    check("stat card shows the TRUE total", "<div class='n'>30</div>" in cards, True)
    check("...never the capped array length", "<div class='n'>3</div>" in cards, False)
    check("report body discloses how many of the total are shown", "Showing 3 of 30 linked assets" in body, True)

    ascii_out = m._ascii_blast_radius(profile_dict)
    check("ascii header discloses the cap too", "showing 3 of 30" in ascii_out, True)


def test_skill_frontmatter():
    """The skill's own frontmatter must be importable. This is not a style check.

    A description over the 1024-character cap makes Claude reject the skill at import, so every user
    on that version loses the skill entirely -- and because SKILL.md is a behaviour contract rather
    than code, no existing test looked at it. It went over one capability sentence at a time."""
    print("[19] SKILL.md frontmatter limits (offline)")
    fm = _skill_frontmatter()
    n = len(fm["description"])
    check("description is present", n > 0, True)
    check("description is within the %d-char import limit (is %d)" % (SKILL_DESCRIPTION_MAX, n),
          n <= SKILL_DESCRIPTION_MAX, True)
    # Headroom, so the next capability blurb fails the suite rather than the import. Tightened from 95%
    # to 90% after the description was measured at 966 -- six characters under the 95% line, i.e. the
    # guard was one clause away from firing and would have had to be argued with mid-feature. 90% of
    # 1024 is 921. Every capability blurb added later costs characters, so the prose around it has to
    # give some back.
    check("...with room to grow (<= 90% of the cap)", n <= int(SKILL_DESCRIPTION_MAX * 0.90), True)
    check("name is within its %d-char limit" % SKILL_NAME_MAX, len(fm["name"]) <= SKILL_NAME_MAX, True)
    check("name is the folder identifier", fm["name"], "meridiancs")
    # The routing surface the description exists to provide must survive any future trimming.
    for term in ("Meridian", "Lucidum", "asset inventory", "SmartLabels", "risk", "connectors",
                 "alert", "HR systems", "managers", "Dayforce", "BambooHR"):
        check("description still triggers on %r" % term, term.lower() in fm["description"].lower(), True)
    check("...and on change-over-time phrasing",
          any(t in fm["description"].lower() for t in ("trend", "changed over time")), True)


def test_hr_routing():
    """SKILL.md routes HR / Human Resources questions to `hr`, with the "no HR data" rule attached.

    The failure this exists for is one sentence said to users who had an HR connector enabled: "you
    don't have HR data". It came from the model, not the code -- nothing in SKILL.md tied "HR" or
    "manager" to Dayforce, BambooHR or ADP, and the paths it did take (the connector group, a search
    for the product name, a sampled breakdown) all return nothing for an HR source. SKILL.md is prose
    the model follows, so no behaviour test can notice that routing being dropped. This asserts it in
    both directions: the question reaches the verb, AND the rules that keep it from being answered
    wrongly travel with it.
    """
    print("[50] SKILL.md routes HR questions to `hr`, and only its state may say 'no HR data'")
    body = open(SKILL_MD, encoding="utf-8").read()
    flat = " ".join(body.split())
    check("the verb table routes HR questions to `hr`",
          "Human Resources systems" in flat and "`meridian.py hr` **first**" in flat, True)
    check("...naming the systems a user actually says", all(s in flat for s in (
        "Dayforce", "BambooHR", "ADP", "Workday", "UKG")), True)
    check("...and manager/employee questions", "managers, employees, hires or terminations" in flat, True)
    check("the rule: 'no HR data' only from its state",
          '"you have no HR data" may only come from its `state`' in flat, True)
    check("...explaining why nothing else can say it (group, name, sample)", all(s in flat for s in (
        "Identity Access Management", "`dayforce_employee`", "samples")), True)
    for st in ("has_data", "configured_no_data", "configured_disabled", "none_configured", "unknown"):
        check("...covering state %s" % st, "`%s` —" % st in flat, True)
    check("...forbidding 'no HR system' when the connector exists but isn't delivering",
          "Never tell the user they have no HR system." in flat, True)
    check("...and 'none' when the check failed", "never that there is none" in flat, True)
    check("...and saying the core manager fields are not HR-only", "not HR-only" in flat, True)
    with open(os.path.join(os.path.dirname(SKILL_MD), "references", "scripts.md"), encoding="utf-8") as f:
        ref = " ".join(f.read().split())
    check("scripts.md documents the verb and its states",
          "`hr` — **which HR systems feed this stack" in ref and "`configured_no_data`" in ref, True)


def test_alert_routing():
    """SKILL.md's routing to `alerts`, and the semantics that must travel with it.

    `alerts` shipped dormant on purpose -- built in PR #65, routed only once the two parked decisions
    were settled from real history. Routing it is the moment its failure mode becomes reachable by a
    natural-language question, and that failure mode is a specific sentence: reporting a rule that
    could not be tested as one that passed. No behaviour test can catch SKILL.md losing that sentence,
    because SKILL.md is prose the model follows rather than code anything executes -- the same gap that
    let the frontmatter description grow past its import cap unnoticed.

    So this asserts the contract in both directions: the routing exists, AND the rules that stop it
    being answered wrongly exist alongside it."""
    print("[40] SKILL.md routes alerts, with the three-verdict rule attached")
    body = open(SKILL_MD, encoding="utf-8").read()

    # Routing: a question has to be able to reach the verb at all.
    check("the verb table routes rule creation", "alerts add" in body, True)
    check("...and evaluation", "alerts eval" in body, True)
    check("...and delivery", "alerts notify" in body, True)

    # The load-bearing semantics. An unevaluable rule read as a passing one is the whole reason this
    # feature has three verdicts instead of two.
    low = body.lower()
    check("SKILL.md says unevaluable is not a pass",
          "unevaluable` is **not** a pass" in body or "not a pass" in low, True)
    check("...and that the exit code is a bit field", "bit field" in low, True)
    check("...and that an empty rule set is not an all-clear", "nothingchecked" in low, True)
    # A `clear` verdict sitting on top of connectors that are still failing is the reassuring-omission
    # case the degraded-context fix was written for; the routing must carry it or the fix is invisible.
    check("...and that standing state is reported, not just change", "stillFailing" in body, True)
    check("...naming the unknown-vs-zero field", "stillDegradedUnknown" in body, True)
    check("...and that absence is never zero", "**never** zero" in body, True)

    # The reference doc the routing points at has to actually document the verb.
    ref = os.path.join(os.path.dirname(HERE), "references", "trend-verbs.md")
    doc = open(ref, encoding="utf-8").read() if os.path.isfile(ref) else ""
    check("trend-verbs.md documents the alerts verb", "### Alerts" in doc, True)
    check("...including the exit-code table", "| 12 | both |" in doc, True)
    check("...and that eval makes no API calls", "no API calls" in doc, True)
    check("...and that webhook/SMTP config is env-var only",
          "MERIDIAN_ALERT_SLACK_WEBHOOK" in doc, True)
    check("...and that delivery is on-change", "on-change only" in doc.lower(), True)
    # The Teams shape is a documented guess. If that caveat is ever dropped the doc starts asserting
    # something nobody verified.
    check("...and that the Teams payload is still flagged unverified", "unverified" in doc.lower(), True)
    # A Workflows webhook answers 202 before the flow runs, whatever it then does with the body, so
    # a doc that let 202 read as "delivered" would bless the silent failure the old shape had.
    check("...and that a 202 is not proof of delivery", "a 202 is not proof of" in " ".join(doc.split()), True)


def test_skill_rule_survival():
    """Every rule in SKILL.md's verb section that stops a confident wrong answer.

    SKILL.md is prose the model follows rather than code anything executes, so no behaviour test can
    notice a rule being dropped from it -- the gap that let the frontmatter description grow past its
    import cap, and the reason test_alert_routing exists. This is that guard widened from one verb to
    all of them, because the section was compressed: the measured evidence behind each rule moved out
    to the reference that already carried it in more depth, and what had to survive is the imperative.

    A rule here is not advice. Each one is the difference between an answer and a *reassuring* wrong
    answer -- a partial breakdown read as whole, an uncaptured metric read as zero, an untested alert
    read as passing, a failed label lookup read as "no such label". Those all fail silently and in the
    direction nobody investigates, which is why they live in the always-loaded body and not behind a
    pointer.

    Both directions, as with test_alert_routing: the imperative is in SKILL.md, AND the reference it
    defers to actually carries the reasoning. A pointer at a doc that does not explain the rule is
    worse than no pointer -- it reads as though the justification was checked."""
    print("[41] SKILL.md keeps every rule that prevents a confident wrong answer")
    raw = open(SKILL_MD, encoding="utf-8").read()
    # Matched against a whitespace-normalised copy on purpose. These are rules, not display
    # templates, so where a sentence happens to wrap is noise -- unlike references/welcome.md, which
    # IS echoed verbatim and whose test is deliberately wrap-sensitive. Normalising costs nothing in
    # detection: a deleted rule is still absent from the normalised text. It only stops a reflow
    # failing the build for a rule that is still there, which would train the next person to
    # "fix" it by loosening the assertion.
    body = " ".join(raw.split())

    # (what the rule prevents, the text that has to survive)
    rules = [
        # -- the customer's own vocabulary; a failed lookup must not read as "no such label" --------
        ("business terms resolve to SmartLabels first", "SmartLabels before reaching for a generic field"),
        ("a failed label lookup is flagged", "fieldMetadataUnavailable"),
        ("...and is never reported as the label not existing", "never that the label doesn't exist"),
        ("an index-only label listing is marked", "purposeOmitted"),

        # -- cache age, so an hour-old verdict is never presented as current ------------------------
        ("a served answer is identifiable", "fromCache"),
        ("...with an age, so staleness is statable", "cacheAgeSeconds"),
        ("snapshots are never served from cache", "Snapshots are always measured, never served"),

        # -- a partial breakdown presented as the whole picture -------------------------------------
        ("a breakdown states its own completeness", "complete"),
        ("...naming the records it could not place", "unaccountedRecords"),
        ("...and refusing to bless an over-count", "overcountedRecords"),
        ("...and saying when only the largest values were counted", "groupsCapped"),

        # -- profile: the analysis is in the payload, and absence is not zero -----------------------
        ("profile's own analysis is what gets presented", "recommendations"),
        ("...without paying for a PDF to reach it", "don't render a PDF just to get them"),
        ("an unscored CVE is not a low one", 'never "low" and never 0'),
        ("an unfixable vulnerability is quoted as such", "notFixableCount"),
        ("...alongside exploitability", "highEpssCount"),
        ("the per-CVE array is not fetched to answer a question", "Don't pass `--vuln-detail`"),

        # -- reports: nothing is summed across subjects that would double-count ---------------------
        ("a multi-subject report totals nothing across subjects", "Nothing is totalled across subjects"),
        ("a digest that quietly shrank says so", "sectionsUnavailable"),

        # -- trends: the three flags that make a percentage meaningless -----------------------------
        ("a movement under changed coverage is not a real change", "coverageChanged"),
        ("too little history is not a flat line", "insufficientHistory"),
        ("unreadable coverage computes nothing", "unverifiable"),
        ("a top-N scope is a window, not the inventory", "cutoffMoved"),
        ("an uncaptured metric is unknown, not zero", "notTracked"),
        ("...and is never rendered as one", "flat line"),
        ("a refused metric registration is explained", "registrationRefused"),
        ("a trended figure is a metric, not a breakdown", "Prefer a metric over a breakdown"),

        # -- alerts: an untested condition is not a passing one -------------------------------------
        ("an untestable alert is not a passing one", "unevaluable"),
        ("standing failures are reported, not just movement", "stillFailing"),
        ("an unrecorded degraded set is unknown, not zero", "stillDegradedUnknown"),
        ("the noisier alert flag stays opt-in", "--include-degraded"),

        # -- the global one: pacing only works if everything goes through the script ----------------
        ("every call is paced through the script", "Route all HTTP through"),
    ]
    for why, frag in rules:
        check("SKILL.md still says: %s" % why, frag in body, True)

    # The routing table is the other half: a rule cannot fire on a verb a question cannot reach.
    for verb in ("connect --with-connectors", "meridian.py top", "meridian.py list",
                 "meridian.py summary", "meridian.py profile", "meridian.py digest",
                 "meridian.py trend", "meridian.py report", "labels --search",
                 "metrics add", "alerts eval"):
        check("...and still routes %s" % verb, verb in body, True)

    # Compression moved evidence, it did not invent a destination. Every reference the section defers
    # to has to carry the reasoning -- a pointer at a doc that does not explain the rule reads as
    # though someone checked, which is worse than no pointer at all.
    refs = os.path.join(os.path.dirname(HERE), "references")
    scripts_doc = open(os.path.join(refs, "scripts.md"), encoding="utf-8").read()
    trend_doc = open(os.path.join(refs, "trend-verbs.md"), encoding="utf-8").read()
    reports_doc = open(os.path.join(refs, "reports.md"), encoding="utf-8").read()

    check("scripts.md carries the breakdown-completeness reasoning",
          "overcountedRecords" in scripts_doc and "groupsCapped" in scripts_doc, True)
    check("...and the --vuln-detail measurement", "--vuln-detail" in scripts_doc, True)
    check("...and the --select/--out payload figures", "229,000 tokens" in scripts_doc, True)
    check("trend-verbs.md carries the trend refusal flags",
          all(f in trend_doc for f in ("coverageChanged", "insufficientHistory",
                                       "unverifiable", "notTracked")), True)
    check("...and the three alert verdicts", "unevaluable" in trend_doc, True)
    check("reports.md documents the report verb", len(reports_doc.strip()) > 0, True)

    # Anything the section points at must exist; a dangling pointer is a rule with no evidence.
    import re as _re
    for target in sorted(set(_re.findall(r"\(references/([a-z-]+\.md)[^)]*\)", raw))):
        check("SKILL.md's link to references/%s resolves" % target,
              os.path.isfile(os.path.join(refs, target)), True)


def test_launch_greeting():
    """The section 0 launch greeting, now a file rather than inline prose.

    SKILL.md tells the model to read references/welcome.md and echo it exactly. That trade -- the
    always-loaded body no longer carries a block it displays once -- moves a load-bearing verbatim
    string somewhere nothing was checking, and the failure it opens up is the worst shape available:
    an absent, emptied or excluded file leaves the launch step with nothing to echo, so the model
    improvises a greeting, and an improvised greeting still looks like a greeting. Nobody reports
    that as a bug. Same reason SKILL.md's frontmatter got a test -- a behaviour contract is not
    covered by tests that only read behaviour."""
    print("[39] launch greeting (offline)")
    path = os.path.join(os.path.dirname(HERE), "references", "welcome.md")
    check("references/welcome.md exists", os.path.isfile(path), True)
    body = open(path, encoding="utf-8").read() if os.path.isfile(path) else ""
    check("...and is not empty", len(body.strip()) > 0, True)

    # "The file holds nothing but that greeting" is what makes "read it and echo it" safe to say.
    # Any explanatory prose added here would be displayed to the user as part of the welcome.
    lines = [l for l in body.strip().splitlines() if l.strip()]
    check("...and holds nothing but the quoted block",
          bool(lines) and all(l.startswith(">") for l in lines), True)

    # The connection instructions are the half a paraphrase rounds off. Without the role name or the
    # shown-only-once warning, a user follows the greeting to the end and still cannot connect.
    for frag in ("Welcome to Meridian for Claude", "FQDN", "API token", "Api_Users",
                 "Generate Token", "only once"):
        check("greeting still carries %r" % frag, frag in body, True)
    # This block ships in every release and the brief goes to customers: keep the sample a placeholder.
    check("...and the sample prompt stays a placeholder", "<person>" in body, True)

    # The usage disclaimer. It is the first thing a user reads each session, so it has to be
    # the first thing in the file -- a disclaimer that arrives after the pitch is a footnote,
    # and one that arrives after an answer is an apology. Position is the assertion, not just
    # presence: nothing else in this file would notice it sliding down the page.
    #
    # Its first half is the coverage caveat, and that half is the one with teeth. Every other
    # completeness guard in this repo exists because a missing input reads as a small number
    # rather than an error; the same is true one layer out, where the missing input is a dead
    # connector or a source nobody onboarded and no amount of care inside the skill can see it.
    # Each fragment sits on one line of the file on purpose: these are substring checks, so a
    # rewrap that splits one across lines fails here. That is the intended trade -- rewrapping
    # this block is editing a display template, and it should not pass unread.
    for frag in ("only as good as the data", "connectors feed it",
                 "Confirm your connectors are healthy", "outcomes are not guaranteed",
                 "outside the path this skill provides", "not professional advice"):
        check("disclaimer still carries %r" % frag, frag in body, True)
    check("...and still leads the greeting",
          body.index("Please read before you start") < body.index("Welcome to Meridian for Claude")
          if "Please read before you start" in body else False, True)

    # Section 0 must still route to the file, and must still demand it unaltered.
    skill = open(SKILL_MD, encoding="utf-8").read()
    sec = skill.split("## 0. Welcome message")[1].split("## 0.5")[0]
    check("section 0 points at the file", "references/welcome.md" in sec, True)
    check("...and still demands it exactly as written", "exactly as written" in sec, True)
    check("...and still forbids paraphrase", "paraphrase" in sec, True)
    check("...and still requires the disclaimer to lead", "must lead your output" in sec, True)
    check("...and still names the coverage caveat as part of it",
          "bounded by what the connectors actually feed" in sec, True)
    # Two copies of a verbatim greeting is one copy that drifts out of date.
    check("...and the body no longer inlines the greeting too",
          "Welcome to Meridian for Claude" in skill, False)


def test_doc_output_guard():
    """`docout.resolve_out` -- the guard on the three documentation generators' output path.

    Snyk reports 18 LOW Path Traversal findings where `sys.argv[1]` reaches `open`/`os.replace`/
    `os.remove` in the generators. That framing is wrong: the operator running the script names the
    file, so there is no trust boundary to cross. But nothing validated the target at all, and
    Chrome's `--print-to-pdf` overwrites whatever is there and still exits 0 -- so
    `make-brief.py SKILL.md` replaced SKILL.md with a 134KB PDF, silently. Verified against the
    unguarded scripts before the guard was written.

    The suffix check is the one that matters; the directory and missing-parent checks only turn a
    traceback into a sentence. All three refusals must exit non-zero, because these run from
    release steps where a zero exit reads as a regenerated document."""
    import contextlib, io, shutil
    print("[37] documentation output-path guard (offline)")
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
    import docout

    tmp = tempfile.mkdtemp()
    default = os.path.join(tmp, "Default-Doc.pdf")

    def resolve(arg):
        """resolve_out reads argv and exits on refusal; capture both."""
        argv, err = sys.argv, io.StringIO()
        sys.argv = ["make-brief.py"] + ([arg] if arg is not None else [])
        try:
            with contextlib.redirect_stderr(err):
                return ("ok", docout.resolve_out(default))
        except SystemExit as e:
            return ("exit%s" % e.code, err.getvalue())
        finally:
            sys.argv = argv

    # The default path must survive untouched -- every documented invocation passes no argument.
    check("no argument resolves to the committed default", resolve(None), ("ok", default))

    # The destructive case. A source file is the plausible mistake, not `../../etc/passwd`.
    source = os.path.join(tmp, "SKILL.md")
    open(source, "w").write("behaviour contract" + chr(10))
    status, msg = resolve(source)
    check("a non-.pdf target is refused", status, "exit2")
    check("...and the refusal names the file", source in msg, True)
    check("...and says why it would be destructive", "destroy" in msg, True)
    check("...and the file is untouched", open(source).read(), "behaviour contract" + chr(10))

    # A directory: the "forgot the filename" mistake.
    check("a directory target is refused", resolve(tmp)[0], "exit2")
    check("...and a missing parent is refused too",
          resolve(os.path.join(tmp, "nope", "doc.pdf"))[0], "exit2")

    # A .pdf under an existing directory is the normal regenerate case and must still work,
    # whether or not it already exists -- refusing to overwrite would break every release.
    fresh = os.path.join(tmp, "New-Doc.pdf")
    check("a new .pdf is accepted", resolve(fresh), ("ok", fresh))
    open(fresh, "w").write("old pdf")
    check("...and regenerating over an existing .pdf still works", resolve(fresh), ("ok", fresh))
    check("...and .PDF is accepted case-insensitively",
          resolve(os.path.join(tmp, "Doc.PDF"))[0], "ok")

    # Relative paths must come back absolute: the generators hand OUT to Chrome, which does not
    # share the script's working directory.
    rel = resolve("relative-doc.pdf")
    check("a relative path is made absolute", os.path.isabs(rel[1]), True)

    shutil.rmtree(tmp, ignore_errors=True)


def test_retention(m):
    """`snapshots list | prune` (design/trends.md phase 5). Pruning is never silent: a history that
    quietly loses its early end changes what a long trend MEANS, and the output looks identical."""
    print("[20] snapshot retention (offline)")
    import contextlib, io
    tmp = tempfile.mkdtemp()
    real = (m.CFG_DIR, m.load_config, m.build_digest)
    m.CFG_DIR = tmp
    m.load_config = lambda: ("s.example", "tok", None)
    try:
        path = m._snapshots_path()
        s = m.snapshots_summary()
        check("an empty history reports zero, not an error", s["records"], 0)
        check("...and says there is no backfill", "no way to backfill" in s["note"], True)
        for i in range(1, 13):
            m.append_snapshot(_snap("2026-06-%02d" % i))
        s = m.snapshots_summary()
        check("list counts the records", s["records"], 12)
        check("...reports oldest and newest", (s["oldest"], s["newest"]), ("2026-06-01", "2026-06-12"))
        check("...the file size", s["bytes"] > 0, True)
        check("...the cap", s["cap"], 400)
        check("...and the distinct stack dates a trend can actually use", s["distinctStackDates"], 12)

        p = m.prune_snapshots(keep=5)
        check("prune keeps the newest N", p["kept"], 5)
        check("...and reports how many it dropped", p["dropped"], 7)
        check("...naming the range that is gone", p["droppedRange"],
              {"from": "2026-06-01", "to": "2026-06-07"})
        check("...and warning what that does to a trend", "unavailable rather than unchanged" in p["note"], True)
        recs, _ = m.load_snapshots()
        check("the newest N are what survived", [r["stackDate"] for r in recs],
              ["2026-06-%02d" % i for i in range(8, 13)])
        check("...and no temp file is left behind", os.path.exists(path + ".tmp"), False)
        p = m.prune_snapshots(keep=5)
        check("pruning an already-short history changes nothing", p["pruned"], False)
        check("...and says so rather than reporting a drop", "Nothing to prune" in p["note"], True)
        check("a keep of zero is refused", m.prune_snapshots(keep=0)["pruned"], False)

        # An unreadable line is dropped by the rewrite, so it is reported rather than vanishing.
        with open(path, "a", encoding="utf-8") as f:
            f.write("{ broken\n")
        p = m.prune_snapshots(keep=2)
        check("unreadable lines are named when the rewrite drops them",
              p["droppedUnreadable"][0]["reason"], "line is not valid JSON")
        check("...and the readable ones still prune correctly", p["kept"], 2)

        # The cap is enforced on capture too, and reported there.
        m.build_digest = lambda *a, **k: _snap_digest()
        m.SNAPSHOT_MAX_RECORDS = 4
        try:
            for i in range(6):
                with contextlib.redirect_stdout(io.StringIO()):
                    with contextlib.redirect_stderr(io.StringIO()):
                        info = m.take_snapshot()
            check("an unattended cadence cannot grow the file forever", info["pruned"]["pruned"], True)
            check("...reporting the drop rather than trimming silently", info["pruned"]["dropped"] >= 1, True)
            check("...and the history holds the cap", info["historyRecords"], 4)
        finally:
            m.SNAPSHOT_MAX_RECORDS = 400

        # `snapshots list` flags a history that holds customer identifiers.
        rec = _snap("2026-07-01")
        rec["entities"] = {"scope": "top500:asset:Risk_Score", "namesStored": True, "saltId": "x",
                           "scores": {"PROD-DB-04": 1.0}}
        m.append_snapshot(rec)
        s = m.snapshots_summary()
        check("list counts entity records", s["withEntities"], 1)
        check("...and flags the ones holding identifiers", s["storingNames"], 1)
        check("...pointing at the permissions.deny protection",
              "permissions.deny" in s["privacyNote"], True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            m.cmd_snapshots(argparse.Namespace(snapshots_cmd="list"))
        check("the CLI prints the summary", json.loads(buf.getvalue())["records"], s["records"])
    finally:
        m.CFG_DIR, m.load_config, m.build_digest = real


def test_live():
    print("[21] live connect (needs a configured, reachable stack)")
    proc = subprocess.run([sys.executable, MERIDIAN_PY, "connect"],
                          capture_output=True, text=True, timeout=60)
    out = json.loads(proc.stdout)
    check("state == connected", out.get("state"), "connected")
    check("assetCount is a number", isinstance(out.get("assetCount"), (int, float)), True)
    check("userCount is a number", isinstance(out.get("userCount"), (int, float)), True)
    # Token must never appear in full — only tokenLast4 (<= 4 chars) is allowed.
    blob = json.dumps(out)
    last4 = out.get("tokenLast4") or ""
    check("no token field longer than 4 chars", len(last4) <= 4, True)
    check("'api_token' key not leaked", "api_token" in blob, False)


def test_live_entities(m):
    """The same no-identifiers-on-disk grep as offline, but against real customer data.

    A fixture can only prove the code hashes what the fixture gave it. This proves it against the real
    asset names a real stack returns -- which is the assertion that actually matters, and the same style
    as the existing live connector-credential checks.

    Writes to a temp directory, never the operator's real history: a test has no business appending to
    the trend data someone is accumulating, and `prune` does not exist until phase 5."""
    print("[23] live entity capture / no identifiers on disk (needs a configured, reachable stack)")
    import tempfile as tf
    tmp = tf.mkdtemp()
    real_dir = m.CFG_DIR
    m.CFG_DIR = tmp                      # history goes here; CFG_PATH is separate, so creds still resolve
    SALT = "live-test-salt-" + "0" * 48   # explicit, so nothing is written to the real config.json
    try:
        spec, problem = m.parse_entity_scope("top50:asset:Risk_Score")
        check("a live scope parses", problem, None)
        # Once WITH names, in memory only, purely to learn what must not appear on disk.
        named = m.capture_entities(spec, SALT, with_names=True)
        identifiers = [k for k in named["scores"]]
        check("the live stack returned identifiable assets", len(identifiers) > 0, True)
        # Then the default path, which is what gets written.
        ents = m.capture_entities(spec, SALT)
        m.append_snapshot(m.snapshot_record({"stack": m.load_config()[0]}, None, ents))
        blob = open(m._snapshots_path(), encoding="utf-8-sig").read()
        leaked = [i for i in identifiers if i and str(i) in blob]
        check("NO real customer identifier is in the written history", leaked, [])
        check("...nor the identity field name", "Asset_Name" in blob, False)
        check("...nor the salt", SALT in blob, False)
        check("the saltId IS recorded, so a diff can check compatibility", m.salt_id(SALT) in blob, True)
        check("every id is a 16-char hash", all(len(k) == 16 for k in ents["scores"]), True)
        # The hashes must be exactly the hash of the real identifiers -- same entity across time.
        expected = {m.hash_entity(SALT, i) for i in identifiers}
        check("...and each is the hash of a real identifier", set(ents["scores"]) <= expected, True)
        check("counts agree between the two captures", ents["count"], named["count"])
        check("a cutoff score was recorded", isinstance(ents.get("cutoffScore"), float), True)
    finally:
        m.CFG_DIR = real_dir


def test_data_currency(m):
    """Every answer is tied to the LDG rebuild it came from (design/data-currency.md, phase 1).

    The LDG changes only when a merger run completes, so the stamp is the last completed, non-failed
    merger run -- never the query time, never the latest ingest. The load-bearing negatives: an
    unreadable stamp is `unknown` (never "current", never "now"), a failed merge is skipped rather
    than trusted, a renamed merger fails loudly and names itself, and a query that straddles a
    rebuild is not an answer.
    """
    print("[51] data currency: rebuild stamps on every data verb (offline)")
    import contextlib
    import io
    import tempfile
    names = ("_CFG_CACHE", "CFG_DIR", "CFG_PATH", "call", "load_config", "ldg_rebuild", "top_n", "list_records",
             "summarize_by", "stack_metrics", "build_profile", "hr_sources", "summarize_connectors",
             "diagnose_connection", "build_digest", "take_snapshot")
    real = {n: getattr(m, n) for n in names}
    real_env = os.environ.get("MERIDIAN_NO_CACHE")
    tmp = tempfile.mkdtemp(prefix="currency-")
    m.CFG_DIR = tmp
    m._CFG_CACHE = ("s.example", "tok", None)   # configured, so ldg_rebuild reaches call()
    m.load_config = lambda: ("s.example", "tok", None)

    T0 = 1790244732                               # 2026-09-24T10:12:12Z, a real merger end_time
    ISO0 = "2026-09-24T10:12:12Z"
    DAY = 86400
    ASSET, USER = "Lucidum Asset Merger", "Lucidum User Merger"

    def run(name, end, status="Warning", dag=None, platform="ML-ENGINE"):
        return {"platform": platform, "bridge_name": name, "status": status, "end_time": end,
                "_time": end * 1000, "_utc": time.strftime("%Y-%m-%dT%H:%M:%S.000+00:00", time.gmtime(end)),
                "dag_run_id": dag or "scheduled__%d" % end}

    def filler(n, end):
        return [run("some_connector", end - i, "Success", platform="api") for i in range(n)]

    served = {"calls": 0}

    def serve(pages, fail=None):
        served["calls"] = 0

        def fake(method, endpoint, body=None, retries=1):
            served["calls"] += 1
            if fail:
                raise RuntimeError(fail)
            check_sort = "sort=_time%2Cdesc" in endpoint
            p = int(re.search(r"[?&]page=(\d+)", endpoint).group(1))
            return {"content": pages[p] if p < len(pages) and check_sort else [], "totalPages": len(pages)}
        m.call = fake

    try:
        os.environ["MERIDIAN_NO_CACHE"] = "1"
        check("the merger names are pinned exactly (a rename is a visible test change)",
              m.LDG_MERGERS, (("asset", ASSET), ("user", USER)))

        # --- found on page 0: one call, end_time as the stamp -----------------------------------
        serve([filler(3, T0 + 50) + [run(ASSET, T0, dag="d1"), run(USER, T0, dag="d1")] + filler(5, T0)])
        s = m.ldg_rebuild()
        check("newest page holds both mergers: one call", (s["apiCalls"], served["calls"]), (1, 1))
        check("stamp is end_time, as ISO UTC", (s["asset"].get("rebuiltUtc"), s["user"].get("rebuiltUtc")), (ISO0, ISO0))
        check("...carrying the pipeline run that produced it", s["asset"].get("dagRunId"), "d1")
        check("a Warning merge is a rebuild, not a failure", "lastRebuildFailed" in s["asset"], False)

        # --- found deeper: pages until both are seen ---------------------------------------------
        serve([filler(4, T0 + 90), filler(4, T0 + 80), [run(ASSET, T0), run(USER, T0)]])
        s = m.ldg_rebuild()
        check("mergers on page 2 are found on page 2", (s["apiCalls"], s["asset"].get("rebuiltUtc")), (3, ISO0))

        # --- missing within the cap: unknown with a reason, never a timestamp -------------------
        pages = [filler(4, T0 + 900 - 10 * i) for i in range(10)]
        pages[0] = pages[0] + [run(ASSET, T0)]
        serve(pages)
        s = m.ldg_rebuild()
        check("a merger absent from the newest pages stops at the cap",
              s["apiCalls"], m.LDG_REBUILD_MAX_PAGES)
        check("...its table is unknown, with a reason", ("unknown" in s["user"], "rebuiltUtc" in s["user"]), (True, False))
        check("...naming the merger it looked for", USER in s["user"]["unknown"], True)
        check("...while the table that WAS found keeps its stamp", s["asset"].get("rebuiltUtc"), ISO0)

        # --- renamed: loud, and names what it found ----------------------------------------------
        serve([[run("Meridian Asset Merger", T0), run(USER, T0)]])
        s = m.ldg_rebuild()
        check("a renamed merger is unknown, not absorbed", "rebuiltUtc" in s["asset"], False)
        check("...and the reason names the unexpected bridge", "'Meridian Asset Merger'" in s["asset"]["unknown"], True)
        check("...and says it may have been renamed", "renamed" in s["asset"]["unknown"], True)
        check("...and the result lists it", s.get("unexpectedMergers"), ["Meridian Asset Merger"])
        check("the unchanged merger still stamps", s["user"].get("rebuiltUtc"), ISO0)

        # --- failed runs are skipped; the previous good one is the stamp -------------------------
        for status in ("Error", "FAILED", "Warning&Error", ""):
            pages = [[run(ASSET, T0 + DAY, status, dag="bad"), run(USER, T0 + DAY, dag="good-u")] + filler(3, T0 + DAY)]
            pages += [filler(4, T0 + DAY - 100 * i) for i in range(1, 4)]
            pages += [[run(ASSET, T0, "Success", dag="good-a")]]
            serve(pages)
            s = m.ldg_rebuild()
            check("newest asset merge %r: stamps the earlier good run" % status,
                  s["asset"].get("rebuiltUtc"), ISO0)
            check("...flags the failed one with its status %r" % status,
                  (s["asset"].get("lastRebuildFailed") or {}).get("status"), status)
            check("...having paged back to find it (%r)" % status, s["apiCalls"], 5)
        check("a Warning newest run stamps itself",
              (s["user"].get("rebuiltUtc"), "lastRebuildFailed" in s["user"]),
              (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(T0 + DAY)), False))

        # --- failed with no earlier success: unknown at the rate-limit backstop -----------------
        pages = [[run(ASSET, T0 + DAY, "Error"), run(USER, T0 + DAY)]]
        pages += [filler(2, T0 + DAY - 10 * i) for i in range(1, 40)]
        serve(pages)
        s = m.ldg_rebuild()
        check("a failed newest run with no earlier success stops at the backstop",
              s["apiCalls"], m.LDG_REBUILD_FAILED_MAX_PAGES)
        check("...and is unknown, saying the newest run failed",
              ("rebuiltUtc" in s["asset"], "failed" in s["asset"].get("unknown", "")), (False, True))

        # --- the failed-run search is paid once per failed run -----------------------------------
        os.environ.pop("MERIDIAN_NO_CACHE", None)
        m.drop_rescache()
        pages = [[run(ASSET, T0 + DAY, "Error", dag="bad1"), run(USER, T0 + DAY)]]
        pages += [filler(3, T0 + DAY - 100 * i) for i in range(1, 6)] + [[run(ASSET, T0, "Success", dag="ok0")]]
        serve(pages)
        first = m.ldg_rebuild()
        second = m.ldg_rebuild()
        check("the backwards search runs once", first["apiCalls"] > 1, True)
        check("...and the next check costs one call", second["apiCalls"], 1)
        check("...with the same stamp and the failure still flagged",
              (second["asset"].get("rebuiltUtc"), (second["asset"].get("lastRebuildFailed") or {}).get("dagRunId")),
              (ISO0, "bad1"))
        pages[0] = [run(ASSET, T0 + 2 * DAY, "Error", dag="bad2"), run(USER, T0 + 2 * DAY)]
        serve(pages)
        third = m.ldg_rebuild()
        check("a NEW failed run is a new key: the search runs again", third["apiCalls"] > 1, True)
        os.environ["MERIDIAN_NO_CACHE"] = "1"

        # --- never raises; unconfigured makes no call ---------------------------------------------
        serve([], fail="HTTP 403: Forbidden")
        s = m.ldg_rebuild()
        check("an unreadable endpoint is unknown for both tables, not an exception",
              ("could not read" in s["asset"].get("unknown", ""), "rebuiltUtc" in s["user"]), (True, False))
        serve([[run(ASSET, T0), run(USER, T0)]])
        # Isolated from THIS machine: with only _CFG_CACHE cleared, an operator's real
        # ~/.meridian/config.json made the stack "configured" and this passed in CI only.
        m._CFG_CACHE = None
        m.CFG_PATH = os.path.join(tmp, "no-such-config.json")
        saved_env = {k: os.environ.pop(k, None) for k in ("MERIDIAN_FQDN", "MERIDIAN_API_TOKEN")}
        try:
            s = m.ldg_rebuild()
        finally:
            os.environ.update({k: v for k, v in saved_env.items() if v is not None})
            m.CFG_PATH = real["CFG_PATH"]
        check("unconfigured: unknown, and no network call",
              (s["asset"].get("unknown"), served["calls"]), ("no stack is configured", 0))
        m._CFG_CACHE = ("s.example", "tok", None)

        # --- _merger_end ----------------------------------------------------------------------------
        r = run(ASSET, T0)
        r["_utc"] = "2026-09-24T10:30:00.000+00:00"
        check("end_time wins over _utc when they disagree", m._merger_end(r)[1], ISO0)
        r.pop("end_time")
        check("_utc is the fallback without end_time", m._merger_end(r)[1], "2026-09-24T10:30:00Z")
        check("milliseconds are tolerated", m._merger_end({"end_time": T0 * 1000})[1], ISO0)
        check("an undatable run is None, not now", m._merger_end({"_utc": "yesterday"}), (None, None))

        # --- data_currency -------------------------------------------------------------------------
        a = {"rebuiltUtc": ISO0, "dagRunId": "d1"}
        u = {"rebuiltUtc": ISO0, "dagRunId": "d1"}
        cur = m.data_currency({"asset": a, "user": u}, ["asset", "user"])
        check("both known: current, both stamped",
              (cur["class"], cur.get("ldgRebuiltUtc")), ("current", {"asset": ISO0, "user": ISO0}))
        check("...with the query time beside it, not instead of it", bool(cur.get("queriedUtc")), True)
        check("...and no split", "mergersSplit" in cur, False)
        split = m.data_currency({"asset": a, "user": dict(u, dagRunId="d0")}, ["asset", "user"])
        check("two pipeline runs: mergersSplit", split.get("mergersSplit"), True)
        unk = m.data_currency({"asset": a, "user": {"unknown": "no run"}}, ["asset", "user"])
        check("one table unknown: the answer is unknown, never current",
              (unk["class"], "ldgRebuiltUtc" in unk, "no run" in unk.get("reason", "")), ("unknown", False, True))
        check("a table the answer doesn't use doesn't taint it",
              m.data_currency({"asset": a, "user": {"unknown": "x"}}, ["asset"])["class"], "current")
        check("an empty stamp is unknown", m.data_currency({}, ["asset"])["class"], "unknown")
        same = m.data_currency({"asset": a}, ["asset"], after={"asset": dict(a)})
        check("re-read unchanged: current", same["class"], "current")
        moved = m.data_currency({"asset": a}, ["asset"], after={"asset": {"rebuiltUtc": "2026-09-24T14:05:00Z"}})
        check("rebuilt mid-query: unknown with rebuildDuringQuery",
              (moved["class"], moved.get("rebuildDuringQuery"), "Re-run" in moved.get("reason", "")),
              ("unknown", True, True))
        unread = m.data_currency({"asset": a}, ["asset"], after={"asset": {"unknown": "HTTP 500"}})
        check("re-read failed: unknown (a mid-query rebuild can't be ruled out)", unread["class"], "unknown")
        check("unexpected mergers travel into the block",
              m.data_currency({"asset": a, "unexpectedMergers": ["X"]}, ["asset"]).get("unexpectedMergers"), ["X"])
        check("label form: absolute, 24-hour, UTC spelled out",
              m.currency_label_utc(ISO0), "2026-09-24 10:12 UTC")

        # --- with_currency ---------------------------------------------------------------------
        reads = []
        stamp = {"asset": dict(a), "user": dict(u)}
        m.ldg_rebuild = lambda: reads.append(1) or stamp
        got = m.with_currency(["asset"], lambda: {"rows": []})
        check("with_currency stamps the result, one read", (got["dataCurrency"]["class"], len(reads)), ("current", 1))
        reads.clear()
        m.with_currency(["asset"], lambda: {"rows": []}, straddle=True)
        check("straddle re-reads after the query", len(reads), 2)
        try:
            m.with_currency(["asset"], lambda: (_ for _ in ()).throw(ValueError("boom")))
            raised = False
        except ValueError:
            raised = True
        check("a failing verb still fails (not swallowed into a stamped non-answer)", raised, True)

        # --- every data verb emits dataCurrency ---------------------------------------------------
        m.top_n = lambda *a_, **k: {"top": []}
        m.list_records = lambda a_: {"rows": [], "totalRecords": 0}
        m.summarize_by = lambda *a_, **k: {"groups": [], "rebuiltSeen": k.get("rebuilt")}
        m.stack_metrics = lambda: {"metrics": {"assetCount": 1, "avg30DaysAssetCount": 1}}
        m.build_profile = lambda *a_, **k: {"type": "user", "stability": {}}
        m.hr_sources = lambda **k: {"state": "has_data"}
        m.summarize_connectors = lambda *a_, **k: {"summary": {}}
        m.diagnose_connection = lambda: {"state": "connected"}
        m.take_snapshot = lambda **k: {"written": True}

        class NS:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        def out_of(fn, ns):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                fn(ns)
            return json.loads(buf.getvalue())

        verbs = {
            "top": (m.cmd_top, NS(table="asset", field="Risk_Score", top=5, where=None, select=None)),
            "list": (m.cmd_list, NS(table="asset", where=None, select=None, all=False, limit=50, count_only=False)),
            "list --count-only": (m.cmd_list, NS(table="user", where=None, select=None, all=False, limit=50, count_only=True)),
            "summary --by": (m.cmd_summary, NS(metrics=False, by="Risk_Level", table="asset", where=None)),
            "summary --metrics": (m.cmd_summary, NS(metrics=True, by=None, table="asset", where=None)),
            "profile": (m.cmd_profile, NS(name="x", type="user")),
            "compare": (m.cmd_compare, NS(name1="x", name2="y", type="user")),
            "hr": (m.cmd_hr, NS(refresh=False)),
            "connectors": (m.cmd_connectors, NS(max_failures=12, max_other=15, full=False, max_warnings=12,
                                                max_detail=10, refresh=False)),
            "connect": (m.cmd_connect, NS(with_connectors=False)),
            "connect --with-connectors": (m.cmd_connect, NS(with_connectors=True, refresh=False, coverage_full=True)),
            "digest": (m.cmd_digest, NS(table="asset", by=None, field="Risk_Score", top=5, snapshot=False)),
            "asof": (m.cmd_asof, NS()),
        }
        outs = {}
        for label, (fn, ns) in verbs.items():
            outs[label] = out_of(fn, ns)
            check("%s carries dataCurrency, current" % label,
                  (outs[label].get("dataCurrency") or {}).get("class"), "current")
        check("summary --by keys its cache on the stamp", outs["summary --by"].get("rebuiltSeen"), ISO0)
        check("summary --metrics labels the 30-day averages historical",
              (outs["summary --metrics"]["dataCurrency"].get("sections") or {})
              .get("metrics.avg30DaysAssetCount", {}).get("class"), "historical")
        check("a user profile labels its change-log verdict historical",
              outs["profile"]["dataCurrency"].get("sections", {}).get("stability", {}).get("class"), "historical")
        check("compare labels both sides' change-log verdicts",
              sorted(outs["compare"]["dataCurrency"].get("sections", {})),
              ["profileA.stability", "profileB.stability"])
        check("digest labels its headline averages historical",
              "headline.assets30DayAvg" in outs["digest"]["dataCurrency"].get("sections", {}), True)
        check("asof speaks the label form",
              outs["asof"].get("message"), "Data as of 2026-09-24 10:12 UTC (latest Meridian rebuild)")
        m.diagnose_connection = lambda: {"state": "auth_error"}
        check("a failed connect carries no currency claim",
              "dataCurrency" in out_of(m.cmd_connect, NS(with_connectors=False)), False)

        # summary --by: a cache hit made no calls, so there is nothing to straddle
        reads.clear()
        m.summarize_by = lambda *a_, **k: {"groups": [], "fromCache": True}
        out_of(m.cmd_summary, NS(metrics=False, by="Risk_Level", table="asset", where=None))
        check("a cached breakdown reads the stamp once", len(reads), 1)
        reads.clear()
        m.summarize_by = lambda *a_, **k: {"groups": []}
        out_of(m.cmd_summary, NS(metrics=False, by="Risk_Level", table="asset", where=None))
        check("a computed breakdown re-reads it after", len(reads), 2)

        # Every top-level verb is classified, so a new one fails here until someone decides.
        src = open(MERIDIAN_PY, encoding="utf-8").read()
        declared = set(re.findall(r'\bsub\.add_parser\("([a-z-]+)"', src))
        stamped = {"top", "list", "summary", "profile", "compare", "hr", "connectors", "connect", "digest", "asof"}
        # Not LDG reads: config, metadata, diagnostics, updates, rendering of an input file, and the raw
        # passthrough (SKILL.md treats `api` output as currency-unknown). The local-history verbs are
        # historical by definition and get their own labels in phase 2 of design/data-currency.md.
        not_ldg = {"api", "refresh-fields", "labels", "check", "selfupdate", "stacks", "report"}
        history = {"snapshot", "snapshots", "trend", "metrics", "alerts"}
        check("every verb is classified for data currency",
              sorted(declared - stamped - not_ldg - history), [])
        check("...and nothing classified has been removed", sorted((stamped | not_ldg | history) - declared), [])
    finally:
        for n, v in real.items():
            setattr(m, n, v)
        if real_env is None:
            os.environ.pop("MERIDIAN_NO_CACHE", None)
        else:
            os.environ["MERIDIAN_NO_CACHE"] = real_env
        shutil.rmtree(tmp, ignore_errors=True)


def test_live_currency(m):
    """The facts design/data-currency.md rests on, re-checked on whichever stack the suite runs against.

    The merger-name check is a FAILING TEST BY DESIGN. The names are stable by platform contract, so
    an ML-ENGINE run under any other name means the platform changed and LDG_MERGERS needs a release.
    Don't add a tolerance or a skip: this failing is the whole mechanism, and CLAUDE.md makes `--live`
    a required release step so a rename is caught before a release ships rather than by a user.
    """
    print("[52] live data currency: merger names, rebuild = LDG (needs a configured, reachable stack)")
    proc = subprocess.run([sys.executable, MERIDIAN_PY, "asof"], capture_output=True, text=True, timeout=120)
    out = json.loads(proc.stdout)
    cur = out.get("dataCurrency") or {}
    check("asof: current, both tables stamped", (cur.get("class"), sorted(cur.get("ldgRebuiltUtc") or {})),
          ("current", ["asset", "user"]))
    iso = r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$"
    check("...as ISO UTC", all(re.match(iso, v or "") for v in (cur.get("ldgRebuiltUtc") or {}).values()), True)

    rows = []
    for p in range(m.LDG_REBUILD_MAX_PAGES):
        r = m.call("GET", "/CMDB/v2/system/metrics/connector?size=%d&page=%d&sort=_time%%2Cdesc"
                   % (m.LDG_REBUILD_PAGE_SIZE, p), retries=0)
        rows += [x for x in (r.get("content") or []) if x.get("platform") == "ML-ENGINE"]
    pinned = {n for _, n in m.LDG_MERGERS}
    seen = {x.get("bridge_name") for x in rows}
    check("every ML-ENGINE run carries a pinned merger name (a rename needs a skill release)",
          sorted(str(s) for s in seen - pinned), [])
    check("...and both pinned mergers are present", sorted(pinned - seen), [])

    # Rebuild = LDG: the newest non-failed merger's output equals the LDG's own current count.
    metrics = m.call("GET", "/CMDB/v2/system/metrics/data", retries=0)
    for table, name, key in (("asset", m.LDG_MERGERS[0][1], "assetCount"), ("user", m.LDG_MERGERS[1][1], "userCount")):
        good = [x for x in rows if x.get("bridge_name") == name and m._run_health(x.get("status")) not in ("fail", "unknown")]
        good.sort(key=lambda x: m._merger_end(x)[0] or 0, reverse=True)
        check("%s merger output_records == the LDG's %s" % (table, key),
              good[0].get("output_records") if good else None, metrics.get(key))


def test_historical_labels(m):
    """History-reading verbs say they are historical, and a trend's data point is an LDG rebuild.

    Phase 2 of design/data-currency.md. The load-bearing claims: (1) a history with no rebuild stamps
    -- all of it before v2.27.0 -- behaves exactly as it did, checked against a reference copy of the
    old per-date rule rather than a restatement of it; (2) a stamped once-a-day history gives the same
    trend; (3) the two cases the old rule got wrong are now right -- several rebuilds on one date are
    several points, and one rebuild seen on two dates is ONE point, not a flat segment; (4) nothing read
    back from local snapshots is ever labelled current.
    """
    print("[53] historical labels + rebuild-keyed trend points (offline)")
    import contextlib
    import io
    import tempfile

    def stamped(rec, *utc):
        rec = dict(rec)
        rec["ldgRebuiltUtc"] = {"asset": utc[0], "user": utc[-1]}
        return rec

    def old_rule(recs):
        """design/trends.md's rule before v2.27.0, verbatim in effect: one point per stackDate, last
        taken wins. Kept here so 'unchanged for old history' is checked, not asserted."""
        by = {}
        for r in recs:
            dd = r.get("stackDate")
            if dd and (dd not in by or (r.get("takenAt") or "") >= (by[dd].get("takenAt") or "")):
                by[dd] = r
        return by

    def strip(t):
        t = json.loads(json.dumps(t))
        t.pop("dataCurrency", None)
        for end in ("from", "to"):
            (t.get(end) or {}).pop("ldgRebuiltUtc", None)
        return t

    # --- (1) unstamped history is untouched ------------------------------------------------------
    unstamped = [_snap("2026-09-01", taken="2026-09-01T08:00:00Z"),
                 _snap("2026-09-01", assets=34300, taken="2026-09-01T20:00:00Z"),
                 _snap("2026-09-02", assets=34400), _snap("2026-09-04", assets=34500)]
    got, _ = m._usable_snapshots(unstamped)
    ref = old_rule(unstamped)
    check("unstamped history: the same points as the old per-date rule", sorted(got), sorted(ref))
    check("...holding the same records (last taken wins)",
          all(got[k] is ref[k] for k in ref), True)

    # --- (2) a stamped once-a-day history gives the same trend -------------------------------------
    daily = [_snap("2026-09-%02d" % dd, assets=34000 + 50 * dd) for dd in range(1, 8)]
    daily_st = [stamped(r, "%sT10:12:12Z" % r["stackDate"]) for r in daily]
    daily_st.insert(3, stamped(_snap("2026-09-03", assets=1, taken="2026-09-03T01:00:00Z"),
                               "2026-09-03T10:12:12Z"))   # an earlier re-run of the same rebuild
    daily.insert(3, _snap("2026-09-03", assets=1, taken="2026-09-03T01:00:00Z"))
    for since in (None, "2026-09-03"):
        check("daily stack, since=%s: stamped trend == unstamped trend" % since,
              strip(m.compute_trend(daily_st, since=since)), strip(m.compute_trend(daily, since=since)))
    t = m.compute_trend(daily_st)
    check("...and the endpoints name their rebuilds",
          ((t["from"].get("ldgRebuiltUtc") or {}).get("asset"), (t["to"].get("ldgRebuiltUtc") or {}).get("asset")),
          ("2026-09-01T10:12:12Z", "2026-09-07T10:12:12Z"))

    # --- (3a) a 4-hour stack: several genuine points on one date ------------------------------------
    four = [stamped(_snap("2026-09-10", assets=30000 + i, taken="2026-09-10T%02d:30:00Z" % (h + 1)),
                    "2026-09-10T%02d:00:00Z" % h) for i, h in enumerate((2, 6, 10))]
    pts, _ = m._usable_snapshots(four)
    check("three rebuilds on one date are three points", len(pts), 3)
    check("...labelled so they sort in time order", sorted(pts),
          ["2026-09-10 rebuild 2026-09-10T02:00:00Z", "2026-09-10 rebuild 2026-09-10T06:00:00Z",
           "2026-09-10 rebuild 2026-09-10T10:00:00Z"])
    t = m.compute_trend(four)
    check("...so a same-day trend is answerable, not insufficientHistory",
          (t.get("insufficientHistory"), t.get("dataPoints"), t.get("distinctStackDates")), (None, 3, 1))
    check("...comparing the first rebuild with the last",
          next(r["change"] for r in t["changes"] if r.get("kind") == "total" and r.get("name") == "assets"), 2)
    t = m.compute_trend(four, since="2026-09-10")
    check("a --since date still selects every point on that date", t.get("dataPoints"), 3)
    check("the insufficient-history refusal counts points too",
          m.compute_trend(four[:1]).get("dataPoints"), 1)

    # --- (3b) one rebuild on two dates is ONE point -----------------------------------------------------
    late = stamped(_snap("2026-09-11", assets=30002, taken="2026-09-11T00:40:00Z"), "2026-09-10T10:00:00Z")
    pts, _ = m._usable_snapshots([four[2], late])
    check("a snapshot after midnight, before that day's merge, is the same point", len(pts), 1)
    check("...so there is no manufactured flat segment to compare",
          m.compute_trend([four[2], late]).get("insufficientHistory"), True)
    check("...where the old rule saw two dates", len(old_rule([four[2], late])), 2)
    check("two snapshots of one rebuild on one date: one point, last taken wins",
          [r["totals"]["assets"] for r in m._usable_snapshots(
              [stamped(_snap("2026-09-12", assets=1, taken="2026-09-12T11:00:00Z"), "2026-09-12T10:00:00Z"),
               stamped(_snap("2026-09-12", assets=2, taken="2026-09-12T12:00:00Z"), "2026-09-12T10:00:00Z")])[0].values()],
          [2])
    mixed = [stamped(_snap("2026-09-13", assets=1, taken="2026-09-13T11:00:00Z"), "2026-09-13T10:00:00Z"),
             _snap("2026-09-13", assets=2, taken="2026-09-13T15:00:00Z")]
    check("a date with any unstamped record keeps the old rule (upgrade day)",
          [r["totals"]["assets"] for r in m._usable_snapshots(mixed)[0].values()], [2])
    check("a half-empty stamp is no stamp", m._rebuild_key({"ldgRebuiltUtc": {"asset": "x", "user": None}}), None)

    # --- (4) every history-reading output is labelled historical, never current ------------------------
    for label, t in (("computed", m.compute_trend(daily_st)), ("insufficient", m.compute_trend(daily_st[:1])),
                     ("unverifiable", m.compute_trend([_snap("2026-09-01", coverage=False), _snap("2026-09-02")]))):
        c = t.get("dataCurrency") or {}
        check("trend (%s) is historical, with its range" % label,
              (c.get("class"), c.get("source"), bool(c.get("from")), bool(c.get("to"))),
              ("historical", "local snapshots", True, True))
    check("an empty history is still historical, not current",
          m.compute_trend([]).get("dataCurrency", {}).get("class"), "historical")

    real = {n: getattr(m, n) for n in ("CFG_DIR", "load_config", "load_snapshots", "load_alerts",
                                       "ldg_rebuild", "name_entities", "add_metric", "measure_metric",
                                       "load_metrics")}
    tmp = tempfile.mkdtemp(prefix="hist-")
    try:
        m.CFG_DIR = tmp
        m.load_config = lambda: ("s.example", "tok", None)
        path = os.path.join(tmp, "snaps.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for r in daily_st:
                f.write(json.dumps(r) + "\n")
        s = m.snapshots_summary(path)
        check("snapshots list is historical, oldest to newest",
              (s["dataCurrency"]["class"], s["dataCurrency"]["from"], s["dataCurrency"]["to"]),
              ("historical", "2026-09-01", "2026-09-07"))
        check("...and counts the records that carry a rebuild stamp", s["withRebuildStamp"], len(daily_st))

        m.load_snapshots = lambda *a_, **k: (list(four), [])
        m.load_alerts = lambda: []
        v = m._current_alert_verdict()
        check("an alert verdict is historical (the latest SNAPSHOT, not the stack now)",
              v.get("dataCurrency", {}).get("class"), "historical")
        check("...and its window counts points: two same-day rebuilds are a 0-day window",
              (v["window"].get("dataPoints"), v["window"].get("days")), (2, 0))

        # --- snapshot records carry the rebuild they measured -----------------------------------------
        cur = {"class": "current", "ldgRebuiltUtc": {"asset": "2026-09-24T10:12:12Z", "user": "2026-09-24T10:12:12Z"}}
        dig = {"generated": "digest", "stack": "s.example", "metrics": {"metrics": {"date": "2026-09-24"}}}
        rec = m.snapshot_record(dict(dig, dataCurrency=cur))
        check("a snapshot records its rebuild", rec.get("ldgRebuiltUtc"), cur["ldgRebuiltUtc"])
        rec = m.snapshot_record(dict(dig, dataCurrency={"class": "unknown", "reason": "rebuilt mid-query"}))
        check("an unknown stamp is recorded as unknown, never guessed",
              ("ldgRebuiltUtc" in rec, rec.get("ldgRebuildUnknown")), (False, "rebuilt mid-query"))
        check("a digest with no currency block writes neither key",
              sorted(k for k in m.snapshot_record(dig) if k.startswith("ldgRebuil")), [])

        # --- trend --name-entities: live names stamped like any LDG read --------------------------------
        m.ldg_rebuild = lambda: {"asset": {"rebuiltUtc": "2026-09-24T10:12:12Z", "dagRunId": "d"},
                                 "user": {"rebuiltUtc": "2026-09-24T10:12:12Z", "dagRunId": "d"}}

        def fake_name(diff, limit=None):
            diff["names"] = {"abc": "HOST1"}
        m.name_entities = fake_name
        ent = {"comparable": True, "scope": "asset:Risk_Score:top:5"}
        m.load_snapshots = lambda *a_, **k: ([dict(daily_st[0], entities=ent), dict(daily_st[-1], entities=ent)], [])

        class TA:
            since = metric = table = by = format = out = None
            name_entities = True
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            m.cmd_trend(TA())
        tout = json.loads(buf.getvalue())
        sec = (tout.get("dataCurrency", {}).get("sections") or {}).get("entities.names") or {}
        check("trend --name-entities stays historical overall", tout["dataCurrency"]["class"], "historical")
        check("...while the live names are stamped current", (sec.get("class"), bool(sec.get("ldgRebuiltUtc"))),
              ("current", True))

        # --- metrics add --derived: the baseline is a live measurement ---------------------------------------
        m.add_metric = lambda *a_, **k: ({"name": "kev", "table": "asset"}, None)
        m.measure_metric = lambda rec_: {"name": "kev", "ok": True, "count": 455}
        m.load_metrics = lambda: [{"name": "kev"}]

        class MA:
            metrics_cmd, name, label, table, where, smart_label, derived = "add", "kev", "KEV", "asset", None, None, True
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            m.cmd_metrics(MA())
        check("a derived metric's baseline is stamped current",
              json.loads(buf.getvalue()).get("dataCurrency", {}).get("class"), "current")
    finally:
        for n_, v_ in real.items():
            setattr(m, n_, v_)
        shutil.rmtree(tmp, ignore_errors=True)

    # --- reports say which kind of data they show -----------------------------------------------------------
    one = "2026-09-24T10:12:12Z"
    check("report line, current", m._currency_line({"dataCurrency": {"class": "current",
                                                     "ldgRebuiltUtc": {"asset": one, "user": one}}}),
          "Data as of 2026-09-24 10:12 UTC (latest Meridian rebuild)")
    check("report line, split tables", m._currency_line({"dataCurrency": {"class": "current",
                                                          "ldgRebuiltUtc": {"asset": one, "user": "2026-09-23T10:11:24Z"}}}),
          "Assets as of 2026-09-24 10:12 UTC; Users as of 2026-09-23 10:11 UTC")
    check("report line, historical", m._currency_line({"dataCurrency": {"class": "historical",
                                                        "source": "local snapshots", "from": "2026-09-01", "to": "2026-09-24"}}),
          "Historical · local snapshots, 2026-09-01 to 2026-09-24")
    check("report line, unknown says so", m._currency_line({"dataCurrency": {"class": "unknown", "reason": "HTTP 403"}}),
          "Data currency could not be confirmed: HTTP 403")
    check("an input with no currency block says so, not nothing", m._currency_line({"rows": []}),
          "Data currency not recorded in this input")
    sub, _, _ = m._trend_html({"generated": "trend", "insufficientHistory": True, "snapshotsRead": 1})
    check("a trend report's subtitle leads with Historical", sub.startswith("Historical"), True)

    tmp = tempfile.mkdtemp(prefix="hist-report-")
    try:
        inp = os.path.join(tmp, "in.json")
        with open(inp, "w", encoding="utf-8") as f:
            json.dump({"table": "asset", "field": "Risk_Score", "matchedAtThreshold": 1, "totalInTail": 1,
                       "top": [{"Asset_Name": "X"}],
                       "dataCurrency": {"class": "current", "ldgRebuiltUtc": {"asset": one}}}, f)

        class R:
            input, out, title, date, html = [inp], os.path.join(tmp, "r.html"), "T", "2026-09-24", True
        real_lc = m.load_config
        m.load_config = lambda: ("s.example", "tok", None)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                m.cmd_report(R())
        finally:
            m.load_config = real_lc
        html = open(os.path.join(tmp, "r.html"), encoding="utf-8").read()
        check("the rendered report carries the currency line",
              "Data as of 2026-09-24 10:12 UTC (latest Meridian rebuild)" in html, True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


CURRENCY_RULES = (
    # (what the assertion protects, text that must survive, whitespace-normalised)
    ("the connected template carries the rebuild", "data as of **{rebuiltUtc}** (latest Meridian rebuild)"),
    ("...and says what to write when it's unknown", "(data currency could not be confirmed: {reason})"),
    ("...and names it the baseline for later answers", "baseline every later answer's rebuild is compared against"),
    ("rule 1: stamp every data answer", "**Stamp every data answer**"),
    ("...in the approved label form", "_Data as of 2026-09-24 10:12 UTC (latest Meridian rebuild) · company.lucidum.cloud_"),
    ("...UTC spelled out", "`UTC` spelled out"),
    ("...never relative", 'Never write "2 hours ago"'),
    ("...never local time", "never convert to local time"),
    ("...split tables give both times", "With `mergersSplit`, give both times"),
    ("...a failed newest rebuild is said", "With `lastRebuildFailed`"),
    ("...unknown is said as unknown", '**"Data currency could not be confirmed"**'),
    ("...and never replaced by the query time", "never put the query time in its place"),
    ("rule 2: historical labelled at the point of use", "**Label historical data every time it's used, where it's used**"),
    ("...with one consistent marker", "**🕘 Historical**"),
    ("...covering the sections map", "every path listed in `dataCurrency.sections`"),
    ("...and mixed answers label each part", "Label each part of an answer that mixes current and historical data"),
    ("rule 3: a rebuild between answers is said first", "say so **before** the new answer"),
    ("...and earlier figures become superseded", "**superseded**"),
    ("...and are referred to with their rebuild", "Meridian has rebuilt since"),
    ("rule 4: never mix rebuilds in one figure", "**Never derive one figure from two rebuilds.**"),
    ("rule 5: asof before reusing earlier rows", "**Before answering from Meridian rows already in the conversation, run `meridian.py asof`**"),
    ("rule 6: a straddled query is not an answer", "**`rebuildDuringQuery: true` is not an answer.**"),
    ("rule 7: api output is currency-unknown", "**`api` output carries no stamp.**"),
    ("the 30-day average arrow is labelled historical", "The 30-day average is **historical**, so label it"),
    ("the verb table routes to asof", "`meridian.py asof`: one call. If the rebuild moved"),
)


def test_currency_routing(m):
    """SKILL.md carries the data-currency rules, and its labels match what the code produces.

    Phase 3 of design/data-currency.md. SKILL.md is prose the model follows, so no behaviour test can
    notice a rule being dropped from it -- the same gap test_alert_routing and test_hr_routing exist
    for. CLAUDE.md warns that a guard authored alongside the rules it guards proves little on its own,
    so each entry in CURRENCY_RULES was also shown to FAIL with its text deleted from a copy of
    SKILL.md (recorded in the PR). Matched whitespace-normalised: these are rules, not a display
    template, so a reflow must not fail the build.

    The label cross-check is the other half: if SKILL.md told the model to write one form and the
    report or `asof` printed another, a user would see two "as of" lines disagree about the same data.
    """
    print("[54] SKILL.md carries the data-currency rules, in the labels the code prints")
    flat = " ".join(open(SKILL_MD, encoding="utf-8").read().split())
    check("SKILL.md has the data-currency section", "### 4.1 Current or historical: say which, every time" in flat, True)
    for what, text in CURRENCY_RULES:
        check(what, " ".join(text.split()) in flat, True)
    one = "2026-09-24T10:12:12Z"
    check("the label form SKILL.md prescribes is the one asof/reports print",
          m.currency_label_utc(one) in flat and
          m._currency_line({"dataCurrency": {"class": "current", "ldgRebuiltUtc": {"asset": one, "user": one}}}) in flat,
          True)
    hist = m._currency_line({"dataCurrency": {"class": "historical", "source": "local snapshots",
                                              "from": "2026-09-01", "to": "2026-09-24"}})
    check("...and the historical form matches too", hist in flat, True)
    ref = " ".join(open(os.path.join(os.path.dirname(SKILL_MD), "references", "scripts.md"),
                        encoding="utf-8").read().split())
    check("scripts.md documents asof and dataCurrency",
          "`asof` — **when the LDG was last rebuilt" in ref and "carries `dataCurrency`" in ref, True)


def test_live_connectors():
    print("[22] live connectors / data coverage (needs a configured, reachable stack)")
    proc = subprocess.run([sys.executable, MERIDIAN_PY, "connectors"],
                          capture_output=True, text=True, timeout=120)
    out = json.loads(proc.stdout)
    s = out.get("summary", {})
    check("profiles endpoint readable", out.get("fetched", {}).get("profiles"), "ok")
    check("ingestion endpoint readable", out.get("fetched", {}).get("ingestion"), "ok")
    check("connector rows match the tally", len(out.get("connectors", [])), s.get("connectorsEnabled"))
    check("health tally sums to the row count",
          s.get("healthy", 0) + s.get("degraded", 0) + s.get("failing", 0) + s.get("idle", 0),
          s.get("connectorsEnabled"))
    check("every row has a health verdict",
          all(c.get("health") in ("ok", "degraded", "failing", "idle") for c in out.get("connectors", [])), True)
    check("summary has a one-line message", bool(s.get("message")), True)
    # The profile endpoint returns connector credentials; none of it may survive extraction.
    blob = json.dumps(out)
    for leak in ("password", "field_metadata", "api_token", "field_display", "gAAAAA"):
        check("no %r in live output" % leak, leak in blob, False)


def _su_package(root, version, entry_body=None, extra=None, prefix="meridiancs/"):
    """Build a minimal but REAL skill package zip at `root`, and return its path.

    Minimal on purpose: the tests here are about the update machinery, and building the actual
    package would need a clean tree. `scripts/meridian.py` is a stub that answers --help, because
    that is precisely what apply_update's smoke test runs.
    """
    import zipfile
    body = entry_body if entry_body is not None else (
        "import argparse\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--marker')\n"
        "p.parse_args()\n")
    path = os.path.join(root, "meridiancs.v%s.skill.zip" % version)
    members = {
        prefix + "SKILL.md": "# stub skill %s\n" % version,
        prefix + "scripts/meridian.py": body,
        prefix + "VERSION.json": json.dumps({"schema": 1, "version": version, "commit": "deadbeef"}),
    }
    members.update(extra or {})
    with zipfile.ZipFile(path, "w") as z:
        for name, content in members.items():
            z.writestr(name, content)
    return path


def _su_install(root, version):
    """A directory that looks like a real packaged install: version-stamped, no repo, no CLAUDE.md."""
    d = os.path.join(root, "skills", "meridiancs")
    os.makedirs(os.path.join(d, "scripts"))
    with open(os.path.join(d, "VERSION.json"), "w", encoding="utf-8") as f:
        json.dump({"schema": 1, "version": version, "commit": "cafe1234"}, f)
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("# installed %s\n" % version)
    with open(os.path.join(d, "scripts", "meridian.py"), "w", encoding="utf-8") as f:
        f.write("print('old')\n")
    return d


class _SUEnv(object):
    """Point the module's install/config globals at a temp tree, and restore them afterwards.

    Everything here is a module global read at call time, so patching them is enough -- and it is
    mandatory: without it these tests would rewrite the operator's own ~/.meridian/.updatecheck and,
    far worse, run install_markers() against the real working tree.
    """
    NAMES = ("INSTALL_DIR", "INSTALL_REAL", "VERSION_PATH", "UPDATE_CHECK_PATH", "CFG_DIR", "CFG_PATH")

    def __init__(self, m, install_dir, tmp):
        self.m, self.saved = m, {n: getattr(m, n) for n in self.NAMES}
        cfg = os.path.join(tmp, "dot-meridian")
        m.INSTALL_DIR = install_dir
        m.INSTALL_REAL = os.path.realpath(install_dir)
        m.VERSION_PATH = os.path.join(m.INSTALL_REAL, "VERSION.json")
        m.CFG_DIR = cfg
        m.CFG_PATH = os.path.join(cfg, "config.json")
        m.UPDATE_CHECK_PATH = os.path.join(cfg, ".updatecheck")

    def restore(self):
        for n, v in self.saved.items():
            setattr(self.m, n, v)


def test_selfupdate(m):
    print("[26] self-update: version compare, dev guards, host allowlist, staged apply + rollback")

    # --- version parsing: never a guess -------------------------------------------------------
    for s, want in [("2.16.1", (2, 16, 1)), ("v2.16.1", (2, 16, 1)), (" 2.0.0 ", (2, 0, 0)),
                    ("2.16", None), ("2.16.1-rc1", None), ("v2.16.1.1", None), ("", None),
                    (None, None), ("latest", None), (2, None)]:
        check("parse_version(%r)" % s, m.parse_version(s), want)

    # --- repo pinning: selects a repo ON github.com, cannot introduce a host ------------------
    for val, want in [("jwood25/meridiancs", "jwood25/meridiancs"), ("cyderes/meridian-cs", "cyderes/meridian-cs"),
                      ("owner", None), ("a/b/c", None), ("../evil", None),
                      ("https://evil.example/x", None), ("owner/name?x=1", None),
                      ("owner name/repo", None), ("/leading", None)]:
        os.environ["MERIDIAN_UPDATE_REPO"] = val
        check("update_repo(%r)" % val, m.update_repo(), want)
    os.environ.pop("MERIDIAN_UPDATE_REPO", None)

    # The CONSTANT is what ships, and an empty one is a build that can never update itself:
    # self-update only works from a copy that already knows where to look, so shipping it blank
    # costs every user a second manual upgrade later. Asserted because nothing else would notice --
    # the feature would report `disabled` forever, which reads as a deliberate setting rather than
    # an omission. That is exactly how it shipped from v2.17.0 through v2.23.0.
    check("UPDATE_REPO is pinned to a repo", bool(m.UPDATE_REPO), True)
    check("... to a valid owner/name pair", bool(m.UPDATE_REPO_RE.match(m.UPDATE_REPO)), True)
    check("... which update_repo() returns with no override", m.update_repo(), m.UPDATE_REPO)
    # An EMPTY override is not an override: it falls through to the constant rather than switching
    # the feature off. MERIDIAN_NO_AUTOUPDATE is the off switch; this variable only redirects.
    os.environ["MERIDIAN_UPDATE_REPO"] = ""
    check("an empty override falls back to the constant", m.update_repo(), m.UPDATE_REPO)
    os.environ.pop("MERIDIAN_UPDATE_REPO", None)

    # --- host allowlist: the one gate between a release URL and executing its contents --------
    for url, ok in [("https://api.github.com/repos/a/b/releases/latest", True),
                    ("https://github.com/a/b/releases/download/v1.0.0/x.skill.zip", True),
                    ("https://objects.githubusercontent.com/blob/1", True),
                    ("https://release-assets.githubusercontent.com/x", True),
                    ("http://github.com/a/b", False),                    # no TLS
                    ("https://github.com.evil.example/a/b", False),      # suffix trick
                    ("https://githubusercontent.com.evil.example/x", False),
                    ("https://evil.example/github.com/x", False),
                    ("file:///etc/passwd", False), ("", False)]:
        check("_github_host(%r)" % url[:44], bool(m._github_host(url)), ok)

    with tempfile.TemporaryDirectory() as tmp:
        install = _su_install(tmp, "2.16.1")
        env = _SUEnv(m, install, tmp)
        os.environ["MERIDIAN_UPDATE_REPO"] = "jwood25/meridiancs-public"
        os.environ.pop("MERIDIAN_NO_AUTOUPDATE", None)
        saved_latest = m.latest_release
        try:
            # --- opt-out -------------------------------------------------------------------
            check("no config file is not an opt-out", m.autoupdate_disabled(), None)
            os.makedirs(m.CFG_DIR, exist_ok=True)
            with open(m.CFG_PATH, "w", encoding="utf-8") as f:
                json.dump({"fqdn": "x.example", "api_token": "t", "autoupdate": False}, f)
            check("config autoupdate:false opts out", bool(m.autoupdate_disabled()), True)
            check("... and check_update reports disabled", m.check_update()["state"], "disabled")
            with open(m.CFG_PATH, "w", encoding="utf-8") as f:
                json.dump({"fqdn": "x.example", "api_token": "t"}, f)
            os.environ["MERIDIAN_NO_AUTOUPDATE"] = "1"
            check("env kill switch opts out", bool(m.autoupdate_disabled()), True)
            os.environ["MERIDIAN_NO_AUTOUPDATE"] = "0"
            check("MERIDIAN_NO_AUTOUPDATE=0 does not", m.autoupdate_disabled(), None)
            os.environ.pop("MERIDIAN_NO_AUTOUPDATE", None)

            # --- install markers: any ONE of them vetoes an apply --------------------------
            check("a stamped package has no markers", m.install_markers(), [])
            for marker, mk in (("CLAUDE.md", lambda: open(os.path.join(install, "CLAUDE.md"), "w").close()),
                               (".git", lambda: os.makedirs(os.path.join(install, ".git")))):
                mk()
                check("%s alone blocks an update" % marker,
                      any(marker in r for r in m.install_markers()), True)
                p = os.path.join(install, marker)
                (shutil.rmtree if os.path.isdir(p) else os.remove)(p)
            check("markers clear again", m.install_markers(), [])
            os.rename(m.VERSION_PATH, m.VERSION_PATH + ".hidden")
            check("an unstamped copy is never updated",
                  any("VERSION.json" in r for r in m.install_markers()), True)
            check("... and check_update calls it dev", m.check_update()["state"], "dev")
            os.rename(m.VERSION_PATH + ".hidden", m.VERSION_PATH)
            with open(m.VERSION_PATH, "w", encoding="utf-8") as f:
                json.dump({"schema": 1, "version": "latest"}, f)   # unusable stamp
            check("an uncomparable version is not usable",
                  any("no usable version" in r for r in m.install_markers()), True)
            with open(m.VERSION_PATH, "w", encoding="utf-8") as f:
                json.dump({"schema": 1, "version": "2.16.1", "commit": "cafe1234"}, f)

            # --- states -------------------------------------------------------------------
            def fake(version, url="https://github.com/o/r/releases/download/v%s/meridiancs.v%s.skill.zip"):
                def _f(repo):
                    return {"version": version, "tag": "v" + version,
                            "assetName": "meridiancs.v%s.skill.zip" % version,
                            "assetUrl": url % (version, version) if "%s" in url else url,
                            "assetSize": 1234}
                return _f

            m.latest_release = fake("2.17.0")
            r = m.check_update(force=True)
            check("newer release -> outdated", r["state"], "outdated")
            check("... names both versions", (r["installedVersion"], r["latestVersion"]), ("2.16.1", "2.17.0"))
            m.latest_release = fake("2.16.1")
            check("same version -> current", m.check_update(force=True)["state"], "current")
            m.latest_release = fake("2.15.0")
            r = m.check_update(force=True)
            check("older release -> ahead, never a downgrade", r["state"], "ahead")

            # The rule this whole feature rests on: a failed check is `unknown`, NOT `current`.
            def boom(repo):
                raise OSError("[Errno 11001] getaddrinfo failed")
            m.latest_release = boom
            r = m.check_update(force=True)
            check("an unreachable check is unknown", r["state"], "unknown")
            check("... and never claims to be up to date", "up to date" in r["message"].lower(), False)
            check("... and carries the reason", "getaddrinfo" in r.get("detail", ""), True)

            # --- cache: cheap at launch, but a failure re-checks sooner --------------------
            m.latest_release = fake("2.17.0")
            m.check_update(force=True)
            calls = []
            def counted(repo):
                calls.append(repo)
                return fake("2.17.0")(repo)
            m.latest_release = counted
            r = m.check_update()
            check("a warm cache makes no network call", (r["checkedVia"], len(calls)), ("cache", 0))
            check("... and still reports outdated", r["state"], "outdated")
            check("force bypasses the cache", m.check_update(force=True)["checkedVia"], "network")
            calls[:] = []
            r = m.check_update(now=time.time() + m.UPDATE_CHECK_INTERVAL + 1)
            check("a stale cache re-checks", (r["checkedVia"], len(calls)), ("network", 1))
            # A cached `unknown` must not suppress the check for a day.
            m.latest_release = boom
            m.check_update(force=True)
            m.latest_release = counted
            calls[:] = []
            r = m.check_update(now=time.time() + m.UPDATE_RETRY_INTERVAL + 1)
            check("a cached failure retries within the day", (r["checkedVia"], r["state"]),
                  ("network", "outdated"))
            check("... having actually asked", len(calls), 1)

            # --- package validation: refuse anything that is not this skill ----------------
            import zipfile
            good = _su_package(tmp, "2.17.0")
            with zipfile.ZipFile(good) as zf:
                check("a well-formed package validates",
                      m._validate_package(zf, "2.17.0").get("version"), "2.17.0")

            def refuses(label, path, want_version="2.17.0", fragment=None):
                with zipfile.ZipFile(path) as zf:
                    try:
                        m._validate_package(zf, want_version)
                        check(label, "accepted", "refused")
                    except ValueError as e:
                        check(label, "refused", "refused")
                        if fragment:
                            check("  ... says why (%s)" % fragment, fragment in str(e), True)

            refuses("a version-mismatched package is refused", good, "2.18.0", "refusing the mismatch")
            refuses("a zip-slip member is refused",
                    _su_package(tmp, "9.0.1", extra={"meridiancs/../../evil.py": "x"}), "9.0.1",
                    "not a safe relative path")
            refuses("a backslash-escaping member is refused",
                    _su_package(tmp, "9.0.2", extra={"meridiancs/..\\..\\evil.py": "x"}), "9.0.2",
                    "not a safe relative path")
            refuses("a member outside the prefix is refused",
                    _su_package(tmp, "9.0.3", extra={"elsewhere/evil.py": "x"}), "9.0.3", "is outside")
            # Missing required members: built by hand so the required file is genuinely absent.
            for miss in ("SKILL.md", "scripts/meridian.py", "VERSION.json"):
                path = os.path.join(tmp, "missing-%s.skill.zip" % miss.replace("/", "-"))
                with zipfile.ZipFile(path, "w") as z:
                    for name, content in (("SKILL.md", "#"), ("scripts/meridian.py", "pass"),
                                          ("VERSION.json", '{"schema":1,"version":"9.0.4"}')):
                        if name != miss:
                            z.writestr("meridiancs/" + name, content)
                refuses("a package with no %s is refused" % miss, path, "9.0.4")

            # --- apply: staged, smoke-tested, swapped -------------------------------------
            def local_download(url, dest, src=None):
                shutil.copyfile(src, dest)
                return os.path.getsize(dest)

            saved_dl = m._download_asset
            try:
                m.latest_release = fake("2.17.0")
                m._download_asset = lambda url, dest: local_download(url, dest, good)
                res = m.apply_update(m.check_update(force=True))
                check("apply reports the new version", (res["applied"], res["toVersion"]),
                      (True, "2.17.0"))
                check("... and the version it replaced", res["fromVersion"], "2.16.1")
                check("... and warns the instructions lag the scripts",
                      "new session" in res["note"], True)
                check("the stamp on disk is the new version",
                      m.installed_version().get("version"), "2.17.0")
                check("the new SKILL.md landed",
                      "stub skill 2.17.0" in open(os.path.join(install, "SKILL.md"), encoding="utf-8").read(),
                      True)
                check("the old tree is not left behind",
                      os.path.exists(m.INSTALL_REAL + ".previous"), False)
                check("no staging directory is left behind",
                      [d for d in os.listdir(os.path.dirname(m.INSTALL_REAL))
                       if d.startswith(m.UPDATE_STAGING_PREFIX)], [])
                check("the next check reads current from cache",
                      m.check_update()["state"], "current")

                # --- rollback: a package that cannot run must not replace a working one ----
                broken = _su_package(tmp, "2.18.0", entry_body="def (:\n")   # syntax error
                m.latest_release = fake("2.18.0")
                m._download_asset = lambda url, dest: local_download(url, dest, broken)
                r = m.check_update(force=True)
                try:
                    m.apply_update(r)
                    check("a package that fails --help is refused", "applied", "refused")
                except ValueError as e:
                    check("a package that fails --help is refused", "refused", "refused")
                    check("... naming the smoke test", "--help" in str(e), True)
                check("the working install survives a rejected package",
                      m.installed_version().get("version"), "2.17.0")
                check("... intact, not half-written",
                      "stub skill 2.17.0" in open(os.path.join(install, "SKILL.md"), encoding="utf-8").read(),
                      True)
                check("... with no leftover backup", os.path.exists(m.INSTALL_REAL + ".previous"), False)
                check("... and no leftover staging dir",
                      [d for d in os.listdir(os.path.dirname(m.INSTALL_REAL))
                       if d.startswith(m.UPDATE_STAGING_PREFIX)], [])

                # A dev tree is refused by apply_update itself, not merely by cmd_selfupdate.
                with open(os.path.join(install, "CLAUDE.md"), "w") as f:
                    f.write("x")
                m.latest_release = fake("2.19.0")
                try:
                    m.apply_update(dict(m.check_update(force=True), state="outdated",
                                        latestVersion="2.19.0", assetUrl="https://github.com/o/r/x.zip"))
                    check("apply_update refuses a working tree", "applied", "refused")
                except ValueError as e:
                    check("apply_update refuses a working tree", "refused", "refused")
                    check("... naming CLAUDE.md", "CLAUDE.md" in str(e), True)
                os.remove(os.path.join(install, "CLAUDE.md"))
            finally:
                m._download_asset = saved_dl
        finally:
            m.latest_release = saved_latest
            env.restore()
            os.environ.pop("MERIDIAN_UPDATE_REPO", None)
            os.environ.pop("MERIDIAN_NO_AUTOUPDATE", None)

    # --- the launch command never fails a session -----------------------------------------
    for args in (["selfupdate"], ["selfupdate", "--check"], ["selfupdate", "--apply"]):
        r = subprocess.run([sys.executable, MERIDIAN_PY] + args, capture_output=True, text=True,
                           env=dict(os.environ, MERIDIAN_UPDATE_REPO="jwood25/does-not-exist-xyz"))
        check("`%s` exits 0" % " ".join(args), r.returncode, 0)
        try:
            out = json.loads(r.stdout)
        except Exception:
            out = {}
        # This working tree is a git checkout, so every state here must be dev -- and an --apply
        # against it must report refusal rather than doing anything at all.
        check("  ... state=dev from a checkout", out.get("state"), "dev")
        if "--apply" in args:
            check("  ... and applies nothing", out.get("applied"), False)


def test_package_stamp():
    print("[27] package carries a version stamp (and only a real one)")
    import zipfile
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(os.path.dirname(HERE), "scripts", "make-package.py")
        out = os.path.join(tmp, "stamped.skill.zip")
        r = subprocess.run([sys.executable, script, out, "--version", "9.9.9", "--allow-dirty"],
                           capture_output=True, text=True, cwd=os.path.dirname(HERE))
        check("build with --version succeeds", r.returncode, 0)
        with zipfile.ZipFile(out) as z:
            names = z.namelist()
            check("VERSION.json is packaged", "meridiancs/VERSION.json" in names, True)
            stamp = json.loads(z.read("meridiancs/VERSION.json").decode("utf-8"))
            check("... stamped with the built version", stamp.get("version"), "9.9.9")
            check("... and a commit", bool(stamp.get("commit")), True)
            check("... schema matches the reader", stamp.get("schema"), 1)
            # install_markers() treats CLAUDE.md as proof of a working tree, so a package containing
            # it would make every install permanently un-updatable.
            check("CLAUDE.md is still excluded", "meridiancs/CLAUDE.md" in names, False)
            check("SKILL.md is packaged", "meridiancs/SKILL.md" in names, True)
            # SKILL.md section 0 reads this at launch and echoes it verbatim, so a package
            # without it ships a skill that improvises its own welcome.
            check("the launch greeting is packaged",
                  "meridiancs/references/welcome.md" in names, True)
            # EXCLUDE_DIRS drops it: CI config is repo, not install.
            check(".github is not packaged",
                  [n for n in names if n.startswith("meridiancs/.github")], [])
            # Same reasoning one directory up: the lint hooks run ON the skill, and an
            # installed copy has no repo for them to check. They shipped in the first
            # build of this branch because EXCLUDE is a filename list and nothing
            # asserted the shape of what lands in a customer package.
            check("repo tooling configs are not packaged",
                  sorted(n for n in names if "pre-commit" in n or "markdownlint" in n), [])
            # Maintenance scripts run ON the skill and need a repo an install does not have.
            # Asserted as a FAMILY rather than one name at a time, because EXCLUDE is a filename
            # list -- the next generator or release helper added beside these would otherwise
            # ship in silence, which is how the lint configs and the .public.md variants both
            # reached a customer package.
            check("maintenance scripts are not packaged",
                  sorted(n.split("/")[-1] for n in names
                         if n.startswith("meridiancs/scripts/")
                         and (n.split("/")[-1].startswith(("make-", "publish-", "check-"))
                              or n.endswith("docout.py"))), [])
            # make-public.py's substitution sources, and the stamp recording when each was last
            # reviewed. A .public.md is a sanitised derivative of the reference doc beside it,
            # carrying illustrative round numbers where the internal one carries measured ones,
            # so packaging both puts a deliberately weaker near-duplicate next to the file
            # SKILL.md actually routes to. All five shipped in every release through v2.23.0 for
            # the reason directly above: EXCLUDE is a filename list, and until this assertion
            # nothing checked the shape of what lands in a customer package.
            check("public reference variants are not packaged",
                  sorted(n for n in names if n.endswith(".public.md")), [])
            check("... nor the review stamp that tracks them",
                  [n for n in names if n.endswith(".public-sync.json")], [])
            # GitHub reads these from the repository; in a skill folder they are dead weight.
            check("repository community docs are not packaged",
                  sorted(n for n in names if n.split("/")[-1] in ("SECURITY.md", "CONTRIBUTING.md")), [])

        # An unversioned one-off is stamped null rather than guessed -- and a null stamp is exactly
        # what install_markers() refuses to update from, so it cannot silently self-replace.
        out2 = os.path.join(tmp, "unversioned.skill.zip")
        r = subprocess.run([sys.executable, script, out2, "--allow-dirty"],
                           capture_output=True, text=True, cwd=os.path.dirname(HERE))
        check("an unversioned one-off still builds", r.returncode, 0)
        with zipfile.ZipFile(out2) as z:
            stamp = json.loads(z.read("meridiancs/VERSION.json").decode("utf-8"))
        check("... stamped version=null, never guessed", stamp.get("version"), None)
        m = load_meridian()
        check("... which parse_version refuses", m.parse_version(stamp.get("version")), None)

        # Determinism, which CI asserts for the release build, must survive the generated member.
        a = os.path.join(tmp, "det-a.skill.zip")
        b = os.path.join(tmp, "det-b.skill.zip")
        for path in (a, b):
            subprocess.run([sys.executable, script, path, "--version", "9.9.9", "--allow-dirty"],
                           capture_output=True, text=True, cwd=os.path.dirname(HERE))
        check("two builds of one tree are byte-identical",
              hashlib.sha256(open(a, "rb").read()).hexdigest()
              == hashlib.sha256(open(b, "rb").read()).hexdigest(), True)


def test_licensing():
    """The distributed package must be licence-compliant, not merely accompanied by a licence.

    OFL 1.1 section 2 requires the copyright notice and the licence to travel with the font in
    every copy. The fonts and their licence texts are separate files, so nothing else in this repo
    would notice them drifting apart -- a font added without its text, or an EXCLUDE entry that
    quietly drops one, both produce a package that ships fonts with no licence. That is the exact
    shape of failure this asserts away.
    """
    print("[28] package is licence-compliant, SBOM current")
    import zipfile
    root = os.path.dirname(HERE)
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(root, "scripts", "make-package.py")
        out = os.path.join(tmp, "lic.skill.zip")
        r = subprocess.run([sys.executable, script, out, "--version", "9.9.9", "--allow-dirty"],
                           capture_output=True, text=True, cwd=root)
        check("package builds", r.returncode, 0)
        with zipfile.ZipFile(out) as z:
            names = set(z.namelist())
            for member in ("LICENSE", "NOTICE", "sbom.cdx.json",
                           "assets/fonts/SpaceGrotesk-OFL.txt",
                           "assets/fonts/SpaceMono-OFL.txt"):
                check("packaged: %s" % member, "meridiancs/" + member in names, True)
            # A placeholder or truncated licence file satisfies "present" but not the OFL.
            lic = z.read("meridiancs/LICENSE").decode("utf-8")
            check("LICENSE is the Apache 2.0 text",
                  "Apache License" in lic and "Version 2.0, January 2004" in lic, True)
            check("... including the terms, not just the header",
                  "END OF TERMS AND CONDITIONS" in lic, True)
            for member in ("SpaceGrotesk", "SpaceMono"):
                ofl = z.read("meridiancs/assets/fonts/%s-OFL.txt" % member).decode("utf-8")
                check("%s ships the real OFL 1.1 text" % member,
                      "SIL OPEN FONT LICENSE Version 1.1" in ofl
                      and "PERMISSION & CONDITIONS" in ofl, True)
            # Every packaged font must have a licence text alongside it.
            fonts = [n for n in names if n.endswith(".ttf")]
            check("all four fonts are packaged", len(fonts), 4)

    # The SBOM is generated, so the committed copy can go stale silently. CI runs this too.
    sbom_script = os.path.join(root, "scripts", "make-sbom.py")
    r = subprocess.run([sys.executable, sbom_script, "--check"],
                       capture_output=True, text=True, cwd=root)
    check("committed sbom.cdx.json is current", r.returncode, 0)

    with open(os.path.join(root, "sbom.cdx.json"), encoding="utf-8") as f:
        sbom = json.load(f)
    check("SBOM declares no timestamp (determinism)", "timestamp" in sbom.get("metadata", {}), False)
    listed = {c["name"]: c for c in sbom["components"] if c["name"].endswith(".ttf")}
    check("SBOM lists all four fonts", len(listed), 4)
    for rel, comp in listed.items():
        digest = hashlib.sha256(open(os.path.join(root, rel), "rb").read()).hexdigest()
        recorded = [h["content"] for h in comp["hashes"] if h["alg"] == "SHA-256"]
        check("SBOM hash matches %s" % os.path.basename(rel), recorded, [digest])
        check("... declared OFL-1.1",
              [l["license"]["id"] for l in comp["licenses"]], ["OFL-1.1"])

    # design/oss-release.md has to NAME the identifiers it removed or the removal stops being
    # auditable, so the repo sweep exempts it -- safe only because design/ never ships. The gate
    # that matters is that make-public.py's audit reads SKIP_FILES and NOT the wider
    # REPO_SWEEP_SKIP, so flipping PUBLISH_INTERNAL_DOCS cannot smuggle those identifiers into a
    # public tree on an exemption written for a different purpose. Verified by hand: with the flag
    # flipped, the audit reports 7 problems in that file and refuses to publish.
    spec = importlib.util.spec_from_file_location(
        "pii_gate", os.path.join(root, "scripts", "check-docs-pii.py"))
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    check("internal-only exemption is disjoint from SKIP_FILES",
          bool(gate.INTERNAL_ONLY_FILES & gate.SKIP_FILES), False)
    check("... repo sweep skips the union of both",
          gate.REPO_SWEEP_SKIP, gate.SKIP_FILES | gate.INTERNAL_ONLY_FILES)
    check("... and the publication audit does not inherit it",
          bool(gate.INTERNAL_ONLY_FILES & gate.SKIP_FILES), False)

    # The repo-wide sweep is only as good as its patterns, and a false positive gets "fixed" by
    # exempting whatever tripped it -- which is how the sweep got narrow the first time. A
    # GitHub Actions ref (`pre-commit/action@v3.0.1`) reads as local@domain, so the email
    # pattern now requires an alphabetic TLD. Assert BOTH directions: silencing the noise must
    # not have silenced a real address, which is the failure that leaves no trace.
    import re as _re
    _email = gate.PATTERNS["email address"]
    for ref in ("pre-commit/action@v3.0.1", "actions/checkout@v4", "ruff-pre-commit@v0.16.4"):
        check("a version ref is not an email address (%s)" % ref,
              _re.findall(_email, ref), [])
    for addr in ("someone@example.com", "first.last+tag@mail.example.org",
                 "svc_account@corp.example"):
        check("...but a real address is still caught (%s)" % addr,
              bool(_re.findall(_email, addr)), True)


def test_brand_fallback(m):
    """A build with no Cyderes brand assets must not assert the brand in text either.

    This is what makes scripts/make-public.py's brand strip work by *absence* rather than by
    patching source: the public tree simply has no brand files. The trap it guards is that the
    fallbacks used to be typographic -- `_logo_svg` returned the literal word "cyderes" and the
    report footer named Cyderes unconditionally -- so an unbranded build still stamped the wordmark
    into every masthead and footer. Removing the artwork and keeping the words is the same
    trademark use, minus the artwork.
    """
    print("[29] report path degrades to unbranded with no brand assets")
    if not m._branded():
        print("  SKIP  unbranded tree - this asserts the fallback FROM a branded install")
        return
    real = m._assets_dir
    check("this repo IS branded", m._branded(), True)
    check("... so the footer names Cyderes", "Cyderes" in m._producer_note(), True)
    check("... and the wordmark is the real SVG", m._logo_svg().strip().startswith("<svg"), True)

    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "fonts"), exist_ok=True)
        m._assets_dir = lambda: tmp
        try:
            check("no brand assets -> not branded", m._branded(), False)
            check("... wordmark renders as nothing", m._logo_svg(), "")
            note = m._producer_note()
            check("... footer drops the brand name", "Cyderes" in note, False)
            check("... and still attributes the tool", "meridiancs" in note, True)
            css = m._load_css()
            check("... stylesheet falls back, no brand colour",
                  "#D4FC68" in css or "cybervolt" in css.lower(), False)
            # Meridian is the queried product: naming it is nominative, and the masthead needs it.
            check("... product slot still names Meridian", m._meridian_svg(), "Meridian")
            check("... and no font faces without the font files", m._font_face_css(), "")
        finally:
            m._assets_dir = real
    check("restored to the branded install", m._branded(), True)


def test_public_variants():
    """The reviewed public reference docs exist, are in sync, and carry no tenant data.

    Three separate failures are possible here and only the first is obvious:

      * a variant missing entirely -- make-public.py already refuses, so this is belt and braces;
      * a variant that has gone STALE because its internal source was edited. That one is silent by
        nature: the internal doc gains a new measured figure, the public one keeps saying something
        slightly false, and nothing looks broken. The sync stamp is what makes it loud, and CI is
        what makes the stamp mean anything;
      * a variant that still contains the measured figures it was written to replace.
    """
    print("[30] public reference variants exist, are in sync, and are sanitised")
    if derived_tree():
        print("  SKIP  derived public tree - the variants are its inputs, not its contents")
        return
    root = os.path.dirname(HERE)
    spec = importlib.util.spec_from_file_location(
        "mk_public", os.path.join(root, "scripts", "make-public.py"))
    mp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mp)

    for internal in sorted(mp.SUBSTITUTE_REQUIRED):
        variant = internal[:-3] + ".public.md"
        path = os.path.join(root, variant)
        check("exists: %s" % variant, os.path.exists(path), True)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            text = f.read()
        # A stub would satisfy "exists" while publishing nothing useful.
        check("... is substantial, not a stub", len(text) > 1500, True)
        for hit in mp.PII.known_identifiers(text):
            check("... carries no removed identifier (%s)" % hit, False, True)
        figures = [fig for fig, rx in mp.PII.FIGURE_PATTERNS if rx.search(text)]
        check("... carries no measured tenant figure", figures, [])

    # The stamp: every internal doc must match the digest its variant was reviewed against.
    stale = mp.stale_variants()
    check("all public variants are in sync with their source",
          [internal for internal, _r, _a in stale], [])

    # The data the guard depends on must actually be loadable, or every check above is vacuous.
    check("regression guard data is loaded", mp.PII.KNOWN_AVAILABLE, True)
    check("... with identifiers", len(mp.PII.KNOWN_REAL_IDENTIFIERS) > 0, True)
    check("... and figures", len(mp.PII.FIGURE_PATTERNS) > 0, True)

    # And it must not ship: design/ is dropped from the package and the public tree, which is the
    # only reason it is safe for that file to name them.
    keep, _subs, dropped, _missing = mp.plan(mp.tracked_files())
    check("identifier data is dropped from the public tree",
          "design/known-identifiers.json" in dropped, True)
    check("... and is not in the kept set", "design/known-identifiers.json" in keep, False)
    # Decided 2026-09-23, not defaulted: the internal record holds counsel's advice, and publishing
    # privileged advice risks waiving the privilege. See design/oss-release.md.
    check("PUBLISH_INTERNAL_DOCS stays False", mp.PUBLISH_INTERNAL_DOCS, False)
    check("... so the legal record is dropped from the public tree",
          sorted(f for f in ("design/oss-release.md", "design/legal-review-packet.md", "CLAUDE.md")
                 if f in dropped), ["CLAUDE.md", "design/legal-review-packet.md", "design/oss-release.md"])


def test_trademark_notice():
    """NOTICE asserts Cyderes ownership of Meridian, and no longer hedges it.

    Counsel confirmed on 2026-08-25 that Cyderes owns the Lucidum marks outright (October 2025
    merger), so three things that used to be in this file are now affirmatively wrong rather than
    merely cautious: attributing Lucidum to "its respective owner", framing the product names as
    nominative fair use, and disclaiming affiliation with the mark owner -- which post-merger is
    Cyderes disclaiming affiliation with itself.

    Asserted rather than reviewed because this is the one file in the repo whose wording IS the
    legal position. A well-meaning edit restoring the "independent API client" disclaimer would read
    as more careful and would in fact be a statement that the mark belongs to someone else.
    """
    print("[31] NOTICE trademark position matches counsel's answer")
    root = os.path.dirname(HERE)
    with open(os.path.join(root, "NOTICE"), encoding="utf-8") as f:
        notice = f.read()

    for phrase in ('"Cyderes", "Meridian" and "Lucidum"', "are trademarks of Cyderes",
                   "acquired Lucidum by merger", "sole and exclusive title",
                   "conveys no right to use"):
        check("NOTICE states %r" % phrase[:34], phrase in notice, True)
    # Apache section 6 framing must survive the rewrite: ownership is why there is something to
    # withhold, not a reason to stop withholding it.
    check("... and still withholds trademark rights",
          "does NOT" in notice and "section 6" in notice, True)
    check("... and still excludes brand assets from public builds",
          "NOT licensed for redistribution" in notice, True)

    for stale in ("respective owner", "nominative fair use", "independent API client",
                  "does not imply any"):
        check("NOTICE no longer says %r" % stale, stale in notice, False)

    # The copyright line is the legally operative statement and is not up for edit.
    check("NOTICE carries the approved copyright line", "Copyright 2026 Cyderes" in notice, True)

    # Authorship: counsel's direction on 2026-08-25 was to state the position rather than leave it
    # silent -- a "clean single-author" framing beside a history carrying Co-Authored-By trailers is
    # the inconsistency, not the trailers. So the section must EXIST, must name the tool assistance,
    # and must still not assert sole authorship. Both directions are asserted because either one
    # alone permits the failure: silence invites a reader to infer the clean-record framing, and an
    # over-claim contradicts a public git log.
    check("NOTICE has an authorship section", "AUTHORSHIP" in notice, True)
    for phrase in ("developed by Cyderes personnel", "assistance of AI", "Co-Authored-By",
                   "Copyright in the", "held by Cyderes"):
        check("... states %r" % phrase[:34], phrase in notice, True)
    check("... and denies a third-party claim rather than hiding the trailers",
          "does not" in notice and "third-party" in notice, True)
    for claim in ("single author", "no outside contributors", "sole author", "single-author"):
        check("NOTICE still makes no sole-authorship claim (%s)" % claim,
              claim.lower() in notice.lower(), False)

    # The statement has to match the repository it ships with: if the trailers were ever stripped
    # from the history, the NOTICE would describe a history that no longer exists.
    #
    # Only answerable with the history present. CI checks out at depth 1, so it is SKIPPED there and
    # says so -- not quietly passed. A check that cannot see its evidence has verified nothing, which
    # is the same rule the update checker follows when GitHub is unreachable. Giving CI a full clone
    # would make it run everywhere, at the cost of fetching 190 commits on every push for one
    # assertion; this is a pre-release and local guard instead, and worth having as one.
    depth = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=root,
                           capture_output=True, text=True)
    commits = int((depth.stdout or "").strip() or 0)
    if commits < 50:
        print("  SKIP  history trailer check - shallow clone (%d commit(s)), nothing to read"
              % commits)
    else:
        trailers = subprocess.run(["git", "log", "--format=%b"], cwd=root,
                                  capture_output=True, text=True, errors="replace")
        n = len(re.findall(r"(?im)^co-authored-by:.*claude", trailers.stdout or ""))
        check("the history still carries the trailers the NOTICE describes", n > 0, True)


def test_dangling_links():
    """The derived public tree carries no dangling reference-doc links.

    `references/nist-csf-mapping.md` and the `compliance` verb it documented were removed outright
    (not merely held back from the public build), so there is no methodology gate left to test and
    no dangling link from it either -- both were retired together rather than one being fixed and the
    other left as a landmine. README was the remaining case and is now substituted too: it linked
    two branded PDFs that every public build drops (`Cyderes-Meridian-Skill-Guide.pdf`,
    `Cyderes-MeridianCS-Brief.pdf`), and `README.public.md` links neither. So the expectation is
    an EMPTY set rather than a named allowance -- any dangling link is now a regression, which is
    a stronger assertion than the old one and needs no list to be kept up to date.
    """
    print("[32] derived public tree carries no dangling links")
    if derived_tree():
        print("  SKIP  derived public tree - deriving again has no sources to substitute from")
        return
    root = os.path.dirname(HERE)
    spec = importlib.util.spec_from_file_location(
        "mk_public2", os.path.join(root, "scripts", "make-public.py"))
    mp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mp)

    keep, _subs, dropped, missing = mp.plan(mp.tracked_files())
    check("no reference doc is missing its variant", missing, [])
    check("the CSF mapping is gone, not just dropped from the public tree",
          "references/nist-csf-mapping.md" in dropped, False)
    # The review ledger is machinery of the derivation. It records digests of internal docs
    # against variants the public tree has none of, so publishing it ships a ledger with nothing
    # left to review -- the same class as the variants themselves riding into the package.
    check("the sync stamp is dropped from the public tree", mp.SYNC_STAMP in dropped, True)
    check("... and is not in the kept set", mp.SYNC_STAMP in keep, False)

    # The audit must SAY what breaks. A derived tree is built for real: the check reads the output,
    # not the plan, which is the whole point of auditing post-conditions.
    import shutil as _shutil
    tmp = tempfile.mkdtemp(prefix="pubtree-")
    try:
        target = os.path.join(tmp, "tree")
        mp.copy_tree(keep, _subs, target)
        problems = mp.audit(target)
        dangling = [p for p in problems if "not in the public tree" in p]
        check("the audit reports no dangling link at all",
              sorted({p.split(" -> ")[-1] for p in dangling}), [])
        # A link the tree DOES satisfy must not be reported, or the check is noise and gets muted.
        # query-syntax.md is substituted from its .public.md variant, so it is present under its own
        # name -- which also proves the check reads the derived tree and not the source repo.
        resolves = os.path.join(target, "references", "query-syntax.md")
        check("a substituted doc is present in the tree", os.path.exists(resolves), True)
        # README ships from README.public.md under its own name. Checked by CONTENT, because
        # "README.md exists" is true either way -- the internal one would satisfy it while
        # carrying the brand prose and the two dead PDF links the substitution exists to remove.
        with open(os.path.join(target, "README.md"), encoding="utf-8") as _f:
            readme = _f.read()
        check("README is the public variant, not the internal one",
              "Cyderes-Meridian-Skill-Guide.pdf" in readme, False)
        check("... and still documents the install", "~/.claude/skills/meridiancs" in readme, True)
        check("... and is not reported as dangling",
              any("query-syntax" in p for p in dangling), False)
    finally:
        _shutil.rmtree(tmp, ignore_errors=True)


# Connector messages that echo exactly what the profile allow-list drops. The allow-list keeps
# `message` verbatim, so before scrubbing every one of these values reached the preflight output.
SCRUB_PROFILES = {"connectorProfiles": [
    {"display_name": "Microsoft Active Directory (AD)", "bridge_name": "ad_ldap", "profile_name": "Corp",
     "group": "Identity Access Management", "host": "10.20.30.40", "username": "CORP\\svc_ldap",
     "password": "gAAAAABscrubsecret",
     "proxy": {"host": "proxy.scrub.invalid", "port": 8080},
     "config": {"api_url": "https://apiuser:hunter22@api.scrub.example.com/v1", "region": "us-east-1"},
     "services_list": [
         {"service": "ad_user", "display_name": "AD User", "status": "FAIL", "activity": True,
          "message": "Login failed for user 'CORP\\svc_ldap' at 10.20.30.40 via proxy.scrub.invalid; "
                     "password=hunter22"},
         # A secret straddling the 200-char cap: truncating before scrubbing would keep half of it.
         {"service": "ad_computer", "display_name": "AD Computer", "status": "FAIL", "activity": True,
          "message": "A" * 195 + " 10.20.30.40 unreachable"}]},
    {"display_name": "Okta", "bridge_name": "okta", "profile_name": "Prod", "group": "Identity",
     "services_list": [
         # One connector's error quoting ANOTHER profile's host: every profile's values scrub every
         # message, not just its own.
         {"service": "okta_user", "display_name": "Okta User", "status": "FAIL", "activity": True,
          "message": "HTTPSConnectionPool(host='10.20.30.40', port=443): Max retries exceeded"}]},
]}
SCRUB_RUNS = {"content": [
    {"bridge_name": "okta_user", "platform": "api", "profile": "Prod", "status": "Error",
     "output_records": 10, "_utc": "2026-09-20T10:00:00.000+00:00", "_time": 1789900000,
     "event_messages": [{"ERROR": "request to https://api.scrub.example.com failed: "
                                  "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"}]},
    # A run whose `profile` is the whole embedded profile object, credentials included.
    {"bridge_name": "ad_user", "platform": "api",
     "profile": {"profile_name": "Corp", "config": {"username": "run_embedded_user"}},
     "status": "Error", "output_records": 0, "_utc": "2026-09-20T10:01:00.000+00:00",
     "_time": 1789900060,
     "event_messages": [{"ERROR": "bind as run_embedded_user rejected; token: tok_live_9f8e7d6c"}]},
]}


def test_security_hardening(m):
    """Fixes from the 2026-09-22 security review. Every check here was run against the pre-fix
    meridian.py and failed there -- a guard written alongside its fix proves nothing otherwise."""
    print("[33] security review fixes: message scrubbing, token channels, output paths (offline)")
    import contextlib, io

    # --- M3: connector messages are scrubbed of credential values ----------------------------------
    def fake_call(method, endpoint, body=None, retries=1):
        if "connector/profile" in endpoint:
            return SCRUB_PROFILES
        if "metrics/connector" in endpoint:
            return SCRUB_RUNS
        raise AssertionError("unexpected endpoint %r" % endpoint)

    real_call = m.call
    m.call = fake_call
    try:
        brief = m.summarize_connectors()
        full = m.summarize_connectors(brief=False)
    finally:
        m.call = real_call
    blob = json.dumps(brief) + json.dumps(full)
    for leak in ("svc_ldap", "10.20.30.40", "10.20.3", "proxy.scrub.invalid", "hunter22", "apiuser",
                 "api.scrub.example.com", "abcdefghijklmnop", "run_embedded_user", "tok_live_9f8e7d6c",
                 "gAAAAAB"):
        check("scrubbed from connector messages: %r" % leak, leak in blob, False)
    check("...replaced by a visible marker, not silently dropped", "[redacted]" in blob, True)
    check("...keeping the diagnostic text around it",
          "Login failed for user" in blob and "Max retries exceeded" in blob, True)

    # --- M2: the action token has a stdin channel too ----------------------------------------------
    tmp = tempfile.mkdtemp()
    real = (m.CFG_DIR, m.CFG_PATH, m.STACKS_PATH, m._CFG_CACHE)
    m.CFG_DIR = tmp
    m.CFG_PATH = os.path.join(tmp, "config.json")
    m.STACKS_PATH = os.path.join(tmp, "stacks.json")

    class A:
        name, fqdn, token, action_token = "sec", "sec.example.com", "-", "-"
    real_stdin = sys.stdin
    sys.stdin = io.StringIO("tok-api\ntok-action\n")
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            m.cmd_stacks_add(A())
        entry = m.load_stacks()["stacks"]["sec"]
        check("--token - and --action-token - read two stdin lines, in order",
              [entry.get("api_token"), entry.get("action_token")], ["tok-api", "tok-action"])
    except SystemExit:
        check("--action-token - is accepted", False, True)
    finally:
        sys.stdin = real_stdin
        m.CFG_DIR, m.CFG_PATH, m.STACKS_PATH, m._CFG_CACHE = real
        shutil.rmtree(tmp, ignore_errors=True)

    # SKILL.md must route saving through the script. A model writing config.json itself skips the
    # 0600 write and drops entity_salt, and `--token '<token>'` puts the token in argv.
    with open(os.path.join(os.path.dirname(HERE), "SKILL.md"), encoding="utf-8") as f:
        skill = re.sub(r"\s+", " ", f.read())
    check("SKILL.md no longer tells the model to write config.json by hand",
          "On yes, write the file" in skill, False)
    check("...and saves through stacks add with the token on stdin",
          "never write `config.json` yourself" in skill and "--token -" in skill, True)
    check("...and shows no token-in-argv example", "--token '<token>'" in skill, False)

    # --- L3: output paths are checked before anything is written -----------------------------------
    work = tempfile.mkdtemp()
    # A HOME with no ~/.meridian and blank credential env vars: if the guard ever regresses, the
    # subprocess dies "not configured" instead of querying whatever stack this machine has saved --
    # which is exactly what the first draft of this check did against the pre-fix code.
    iso = dict(os.environ, HOME=work, USERPROFILE=work, MERIDIAN_FQDN="", MERIDIAN_API_TOKEN="",
               MERIDIAN_ACTION_TOKEN="")
    try:
        victim = os.path.join(work, "victim.py")
        with open(victim, "w", encoding="utf-8") as f:
            f.write("print('original')\n")
        inp = os.path.join(work, "in.json")
        with open(inp, "w", encoding="utf-8") as f:
            json.dump({"table": "asset", "totalRecords": 0, "rows": []}, f)
        r = subprocess.run([sys.executable, MERIDIAN_PY, "report", "--html", "--input", inp,
                            "--out", victim], capture_output=True, text=True, encoding="utf-8", env=iso)
        with open(victim, encoding="utf-8") as f:
            check("report --out refuses a non-report suffix (the script survives)",
                  f.read() == "print('original')\n", True)
        check("...exiting 2 with the reason", (r.returncode, "must end in" in r.stderr), (2, True))
        with open(victim, "w", encoding="utf-8") as f:   # independent of the check above
            f.write("print('original')\n")
        r = subprocess.run([sys.executable, MERIDIAN_PY, "list", "--format", "csv",
                            "--out", victim], capture_output=True, text=True, encoding="utf-8",
                           env=iso)
        with open(victim, encoding="utf-8") as f:
            check("--format csv --out refuses a non-.csv target", f.read() == "print('original')\n", True)
        check("...before any credential or API work", "must end in" in r.stderr, True)
        r = subprocess.run([sys.executable, MERIDIAN_PY, "report", "--html", "--input", inp,
                            "--out", os.path.join(work, "missing", "r.html")],
                           capture_output=True, text=True, encoding="utf-8", env=iso)
        check("report --out into a missing directory is refused", r.returncode, 2)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # --- L1: ~/.meridian is tightened to 0700 even when something else created it --------------------
    if os.name != "nt":
        tmp = tempfile.mkdtemp()
        os.chmod(tmp, 0o755)
        real = (m.CFG_DIR, m.CFG_PATH, m.STACKS_PATH, m._CFG_CACHE)
        m.CFG_DIR = tmp
        m.CFG_PATH = os.path.join(tmp, "config.json")
        m.STACKS_PATH = os.path.join(tmp, "stacks.json")
        m._CFG_DIR_CHECKED = False
        try:
            m.save_stacks({"active": None, "stacks": {}})
            check("a 0755 ~/.meridian is tightened to 0700", oct(os.stat(tmp).st_mode & 0o777), "0o700")
        finally:
            m.CFG_DIR, m.CFG_PATH, m.STACKS_PATH, m._CFG_CACHE = real
            m._CFG_DIR_CHECKED = False
            shutil.rmtree(tmp, ignore_errors=True)

        # prune_snapshots' fixed-name temp file must not follow a planted symlink.
        tmp = tempfile.mkdtemp()
        try:
            hist = os.path.join(tmp, "snapshots.jsonl")
            with open(hist, "w", encoding="utf-8") as f:
                for i in range(3):
                    f.write(json.dumps({"schema": m.SNAPSHOT_SCHEMA, "n": i}) + "\n")
            victim = os.path.join(tmp, "victim.txt")
            with open(victim, "w", encoding="utf-8") as f:
                f.write("untouched")
            os.symlink(victim, hist + ".tmp")
            m.prune_snapshots(keep=1, path=hist)
            with open(victim, encoding="utf-8") as f:
                check("prune's temp file does not write through a planted symlink", f.read(), "untouched")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    else:
        print("  SKIP  POSIX permission checks (Windows)")

    # --- L7: alert delivery ------------------------------------------------------------------------
    try:
        m._post_webhook("http://hooks.example/slack", {"text": "x"})
        check("a plain-http webhook URL is refused", "sent", "refused")
    except ValueError:
        check("a plain-http webhook URL is refused", "refused", "refused")
    except Exception as e:   # the pre-fix code tries to connect instead
        check("a plain-http webhook URL is refused", type(e).__name__, "ValueError")

    class FakeSMTP:
        log = []
        def __init__(self, host, port, timeout=None): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, context=None): FakeSMTP.log.append("starttls")
        def login(self, user, password): FakeSMTP.log.append("login")
        def send_message(self, msg): FakeSMTP.log.append("sent")

    m._SMTP_CLIENT = FakeSMTP
    try:
        try:
            m._send_email({"host": "smtp.example", "port": 25, "from": "a@example.com",
                           "to": ["b@example.com"], "user": "u", "password": "p", "starttls": False},
                          "s", "t", "<p>h</p>")
        except ValueError:
            pass
        check("SMTP login without TLS is refused, before connecting", FakeSMTP.log, [])
    finally:
        m._SMTP_CLIENT = None

    v = {"firing": [{"rule": "<!channel> ping", "message": "see <https://evil.example|your bank>"}],
         "clear": [], "unevaluable": [], "stack": "demo",
         "summary": {"firing": 1, "unevaluable": 0, "clear": 0}}
    try:
        text = m.render_alerts_slack(v)["text"]
        check("Slack control sequences from rule text are escaped",
              ("<!channel>" in text, "<https://evil" in text, "&lt;!channel&gt;" in text),
              (False, False, True))
    except Exception as e:
        check("Slack renderer handles the fixture", type(e).__name__, None)


def test_selfupdate_held_dir(m):
    """An install directory some process is sitting in must still update.

    Windows refuses to rename a directory that is any process's working directory, and the likeliest
    such process is the assistant's own shell after `cd <skill dir>`. Found verifying v2.24.2 against
    the live feed: a stamped-back install with a process parked in it failed every apply with
    WinError 32, silently, while every earlier end-to-end check had run from outside the directory.
    The simulated lock runs everywhere; the real one runs wherever the OS enforces it (Windows CI).
    """
    print("[34] selfupdate: an install directory in use still updates (offline)")

    def fake(version):
        def _f(repo):
            return {"version": version, "tag": "v" + version,
                    "assetName": "meridiancs.v%s.skill.zip" % version,
                    "assetUrl": "https://github.com/o/r/releases/download/v%s/x.skill.zip" % version,
                    "assetSize": 1234}
        return _f

    def leftovers(install):
        return sorted(d for d in os.listdir(os.path.dirname(install))
                      if d.startswith((m.UPDATE_STAGING_PREFIX, m.UPDATE_HELD_PREFIX, "meridiancs.previous")))

    def read(install, rel):
        with open(os.path.join(install, rel), encoding="utf-8") as f:
            return f.read()

    # getattr with a default so this can run against a build without the hooks -- which is how it
    # was shown to fail against the pre-fix apply path rather than only to pass against the fix.
    saved = {n: getattr(m, n, None) for n in ("latest_release", "_download_asset", "_rename_dir",
                                              "_move_entry")}
    real_replace = os.replace

    def locked_rename(src, dst):
        if os.path.realpath(src) == m.INSTALL_REAL:
            raise PermissionError(13, "simulated WinError 32: directory in use", src)
        return real_replace(src, dst)

    def scenario(version, pkg_version=None):
        tmp = tempfile.mkdtemp()
        install = _su_install(tmp, version)
        with open(os.path.join(install, "old-only.txt"), "w", encoding="utf-8") as f:
            f.write("from the old install")
        env = _SUEnv(m, install, tmp)
        pkg = _su_package(tmp, pkg_version or "2.24.3")
        m.latest_release = fake(pkg_version or "2.24.3")
        m._download_asset = lambda url, dest: (shutil.copyfile(pkg, dest), os.path.getsize(dest))[1]
        return tmp, install, env

    os.environ["MERIDIAN_UPDATE_REPO"] = "jwood25/meridiancs-public"
    try:
        # --- 1. the directory cannot be renamed: the contents are swapped instead ----------------
        tmp, install, env = scenario("2.24.2")
        try:
            m._rename_dir = locked_rename
            ident = os.stat(install)
            res = m.apply_update(m.check_update(force=True))
            check("a locked install directory still updates", (res.get("applied"), res.get("toVersion")),
                  (True, "2.24.3"))
            check("... by swapping its contents", res.get("swap"), "contents")
            check("... in place (same directory, not a replacement)",
                  os.path.samestat(ident, os.stat(install)), True)
            check("the new stamp and SKILL.md landed",
                  (m.installed_version().get("version"), "stub skill 2.24.3" in read(install, "SKILL.md")),
                  ("2.24.3", True))
            check("the old install's files are gone, as with a directory swap",
                  os.path.exists(os.path.join(install, "old-only.txt")), False)
            check("nothing is left beside the install", leftovers(install), [])
        finally:
            env.restore(); shutil.rmtree(tmp, ignore_errors=True)

        # --- 2. a failure mid-swap puts every old entry back -------------------------------------
        tmp, install, env = scenario("2.24.2")
        try:
            m._rename_dir = locked_rename

            def failing_move(src, dst):
                # Only the move IN from staging fails; moving the old entry back must still work.
                if (os.path.basename(dst) == "scripts" and os.path.dirname(dst) == m.INSTALL_REAL
                        and "unpacked" in src):
                    raise PermissionError(13, "simulated: cannot move scripts in", dst)
                return real_replace(src, dst)
            m._move_entry = failing_move
            try:
                m.apply_update(m.check_update(force=True))
                check("a failed contents swap is reported", "applied", "raised")
            except OSError:
                check("a failed contents swap is reported", "raised", "raised")
            check("... and the old install is back, whole",
                  (m.installed_version().get("version"), read(install, "SKILL.md"),
                   read(install, "scripts/meridian.py"), read(install, "old-only.txt")),
                  ("2.24.2", "# installed 2.24.2\n", "print('old')\n", "from the old install"))
            check("... with nothing left beside it", leftovers(install), [])
        finally:
            m._move_entry = saved["_move_entry"] or os.replace
            env.restore(); shutil.rmtree(tmp, ignore_errors=True)

        # --- 3. a restore that cannot finish never deletes the only copy -------------------------
        tmp, install, env = scenario("2.24.2")
        try:
            m._rename_dir = locked_rename

            def stuck_move(src, dst):
                parent = os.path.dirname(dst)
                if os.path.basename(dst) == "scripts" and parent == m.INSTALL_REAL and "unpacked" in src:
                    raise PermissionError(13, "simulated: cannot move scripts in", dst)
                if os.path.basename(src) == "SKILL.md" and m.UPDATE_HELD_PREFIX in os.path.basename(os.path.dirname(src)):
                    raise PermissionError(13, "simulated: cannot restore SKILL.md", src)
                return real_replace(src, dst)
            m._move_entry = stuck_move
            try:
                m.apply_update(m.check_update(force=True))
                check("an incomplete restore is reported", "applied", "raised")
            except RuntimeError as e:
                check("an incomplete restore is reported, naming where the old files are",
                      m.UPDATE_HELD_PREFIX in str(e) and "SKILL.md" in str(e), True)
            kept = [d for d in leftovers(install) if d.startswith(m.UPDATE_HELD_PREFIX)]
            check("... and that directory is kept, holding the stuck file",
                  len(kept) == 1 and os.path.exists(os.path.join(os.path.dirname(install), kept[0],
                                                                 "SKILL.md")) if kept else False, True)
        finally:
            m._move_entry = saved["_move_entry"] or os.replace
            env.restore(); shutil.rmtree(tmp, ignore_errors=True)

        # --- 4. for real: another process's working directory is the install ---------------------
        m._rename_dir = saved["_rename_dir"] or os.replace
        tmp, install, env = scenario("2.24.2")
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=install)
        try:
            res = m.apply_update(m.check_update(force=True))
            check("an install another process is parked in updates", res.get("applied"), True)
            # Where the OS enforces the lock this has to take the fallback; elsewhere the plain
            # rename works and must still be what runs.
            check("... via the swap this OS needs", res.get("swap"),
                  "contents" if os.name == "nt" else "directory")
            check("... and lands the new version", m.installed_version().get("version"), "2.24.3")
        finally:
            holder.kill(); holder.wait()
            env.restore(); shutil.rmtree(tmp, ignore_errors=True)
    finally:
        for n, v in saved.items():
            if v is None:
                if hasattr(m, n):
                    delattr(m, n)
            else:
                setattr(m, n, v)
        os.environ.pop("MERIDIAN_UPDATE_REPO", None)


WN_CHANGELOG = """Intro prose that is not an entry.

## 2.25.0 - 2026-09-23

- **Newest.** first item
  with a continuation line
- second
- third
- fourth
- fifth

## 2.24.3

- **Middle.** item

## 2.24.2

- **Oldest.** item
"""


def test_whats_new(m):
    """The release an install moved to announces itself once, from its own CHANGELOG.md.

    Users never open the skill folder, so the changelog only reaches anyone if the skill says it.
    The detection has to survive being updated BY an older updater, which rewrites `.updatecheck`
    with the new version straight after an apply -- which is why `.lastseen` is its own file.
    """
    print("[35] what's new: changelog parsing, first-session announcement, release guard (offline)")
    root = os.path.dirname(HERE)

    # --- the shipped CHANGELOG.md is well-formed ---------------------------------------------------
    with open(os.path.join(root, "CHANGELOG.md"), encoding="utf-8-sig") as f:
        real = m.parse_changelog(f.read())
    versions = [m.parse_version(v) for v, _ in real]
    check("CHANGELOG.md parses into version entries", len(real) >= 4 and all(versions), True)
    check("... newest first, no duplicates", versions == sorted(set(versions), reverse=True), True)
    check("... every entry says something", all(items for _, items in real), True)

    # --- parsing and range selection ---------------------------------------------------------------
    parsed = m.parse_changelog(WN_CHANGELOG)
    check("entries and items parse, prose ignored", [(v, len(i)) for v, i in parsed],
          [("2.25.0", 5), ("2.24.3", 1), ("2.24.2", 1)])
    check("continuation lines fold into their item", parsed[0][1][0],
          "**Newest.** first item with a continuation line")
    tmp = tempfile.mkdtemp()
    try:
        cl = os.path.join(tmp, "CHANGELOG.md")
        with open(cl, "w", encoding="utf-8") as f:
            f.write(WN_CHANGELOG)
        wn = m.whats_new("2.24.2", "2.25.0", path=cl)
        check("everything newer than the last-seen version, newest first",
              [r["version"] for r in wn], ["2.25.0", "2.24.3"])
        check("... items capped, with the remainder counted", (len(wn[0]["items"]), wn[0].get("moreItems")),
              (m.WHATS_NEW_MAX_ITEMS, 5 - m.WHATS_NEW_MAX_ITEMS))
        check("an unknown earlier version announces only the current entry",
              [r["version"] for r in m.whats_new(None, "2.25.0", path=cl)], ["2.25.0"])
        check("a version with no entry announces nothing, not 'nothing changed'",
              m.whats_new("2.25.0", "2.26.0", path=cl), [])
        check("no changelog is no announcement, not an error", m.whats_new("2.24.2", "2.25.0",
                                                                           path=cl + ".missing"), [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # --- the first-session announcement ------------------------------------------------------------
    tmp = tempfile.mkdtemp()
    install = _su_install(tmp, "2.25.0")
    with open(os.path.join(install, "CHANGELOG.md"), "w", encoding="utf-8") as f:
        f.write(WN_CHANGELOG)
    env = _SUEnv(m, install, tmp)
    os.makedirs(m.CFG_DIR, exist_ok=True)
    lastseen = os.path.join(m.CFG_DIR, ".lastseen")

    def seen(v):
        with open(lastseen, "w", encoding="utf-8") as f:
            json.dump({"schema": 1, "version": v}, f)

    def announce(prior=None, local="2.25.0", state="current"):
        return m.announce_whats_new({"installedVersion": local, "state": state}, prior)

    try:
        r = announce()
        check("a fresh install announces nothing", "whatsNew" in r, False)
        check("... but records the version it started on",
              json.load(open(lastseen, encoding="utf-8")).get("version"), "2.25.0")
        check("the next session on the same version announces nothing", "whatsNew" in announce(), False)

        seen("2.24.3")
        r = announce()
        check("the first session after an update announces the new entries",
              ([x["version"] for x in r.get("whatsNew", [])], r.get("updatedFrom")), (["2.25.0"], "2.24.3"))
        check("... once: the session after says nothing", "whatsNew" in announce(), False)

        os.remove(lastseen)
        r = announce(prior={"installedVersion": "2.25.0", "state": "current"})
        check("updated by a pre-2.25 updater (cache already says the new version): current entry only",
              ([x["version"] for x in r.get("whatsNew", [])], r.get("updatedFrom")), (["2.25.0"], None))

        os.remove(lastseen)
        r = announce(prior={"installedVersion": "2.24.2", "state": "current"})
        check("replaced by hand (cache still says the old version): the whole range",
              [x["version"] for x in r.get("whatsNew", [])], ["2.25.0", "2.24.3"])

        os.remove(lastseen)
        r = announce(local=None, state="dev", prior={"installedVersion": "2.24.2"})
        check("a working tree never announces", "whatsNew" in r, False)
        check("... and never records a version", os.path.exists(lastseen), False)

        # --- the apply path announces straight away, and records it -------------------------------
        seen("2.25.0")
        pkg = _su_package(tmp, "2.26.0", extra={"meridiancs/CHANGELOG.md":
                                                "## 2.26.0\n\n- **Brand new.** thing\n\n" + WN_CHANGELOG})
        saved = (m.latest_release, m._download_asset)
        m.latest_release = lambda repo: {"version": "2.26.0", "tag": "v2.26.0",
                                         "assetName": "x.skill.zip", "assetSize": 1,
                                         "assetUrl": "https://github.com/o/r/releases/download/v2.26.0/x.skill.zip"}
        m._download_asset = lambda url, dest: (shutil.copyfile(pkg, dest), os.path.getsize(dest))[1]
        os.environ["MERIDIAN_UPDATE_REPO"] = "jwood25/meridiancs-public"
        try:
            with open(os.path.join(install, "VERSION.json"), "w", encoding="utf-8") as f:
                json.dump({"schema": 1, "version": "2.25.0", "commit": "c"}, f)
            res = m.apply_update(m.check_update(force=True))
            check("an apply reports the new version's notes from the NEW changelog",
                  [x["version"] for x in res.get("whatsNew", [])], ["2.26.0"])
            check("... and records them as seen, so the next session doesn't repeat them",
                  "whatsNew" in announce(local="2.26.0", prior=m._read_updatecheck()), False)
        finally:
            m.latest_release, m._download_asset = saved
            os.environ.pop("MERIDIAN_UPDATE_REPO", None)
    finally:
        env.restore()
        shutil.rmtree(tmp, ignore_errors=True)

    # --- a release cannot be built without its entry ----------------------------------------------
    script = os.path.join(root, "scripts", "make-package.py")
    r = subprocess.run([sys.executable, script, "--version", "9.9.7"], capture_output=True, text=True,
                       cwd=root)
    check("make-package refuses a release with no changelog entry",
          (r.returncode, "no `## 9.9.7` entry" in r.stdout), (1, True))
    check("... and writes no package", os.path.exists(os.path.join(root, "meridiancs.v9.9.7.skill.zip")),
          False)
    out = os.path.join(tempfile.mkdtemp(), "probe.skill.zip")
    subprocess.run([sys.executable, script, out, "--version", "9.9.7", "--allow-dirty"],
                   capture_output=True, text=True, cwd=root)
    import zipfile
    with zipfile.ZipFile(out) as z:
        check("CHANGELOG.md ships in the package", "meridiancs/CHANGELOG.md" in z.namelist(), True)
    shutil.rmtree(os.path.dirname(out), ignore_errors=True)

    # --- SKILL.md carries the rules, not just the code ---------------------------------------------
    with open(os.path.join(root, "SKILL.md"), encoding="utf-8") as f:
        skill = re.sub(r"\s+", " ", f.read())
    for rule in ('`whatsNew` on a check result is the one exception to "say nothing".',
                 "Show it **once**",
                 'never "nothing new"',
                 "read `CHANGELOG.md` in the skill folder",
                 "never reconstruct release history from memory"):
        check("SKILL.md still says: %s" % rule[:48], rule in skill, True)


def test_publish_public():
    """publish-public.py makes the release commit BEFORE it builds, so the stamp names that commit.

    Through v2.25.0 the build ran on the synced-but-uncommitted clone with --allow-dirty and the
    operator committed afterwards, so every public package's VERSION.json named the PREVIOUS public
    commit plus `dirty: true` (the v2.24.2 asset names the v2.24.0 seed). Driven end to end against a
    throwaway clone of a local bare repo, so nothing here touches the network or the real clone.
    """
    print("[36] publish-public: release commit precedes the build; tip and no-op checks hold (offline)")
    if derived_tree():
        print("  SKIP  derived public tree - publishing derives from the internal tree it lacks")
        return
    import zipfile
    root = os.path.dirname(HERE)
    script = os.path.join(root, "scripts", "publish-public.py")
    with open(os.path.join(root, "CHANGELOG.md"), encoding="utf-8-sig") as f:
        version = re.search(r"^##\s+\[?v?(\d+\.\d+\.\d+)", f.read(), re.M).group(1)

    def git(cwd, *args):
        return subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True)

    def rev(cwd, ref="HEAD"):
        return git(cwd, "rev-parse", ref).stdout.strip()

    def publish(clone, ver=version):
        # --allow-dirty applies to the INTERNAL tree only, so the suite runs on a working copy with
        # edits in it. The clone-side build is what this test is about, and it takes no such flag.
        return subprocess.run([sys.executable, script, "--version", ver, "--clone", clone,
                               "--allow-dirty"], capture_output=True, text=True, cwd=root)

    def force_rm(func, path, _exc):
        os.chmod(path, 0o700)   # git's object files are read-only, which Windows refuses to delete
        func(path)

    tmp = tempfile.mkdtemp(prefix="pubtest-")
    try:
        # The URL guard wants the public repo's name in the remote, so the bare repo carries it.
        origin = os.path.join(tmp, "CyderesInc", "MeridianCS.git")
        clone = os.path.join(tmp, "clone")
        os.makedirs(origin)
        git(origin, "init", "--quiet", "--bare")
        # Forward slashes, so the remote URL reads CyderesInc/MeridianCS on Windows too.
        git(tmp, "clone", "--quiet", origin.replace(os.sep, "/"), clone)
        for k, v in (("user.name", "test"), ("user.email", "test@example.com"),
                     ("commit.gpgsign", "false")):
            git(clone, "config", k, v)
        git(clone, "symbolic-ref", "HEAD", "refs/heads/main")
        with open(os.path.join(clone, "README.md"), "w") as f:
            f.write("seed\n")
        git(clone, "add", "-A")
        git(clone, "commit", "--quiet", "-m", "seed")
        git(clone, "push", "--quiet", "-u", "origin", "main")
        seed = rev(clone)
        check("fixture: a seeded clone tracking its bare origin",
              bool(seed) and rev(clone, "@{u}") == seed, True)

        r = publish(clone)
        out = r.stdout + r.stderr
        check("a changed public tree builds and verifies (exit 0)", r.returncode, 0)
        if r.returncode:
            print(out[-2000:])
        head = rev(clone)
        check("... the release commit is made in the clone",
              git(clone, "rev-list", "--count", "@{u}..HEAD").stdout.strip(), "1")
        check("... on top of the published tip", rev(clone, "HEAD~1"), seed)
        check("... and never pushed", rev(origin, "main"), seed)
        check("... leaving the clone clean", git(clone, "status", "--porcelain").stdout.strip(), "")
        stamp = {}
        pkg = os.path.join(clone, "meridiancs.v%s.skill.zip" % version)
        if os.path.exists(pkg):
            with zipfile.ZipFile(pkg) as z:
                stamp = json.loads(z.read("meridiancs/VERSION.json"))
        # Both halves: with no commit made, HEAD is still the seed and "stamp == HEAD" holds
        # vacuously -- which is exactly the v2.24.2 stamp.
        check("the stamp names the release commit, not the one before it",
              (stamp.get("commit") == head, stamp.get("commit") != seed), (True, True))
        check("... and is not marked dirty", "dirty" in stamp, False)
        check("the next steps no longer ask the operator to commit", "git commit" in out, False)
        check("... still hand over the push", "git push origin HEAD" in out, True)
        check("... and target the release at the stamped commit", ("--target %s" % head) in out, True)

        # An unpushed release commit is exactly the state the run above leaves behind.
        r = publish(clone)
        check("a rerun over an unpushed release commit is refused (exit 2)", r.returncode, 2)
        check("... without adding a second one", rev(clone), head)

        # The previous release's package now sits in the clone, ignored by .gitignore. Walking the
        # directory counted it as a difference, so the no-op check could never fire after a release.
        git(clone, "push", "--quiet", "origin", "main")
        r = publish(clone)
        check("once pushed, an unchanged public tree is a no-op",
              (r.returncode, "byte-identical" in r.stdout), (0, True))
        check("... that commits nothing", rev(clone), head)

        # A failure after the commit has to take it back, or the next run is refused as "ahead".
        with open(os.path.join(clone, "stale.txt"), "w") as f:
            f.write("drift\n")
        git(clone, "add", "-A")
        git(clone, "commit", "--quiet", "-m", "drift")
        git(clone, "push", "--quiet", "origin", "main")
        drift = rev(clone)
        r = publish(clone, "0.0.1")   # no CHANGELOG entry, so make-package.py refuses the build
        check("a build failing after the commit exits non-zero", r.returncode != 0, True)
        check("... and undoes its release commit", rev(clone), drift)
        check("... leaving the sync staged for inspection",
              "stale.txt" in git(clone, "diff", "--cached", "--name-only").stdout, True)
    finally:
        shutil.rmtree(tmp, onerror=force_rm)


def test_low_findings(m):
    """The 2026-09-22 review's low findings L4, L5, L6 and L8 -- each asserted against the old code.

    L4  the browser lookup ran whatever `chrome.bat` sat in the current directory;
    L5  outputs written from the skill folder are deleted by the next self-update, unwarned;
    L6  `api` would PUT/DELETE on the model's say-so alone, and SKILL.md's untrusted-input list
        left out the two sources most able to carry someone else's text;
    L8  the digest report interpolated non-numeric API values into HTML unescaped.
    """
    print("[42] review low findings: browser lookup, output location, api writes, digest escaping (offline)")
    root = os.path.dirname(HERE)

    # --- L4: only absolute PATH entries, and on Windows only .exe -------------------------------
    tmp = tempfile.mkdtemp(prefix="l4-")
    here, saved_path = os.getcwd(), os.environ.get("PATH", "")
    # The assistant's own shell sets this, which suppresses the Windows cwd search and hid the bug
    # on the machine it was found from. An ordinary shell does not, so the test must not either.
    saved_nodef = os.environ.pop("NoDefaultCurrentDirectoryInExePath", None)
    try:
        exe = ".exe" if os.name == "nt" else ""
        planted, trusted = os.path.join(tmp, "work"), os.path.join(tmp, "bin")
        os.makedirs(planted); os.makedirs(trusted)

        def touch(path):
            with open(path, "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(path, 0o755)

        for name in ("chrome.bat", "chrome.cmd", "chrome" + exe):
            touch(os.path.join(planted, name))
        os.chdir(planted)
        # An empty entry is POSIX's spelling of "the current directory"; Windows searches it anyway.
        os.environ["PATH"] = os.pathsep.join(["", ".", trusted])
        found = m._find_browser()
        check("a browser planted in the current directory is not picked",
              found is None or (os.path.isabs(found) and os.path.dirname(found) != planted), True)
        check("...not even via an empty or relative PATH entry", m._which_trusted("chrome"), None)
        touch(os.path.join(trusted, "chrome" + exe))
        check("a browser in an absolute PATH directory is still found",
              m._which_trusted("chrome"), os.path.join(trusted, "chrome" + exe))
        if os.name == "nt":
            os.remove(os.path.join(trusted, "chrome.exe"))
            touch(os.path.join(trusted, "chrome.bat"))
            check("...but on Windows only as an .exe", m._which_trusted("chrome"), None)
    finally:
        os.chdir(here)
        os.environ["PATH"] = saved_path
        if saved_nodef is not None:
            os.environ["NoDefaultCurrentDirectoryInExePath"] = saved_nodef
        shutil.rmtree(tmp, ignore_errors=True)

    # --- L5: an output inside the skill folder is warned about ----------------------------------
    inside = os.path.join(m.INSTALL_DIR, "Weekly-Posture.pdf")
    warn = m.out_in_skill_dir(inside)
    check("an --out inside the skill folder is warned about", bool(warn), True)
    check("...saying what will happen to it", "deletes" in (warn or ""), True)
    check("...including a relative one from the scripts/ directory",
          bool(m.out_in_skill_dir(os.path.relpath(os.path.join(m.INSTALL_DIR, "scripts", "x.csv")))), True)
    check("an --out elsewhere is not", m.out_in_skill_dir(os.path.join(tempfile.gettempdir(), "x.pdf")), None)
    # End to end: the warning is emitted before dispatch, and report then fails on its missing
    # input -- so nothing is written into the skill folder by this test.
    r = subprocess.run([sys.executable, os.path.join(root, "scripts", "meridian.py"), "report",
                        "--input", os.path.join(tempfile.gettempdir(), "no-such-input.json"),
                        "--out", inside], capture_output=True, text=True, cwd=root)
    check("the CLI prints the warning on stderr", "skill folder" in r.stderr, True)
    check("...and wrote nothing there", os.path.exists(inside), False)
    ignored = subprocess.run(["git", "check-ignore", "-q", "top.json"], cwd=root).returncode == 0
    check("a stray output JSON at the root is gitignored", ignored, True)
    check("...but the tracked SBOM is not",
          subprocess.run(["git", "check-ignore", "-q", "sbom.cdx.json"], cwd=root).returncode, 1)

    # --- L6: writes need --allow-write; the untrusted list names every free-text source ---------
    for method, ep in (("PUT", "CMDB/v2/connector/profile/service"), ("DELETE", "CMDB/v2/smartlabel?id=1"),
                       ("PATCH", "CMDB/v2/data/cmdb"), ("POST", "CMDB/v2/connector/test/async"),
                       ("POST", "CMDB/v2/data/cmdb/../../connector/test/async")):
        check("api -X %s %s needs --allow-write" % (method, ep), bool(m.api_write_problem(method, ep)), True)
        check("...and runs with it", m.api_write_problem(method, ep, True), None)
    for method, ep in (("GET", "CMDB/v2/connector"), ("POST", "CMDB/v2/data/cmdb"),
                       ("POST", "/CMDB/v2/data/ldg"), ("POST", "CMDB/v2/data/cmd%62"),
                       ("post", "CMDB/v2/smartlabel/search")):
        check("api -X %s %s is a read" % (method, ep), m.api_write_problem(method, ep), None)
    import contextlib, io
    calls, real_call = [], m.call
    m.call = lambda method, endpoint, body=None, retries=1: calls.append(method) or {}
    try:
        class A:
            method, endpoint, body, body_file = "PUT", "CMDB/v2/connector/profile/service", None, None
        refused = False
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                m.cmd_api(A())
        except SystemExit:
            refused = True
        check("cmd_api refuses the PUT before anything reaches the wire", (refused, calls), (True, []))
        A.allow_write = True
        with contextlib.redirect_stdout(io.StringIO()):
            m.cmd_api(A())
        check("...and sends it once allowed", calls, ["PUT"])
    finally:
        m.call = real_call
    with open(os.path.join(root, "SKILL.md"), encoding="utf-8") as f:
        skill = " ".join(f.read().split())
    untrusted = skill[skill.find("### Meridian records are untrusted input"):][:700]
    check("SKILL.md's untrusted-input list names connector messages", "connector messages" in untrusted, True)
    check("...and raw api responses", "raw `api` responses" in untrusted, True)
    check("SKILL.md tells the model when --allow-write may be added",
          "Add the flag only after the user confirms that change" in skill, True)
    check("SKILL.md keeps outputs out of the skill folder",
          "Never write an output" in skill and "inside the skill folder" in skill, True)

    # --- L8: the digest escapes what it did not compute -----------------------------------------
    evil = "<img src=x onerror=alert(1)>"
    data = {"headline": {"assets": evil, "users": 5},
            "connectors": {"summary": {"connectorsEnabled": 1, "failing": evil},
                           "failures": [{"service": "S", "serviceCount": evil, "message": "m"}]},
            "breakdown": {"by": "A&B", "complete": False, "unaccountedRecords": evil,
                          "groups": [{"value": "x", "count": 1}]}}
    _, cards, body = m._digest_html(data)
    html = cards + body
    check("a string value from the API is escaped in the digest", "<img" in html, False)
    check("...rendered as text rather than dropped", "&lt;img" in html, True)
    check("...and the breakdown label is escaped exactly once",
          ("A&amp;B" in html, "&amp;amp;" in html, "&amp;lt;" in html), (True, False, False))


def test_update_redirects(m):
    """Review finding L2: every redirect on the self-update path is held to the https-github rule.

    Driven through urllib's real redirect machinery, with a fake HTTPS handler serving canned
    responses -- so what is tested is the chain _gh_get and _download_asset actually run, not a
    handler called by hand. Nothing here touches the network.
    """
    print("[44] self-update follows redirects only to https github hosts (offline)")
    import email.message, io, urllib.request, urllib.response

    routes = {}

    class FakeHTTPS(urllib.request.HTTPSHandler):
        def https_open(self, req):
            code, location, body = routes.get(req.full_url, (404, None, b"not found"))
            hdrs = email.message.Message()
            if location:
                hdrs["Location"] = location
            resp = urllib.response.addinfourl(io.BytesIO(body), hdrs, req.full_url, code)
            resp.msg = "fake"
            return resp

    class FakeHTTP(urllib.request.HTTPHandler):
        def http_open(self, req):          # a downgrade that got past the check would land here
            resp = urllib.response.addinfourl(io.BytesIO(b"PLAINTEXT"), email.message.Message(),
                                              req.full_url, 200)
            resp.msg = "fake"
            return resp

    real_build = urllib.request.build_opener
    urllib.request.build_opener = lambda *h: real_build(*(h + (FakeHTTPS, FakeHTTP)))
    # A regression back to plain urlopen() would use urllib's cached global opener, which the patch
    # above never reaches -- so clear it too, and a regressed fetch fails here instead of going to the
    # network. Restored in the finally.
    saved_opener = urllib.request._opener
    urllib.request._opener = None
    tmp = tempfile.mkdtemp(prefix="l2-")
    try:
        start = "https://github.com/o/r/releases/download/v1/p.skill.zip"
        store = "https://release-assets.githubusercontent.com/p.skill.zip"

        def attempt(fn):
            try:
                return fn(), None
            except Exception as e:  # noqa
                return None, str(e)

        routes.clear()
        routes.update({start: (302, store, b""), store: (200, None, b"PKG")})
        dest = os.path.join(tmp, "ok.zip")
        got, err = attempt(lambda: m._download_asset(start, dest))
        check("a redirect to a github storage host is followed", (got, err), (3, None))

        dest = os.path.join(tmp, "evil.zip")
        routes[start] = (302, "https://evil.example/p.skill.zip", b"")
        routes["https://evil.example/p.skill.zip"] = (200, None, b"EVIL")
        got, err = attempt(lambda: m._download_asset(start, dest))
        check("a redirect to another host is refused", (got, "refusing a redirect" in (err or "")),
              (None, True))
        check("...before anything is written", os.path.exists(dest), False)

        routes[start] = (302, "http://release-assets.githubusercontent.com/p.skill.zip", b"")
        got, err = attempt(lambda: m._download_asset(start, os.path.join(tmp, "http.zip")))
        check("a downgrade to plain http is refused", (got, "refusing a redirect" in (err or "")),
              (None, True))

        # A later hop, not just the first: the check has to hold along the whole chain.
        hop = "https://objects.githubusercontent.com/hop"
        routes[start] = (302, hop, b"")
        routes[hop] = (302, "https://evil.example/p.skill.zip", b"")
        got, err = attempt(lambda: m._download_asset(start, os.path.join(tmp, "hop.zip")))
        check("...on the second hop as well as the first", got, None)

        chain = ["https://objects.githubusercontent.com/%d" % i for i in range(m.UPDATE_MAX_REDIRECTS + 2)]
        routes[start] = (302, chain[0], b"")
        for a, b in zip(chain, chain[1:]):
            routes[a] = (302, b, b"")
        got, err = attempt(lambda: m._download_asset(start, os.path.join(tmp, "long.zip")))
        check("a redirect chain past UPDATE_MAX_REDIRECTS is refused", (got is None, bool(err)), (True, True))

        api = "https://api.github.com/repos/o/r/releases/latest"
        routes[api] = (302, "https://evil.example/latest.json", b"")
        routes["https://evil.example/latest.json"] = (200, None, b'{"tag_name":"v99.0.0"}')
        got, err = attempt(lambda: m._gh_get(api))
        check("the release lookup refuses an off-github redirect too", got, None)
        routes[api] = (200, None, b'{"tag_name":"v1.0.0"}')
        check("...and still reads a direct answer", attempt(lambda: m._gh_get(api))[0], b'{"tag_name":"v1.0.0"}')
    finally:
        urllib.request.build_opener = real_build
        urllib.request._opener = saved_opener
        shutil.rmtree(tmp, ignore_errors=True)


def test_community_files(m):
    """The public repository's security and contribution front door: present, pointed at the feed,
    and warning people off pasting their environment into a public issue.

    The skill's inputs and outputs are customer inventory -- names, devices, vulnerability data --
    and the natural way to report a bug is to paste what you saw. So the redaction warning is the
    load-bearing line in every one of these files, and is asserted rather than trusted. The private
    reporting link must name the UPDATE_REPO feed: that repository is the one whose releases run on
    every install, so it is where a vulnerability report has to land.
    """
    print("[46] community files: private security reporting, redaction warnings, dependabot stays internal")
    root = os.path.dirname(HERE)

    def read(rel):
        path = os.path.join(root, *rel.split("/"))
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return f.read()

    advisories = "https://github.com/%s/security/advisories/new" % m.UPDATE_REPO
    sec, contrib = read("SECURITY.md"), read("CONTRIBUTING.md")
    cfg, bug = read(".github/ISSUE_TEMPLATE/config.yml"), read(".github/ISSUE_TEMPLATE/bug_report.yml")
    feat = read(".github/ISSUE_TEMPLATE/feature_request.yml")
    check("SECURITY.md and CONTRIBUTING.md exist", (sec is not None, contrib is not None), (True, True))
    check("issue forms exist (config, bug, feature)",
          (cfg is not None, bug is not None, feat is not None), (True, True, True))
    if None in (sec, contrib, cfg, bug, feat):
        return
    check("SECURITY.md reports privately, to the update feed's repository", advisories in sec, True)
    check("... and steers away from a public issue", "public issue" in sec, True)
    check("... and asks for no tokens, hostnames or inventory", "API tokens" in sec and "hostnames" in sec, True)
    check("... and says only the latest release is supported", "latest release is supported" in sec, True)
    check("the issue chooser offers private reporting and no blank issue",
          ("blank_issues_enabled: false" in cfg, advisories in cfg), (True, True))
    check("the bug form warns against pasting a token, hostname or inventory",
          all(w in bug for w in ("API token", "hostname", "inventory")), True)
    check("... and links private reporting", advisories in bug, True)
    check("the feature form warns too", "no tokens" in feat, True)
    check("CONTRIBUTING.md carries the redaction warning",
          all(w in contrib for w in ("API token", "hostname", "inventory")), True)
    check("... and says a pull request is reapplied internally, not merged",
          ("reapplied internally" in contrib, "not merged here" in contrib), (True, True))
    check("... and states the inbound licence", "Apache License 2.0" in contrib, True)

    # Dependabot belongs to the SOURCE repo: in the derived tree its pull requests could never merge.
    if derived_tree():
        check("the derived tree carries no dependabot config",
              os.path.exists(os.path.join(root, ".github", "dependabot.yml")), False)
        return
    dep = read(".github/dependabot.yml")
    check("dependabot keeps the pinned actions current", dep is not None and "github-actions" in dep, True)
    spec = importlib.util.spec_from_file_location("mk_public_cf", os.path.join(root, "scripts", "make-public.py"))
    mp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mp)
    keep, _subs, dropped, _missing = mp.plan(mp.tracked_files())
    check("make-public drops dependabot.yml", ".github/dependabot.yml" in dropped, True)
    check("... and publishes the community files",
          sorted(f for f in ("SECURITY.md", "CONTRIBUTING.md", ".github/ISSUE_TEMPLATE/config.yml",
                             ".github/ISSUE_TEMPLATE/bug_report.yml",
                             ".github/ISSUE_TEMPLATE/feature_request.yml") if f in keep),
          [".github/ISSUE_TEMPLATE/bug_report.yml", ".github/ISSUE_TEMPLATE/config.yml",
           ".github/ISSUE_TEMPLATE/feature_request.yml", "CONTRIBUTING.md", "SECURITY.md"])


def test_ci_workflow():
    """ci.yml's two supply-chain rules hold on every step, not just the ones someone remembered.

    Actions are pinned to full commit SHAs, because a tag can be re-pointed by whoever controls the
    action's repo. And every checkout sets persist-credentials: false, because nothing in CI pushes,
    so a token left in the runner's git config is only something for a later step to read. Both are
    one-line omissions in a new job, and Dependabot rewrites these lines weekly.
    """
    print("[47] CI workflow: actions pinned to commit SHAs, checkout persists no credentials")
    path = os.path.join(os.path.dirname(HERE), ".github", "workflows", "ci.yml")
    if not os.path.exists(path):
        print("  SKIP  no .github/workflows/ci.yml in this tree")
        return
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    uses = [(i, l.split("uses:", 1)[1].split("#", 1)[0].strip()) for i, l in enumerate(lines)
            if l.strip().startswith(("uses:", "- uses:"))]
    check("the workflow has steps to check", len(uses) > 0, True)
    check("every action is pinned to a full commit SHA",
          [u for _i, u in uses if not re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", u)], [])
    checkouts = [i for i, u in uses if u.startswith("actions/checkout@")]
    check("... including every checkout", len(checkouts) > 0, True)

    def step(i):
        out = []
        for l in lines[i + 1:]:
            if l.strip().startswith("- ") or (l.strip() and len(l) - len(l.lstrip()) <= 6):
                break
            out.append(l.strip())
        return out
    check("every checkout sets persist-credentials: false",
          [i + 1 for i in checkouts if "persist-credentials: false" not in step(i)], [])


def test_hr_sources(m):
    """`hr` answers "do we have HR data?" from configured connectors and exact counts, never a sample.

    Three things together made the skill tell users with an HR connector enabled that they had no HR
    data: Meridian files HR systems under Identity Access Management beside Okta, the product and data
    names diverge (Dayforce is bridge `ceridian`, records `dayforce_employee`), and `summary --by
    sourcetype` samples values, so a small HR source behind a large one never shows. The verb takes all
    three out of the model's hands. The negative space matters most: only `none_configured` may mean
    "no HR system", and only when the profiles were actually read.
    """
    print("[48] hr: HR systems resolved from connectors, counted exactly, never an unread 'none'")
    real = {n: getattr(m, n) for n in ("_fetch_connector_profiles", "summarize_connectors", "call",
                                       "load_field_map", "_refetch_field_map")}
    secret = "hunter2-dayforce-secret"

    def prof(connector, bridge, services, profile="Prod"):
        return {"connector": connector, "bridge": bridge, "profile": profile,
                "group": "Identity Access Management",
                "services": [{"service": s, "enabled": on, "testStatus": "SUCCESS", "message": ""}
                             for s, on in services]}

    state = {"profiles": [], "counts": {}, "calls": [], "health": "ok"}

    def fake_profiles():
        if isinstance(state["profiles"], Exception):
            raise state["profiles"]
        return list(state["profiles"]), {secret}

    def fake_summary(brief=True, refresh=False, **k):
        return {"connectors": [{"connector": p["connector"], "profile": p["profile"],
                                "health": state["health"],
                                "lastIngest": {"status": "Error", "records": 0}}
                               for p in state["profiles"]]}

    def fake_call(method, endpoint, body=None, retries=1):
        vals = tuple(sorted(c["value"] for c in body["query"][0]))
        state["calls"].append(vals)
        v = state["counts"].get(vals, 0)
        if isinstance(v, Exception):
            raise v
        return {"totalRecords": v}

    fields = {"Owner_Manager": "String", "Dayforce_Department_SmartLabel": "String",
              "alias_dayforce_employee_displayName": "String", "alias_okta_user_Owner_Manager": "String",
              "Message_Usage_Adapter": "String", "Workday_Status_SmartLabel": "String"}
    m._fetch_connector_profiles, m.summarize_connectors, m.call = fake_profiles, fake_summary, fake_call
    m.load_field_map = lambda t, allow_fetch=True: dict(fields)
    m._refetch_field_map = lambda t: False
    try:
        # 1. an unreadable profile endpoint is unknown -- the one case that must never read as "none"
        state["profiles"] = RuntimeError("HTTP 403: Forbidden")
        r = m.hr_sources()
        check("unreadable profiles are unknown, not none", (r["state"], r["userRecords"]), ("unknown", None))
        check("... and the summary says unknown is not none", "not the same as none" in r["summary"], True)

        # 2. only an IdP configured: none, and only because the profiles WERE read
        state["profiles"] = [prof("Okta SSO", "okta", [("okta_user", True)])]
        r = m.hr_sources()
        check("an IdP alone is none_configured", (r["state"], r["systems"], r["where"]),
              ("none_configured", [], None))
        check("... noting the IdP may still fill manager and department", "identity provider" in r["summary"], True)
        check("... and making no count query", state["calls"], [])

        # 3. Dayforce: catalog bridge `ceridian`, data `dayforce_employee`, nothing delivered
        state["profiles"] = [prof("Dayforce", "ceridian", [("dayforce_employee", True)])]
        state["counts"], state["health"] = {("dayforce_employee",): 0}, "degraded"
        r = m.hr_sources()
        s = r["systems"][0]
        check("Dayforce is recognised by its bridge name", (s["system"], s["bridge"]), ("Dayforce", "ceridian"))
        check("configured but empty is configured_no_data, never none", r["state"], "configured_no_data")
        check("... carrying the connector's health and last run",
              (s["health"], s["lastIngest"]["status"]), ("degraded", "Error"))
        check("... and the exact --where for its records", r["where"], "sourcetype match List dayforce_employee")
        check("its sourcetype is counted exactly", s["sourcetypes"],
              [{"sourcetype": "dayforce_employee", "userRecords": 0}])

        # 4. two HR systems and an IdP: a person in both HR systems is ONE record
        state["profiles"] = [prof("Dayforce", "ceridian", [("dayforce_employee", True)]),
                             prof("BambooHR", "bamboohr", [("bamboohr_employee", True), ("bamboohr_extra", False)]),
                             prof("Okta SSO", "okta", [("okta_user", True)])]
        state["counts"] = {("dayforce_employee",): 700, ("bamboohr_employee",): 400,
                           ("bamboohr_employee", "dayforce_employee"): 900}
        state["health"], state["calls"] = "ok", []
        r = m.hr_sources()
        check("data from either system is has_data", r["state"], "has_data")
        check("the total is the de-duplicated OR count, not 700 + 400", r["userRecords"], 900)
        check("... and the summary states that total", "900 user records" in r["summary"], True)
        check("the IdP is not counted as an HR system", sorted(x["system"] for x in r["systems"]),
              ["BambooHR", "Dayforce"])
        check("a disabled service is reported but never counted",
              ([x["servicesDisabled"] for x in r["systems"] if x["system"] == "BambooHR"],
               any("bamboohr_extra" in c for c in state["calls"])), ([1], False))
        check("the --where covers every HR sourcetype", r["where"],
              "sourcetype in List dayforce_employee,bamboohr_employee")
        check("HR fields: the source's alias copies and HR-named SmartLabels, nothing else", r["hrFields"],
              ["Dayforce_Department_SmartLabel", "Workday_Status_SmartLabel",
               "alias_dayforce_employee_displayName"])
        check("no credential value reaches the output", secret in json.dumps(r), False)
        state["health"] = "failing"
        r = m.hr_sources()
        check("data from a failing connector is flagged as possibly stale", "may be stale" in r["summary"], True)

        # 5. a failed count with nothing positive is unknown, never a zero
        state["profiles"] = [prof("Dayforce", "ceridian", [("dayforce_employee", True)])]
        state["counts"] = {("dayforce_employee",): RuntimeError("HTTP 500: boom")}
        r = m.hr_sources()
        check("a failed count is unknown, not configured_no_data", (r["state"], r["userRecords"]), ("unknown", None))
        check("... keeping the error on the row", "HTTP 500" in r["systems"][0]["sourcetypes"][0]["error"], True)

        # 6. configured with every service switched off
        state["profiles"] = [prof("Workday", "workday", [("workday_worker", False)])]
        state["counts"], state["calls"] = {}, []
        r = m.hr_sources()
        check("every service off is configured_disabled, with no count query",
              (r["state"], state["calls"]), ("configured_disabled", []))

        # 7. a recognised product under a bridge name the list has not seen yet
        state["profiles"] = [prof("UKG", "ukg_ready_v2", [("ukg_employee", True)])]
        state["counts"] = {("ukg_employee",): 3}
        r = m.hr_sources()
        check("recognised by display name when the bridge is new",
              (r["state"], r["systems"][0]["system"]), ("has_data", "UKG"))
    finally:
        for n, v in real.items():
            setattr(m, n, v)


def test_field_cache_refetch(m):
    """A cached field map that misses a name re-reads the metadata once before calling it missing.

    The disk cache never expired, so a field that appeared after it was written was refused as
    "doesn't exist" by every verb. Enabling a connector is exactly what adds fields -- an HR connector
    brings its `alias_<sourcetype>_*` copies and the customer's SmartLabels for it -- so the skill told
    users their newly connected HR system's fields did not exist. Measured live: 280 cached against 282
    real, the missing two the HR source's. Bounded to one metadata call per table per process, so a
    run of typos costs one call, not one each.
    """
    print("[49] field cache: a miss re-fetches once, so a newly added field is not 'doesn't exist'")
    import tempfile
    real = {n: getattr(m, n) for n in ("CFG_DIR", "call", "load_config", "drop_labels_cache", "drop_rescache")}
    saved_map, saved_live, saved_disk = dict(m._FIELD_MAP), set(m._FIELD_MAP_LIVE), set(m._FIELD_MAP_DISK)
    tmp = tempfile.mkdtemp(prefix="fieldcache-")
    calls, dropped = [], []
    api = {"user": [{"fieldName": "Owner_Manager", "dataType": "String"},
                    {"fieldName": "alias_dayforce_employee_displayName", "dataType": "String"}]}

    def fake_call(method, endpoint, body=None, retries=1):
        calls.append(endpoint)
        if api.get("fail"):
            raise RuntimeError("HTTP 403: Forbidden")
        return {"metadata": api["user"]}

    m.CFG_DIR, m.call = tmp, fake_call
    m.load_config = lambda: ("s.example", "tok", None)
    m.drop_labels_cache = lambda: dropped.append("labels") or True
    m.drop_rescache = lambda: dropped.append("rescache") or True
    cached = [{"fieldName": "Owner_Manager", "dataType": "String"}]

    def reset():
        m._FIELD_MAP.clear()
        m._FIELD_MAP_LIVE.clear()
        m._FIELD_MAP_DISK.clear()
        del calls[:], dropped[:]
        with open(m._fields_path(), "w", encoding="utf-8") as f:
            json.dump({"fqdn": "s.example", "user": cached}, f)

    try:
        # the cache predates the HR connector: it knows Owner_Manager only
        reset()
        check("a field added after the cache was written is accepted",
              m.field_problem("user", "alias_dayforce_employee_displayName"), None)
        check("... at the cost of one metadata call", calls, ["/CMDB/v2/data/metadata/user"])
        check("... dropping the caches derived from metadata", sorted(dropped), ["labels", "rescache"])
        with open(m._fields_path(), encoding="utf-8") as f:
            check("... and the disk cache now holds it",
                  "alias_dayforce_employee_displayName" in {x["fieldName"] for x in json.load(f)["user"]}, True)

        # a typo: still refused, after exactly one refetch however many typos follow
        reset()
        api["user"] = list(cached)
        p1, p2 = m.field_problem("user", "Owner_Mnager"), m.field_problem("user", "Owner_Mangr")
        check("a real typo is still refused, with the suggestion",
              (p1 is not None and "Owner_Manager" in p1, p2 is not None), (True, True))
        check("... after one refetch, not one per query", len(calls), 1)
        check("... and an unchanged field set drops no cache", dropped, [])

        # metadata unreachable on refetch: the cached answer stands, and it is not retried per query
        reset()
        api["fail"] = True
        p1, p2 = m.field_problem("user", "Owner_Mnager"), m.field_problem("user", "Nope")
        check("an unreachable refetch still refuses from the cache", (p1 is not None, p2 is not None), (True, True))
        check("... trying the endpoint once", len(calls), 1)
        api.pop("fail")

        # a field the cache already has costs nothing
        reset()
        check("a known field makes no call", (m.field_problem("user", "Owner_Manager"), calls), (None, []))

        # a cold start with no disk cache fetches exactly once, even when a name then misses
        reset()
        os.remove(m._fields_path())
        m.field_problem("user", "Not_There")
        check("a cold start fetches once, not a second time for the miss", len(calls), 1)

        # a map placed in the memo (not read from disk) is never refetched: this is what keeps the
        # offline suite offline, since other tests inject a map and then probe a misspelt field
        reset()
        m._FIELD_MAP["user"] = {"Owner_Manager": "String"}
        m.call = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network reached from a memo map"))
        check("an injected map is judged as given, with no call",
              "Did you mean" in (m.field_problem("user", "Owner_Mnager") or ""), True)
        m.call = fake_call
    finally:
        for n, v in real.items():
            setattr(m, n, v)
        m._FIELD_MAP.clear()
        m._FIELD_MAP.update(saved_map)
        m._FIELD_MAP_LIVE.clear()
        m._FIELD_MAP_LIVE.update(saved_live)
        m._FIELD_MAP_DISK.clear()
        m._FIELD_MAP_DISK.update(saved_disk)
        shutil.rmtree(tmp, ignore_errors=True)


def test_selfupdate_followups(m):
    """Two defects found verifying v2.26.0 end to end, both on the self-update path.

    1. A cached check reported ANOTHER install's commit and folder. The cache is one file per user,
       keyed on repo + version, and the cached record was returned whole -- so a second install of
       the same version reported the first one's `installedCommit`/`installDir` until --force.
    2. A deep install folder failed its update on Windows with a bare "No such file or directory":
       staging sat 37 chars deeper than the install, and a staged font path reached 263 chars against
       a 260 limit. Now staging is shallower, and a path that cannot fit is refused before a byte is
       extracted, with a reason that says what to do.
    """
    print("[45] selfupdate: the cache never speaks for another install; deep paths refused up front (offline)")

    def fake(version):
        def _f(repo):
            return {"version": version, "tag": "v" + version,
                    "assetName": "meridiancs.v%s.skill.zip" % version,
                    "assetUrl": "https://github.com/o/r/releases/download/v%s/x.skill.zip" % version,
                    "assetSize": 1234}
        return _f

    def stamp(install, commit):
        path = os.path.join(install, "VERSION.json")
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
        rec["commit"] = commit
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rec, f)

    saved = {n: getattr(m, n, None) for n in ("latest_release", "_download_asset", "_long_paths_enabled")}
    os.environ["MERIDIAN_UPDATE_REPO"] = "jwood25/meridiancs-public"
    try:
        # --- 1. two installs of one version share the cache, never each other's identity ----------
        tmp = tempfile.mkdtemp()
        a = _su_install(os.path.join(tmp, "a"), "2.26.0")
        b = _su_install(os.path.join(tmp, "b"), "2.26.0")
        stamp(a, "aaaa1111")
        stamp(b, "bbbb2222")
        m.latest_release = fake("2.26.0")
        env = _SUEnv(m, a, tmp)
        try:
            first = m.check_update(force=True)
        finally:
            env.restore()
        env = _SUEnv(m, b, tmp)             # same tmp -> the same CFG_DIR and .updatecheck
        try:
            m.latest_release = lambda repo: (_ for _ in ()).throw(AssertionError("cache should answer"))
            second = m.check_update()
        finally:
            env.restore()
        check("the first install checks over the network", (first.get("checkedVia"), first.get("installedCommit")),
              ("network", "aaaa1111"))
        check("a second install of the same version is answered from the shared cache",
              second.get("checkedVia"), "cache")
        check("...but reports its OWN commit", second.get("installedCommit"), "bbbb2222")
        check("...and its own folder", second.get("installDir"), os.path.realpath(b))
        check("...while the feed facts still come from the cache",
              (second.get("state"), second.get("latestVersion")), ("current", "2.26.0"))
        shutil.rmtree(tmp, ignore_errors=True)

        # --- 2. a deep install is refused before extraction, a normal one still updates -----------
        font = {"meridiancs/assets/fonts/SpaceGrotesk-Bold.ttf": "x"}
        m._long_paths_enabled = lambda: False       # the Windows default, forced on every platform

        tmp = tempfile.mkdtemp()
        # Deep enough that STAGED paths pass 259 chars while every INSTALLED path still fits, which
        # is exactly the v2.26.0 case: the install works, only its update cannot.
        target = 259 - len("/assets/fonts/SpaceGrotesk-Bold.ttf") - 4
        root = os.path.join(tmp, "d")
        while len(os.path.join(root, "skills", "meridiancs")) < target - 12:
            root = os.path.join(root, "deepfolder")
        install = _su_install(root, "2.25.0")
        installed = len(os.path.join(os.path.realpath(install), "assets", "fonts", "SpaceGrotesk-Bold.ttf"))
        staged = installed + len(m.UPDATE_STAGING_PREFIX) + 8 + len("/unpacked/")
        check("fixture: the installed path fits and the staged one does not",
              (installed <= m.WINDOWS_MAX_PATH, staged > m.WINDOWS_MAX_PATH), (True, True))
        pkg = _su_package(tmp, "2.26.0", extra=font)
        m.latest_release = fake("2.26.0")
        m._download_asset = lambda url, dest: (shutil.copyfile(pkg, dest), os.path.getsize(dest))[1]
        env = _SUEnv(m, install, tmp)
        try:
            err = ""
            try:
                m.apply_update(m.check_update(force=True))
            except Exception as e:  # noqa
                err = str(e)
            check("an update whose staged paths cannot fit is refused", "too deep" in err, True)
            check("...saying how long the path is and what to do",
                  ("characters" in err, "LongPathsEnabled" in err), (True, True))
            with open(os.path.join(install, "VERSION.json"), encoding="utf-8") as f:
                check("...leaving the install untouched", json.load(f)["version"], "2.25.0")
            check("...and nothing staged beside it",
                  [d for d in os.listdir(os.path.dirname(install)) if d.startswith(m.UPDATE_STAGING_PREFIX)], [])
        finally:
            env.restore()
            shutil.rmtree(tmp, ignore_errors=True)

        tmp = tempfile.mkdtemp()
        install = _su_install(tmp, "2.25.0")
        pkg = _su_package(tmp, "2.26.0", extra=font)
        m._download_asset = lambda url, dest: (shutil.copyfile(pkg, dest), os.path.getsize(dest))[1]
        env = _SUEnv(m, install, tmp)
        try:
            res = m.apply_update(m.check_update(force=True))
            check("a normal-depth install still updates with the check in force",
                  (res.get("applied"), res.get("toVersion")), (True, "2.26.0"))
        finally:
            env.restore()
            shutil.rmtree(tmp, ignore_errors=True)
    finally:
        for n, v in saved.items():
            if v is None:
                if hasattr(m, n):
                    delattr(m, n)
            else:
                setattr(m, n, v)
        os.environ.pop("MERIDIAN_UPDATE_REPO", None)


def main():
    live = "--live" in sys.argv
    print("Meridian connect preflight — smoke tests\n" + "-" * 42)
    # The aggregate cache short-circuits BEFORE call() is reached, so on a machine with a real cache
    # file a fixture-driven test was served the operator's live stack instead of its fixture -- the
    # suite silently stopped being deterministic. Off for the whole run; the cache's own tests turn it
    # back on around a temp CFG_DIR, which is the only place cached reads are exercised.
    os.environ["MERIDIAN_NO_CACHE"] = "1"
    m = load_meridian()
    test_classify(m)
    test_lazy_imports()
    test_not_configured()
    test_connector_rollup(m)
    test_connector_warning_messages(m)
    test_connector_brief_shape(m)
    test_preflight_coverage(m)
    test_result_cache(m)
    test_connector_runs_pagination(m)
    test_api_guard(m)
    test_insights(m)
    test_profile_shape(m)
    test_compare(m)
    test_tls_posture(m)
    test_summary_completeness(m)
    test_transport(m)
    test_pace(m)
    test_check_classification(m)
    test_field_metadata(m)
    test_top_ladder(m)
    test_stacks_registry(m)
    test_clause_parsing(m)
    test_smartlabels(m)
    test_csv_export(m)
    test_list(m)
    test_digest(m)
    test_report_cleanup(m)
    test_multi_profile_report(m)
    test_snapshots(m)
    test_coverage_identity(m)
    test_trend(m)
    test_trend_report(m)
    test_metrics(m)
    test_entities(m)
    test_vuln_detail(m)
    test_linked_assets_cap(m)
    test_skill_frontmatter()
    test_alert_routing()
    test_skill_rule_survival()
    test_launch_greeting()
    test_doc_output_guard()
    test_retention(m)
    test_alerts(m)
    test_alerts_notify(m)
    test_selfupdate(m)
    test_selfupdate_held_dir(m)
    test_whats_new(m)
    test_package_stamp()
    test_publish_public()
    test_low_findings(m)
    test_update_redirects(m)
    test_selfupdate_followups(m)
    test_community_files(m)
    test_ci_workflow()
    test_hr_routing()
    test_hr_sources(m)
    test_field_cache_refetch(m)
    test_licensing()
    test_brand_fallback(m)
    test_public_variants()
    test_trademark_notice()
    test_dangling_links()
    test_security_hardening(m)
    test_data_currency(m)
    test_historical_labels(m)
    test_currency_routing(m)
    if live:
        test_live()
        test_live_connectors()
        test_live_entities(m)
        test_live_currency(m)
    else:
        print("[21] live connect: SKIPPED (pass --live to run against your stack)")
    print("-" * 42)
    print("%d passed, %d failed" % (_passed, _failed))
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
