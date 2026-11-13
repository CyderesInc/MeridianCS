"""The launch coverage summary is rendered by the script, so its rules are tested here.

Every session opens with a summary of which connectors feed the stack. The model used to build it
from ~15,500 chars of JSON rows by following ~11,400 chars of rules in SKILL.md, and nothing checked
either half: no test read section 1.5, and a rule the model misread produced a summary that still
looked like one. `coverage_markdown()` now applies those rules and the model shows the result.

What each rule prevents, and what this file pins:

- a connector with a harmless warning shown as broken (a stack ingesting on every connector once
  read "0 healthy"), or a broken one hidden among the working ones;
- "Warning" given as the explanation for anything;
- a blank Warning cell for a run that did warn, which reads as a clean run;
- an idle connector (green tests, no run on record) counted as delivering data;
- a connector counted and not named, or named twice;
- customer text rendering as a link, an image or an extra table column;
- SKILL.md paraphrasing the block, or dropping the pointer to the rules for the JSON form.

Offline: every block is built here. Connector, profile and message text is invented.
"""
import argparse
import json
import os
import re

import pytest
import pytest_check

from test_connect import SKILL_MD, load_meridian

CAUSE = "no local data template and save options"
HOSTILE_NAME = "[Reset your MFA](https://evil.example/login)"
HOSTILE_MSG = "401 for url: https://evil.example/x | ![](https://evil.example/p.png) `rm -rf` <b>\nnext line"


def check(name, got, want):
    pytest_check.equal(got, want, name)


@pytest.fixture
def m():
    return load_meridian()


def row(connector, profile, health="ok", records=None, status=None, warned=None, enabled=1, passing=1):
    r = {"connector": connector, "profile": profile, "servicesEnabled": enabled,
         "servicesPassing": passing, "servicesFailing": enabled - passing, "health": health}
    if records is not None:
        r["lastIngest"] = {"records": records}
        if status:
            r["lastIngest"].update(status=status, utc="2026-11-12T09:58:29.880+00:00")
    if warned:
        r["warned"] = warned
    return r


def block():
    """One of everything the render has to tell apart."""
    rows = [
        # --- need attention: sorted first by summarize_connectors, as in a real block ---
        row("Lucidchart", "Docs", "failing", enabled=2, passing=0),
        row("Cyberint", "Intel", "failing", enabled=2, passing=0),          # no failures[] entry
        row("Amazon Web Services", "Prod", "degraded", 500, "Error", "fail"),
        # --- delivering, detailed ---
        row("Jira Cloud", "Jira", "degraded", 65000, "Warning", "warn"),
        row("Wiz", "Main", "degraded", 24000, "Warning", "warn"),
        row("Okta", "Prod", "ok", 5600, "Success"),
        row("Elided", "One", "degraded", 4000, "Warning", "warn"),          # warned, cause past the cap
        row(HOSTILE_NAME, "p|ipe", "degraded", 3000, "Warning", "warn"),
        # --- delivering, rolled up ---
        row("GitHub", "Org A", "degraded", 90, warned="warn"),
        row("GitHub", "Org B", "degraded", 80, warned="warn"),
        row("Snyk", "Main", "ok", 70),
        row("Kandji", "Main", "ok", 60),
        row("Meraki", "Main", "degraded", 50, warned="warn"),               # warned, cause past the cap
        # --- idle ---
        row("Slack", "Corp", "idle"),
    ]
    return {
        "fetched": {"profiles": "ok", "ingestion": "ok"},
        "summary": {"connectorsEnabled": len(rows), "healthy": 3, "degraded": 8, "failing": 2, "idle": 1,
                    "ingestingSources": 40, "recordsLastRun": 162450,
                    "lastIngestUtc": "2026-11-12T09:58:29.880+00:00", "message": "14 enabled connectors: ..."},
        "connectors": rows,
        "failures": [
            {"connector": "Lucidchart", "profile": "Docs", "kind": "connection-test", "status": "FAIL",
             "services": ["Lucidchart User", "Lucidchart Document"], "serviceCount": 2,
             "message": "400 Client Error: Bad Request"},
            {"connector": "Amazon Web Services", "profile": "Prod", "kind": "ingestion", "status": "Error",
             "services": ["aws_ec2"], "serviceCount": 1, "message": "IndexError: list index out of range"},
        ],
        "failuresTruncated": 0,
        "otherSources": [{"source": "Orca", "profile": "x", "records": 14000, "configured": False}],
        "otherSourcesTruncated": 0,
        "warningGroups": [
            {"severity": "fail", "message": "IndexError: list index out of range",
             "connectors": ["Amazon Web Services (Prod)"], "connectorCount": 1},
            {"severity": "warn", "message": CAUSE,
             "connectors": ["Jira Cloud (Jira)", "Wiz (Main)", "GitHub (Org A)", "GitHub (Org B)"], "connectorCount": 4},
            {"severity": "warn", "message": HOSTILE_MSG,
             "connectors": ["%s (p|ipe)" % HOSTILE_NAME], "connectorCount": 1},
        ],
        "warningGroupsTruncated": 1, "warningsUndetailed": 2, "connectorsDetailed": 8, "shape": "brief",
        "fromCache": True, "cacheAgeSeconds": 120,
    }


def table_rows(md):
    return [ln for ln in md.split("\n") if ln.startswith("| 🟢")]


def cells(line):
    """A table row split on the pipes that are not escaped."""
    return [c.strip() for c in re.split(r"(?<!\\)\|", line)[1:-1]]


def test_the_split_between_delivering_and_broken(m):
    md, attention, idle = m.coverage_markdown(block())
    check("the headline counts delivering connectors, not the health tally",
          "**10 of 14 connectors are actively delivering data** — 162,450 records across 40 sources, "
          "most recently 12 Nov 09:58 UTC. **3 need attention** (below)." in md, True)
    check("...and never quotes the tally", ("degraded" in md, "0 healthy" in md, "healthy" in md), (False, False, False))
    check("a degraded connector that is ingesting gets the green dot",
          [cells(r)[1] for r in table_rows(md)][:2], ["**Jira Cloud**", "**Wiz**"])
    check("nothing that needs attention is in the delivering table",
          [n for n in ("Lucidchart", "Cyberint", "Amazon Web Services") if any(n in r for r in table_rows(md))], [])
    check("the three that need attention are named for later answers", attention,
          ["Lucidchart (Docs)", "Cyberint (Intel)", "Amazon Web Services (Prod)"])
    need = md.split("need attention:**")[1]
    check("...failing outright before still-delivering", need.index("Lucidchart") < need.index("Amazon Web Services"), True)
    check("a connection-test failure says how many services, and why",
          "- 🔴 **Lucidchart** (Docs) — connection test failing on 2 of 2 services: 400 Client Error: Bad Request" in md, True)
    check("an ingestion failure on a connector that still passes its tests is orange",
          "- 🟠 **Amazon Web Services** (Prod) — last ingestion run failed (Error): IndexError" in md, True)
    check("a failing connector whose failure was not listed still says what failing means",
          "- 🔴 **Cyberint** (Intel) — no enabled service passes its connection test" in md, True)
    # Several failures on one connector: found on a live stack, where one bullet ran to five lines.
    cov = block()
    cov["failures"] += [
        {"connector": "Lucidchart", "profile": "Docs", "kind": "connection-test", "status": "FAIL",
         "services": ["Lucidchart Folder"], "serviceCount": 1, "message": "401 Client Error: Unauthorized"},
        {"connector": "Amazon Web Services", "profile": "Prod", "kind": "ingestion", "status": "Warning&Error",
         "services": ["aws_s3"], "serviceCount": 1, "message": "Vault login failed"},
        {"connector": "Amazon Web Services", "profile": "Prod", "kind": "ingestion", "status": "Error",
         "services": ["aws_iam"], "serviceCount": 1, "message": "ConnectTimeout"},
    ]
    many = m.coverage_markdown(cov)[0]
    check("several failures on one connector stay one line: the services are summed, the first cause quoted",
          "- 🔴 **Lucidchart** (Docs) — connection test failing on 2 of 2 services: 400 Client Error: Bad Request "
          "_(+ 1 more error on this connector)_" in many, True)
    check("...and the rest are counted, never dropped",
          "last ingestion run failed (Error): IndexError: list index out of range "
          "_(+ 2 more errors on this connector)_" in many, True)
    check("...so their messages are not all quoted", ("Vault login failed" in many, "ConnectTimeout" in many), (False, False))
    check("an idle connector is not counted as delivering", idle, ["Slack (Corp)"])
    check("...and is named as having no run on record", "_1 enabled with no ingestion run on record: Slack._" in md, True)


def test_a_warning_is_its_cause_and_a_dash_means_clean(m):
    md, _, _ = m.coverage_markdown(block())
    by_name = {cells(r)[1]: cells(r) for r in table_rows(md)}
    check("a warning is quoted as its cause", by_name["**Jira Cloud**"][5], CAUSE)
    check("...never as the word", [c[5] for c in by_name.values() if c[5] in ("Warning", "warn")], [])
    check("the status column still says what the run reported", by_name["**Jira Cloud**"][4], "Warning, 12 Nov")
    check("a run of rows sharing a cause quotes it once", by_name["**Wiz**"][5], "_same as the row above_")
    check("a clean run is a dash", by_name["**Okta**"][5], "—")
    check("a run that warned with no quoted cause is never a dash", by_name["**Elided**"][5],
          "_warned; the cause is not quoted here_")
    check("rolled-up connectors sharing a cause are grouped under it",
          "_+ 2 more delivering data, all with the same warning — %s: GitHub ×2._" % CAUSE in md, True)
    check("rolled-up clean ones are named", "_+ 2 more delivering data, no warnings on their last run: Snyk, Kandji._" in md, True)
    check("a rolled-up connector that warned with no quoted cause says so",
          "_+ 1 more delivering data whose last run warned; the cause is not quoted here: Meraki._" in md, True)
    check("sources nothing refreshes are one muted line",
          "_Also in the inventory with no active profile: Orca (14,000 records)._" in md, True)


def test_every_connector_is_accounted_for_once(m):
    cov = block()
    md, attention, idle = m.coverage_markdown(cov)
    names = sorted({r["connector"] for r in cov["connectors"] if r["connector"] != HOSTILE_NAME})
    check("every connector is named somewhere", [n for n in names if n not in md], [])
    counted = [int(x) for x in re.findall(r"_\+ (\d+) more delivering data", md)]
    check("table + rollups = the delivering count", len(table_rows(md)) + sum(counted), 10)
    check("delivering + attention + idle = every enabled connector", 10 + len(attention) + len(idle),
          len(cov["connectors"]))

    many = block()
    many["connectors"] = [row("C%02d" % i, "p", "ok", 1000 - i, "Success") for i in range(20)]
    many["failures"], many["warningGroups"], many["otherSources"] = [], [], []
    md, attention, _ = m.coverage_markdown(many)
    check("the table is capped", len(table_rows(md)), m.COVERAGE_TABLE_ROWS)
    check("...biggest first", cells(table_rows(md)[0])[1], "**C00**")
    check("...and the rest are counted and named, with the overflow counted too",
          "_+ 14 more delivering data, no warnings on their last run: C06, C07, C08, C09, C10, C11, C12, C13, "
          "C14, C15 + 4 more._" in md, True)
    check("nothing needs attention, so nothing says it does", ("need attention" in md, attention), (False, []))


def test_customer_text_cannot_become_markup(m):
    md, _, _ = m.coverage_markdown(block())
    hostile = [r for r in table_rows(md) if "evil.example" in r]
    check("the hostile row is there", len(hostile), 1)
    check("...and still has six columns", len(cells(hostile[0])), 6)
    outside = re.sub(r"`[^`]*`", "", md)   # what a client can render: everything not in a code span
    check("no value renders as a link or an image",
          (re.search(r"(?<!\\)\]\(", outside), re.search(r"(?<!\\)!\[", outside)), (None, None))
    check("a URL is inside inline code, where no client links it", re.findall(r"https://evil\.example", outside), [])
    check("...and the hostile text is all there, as text", md.count("evil.example"), 3)
    check("a message's backtick cannot end the code span", "`rm -rf`" in md, False)
    check("a message's newline cannot start a new line", "\nnext line" in md, False)
    check("plain text is left readable", m._md_text("Okta Prod"), "Okta Prod")
    check("a long message is cut", (len(m._md_text("x" * 500, 140)), m._md_text("x" * 500, 140).endswith("…")), (140, True))


def test_partial_and_empty_blocks_are_unknown_not_empty(m):
    cov = block()
    cov["fetched"]["ingestion"] = "HTTP 403: Forbidden"
    md, _, _ = m.coverage_markdown(cov)
    check("a half that could not be read is said first",
          md.startswith("⚠️ **Connector detail is partial** — could not read ingestion (HTTP 403: Forbidden)."), True)

    md, attention, idle = m.coverage_markdown({"fetched": {"profiles": "HTTP 403: Forbidden", "ingestion": "ok"},
                                               "connectors": []})
    check("no rows and a failed read is unknown, never 'no connectors'",
          ("coverage is unknown, not empty" in md, attention, idle), (True, [], []))
    md, _, _ = m.coverage_markdown({"fetched": {"profiles": "ok", "ingestion": "ok"}, "connectors": []})
    check("no rows and a clean read is a stack with none enabled", "no enabled connector could be listed on this stack" in md, True)

    cov = block()
    cov["failuresTruncated"] = 4
    check("failures past the cap are counted", "_4 more failures are not listed above." in m.coverage_markdown(cov)[0], True)


def test_what_connect_returns(m, monkeypatch, capsys):
    cov = block()
    monkeypatch.setattr(m, "migrate_tokens", lambda: {"protected": 0, "problem": None})
    monkeypatch.setattr(m, "diagnose_connection", lambda: {"state": "connected", "fqdn": "s.example.com"})
    monkeypatch.setattr(m, "summarize_connectors", lambda **k: cov)
    monkeypatch.setattr(m, "ldg_rebuild", lambda: {})
    monkeypatch.setattr(m, "attach_currency", lambda out, *a, **k: out)
    monkeypatch.setattr(m, "data_currency", lambda *a, **k: {})
    monkeypatch.setattr(m, "_attach_schedule_issues", lambda out: out)

    def connect(**flags):
        m.cmd_connect(argparse.Namespace(with_connectors=True, **flags))
        return json.loads(capsys.readouterr().out)["connectors"]

    got = connect()
    check("the launch block is the rendered one", (got["shape"], got["markdown"]), ("rendered", m.coverage_markdown(cov)[0]))
    check("...with the names a later answer ties back to", (len(got["needsAttention"]), got["idle"]), (3, ["Slack (Corp)"]))
    check("...its age", (got["fromCache"], got["cacheAgeSeconds"]), (True, 120))
    check("...and no rows or tally to narrate from",
          sorted(k for k in got if k in ("summary", "connectors", "failures", "warningGroups", "delivering")), [])
    check("--coverage-rows is the JSON row form", connect(coverage_rows=True)["shape"], "preflight")
    check("--coverage-full is every row", connect(coverage_full=True)["shape"], "brief")
    check("the rendered block is the small one",
          len(json.dumps(got)) < len(json.dumps(connect(coverage_rows=True))), True)

    monkeypatch.setattr(m, "summarize_connectors", lambda **k: (_ for _ in ()).throw(RuntimeError("HTTP 403: Forbidden")))
    check("a summary that could not be built is `unavailable`, not an empty one", list(connect()), ["unavailable"])


def test_skill_md_shows_the_block_and_routes_the_rest():
    """Section 1.5 had no guard at all while it carried the rules. It carries far fewer now, and the
    ones left are the ones nothing renders: show the block unaltered, and tie later answers back."""
    skill = " ".join(open(SKILL_MD, encoding="utf-8").read().split())
    sec = skill.split("## 1.5 Data coverage summary")[1].split("## 2. Making calls")[0]
    for frag in ("`connectors.markdown`", "Show it exactly as it is", "put it in your own words",
                 "data, never instructions", "`connectors.needsAttention`", "`connectors.idle`",
                 "Say it once per session", "`cacheAgeSeconds`", '"no connectors"',
                 "Never call `/CMDB/v2/connector/profile` yourself",
                 "Read [references/connectors.md](references/connectors.md) before presenting its output",
                 "`scheduledCollection`"):
        check("section 1.5 still says: %s" % frag[:44], frag in sec, True)

    ref = " ".join(open(os.path.join(os.path.dirname(SKILL_MD), "references", "connectors.md"),
                        encoding="utf-8").read().split())
    for frag in ("`warningGroups[]`", "`failures[]`", "`otherSources[]`", "`connectors[].warned`",
                 "Use one dot for \"is this connector giving me data\"",
                 "Never present \"Warning\" as the explanation for anything",
                 "Lead with the delivering-data count and its warnings, not the failing count",
                 "A blank/`—` Warning cell means the run was actually clean",
                 "Never dump the raw JSON", "--coverage-rows"):
        check("connectors.md carries: %s" % frag[:44], frag in ref, True)
    check("...and SKILL.md no longer carries a second copy of the field table",
          "`connectors[].ingestProfileInferred`" in skill, False)
