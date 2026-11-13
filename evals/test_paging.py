"""Paging on a slow stack: fewer records, in smaller pages, and the same answer. Or a flag.

`top` and `list` counted pages, which is the right unit while a 100-record page takes a couple of
seconds. On one stack it takes about twelve, whatever the records weigh: 100 assets 11.6s, 100 users
11.2s. The same reads as one concurrent wave of small pages took 2-4s, and six count probes (0.5s)
showed a tighter `top` threshold left 12 records to read instead of 50. So the seconds a record costs
are measured, a slow page is split, and `top` tightens while the surplus outweighs the probes.

The same measurements found that the API can return one record on two pages, and then another on
none: about a third of the rows in 4 pages of 100 from the unfiltered asset list were repeats. More pages
is more ways to be wrong, so a split is kept only when `_id`s prove its pages disjoint, and an
ordinary multi-page read that overlaps now says so instead of passing for complete.

What has to stay true, and what each test here pins:

- a stack with ordinary timings, or one never measured, reads `top` exactly as it always did;
- a split read returns every record once, in order, and a failed page still raises;
- a split whose pages overlap, come back the wrong length or carry no `_id` is thrown away;
- a tightened threshold gives the same ranking as the loose one;
- overlapping pages in a read that cannot be repeated are dropped, counted and reported;
- the cache gains a duration and nothing else.

Offline: `call` is a fake stack, and every file lives in a temp directory.
"""
import argparse
import json
import os

import pytest
import pytest_check

from test_connect import SKILL_MD, load_meridian

SLOW = 0.28    # seconds per record: 50 records in 14s, as measured on the stack that prompted this
FAST = 0.023   # 100 records in 2.3s


def check(name, got, want):
    pytest_check.equal(got, want, name)


@pytest.fixture
def m(monkeypatch, tmp_path):
    mod = load_meridian()
    monkeypatch.setattr(mod, "CFG_DIR", str(tmp_path))
    monkeypatch.setattr(mod, "load_config", lambda: ("s.example", "tok", None))
    monkeypatch.setattr(mod, "check_fields", lambda *a, **k: None)
    return mod


class Stack:
    """A fake /data/cmdb: `scores` are the records, in the server's own (unsorted) order.

    `overlap(size)` returns how many records each page of that size repeats from the page before it,
    which is the API fault this file exists for."""

    def __init__(self, scores, honour_page_size=True, seconds_per_record=None, fail_page=None,
                 fail_count_at=(), overlap=lambda size: 0, ids=True):
        self.records = [{"Asset_Name": "A%d" % i, "Risk_Score": s, "Risk_Level": "x",
                         "IP_Address": "192.0.2.1", "OS": "linux", "Junk": "j"}
                        for i, s in enumerate(scores)]
        if ids:
            for i, r in enumerate(self.records):
                r["_id"] = "%024x" % i
        self.honour, self.spr, self.fail_page = honour_page_size, seconds_per_record, fail_page
        self.fail_count_at, self.overlap = set(fail_count_at), overlap
        self.sent = []      # every paging dict, in order
        self.probes = []    # every count threshold
        self.mod = None

    def __call__(self, method, endpoint, body=None, retries=1):
        paging = body["paging"]
        self.sent.append(dict(paging))
        clauses = [c for group in body["query"] for c in group]
        floor = max([c["value"] for c in clauses if c["operator"] == ">="] or [float("-inf")])
        match = [r for r in self.records if r["Risk_Score"] >= floor]
        if paging["page"] == self.mod.COUNT_PAGE:
            self.probes.append(floor)
            if floor in self.fail_count_at:
                raise RuntimeError("HTTP 500: boom")
            return {"totalRecords": len(match), "data": []}
        if paging["page"] == self.fail_page:
            raise RuntimeError("HTTP 500: page lost")
        size = paging["recordsPerPage"] if self.honour else 100
        start = paging["page"] * (size - self.overlap(size))
        data = match[start:start + size]
        if self.spr:
            self.mod._CALL_STATS.seconds = self.spr * len(data)
        return {"totalRecords": len(match), "data": data}

    def pages(self):
        return [p for p in self.sent if p["page"] != self.mod.COUNT_PAGE]


def stack(m, monkeypatch, *args, **kw):
    s = Stack(*args, **kw)
    s.mod = m
    monkeypatch.setattr(m, "call", s)
    return s


def slow(m, table="asset"):
    m._record_seconds_save(table, SLOW * 50, 50, alone=True)


def names(page):
    return [r["Asset_Name"] for r in page]


# 60 records from 3000 to 8900 above a floor of 400 low ones, shuffled the same way every run.
TAIL = [3000 + 100 * ((i * 37) % 60) for i in range(60)]
SCORES = TAIL[:30] + [float(i % 900) for i in range(400)] + TAIL[30:]
TOP10 = sorted(TAIL, reverse=True)[:10]


def test_page_plan(m):
    for n, spr, want in [
        (50, None, (100, 1)),          # never measured: as before
        (50, FAST, (100, 1)),
        (300, FAST, (100, 3)),
        (100, 0.05, (100, 1)),         # a 5s page is not yet slow
        (50, 0.116, (7, 8)),           # the measured case: 6s as one page of 50, 1.9s as 8 pages of 7
        (106, 0.106, (14, 8)),         # ...and the user tail: 11.2s as two pages, 2.4s as eight
        (12, 0.116, (6, 2)),
        (300, 0.116, (38, 8)),
        (800, 0.116, (100, 8)),        # already a full wave: left alone
        (2000, 0.116, (100, 20)),
        (1, 5.0, (100, 1)),            # one record cannot be split
        (0, 0.116, (100, 0)),
    ]:
        check("page_plan(%s, %s)" % (n, spr), m.page_plan(n, spr), want)
    bad = []
    for spr in (None, FAST, 0.05, 0.116, SLOW, 5.0):
        for n in range(0, 2100):
            per, pages = m.page_plan(n, spr)
            old = (n + 99) // 100
            if per * pages < n or pages > max(old, m.PAGE_SPLIT_MAX) or not 1 <= per <= 100:
                bad.append((n, spr, per, pages))
    check("every plan covers its records, in at most one wave", bad, [])


def test_what_counts_as_a_measurement(m):
    m._record_seconds_save("asset", 0.4, 5, alone=True)
    check("a few records measure the round trip, not the records", m._record_seconds("asset"), None)
    m._record_seconds_save("asset", 3.0, 14, alone=False)
    check("pages fetched together never set the figure", m._record_seconds("asset"), None)
    m._record_seconds_save("asset", 11.6, 100, alone=True)
    check("a page fetched alone does", m._record_seconds("asset"), 0.116)
    m._record_seconds_save("asset", 3.2, 14, alone=False)
    check("...and pages fetched together cannot raise it", m._record_seconds("asset"), 0.116)
    m._record_seconds_save("asset", 0.3, 14, alone=False)
    check("...only lower it, which is how a stack that got faster stops being split",
          m._record_seconds("asset"), 0.0214)
    m._record_seconds_save("asset", 2.3, 100, alone=True)
    check("a new figure within a fifth of the old one is not worth a write", m._record_seconds("asset"), 0.0214)
    m._record_seconds_save("asset", 11.6, 100, alone=True)
    with open(m._top_cache_path(), encoding="utf-8") as f:
        check("the cache holds a duration and nothing from a record", json.load(f), {"recordSeconds:asset": 0.116})

    check("a table never measured borrows the stack's other figure", m._record_seconds("user"), 0.116)
    m._record_seconds_save("user", 0.3, 14, alone=False)
    check("...which pages fetched together still cannot turn into one of its own",
          m._record_seconds("user", own=True), None)
    m._record_seconds_save("user", 2.3, 100, alone=True)
    check("...and its own measurement wins once it has one", (m._record_seconds("user"), m._record_seconds("asset")),
          (0.023, 0.116))


def test_fast_and_unmeasured_stacks_read_top_as_before(m, monkeypatch):
    s = stack(m, monkeypatch, SCORES)
    base = m.top_n("asset", "Risk_Score", 10)
    unmeasured = list(s.sent)
    check("unmeasured: the ranking", [r["Risk_Score"] for r in base["top"]], TOP10)
    check("...read as one page of 100", s.pages(), [{"page": 0, "recordsPerPage": 100}])
    check("...and an untimed response measures nothing", m._record_seconds("asset"), None)

    os.remove(m._top_cache_path())   # the rung it just cached would change the descent
    m._record_seconds_save("asset", FAST * 100, 100, alone=True)
    s.sent.clear()
    fast = m.top_n("asset", "Risk_Score", 10)
    check("fast: exactly the same requests", s.sent, unmeasured)
    check("...and the same answer", fast, base)


def test_top_tightens_and_splits_on_a_slow_stack(m, monkeypatch):
    s = stack(m, monkeypatch, SCORES, seconds_per_record=SLOW)
    base = m.top_n("asset", "Risk_Score", 10)
    check("the first read is whole, and is what measures the stack", (s.pages(), m._record_seconds("asset")),
          ([{"page": 0, "recordsPerPage": 100}], SLOW))
    os.remove(m._top_cache_path())
    slow(m)
    s.sent.clear()
    s.probes.clear()

    out = m.top_n("asset", "Risk_Score", 10)
    check("the same ranking as the loose threshold gave", out["top"], base["top"])
    check("...from far fewer records", (base["totalInTail"], out["totalInTail"]), (60, 11))
    check("...at a threshold that was seen to fill", out["matchedAtThreshold"], 7812.5)
    check("...after two rounds of three probes", s.probes[-6:], [4750.0, 6500.0, 8250.0, 6937.5, 7375.0, 7812.5])
    check("...read as small pages", s.pages(), [{"page": p, "recordsPerPage": 3} for p in range(4)])
    check("...with every call counted", out["apiCalls"], len(s.sent))
    check("...nothing flagged", (out.get("tailIncomplete"), out.get("duplicateRows"), out.get("note")), (None, None, None))
    check("...the ladder rung, not the refined point, remembered",
          m.TOP_LADDER[m._top_rung_start("asset", "Risk_Score")], 3000)
    check("...and the stack still measured slow afterwards", m._record_seconds("asset"), SLOW)


def test_a_failed_probe_moves_neither_end(m, monkeypatch):
    stack(m, monkeypatch, SCORES, fail_count_at=[6500.0])
    slow(m)
    out = m.top_n("asset", "Risk_Score", 10)
    check("the ranking survives a probe that errored", [r["Risk_Score"] for r in out["top"]], TOP10)
    check("...at the tightest threshold a successful probe filled", (out["matchedAtThreshold"], out["totalInTail"]),
          (7375.0, 16))


def test_warm_tight_hit_brackets_only_when_slow(m, monkeypatch):
    scores = [1000.0 + 80 * i for i in range(25)] + [5.0] * 50
    s = stack(m, monkeypatch, scores)
    m._top_rung_save("asset", "Risk_Score", 1000)
    m.top_n("asset", "Risk_Score", 10)
    check("fast and tight (25 for a top 10): one probe, as before", s.probes, [1000.0])

    slow(m)
    s.probes.clear()
    out = m.top_n("asset", "Risk_Score", 10)
    check("slow: the rung above is probed to bracket it", s.probes[:2], [1000.0, 3000.0])
    check("...and the tail shrinks", (out["matchedAtThreshold"], out["totalInTail"]), (2000.0, 12))
    check("...to the same top 10", [r["Risk_Score"] for r in out["top"]], sorted(scores, reverse=True)[:10])


@pytest.mark.parametrize("why, kw", [
    ("the server ignores the page size", {"honour_page_size": False}),
    ("its pages overlap", {"overlap": lambda size: 0 if size == 100 else 2}),
    ("its records carry no _id to check", {"ids": False}),
])
def test_a_split_that_cannot_be_trusted_is_thrown_away(m, monkeypatch, why, kw):
    s = stack(m, monkeypatch, [float(i) for i in range(50)], **kw)
    rows, calls, total, duplicates = m.read_records("asset", [[]], 50, names, total=50, spr=SLOW)
    check("%s: every record once, in order" % why, rows, ["A%d" % i for i in range(50)])
    check("...from the split wave, then one whole page", (calls, s.sent[-1]), (9, {"page": 0, "recordsPerPage": 100}))
    check("...which cannot overlap itself, so nothing is flagged", (total, duplicates), (50, 0))


def test_split_read_details(m, monkeypatch):
    s = stack(m, monkeypatch, [float(i) for i in range(300)], seconds_per_record=SLOW)
    slow(m)
    rows, calls, total, duplicates = m.read_records("asset", [[]], 300, names, spr=SLOW)
    check("300 slow records: 8 pages of 38", s.pages(), [{"page": p, "recordsPerPage": 38} for p in range(8)])
    check("...every record once, in order", rows, ["A%d" % i for i in range(300)])
    check("...total read from the pages, nothing flagged", (calls, total, duplicates), (8, 300, 0))

    stack(m, monkeypatch, [float(i) for i in range(50)], fail_page=5)
    with pytest.raises(RuntimeError, match="page lost"):
        m.read_records("asset", [[]], 50, names, total=50, spr=SLOW)


def _list(m, **kw):
    a = argparse.Namespace(table="asset", where=None, select="Asset_Name", all=False, limit=50,
                           count_only=False)
    for k, v in kw.items():
        setattr(a, k, v)
    return a, m.list_records(a)


FILTER = ["Risk_Score >= Float 0"]


def test_list(m, monkeypatch):
    s = stack(m, monkeypatch, [float(i) for i in range(5000)], seconds_per_record=FAST)
    _, first = _list(m)
    check("one page of the 50 asked for, not 100 to show 50", s.sent, [{"page": 0, "recordsPerPage": 50}])
    check("...50 rows of 5000", (first["shown"], first["totalRecords"], first["truncated"], first["apiCalls"]),
          (50, 5000, True, 1))
    check("...and it measured the stack", m._record_seconds("asset"), FAST)

    os.remove(m._top_cache_path())
    s = stack(m, monkeypatch, [float(i) for i in range(5000)], seconds_per_record=SLOW)
    slow(m)
    for label, where in (("unfiltered", None), ("filtered", FILTER)):
        s.sent.clear()
        _, out = _list(m, where=where)
        check("slow, %s: still one page -- a slice of a larger result is never split" % label, s.sent,
              [{"page": 0, "recordsPerPage": 50}])
        check("...the same rows", out["rows"], first["rows"])

    s = stack(m, monkeypatch, [float(i) for i in range(12)])
    _, out = _list(m, where=FILTER)
    check("fewer matches than the limit: all 12, not truncated",
          (out["shown"], out["totalRecords"], out["truncated"], out.get("note")), (12, 12, False, None))

    s = stack(m, monkeypatch, [float(i) for i in range(300)])
    _, out = _list(m, all=True)
    check("slow --all, the whole result: a count, then 8 pages of 38", s.sent,
          [{"page": m.COUNT_PAGE, "recordsPerPage": 1}] + [{"page": p, "recordsPerPage": 38} for p in range(8)])
    check("...every row once, in order", [r["Asset_Name"] for r in out["rows"]], ["A%d" % i for i in range(300)])
    check("...9 calls, complete", (out["apiCalls"], out["truncated"], out.get("note")), (9, False, None))

    s = stack(m, monkeypatch, [float(i) for i in range(5000)])
    _, out = _list(m, limit=250)
    check("slow --limit 250 of 5000, a slice: pages of 100, as before", s.pages(),
          [{"page": p, "recordsPerPage": 100} for p in range(3)])
    check("...250 rows", (out["shown"], out["apiCalls"]), (250, 4))

    s = stack(m, monkeypatch, [float(i) for i in range(900)])
    _, out = _list(m, all=True)
    check("slow --all past one wave: pages of 100, as before", s.pages(),
          [{"page": p, "recordsPerPage": 100} for p in range(9)])
    check("...all 900", out["shown"], 900)


def test_overlapping_pages_are_reported_not_passed_off(m, monkeypatch):
    """The fault as measured: pages of 100 that repeat records. Nothing can be re-read to fix it, so
    the repeats go and the result says how many matching records it is missing."""
    stack(m, monkeypatch, [float(i) for i in range(300)], overlap=lambda size: 20)
    _, out = _list(m, all=True)
    got = [r["Asset_Name"] for r in out["rows"]]
    check("list --all: no row twice", len(got) == len(set(got)), True)
    check("...40 short, and it says so", (out["shown"], out["totalRecords"], out["duplicateRows"], out["truncated"]),
          (260, 300, 40, True))
    check("...in words that don't suggest --all would help",
          ("on more than one page" in out["note"], "Pass --all" in out["note"]), (True, False))

    stack(m, monkeypatch, [float(i) for i in range(900)], overlap=lambda size: 20)
    _, out = _list(m, limit=250)
    check("list --limit 250 of 900: both notes", ("on more than one page" in out["note"], "Showing" in out["note"]),
          (True, True))

    # 300 records over the 3000 rung on a fast stack: three pages, no tightening, and they overlap.
    stack(m, monkeypatch, [3000.0 + i for i in range(300)] + [1.0] * 50, overlap=lambda size: 20)
    out = m.top_n("asset", "Risk_Score", 10)
    check("top: a tail read short is flagged", (out["tailIncomplete"], out["duplicateRows"]), (True, 40))
    check("...as possibly not the true top N", out["note"].startswith("This may not be the true top 10."), True)

    stack(m, monkeypatch, [3000.0 + i for i in range(300)] + [1.0] * 50, overlap=lambda size: 20, ids=False)
    out = m.top_n("asset", "Risk_Score", 10)
    check("no _id to check with: unchanged, as before this existed", out.get("tailIncomplete"), None)


def test_overlap_routing(m, monkeypatch, capsys, tmp_path):
    """SKILL.md tells the model what `duplicateRows` and `tailIncomplete` mean, and the script emits
    what SKILL.md names.

    The flags shipped in v2.33.0 with only a `note` to carry them, and a note is advice. The failure
    this exists for is one sentence: "here are your 300 assets" over a list that holds 212 of them, or
    "the top 10" over a ranking that never read every candidate. SKILL.md is prose the model follows,
    so no behaviour test notices that rule being dropped; and a rule naming a key the script does not
    emit would be a rule that never fires. Both directions, as with test_alert_routing."""
    skill = " ".join(open(SKILL_MD, encoding="utf-8").read().split())
    for why, frag in [
        ("the flag is named and means records are missing",
         "`duplicateRows` means the stack sent some records twice, so the same number are missing"),
        ("...and has to be said, with the count", "say so, with the count"),
        ("a list's count is still right", "`totalRecords` is still exact, because it is a count"),
        ("a short ranking is named too", "`tailIncomplete: true`"),
        ("...and is never presented as the top N", 'may not be the true top N'),
        ("...provisional, not final", 'never as "the top N"'),
        ("re-running is not the fix", "Don't re-run to fix it"),
        ("the fix that works is offered", "fits one page (100 records), which cannot overlap"),
        ("a CSV carries the same gap", "A `--format csv` file has the same gap"),
        ("the answer's caveat line lists both", "a `truncated` or `tailIncomplete` top-N, `duplicateRows`"),
        ("a report hands the caveat on", "(`truncated`, `complete`, `unaccountedRecords`, `duplicateRows`)"),
    ]:
        check("SKILL.md says: %s" % why, frag in skill, True)

    ref = " ".join(open(os.path.join(os.path.dirname(SKILL_MD), "references", "scripts.md"),
                        encoding="utf-8").read().split())
    for frag in ("`duplicateRows` means the pages overlapped", "checked by `_id`", "Re-running does not help",
                 "`tailIncomplete: true` with `duplicateRows`"):
        check("scripts.md carries the reasoning: %s" % frag[:40], frag in ref, True)

    # The other direction: every key and phrase the rule relies on is what the script produces.
    stack(m, monkeypatch, [float(i) for i in range(300)], overlap=lambda size: 20)
    _, out = _list(m, all=True)
    check("list emits the keys SKILL.md names", (out["duplicateRows"], out["shown"], out["totalRecords"], out["truncated"]),
          (40, 260, 300, True))
    check("...and its note gives the remedy SKILL.md offers", "fits one page (100 records)" in out["note"], True)
    csv_path = str(tmp_path / "rows.csv")
    m.emit(dict(out), "rows", "csv", csv_path)
    envelope = json.loads(capsys.readouterr().out)
    check("a CSV export keeps the flag in the envelope it prints", (envelope.get("duplicateRows"), "rows" in envelope),
          (40, False))

    stack(m, monkeypatch, [3000.0 + i for i in range(300)] + [1.0] * 50, overlap=lambda size: 20)
    out = m.top_n("asset", "Risk_Score", 10)
    check("top emits both keys together", (out["tailIncomplete"], out["duplicateRows"]), (True, 40))
    check("...and says it in the words SKILL.md uses", "may not be the true top" in out["note"], True)

    stack(m, monkeypatch, [float(i) for i in range(300)])
    _, out = _list(m, all=True)
    check("a clean read carries neither key, so their absence means something",
          [k for k in out if k in ("duplicateRows", "tailIncomplete")], [])
