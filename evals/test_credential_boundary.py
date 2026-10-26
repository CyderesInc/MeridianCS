"""Credential-boundary findings from the 2026-10-19 security review of scripts/meridian.py.

Each test here was run against the code before its fix and failed there; the docstrings say what
it caught. Offline: every request goes to a fake _round_trip, and every file to a temp CFG_DIR, so
nothing reads or writes the operator's real ~/.meridian.
"""
import contextlib
import io
import json
import os
import ssl
import tempfile
import zipfile

import pytest
import pytest_check

from test_connect import load_meridian


def check(name, got, want):
    pytest_check.equal(got, want, name)


@pytest.fixture(scope="module")
def m():
    return load_meridian()


def _env(m, monkeypatch, tmp_path, config=None, stacks=None):
    tmp = tempfile.mkdtemp(dir=tmp_path)
    monkeypatch.setattr(m, "CFG_DIR", tmp)
    monkeypatch.setattr(m, "CFG_PATH", os.path.join(tmp, "config.json"))
    monkeypatch.setattr(m, "STACKS_PATH", os.path.join(tmp, "stacks.json"))
    monkeypatch.setattr(m, "_CFG_CACHE", None)
    monkeypatch.setattr(m, "pace", lambda: None)
    for v in ("MERIDIAN_FQDN", "MERIDIAN_API_TOKEN", "MERIDIAN_ACTION_TOKEN", "MERIDIAN_INSECURE_TLS",
              "MERIDIAN_CA_BUNDLE"):
        monkeypatch.delenv(v, raising=False)
    if config is not None:
        with open(m.CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f)
    if stacks is not None:
        with open(m.STACKS_PATH, "w", encoding="utf-8") as f:
            json.dump(stacks, f)
    return tmp


def _recorder(m, monkeypatch, status=200, body='{"assetCount": 1, "userCount": 1}'):
    sent = []

    def rt(fqdn, key, ctx, method, endpoint, data, headers):
        sent.append((fqdn, endpoint, headers.get("Authorization")))
        return status, body, None, None

    monkeypatch.setattr(m, "_round_trip", rt)
    return sent


# --- 1. MERIDIAN_FQDN and the saved token resolved independently ------------------------------------

def test_env_fqdn_does_not_borrow_saved_token(m, monkeypatch, tmp_path):
    """MERIDIAN_FQDN=other-host with no MERIDIAN_API_TOKEN sent config.json's token, which belongs
    to the stack config.json names, to other-host -- and with it that stack's insecure_tls."""
    _env(m, monkeypatch, tmp_path, config={"fqdn": "home.example.com", "api_token": "tok-home-1234",
                                          "action_token": "act-home-5678", "insecure_tls": True})
    monkeypatch.setattr(m, "_ssl_context", lambda: None)
    monkeypatch.setenv("MERIDIAN_FQDN", "other.example.com")
    sent = _recorder(m, monkeypatch)

    check("the saved token is not resolved for another host", m.load_config()[1:], (None, None))
    check("...so nothing is resolvable", m._config_resolvable(), False)
    check("...and the saved stack's insecure_tls does not apply there", m.insecure_tls(), False)
    with pytest.raises(SystemExit):
        m.call("GET", "/CMDB/v2/system/metrics/data", retries=0)
    out = m.diagnose_connection()
    check("connect reports not_configured", out.get("state"), "not_configured")
    check("...naming the env var as the cause", "MERIDIAN_FQDN" in out.get("message", ""), True)
    check("...and the saved stack's address", "home.example.com" in out.get("message", ""), True)
    check("no request was sent anywhere", sent, [])

    # Both directions: the env address naming the SAME stack keeps the saved token, whatever its case
    # or scheme, and so does no env address at all.
    for env_fqdn in ("https://HOME.example.com/", None):
        monkeypatch.setattr(m, "_CFG_CACHE", None)
        if env_fqdn:
            monkeypatch.setenv("MERIDIAN_FQDN", env_fqdn)
        else:
            monkeypatch.delenv("MERIDIAN_FQDN")
        check("the saved token still applies to its own stack (%s)" % env_fqdn,
              m.load_config()[1], "tok-home-1234")
        check("...as does its insecure_tls (%s)" % env_fqdn, m.insecure_tls(), True)
        check("...and diagnose sends it (%s)" % env_fqdn, m.diagnose_connection().get("state"), "connected")
    check("...to its own host only", sorted({s[0].lower() for s in sent}), ["home.example.com"])

    # A token from the environment goes wherever the environment says: both halves are the user's.
    sent.clear()
    monkeypatch.setattr(m, "_CFG_CACHE", None)
    monkeypatch.setenv("MERIDIAN_FQDN", "other.example.com")
    monkeypatch.setenv("MERIDIAN_API_TOKEN", "tok-env-9999")
    m.call("GET", "/CMDB/v2/system/metrics/data", retries=0)
    check("an env token goes to the env address", sent, [("other.example.com", "/CMDB/v2/system/metrics/data",
                                                         "Bearer tok-env-9999")])


# --- 2. _scrub's quoted-key shapes ---------------------------------------------------------------------

@pytest.mark.parametrize("text,secret", [
    ("{'password': 'hunter2'}", "hunter2"),
    ('{"client_secret": "y-secret-val"}', "y-secret-val"),
    (json.dumps(json.dumps({"client_secret": "esc-secret"})), "esc-secret"),
    ("password: 'two words here'", "words"),
    ('{"api_key": "has spaces in it"}', "spaces"),
    ("login pass=pw-plain-1 rejected", "pw-plain-1"),
    ("{'pass': 'pw-quoted-2'}", "pw-quoted-2"),
    ("key AKIAABCDEFGHIJKLMNOP leaked", "AKIAABCDEFGHIJKLMNOP"),
    ("password=still-works", "still-works"),
    ("Authorization: Bearer abcdefghijklmnop", "abcdefghijklmnop"),
])
def test_scrub_quoted_keys(m, text, secret):
    """{'password': 'x'} and {"client_secret": "y"} passed straight through: the closing quote sits
    between the key and the colon, and the pattern wanted the colon next."""
    out = m._scrub(text)
    check("%r is redacted" % text, secret in out, False)
    check("...and marked", m.REDACTED in out, True)


@pytest.mark.parametrize("text", [
    "HTTPSConnectionPool(host='api.example.com', port=443): Read timed out.",
    "401 Client Error: Unauthorized for url",
    "Login failed: password expired",
    "The token has expired",
    "tests passed: 3 of 4",
    "password_reset_required: true",
    '{"status": "FAIL", "message": "Connection refused"}',
    "Passing the AKIA prefix alone is not a key",
    "'pass' was not set",
])
def test_scrub_leaves_ordinary_text(m, text):
    """The other direction: connector messages with no secret in them come through unchanged."""
    check("%r unchanged" % text, m._scrub(text), text)


# --- 3. selfupdate --apply --force downgraded ---------------------------------------------------------

def test_selfupdate_never_downgrades(m, monkeypatch, tmp_path):
    """--apply --force on state=ahead installed the OLDER release; --force was only meant to
    reinstall the version already installed."""
    install = tmp_path / "skills" / "meridiancs"
    install.mkdir(parents=True)
    applied = []
    monkeypatch.setattr(m, "_read_updatecheck", lambda: None)
    monkeypatch.setattr(m, "apply_update", lambda rel: applied.append(rel["latestVersion"]) or {"applied": True})

    def run(state, installed, latest):
        monkeypatch.setattr(m, "check_update", lambda force=False: {
            "state": state, "installedVersion": installed, "latestVersion": latest,
            "assetUrl": "https://example.com/p.zip", "sigUrl": "https://example.com/p.zip.sig"})

        class A:
            apply, force = True, True
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            m.cmd_selfupdate(A())
        return json.loads(buf.getvalue())

    out = run("ahead", "2.31.0", "2.30.0")
    check("a forced apply onto an older release is refused", out.get("applied"), False)
    check("...without installing anything", applied, [])
    check("...saying why", "older" in (out.get("reason") or ""), True)
    out = run("current", "2.31.0", "2.31.0")
    check("--force still reinstalls the same version", applied, ["2.31.0"])

    # apply_update itself refuses, before a byte is downloaded, whoever calls it.
    monkeypatch.undo()
    fetched = []
    monkeypatch.setattr(m, "INSTALL_REAL", str(install))
    monkeypatch.setattr(m, "install_markers", lambda: [])
    monkeypatch.setattr(m, "_download_asset", lambda url, dest: fetched.append(url))
    with pytest.raises(ValueError):
        m.apply_update({"installedVersion": "2.31.0", "latestVersion": "2.30.0",
                        "assetUrl": "https://example.com/p.zip", "sigUrl": "https://example.com/s"})
    check("apply_update downloads nothing for a downgrade", fetched, [])


# --- 4. a stack switch dropped the global config keys ---------------------------------------------------

def test_switch_keeps_global_config_keys(m, monkeypatch, tmp_path):
    """Switching to a different stack rebuilt config.json from scratch, so `"autoupdate": false`
    -- the self-update opt-out -- and `ca_bundle` silently disappeared."""
    _env(m, monkeypatch, tmp_path,
         config={"fqdn": "a.example.com", "api_token": "tok-a", "insecure_tls": True,
                 "entity_salt": "salt-a", "autoupdate": False, "ca_bundle": "C:/certs/proxy.pem",
                 "hand_set_per_stack": 1})
    reg = {"active": "b", "stacks": {"a": {"fqdn": "a.example.com", "api_token": "tok-a"},
                                     "b": {"fqdn": "b.example.com", "api_token": "tok-b"}}}
    m.mirror_active_to_config(reg)
    with open(m.CFG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    check("the switch lands", (cfg.get("fqdn"), cfg.get("api_token")), ("b.example.com", "tok-b"))
    check("the self-update opt-out survives it", cfg.get("autoupdate"), False)
    check("...and so does ca_bundle", cfg.get("ca_bundle"), "C:/certs/proxy.pem")
    check("autoupdate_disabled() still says so", bool(m.autoupdate_disabled()), True)
    check("the previous stack's insecure_tls does not come along", "insecure_tls" in cfg, False)
    check("...nor its salt", "entity_salt" in cfg, False)
    check("...nor anything else set for it", "hand_set_per_stack" in cfg, False)


# --- 5. an unreadable stacks.json was overwritten ---------------------------------------------------------

@pytest.mark.parametrize("bad", [b'{"active": "a", "stacks": {"a": {"fqdn": "a.ex', b"[1, 2]"])
def test_unreadable_registry_is_not_overwritten(m, monkeypatch, capsys, tmp_path, bad):
    """load_stacks() turned any read failure into an empty registry, and the next save_stacks()
    wrote it back -- every other stack's token and entity_salt gone."""
    _env(m, monkeypatch, tmp_path)
    with open(m.STACKS_PATH, "wb") as f:
        f.write(bad)

    class A:
        name, fqdn, token, action_token = "new", "new.example.com", "tok-new", None
    with pytest.raises(SystemExit):
        m.cmd_stacks_add(A())
    check("stacks add refuses", "stacks.json" in capsys.readouterr().err, True)
    with open(m.STACKS_PATH, "rb") as f:
        check("...and the file is untouched", f.read(), bad)
    with pytest.raises(Exception):
        m.save_stacks(m.load_stacks())
    with open(m.STACKS_PATH, "rb") as f:
        check("save_stacks refuses a registry that was never read", f.read(), bad)

    for verb, args in ((m.cmd_stacks_switch, {"name": "a"}), (m.cmd_stacks_rm, {"name": "a"}),
                       (m.cmd_stacks_move, {"name": "a", "fqdn": "acme.meridian-cyderes.com"})):
        with pytest.raises(SystemExit):
            verb(type("A", (), args)())
        check("%s refuses" % verb.__name__, "stacks.json" in capsys.readouterr().err, True)
    with open(m.STACKS_PATH, "rb") as f:
        check("...and nothing rewrote it", f.read(), bad)


# --- 6. stacks move -----------------------------------------------------------------------------------------

def test_move_verifies_and_stays_on_successor(m, monkeypatch, capsys, tmp_path):
    """`stacks move` probed the new host with the old stack's insecure_tls and carried the opt-out
    there, and took any --fqdn at all, so one command sent the saved token to an arbitrary host over
    an unverified connection."""
    _env(m, monkeypatch, tmp_path)
    monkeypatch.setattr(m, "_ssl_context", lambda: None)

    class Add:
        name, fqdn, token, action_token, insecure_tls = "acme", "acme.lucidum.cloud", "tok-acme-1111", None, True
    m.cmd_stacks_add(Add())
    capsys.readouterr()
    probes = []

    def rt(fqdn, key, ctx, method, endpoint, data, headers):
        probes.append((fqdn, m.insecure_tls(), key[1]))
        return 200, '{"assetCount": 1, "userCount": 1}', None, None
    monkeypatch.setattr(m, "_round_trip", rt)

    def move(fqdn, **kw):
        a = type("A", (), dict({"name": "acme", "fqdn": fqdn}, **kw))()
        m.cmd_stacks_move(a)
        return json.loads(capsys.readouterr().out)

    with pytest.raises(SystemExit):
        move("evil.example.net")
    check("an address that isn't the successor is refused", "successor" in capsys.readouterr().err, True)
    check("...before any request", probes, [])

    out = move("acme.meridian-cyderes.com")
    check("the successor move goes through", out.get("moved"), True)
    check("...probed with verification ON", probes, [("acme.meridian-cyderes.com", False, False)])
    entry = m.load_stacks()["stacks"]["acme"]
    check("...and insecure_tls did not move with it", "insecure_tls" in entry, False)
    with open(m.CFG_PATH, encoding="utf-8") as f:
        check("...nor into config.json", "insecure_tls" in json.load(f), False)
    check("...and the result says so", "insecure_tls" in json.dumps(out), True)

    # The override exists for a genuinely different address, and still verifies.
    probes.clear()
    out = move("acme.other.example.com", any_fqdn=True)
    check("--any-fqdn allows another address", out.get("moved"), True)
    check("...still verified", probes, [("acme.other.example.com", False, False)])


# --- 7. the `api` guard -----------------------------------------------------------------------------------------

def _api(m, endpoint, method="GET"):
    a = type("A", (), {"method": method, "body": None, "body_file": None, "endpoint": endpoint})()
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            m.cmd_api(a)
    except SystemExit:
        return None, err.getvalue()
    return out.getvalue(), None


@pytest.mark.parametrize("endpoint", [
    "CMDB/v2/connector/profile/123",
    "CMDB/v2/connector/profile/service",
    "CMDB/v2/system/metrics/connector/aws_ec2",
    "CMDB/v2/connector/profile%20",
    "CMDB/v2/connector/profile.",
    "CMDB/v2/connector/profile.%20.",
    "CMDB/v2/connector/profile%3Fsize=1",
    "CMDB/v2/connector/profil%2565%253F",
])
def test_api_guard_spellings(m, monkeypatch, endpoint):
    """The guard compared the exact path, so a sub-path, a trailing dot or space a server may strip,
    and an encoded `?` all named the credential endpoint without matching it."""
    sent = []
    monkeypatch.setattr(m, "call", lambda *a, **k: sent.append(a) or {})
    out, err = _api(m, endpoint)
    check("%s is refused" % endpoint, err is not None, True)
    check("...without a request", sent, [])


@pytest.mark.parametrize("endpoint", ["CMDB/v2/connector", "CMDB/v2/connector/profiles-help",
                                      "CMDB/v2/system/metrics/data", "CMDB/v2/data/cmdb?page=1"])
def test_api_guard_still_allows(m, monkeypatch, endpoint):
    sent = []
    monkeypatch.setattr(m, "call", lambda *a, **k: sent.append(a[1]) or {})
    out, err = _api(m, endpoint)
    check("%s still allowed" % endpoint, (err, len(sent)), (None, 1))


def test_api_credential_write_withholds_response(m, monkeypatch):
    """Prefix matching would have blocked `PUT connector/profile/service`, the documented way to
    switch a connector's services. It still runs once confirmed, but its response is not printed."""
    sent = []
    monkeypatch.setattr(m, "call", lambda *a, **k: sent.append(a[:2]) or {"password": "hunter2-put"})
    a = type("A", (), {"method": "PUT", "body": "{}", "body_file": None,
                       "endpoint": "CMDB/v2/connector/profile/service"})()
    with pytest.raises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
        m.cmd_api(a)
    check("without --allow-write it is refused", sent, [])
    a.allow_write = True
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m.cmd_api(a)
    check("with it, the write is sent", sent, [("PUT", "CMDB/v2/connector/profile/service")])
    check("...and the response withheld", "hunter2-put" in buf.getvalue(), False)
    check("...saying so", "responseWithheld" in buf.getvalue(), True)
    sent.clear()
    a.endpoint = "CMDB/v2/connector/profile"
    with pytest.raises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
        m.cmd_api(a)
    check("a write to the credential endpoint itself is still refused", sent, [])


def test_api_redirect_is_guarded(m, monkeypatch, tmp_path):
    """call() follows a same-host redirect, and cmd_api checked only the endpoint it was given, so a
    redirect from an innocuous path to connector/profile printed the credentials anyway."""
    _env(m, monkeypatch, tmp_path)
    monkeypatch.setattr(m, "_CFG_CACHE", ("stack.example.com", "tok-1234", None))
    monkeypatch.setattr(m, "_ssl_context", lambda: None)
    hops = []

    def rt(fqdn, key, ctx, method, endpoint, data, headers):
        hops.append(endpoint)
        if "profile" in endpoint:
            return 200, '{"password": "hunter2-redirected"}', None, None
        return 302, "", "/CMDB/v2/connector/profile", None
    monkeypatch.setattr(m, "_round_trip", rt)
    with pytest.raises(Exception) as e:
        _api(m, "CMDB/v2/system/metrics/data")
    check("the redirect to the credential endpoint is not followed", hops, ["/CMDB/v2/system/metrics/data"])
    check("...and the error says so", "redirect" in str(e.value), True)

    # An ordinary same-host redirect still works for `api`.
    hops.clear()

    def rt2(fqdn, key, ctx, method, endpoint, data, headers):
        hops.append(endpoint)
        if endpoint.endswith("/"):
            return 200, '{"ok": 1}', None, None
        return 301, "", endpoint + "/", None
    monkeypatch.setattr(m, "_round_trip", rt2)
    out, err = _api(m, "CMDB/v2/system/metrics/data")
    check("a harmless redirect is followed", json.loads(out), {"ok": 1})


@pytest.mark.parametrize("endpoint,payload,secret", [
    ("CMDB/v2/connector/test/result?traceId=abc",
     {"test_result": [{"name": "x", "status": "FAIL", "message": "login failed password=hunter2-test",
                       "details": ["url https://svc:pw-in-url@example.com/x"]}], "status": "done"},
     ["hunter2-test", "pw-in-url"]),
    ("CMDB/v2/system/metrics/action-jobs/7",
     [{"id": 1, "action_status_msg": "POST failed: Authorization: Bearer abcdefghijklmnop rejected"}],
     ["abcdefghijklmnop"]),
])
def test_api_scrubs_free_text_endpoints(m, monkeypatch, endpoint, payload, secret):
    """Connector test results and Action run messages are free text a connector wrote, and quote the
    same credentials the `connectors` allow-list scrubs -- `api` printed them raw."""
    monkeypatch.setattr(m, "call", lambda *a, **k: payload)
    out, err = _api(m, endpoint)
    for s in secret:
        check("%s scrubbed from %s" % (s, endpoint), s in out, False)
    check("...the rest intact", "status" in out or "action_status_msg" in out, True)


def test_call_error_bodies_are_scrubbed(m, monkeypatch, tmp_path):
    """call() quotes up to 300 characters of a failed response in its error, and for the
    credential endpoints that body can be the profile itself."""
    _env(m, monkeypatch, tmp_path)
    monkeypatch.setattr(m, "_CFG_CACHE", ("stack.example.com", "tok-1234", None))
    monkeypatch.setattr(m, "_ssl_context", lambda: None)
    for status, body in ((401, '{"error": "denied", "password": "hunter2-err"}'),
                         (200, "<html>client_secret: 'hunter2-err'</html>")):
        _recorder(m, monkeypatch, status=status, body=body)
        with pytest.raises(RuntimeError) as e:
            m.call("GET", "/CMDB/v2/connector/profile", retries=0)
        check("HTTP %s body scrubbed" % status, "hunter2-err" in str(e.value), False)
        check("...keeping the HTTP shape classify_connect_error parses",
              str(e.value).startswith("HTTP %s: " % status), True)


# --- 8. small hardening -------------------------------------------------------------------------------------------

def test_tls_minimum_version(m, monkeypatch):
    """Python 3.9's default context still allows TLS 1.0/1.1; the floor is now set explicitly."""
    real = ssl.create_default_context

    def old_defaults(*a, **k):
        ctx = real(*a, **k)
        ctx.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
        return ctx
    monkeypatch.setattr(m.ssl, "create_default_context", old_defaults)
    monkeypatch.setattr(m, "_SSL_CTX", {})
    monkeypatch.setattr(m, "_EXTRA_TRUST", ([], {}))
    check("a verifying context refuses below TLS 1.2", m.verifying_context().minimum_version,
          ssl.TLSVersion.TLSv1_2)
    monkeypatch.setenv("MERIDIAN_INSECURE_TLS", "1")
    check("...and so does the insecure_tls one", m._ssl_context().minimum_version, ssl.TLSVersion.TLSv1_2)


def test_action_token_by_canonical_path(m, monkeypatch, tmp_path):
    """The Action token went to any endpoint whose text contained `/data/ldg`, a query string
    included."""
    _env(m, monkeypatch, tmp_path)
    monkeypatch.setattr(m, "_CFG_CACHE", ("stack.example.com", "tok-api-1111", "tok-act-2222"))
    monkeypatch.setattr(m, "_ssl_context", lambda: None)
    sent = _recorder(m, monkeypatch, body="{}")
    for ep, want in (("/CMDB/v2/data/cmdb?x=/data/ldg", "tok-api-1111"),
                     ("/CMDB/v2/system/metrics/data/ldg-notes", "tok-api-1111"),
                     ("/CMDB/v2/data/ldg", "tok-act-2222"),
                     ("/CMDB/v2/data/ldg/x", "tok-act-2222"),
                     ("CMDB/v2/data/ldg?page=1", "tok-act-2222")):
        sent.clear()
        m.call("POST", ep, {}, retries=0)
        check("%s gets %s" % (ep, want), sent[0][2], "Bearer " + want)


def test_update_zip_read_from_verified_bytes(m, monkeypatch, tmp_path):
    """apply_update verified the signature over bytes read from package.zip, then reopened the file
    by path for zipfile, so whatever was on disk at that moment is what got parsed and extracted."""
    install = tmp_path / "skills" / "meridiancs"
    install.mkdir(parents=True)
    monkeypatch.setattr(m, "INSTALL_REAL", str(install))
    monkeypatch.setattr(m, "install_markers", lambda: [])
    good = io.BytesIO()
    with zipfile.ZipFile(good, "w") as zf:
        zf.writestr("meridiancs/SIGNED", "signed")
    paths = {}

    def download(url, dest):
        data = good.getvalue() if url.endswith(".zip") else b"sig"
        with open(dest, "wb") as f:
            f.write(data)
        paths[url] = dest
        return len(data)

    def verify(body, sig):
        # The attacker's window: the file on disk changes after the bytes were verified.
        with open(paths["https://example.com/p.zip"], "wb") as f:
            f.write(b"not the package that was signed")
        return "test-key"
    seen = []

    def validate(zf, want):
        seen.extend(zf.namelist())
        raise ValueError("stop here")
    monkeypatch.setattr(m, "_download_asset", download)
    monkeypatch.setattr(m, "verify_release_signature", verify)
    monkeypatch.setattr(m, "_validate_package", validate)
    with pytest.raises(Exception):
        m.apply_update({"installedVersion": "2.30.0", "latestVersion": "2.31.0",
                        "assetUrl": "https://example.com/p.zip", "sigUrl": "https://example.com/p.zip.sig"})
    check("the zip parsed is the one whose signature was checked", seen, ["meridiancs/SIGNED"])
