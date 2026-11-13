"""No child process inherits the Meridian token or the alert secrets (2026-11-12 security pass).

Credentials and alert targets can be set as environment variables. Until this change no subprocess
call in scripts/meridian.py passed `env=`, so headless Chrome (handed a page of customer data), the
macOS `security` tool and the staged copy an update smoke-tests all started with the bearer token,
the webhook URLs and the SMTP password in their environment.

Each test here was run against the code before the change and failed there. Offline: subprocess.run
is replaced, so nothing is started.
"""
import ast
import os
import subprocess
import types

import pytest
import pytest_check

from test_connect import MERIDIAN_PY, load_meridian

SECRETS = {
    "MERIDIAN_API_TOKEN": "tok-child-env-0001",
    "MERIDIAN_ACTION_TOKEN": "tok-child-env-0002",
    "MERIDIAN_ALERT_SLACK_WEBHOOK": "https://hooks.example.com/services/T0/B0/secret",
    "MERIDIAN_ALERT_TEAMS_WEBHOOK": "https://teams.example.com/webhook/secret",
    "MERIDIAN_ALERT_EMAIL_SMTP_PASS": "smtp-secret",
    "meridian_api_token": "tok-lowercase-0003",   # Windows names are case-insensitive
}


def check(name, got, want):
    pytest_check.equal(got, want, name)


@pytest.fixture
def m(monkeypatch):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin"))
    monkeypatch.setenv("UNRELATED_SETTING", "kept")
    return load_meridian()


@pytest.fixture
def started(monkeypatch):
    """Every subprocess.run call the code under test makes, instead of running it."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append((list(cmd), kw))
        return types.SimpleNamespace(returncode=0, stdout=b"" if not kw.get("text") else "usage: ...", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def leaked(env):
    return sorted(k for k in env if k.upper().startswith("MERIDIAN_")) + sorted(
        v for v in SECRETS.values() if v in env.values())


def test_child_env_drops_meridian_variables_and_keeps_the_rest(m):
    env = m._child_env()
    check("no MERIDIAN_* variable survives, whatever its case", leaked(env), [])
    check("PATH is kept, or nothing could start", "PATH" in env, True)
    check("...and so is everything that isn't ours to guard", env.get("UNRELATED_SETTING"), "kept")
    # The token is still there for this process's own calls. (Which of the two spellings set above
    # won depends on the OS, so only its presence is checked.)
    check("the parent's own environment is untouched", bool(os.environ.get("MERIDIAN_API_TOKEN")), True)


def test_chrome_is_started_without_the_token(m, started, tmp_path):
    html = tmp_path / "r.html"
    html.write_text("<html></html>", encoding="utf-8")
    try:
        m._print_to_pdf("chrome", str(html), str(tmp_path / "r.pdf"))
    except Exception:
        pass   # no PDF appears, since nothing ran; the calls are what is being checked
    check("the browser was started", len(started) >= 1 and started[0][0][0], "chrome")
    for cmd, kw in started:
        check("...with an explicit environment (%s)" % cmd[1], "env" in kw, True)
        check("...carrying no Meridian secret (%s)" % cmd[1], leaked(kw.get("env") or os.environ), [])

    # The retry with the older headless flag is a second call site, and it is the one that runs on
    # exactly the browsers the first call fails on.
    started.clear()
    first = {"n": 0}
    real = subprocess.run

    def fail_once(cmd, **kw):
        first["n"] += 1
        if first["n"] == 1:
            started.append((list(cmd), kw))
            raise subprocess.CalledProcessError(1, cmd)
        return real(cmd, **kw)
    import pytest as _p
    mp = _p.MonkeyPatch()
    mp.setattr(subprocess, "run", fail_once)
    try:
        try:
            m._print_to_pdf("chrome", str(html), str(tmp_path / "r2.pdf"))
        except Exception:
            pass
    finally:
        mp.undo()
    check("the legacy-flag retry ran", [c[0][1] for c in started], ["--headless=new", "--headless"])
    check("...also without the secrets", [leaked(kw.get("env") or os.environ) for _, kw in started], [[], []])


def test_update_smoke_test_is_started_without_the_token(m, started, tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "meridian.py").write_text("print('usage')", encoding="utf-8")
    check("the staged copy passes", m._smoke_test(str(tmp_path)), None)
    check("one interpreter was started", len(started), 1)
    check("...with no Meridian secret: a downloaded copy is never handed the token",
          leaked(started[0][1].get("env") or os.environ), [])


def test_macos_keychain_read_is_started_without_the_token(m, started):
    m._keychain_pems()
    check("the security tool was started", len(started) == 1 and started[0][0][0], m.MACOS_SECURITY)
    check("...with no Meridian secret", leaked(started[0][1].get("env") or os.environ), [])


def test_every_child_process_gets_the_scrubbed_environment():
    """The guard for the fourth call site. Read from the source, because a call nobody has written
    yet cannot be exercised: any way of starting a process must pass env=_child_env()."""
    with open(MERIDIAN_PY, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    starters = {"run", "Popen", "check_output", "check_call", "call", "getoutput", "getstatusoutput"}
    bad, seen = [], 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if isinstance(owner, ast.Name) and owner.id == "subprocess" and node.func.attr in starters:
            seen += 1
            env = [k.value for k in node.keywords if k.arg == "env"]
            ok = (len(env) == 1 and isinstance(env[0], ast.Call)
                  and isinstance(env[0].func, ast.Name) and env[0].func.id == "_child_env")
            if not ok:
                bad.append("line %d: subprocess.%s without env=_child_env()" % (node.lineno, node.func.attr))
        if isinstance(owner, ast.Name) and owner.id == "os" and (
                node.func.attr in ("system", "popen", "startfile") or node.func.attr.startswith(("spawn", "exec"))):
            bad.append("line %d: os.%s starts a process with the full environment" % (node.lineno, node.func.attr))
    check("the scan found the call sites it exists to check", seen >= 4, True)
    check("every child process is started with the scrubbed environment", bad, [])
