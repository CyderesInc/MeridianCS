#!/usr/bin/env python3
"""Sweep this repository for real identifiers before they reach an audience.

    python scripts/check-docs-pii.py                    # generators + every tracked text file
    python scripts/check-docs-pii.py scripts/make-brief.py   # one file, full pattern set

Originally this checked only the three PDF generators, because those PDFs ship inside the package
and one goes to customers. That scope was too narrow, and the gap was not theoretical: a repo-wide
sweep added later found a real demo-stack username in `references/`, the same username in ALLCAPS
form in two more files, a real asset id in two files, and rendered demo-tenant data in SKILL.md's
own presentation example -- which ships in *every* release. Every one of those sat in a file this
script never opened.

So there are now two scopes, and they run different pattern sets on purpose:

  * **The three generators** get the full set, ALLCAPS included. They are prose-light, so a
    name-shaped ALLCAPS token there is nearly always a real name.
  * **Every tracked text file** gets the high-signal patterns only. ALLCAPS is deliberately NOT
    run repo-wide: measured over this tree it produced 80+ matches, essentially all of them
    ordinary words set in caps for emphasis (ABSENCE, COVERAGE, MANDATORY...). A check that noisy
    is a check people learn to skip, which is worse than one with a documented hole.

**The known hole, stated rather than papered over:** a *new* name-shaped ALLCAPS username added to
a non-generator file is not caught automatically. `KNOWN_REAL_IDENTIFIERS` covers re-introduction of
the ones already found -- the realistic failure mode, since it happens by pasting fresh command
output into a doc -- but a genuinely new one needs review. Widening ALLCAPS repo-wide needs a word
list, which is not worth carrying for this.

Two things about how this checks, both learned the hard way:

  * **It reads source, never a rendered PDF.** Headless Chrome subsets the embedded brand fonts, so
    text extracted from the finished PDF is glyph indices -- a grep over it passes silently no
    matter what the page says.
  * **It scans whole files, not a module's public strings.** An earlier version exec'd each
    generator and inspected its UPPERCASE variables, which missed the customer brief's largest
    prose block entirely: that document assembles most of its copy inside `main()`, so the single
    file with the widest audience was the one least covered.

Importable: `make-public.py` reuses `PATTERNS`, the allowlists and `scan_text()` so the public-tree
audit and this gate cannot drift apart.
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
GENERATORS = [os.path.join(HERE, f) for f in ("make-impact.py", "make-guide.py", "make-brief.py")]


def derived_public_tree():
    """True when this is make-public.py's OUTPUT rather than the source repo.

    The public build drops design/ and the three branded-doc generators together, so their joint
    absence is the signal -- and joint is the load-bearing word. A generator missing from the
    source repo means someone deleted a document that then silently stopped being checked, which
    has to stay a hard failure; a generator missing beside an absent design/ is the derivation
    doing what it exists for. Testing only for the generators would turn the first case into the
    second and hand back a green check.

    The public repository runs this same script in CI, where insisting on files that were
    deliberately dropped is a red check describing work already done.
    """
    return (not os.path.isdir(os.path.join(SKILL, "design"))
            and not any(os.path.isfile(g) for g in GENERATORS))

PATTERNS = {
    "IPv4 literal":        r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
    "Meridian stack FQDN": r"\b[a-z0-9][a-z0-9-]*\.lucidum\.cloud\b",
    "asset-id shaped":     r"\bI-[0-9A-Z]{8,}\b",
    "name-shaped ALLCAPS": r"\b[A-Z]{7,}\b",
    "email address":       r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b",
}

# Run repo-wide. ALLCAPS is excluded -- see the module docstring.
REPO_WIDE = ("IPv4 literal", "Meridian stack FQDN", "asset-id shaped", "email address")

# Product, framework and platform words, plus identifiers that appear in the generators as code.
ALLOWED_CAPS = {
    "MERIDIAN", "CYDERES", "LUCIDUM", "SMARTLABEL", "SMARTLABELS", "CROWDSTRIKE", "SENTINELONE",
    "WINDOWS", "USERPROFILE", "POWERSHELL", "IDENTIFY", "PROTECT", "DETECT", "GOVERN", "RESPOND",
    "RECOVER", "DEFAULT", "PATTERNS", "SUBTITLE", "STATCARDS", "PROSE", "STATS", "CARDS", "ASKS",
    "SEATS", "FLOW", "SKILL", "OUTPUT", "SECTION", "CONTENT",
}

# Deliberate placeholder stacks. The packaged docs must never name a real one, and the walkthroughs
# need *something* in the example command, so these are the sanctioned stand-ins.
ALLOWED_FQDN = {"acme.lucidum.cloud", "company.lucidum.cloud", "your-stack.lucidum.cloud",
                "customer.lucidum.cloud"}

# Test fixtures, not disclosures. Explicit rather than a looser pattern, for the same reason the
# FQDN allowlist is explicit: these are boundary cases the suite deliberately exercises (addresses
# just outside RFC1918, CGNAT, multicast, a malformed quad), so a rule broad enough to cover them
# would also cover a real public address.
ALLOWED_IPV4 = {
    "1.2.3.4", "3.4.5.6", "8.8.8.8",            # obvious placeholders / public resolver
    "3.165.255.191",                            # public-IP classification fixture
    "100.64.0.1", "100.128.0.1",                # CGNAT boundary: inside / just outside
    "172.32.0.1", "192.169.0.1",                # just outside RFC1918
    "999.1.1.1",                                # deliberately malformed
}

# Placeholder identifiers introduced when the real ones were removed. Shape-compatible on purpose,
# so the examples still teach the real key format.
ALLOWED_ASSET_ID = {"I-0EXAMPLE0000000"}

ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "corp.example", "example")

# The regression list lives OUTSIDE this file, in design/known-identifiers.json.
#
# It used to be inline, which quietly defeated the purpose: this script ships (make-public.py keeps
# it), so the public tree carried a file naming the exact identifiers that had just been removed --
# and because scanning it reported every entry in its own denylist, the file was skipped by the
# audit, which therefore said "clean". The data and the logic had to be separated: design/ is
# dropped from both the package and the public tree, so that is where the data goes.
#
# Absent (i.e. a public checkout) means pattern-only, reported rather than silent. A contributor
# outside Cyderes has no business holding a list of one tenant's identifiers, and the patterns are
# the part worth sharing.
IDENTIFIER_DATA = os.path.join(SKILL, "design", "known-identifiers.json")


def load_known():
    """(identifiers, figures, available). `available` False == the regression guard is not loaded."""
    try:
        with open(IDENTIFIER_DATA, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return (), (), False
    if data.get("schema") != 1:
        return (), (), False
    figures = tuple(data.get("figures") or ()) + tuple(data.get("percentages") or ())
    return tuple(data.get("identifiers") or ()), figures, True


KNOWN_REAL_IDENTIFIERS, KNOWN_FIGURES, KNOWN_AVAILABLE = load_known()


def figure_pattern(figure):
    """Word-boundary matcher for a measured count, bare or comma-grouped.

    Hand-rolled rather than a word boundary, because a word boundary treats a comma and a dot as
    boundaries: a four-digit figure would then match inside a longer comma-grouped number, and a
    decimal would match inside a version string. A Python version flagged as a tenant statistic is
    exactly the false positive that gets a gate switched off.

    (Deliberately no real figures in this docstring -- the audit scans this file too, so an example
    lifted from the list would flag itself. It did, on the first run.)
    """
    alts = [re.escape(figure)]
    if figure.isdigit() and len(figure) > 3:
        alts.append(re.escape("%s,%s" % (figure[:-3], figure[-3:])))
    # Trailing comma is only disqualifying when it is thousands grouping (comma + exactly three
    # digits). Treating ANY trailing comma as a boundary made every value in a Python dict or
    # list literal invisible -- `: <figure>,` -- which is precisely how the test fixtures are
    # written, so the largest concentration of these figures in the tree was the part the gate
    # could not see. Found by replacing them and noticing the gate had never reported them.
    return re.compile(r"(?<![\d.,])(?:%s)(?![\d.])(?!,\d{3})" % "|".join(alts))


FIGURE_PATTERNS = [(f, figure_pattern(f)) for f in KNOWN_FIGURES]


TEXT_SUFFIXES = (".md", ".py", ".json", ".css", ".svg", ".yml", ".yaml", ".txt", ".ps1")
# LICENSE/NOTICE carry third-party licence text verbatim; their content is not ours to edit.
# This script is NOT skipped any more -- the identifiers moved to design/known-identifiers.json, so
# there is no self-reference left to work around, and the file is scanned like any other.
SKIP_FILES = {"LICENSE", "NOTICE"}

# Internal-only records that must NAME the identifiers they removed, or the removal stops being
# auditable. Safe only because `design/` never reaches an audience: make-package.py drops it
# (EXCLUDE_DIRS) and make-public.py drops it (INTERNAL_DIRS).
#
# make-public.py's audit reads SKIP_FILES, deliberately NOT this -- so if PUBLISH_INTERNAL_DOCS is
# ever flipped, these files fail the publication gate instead of riding out on an exemption that was
# written for a different purpose. An exemption that silently widens when a config flag changes is
# how a gate stops being one.
INTERNAL_ONLY_FILES = {"design/oss-release.md", "design/known-identifiers.json"}

REPO_SWEEP_SKIP = SKIP_FILES | INTERNAL_ONLY_FILES

def real_ip(ip):
    """True if `ip` could plausibly be a real, routable address.

    Narrow on purpose: only provably-reserved ranges are dismissed, so an address this cannot
    classify is reported rather than assumed harmless.
    """
    try:
        o = [int(p) for p in ip.split(".")]
    except ValueError:
        return False
    if len(o) != 4 or any(p > 255 for p in o):
        return False                                       # not an address at all
    if o[0] in (0, 10, 127) or o[:2] == [169, 254]:
        return False                                       # this-host / private / link-local
    if o[0] == 192 and o[1] == 168:
        return False
    if o[0] == 172 and 16 <= o[1] <= 31:
        return False
    if o[:3] in ([192, 0, 2], [198, 51, 100], [203, 0, 113]):
        return False                                       # RFC 5737 documentation ranges
    if o[0] >= 224:
        return False                                       # multicast / reserved
    return True


def scan_text(text, names):
    """[(pattern name, [matches])] for one blob, allowlists applied."""
    out = []
    for name in names:
        hits = set(re.findall(PATTERNS[name], text))
        if name == "name-shaped ALLCAPS":
            hits = {h for h in hits if h not in ALLOWED_CAPS and "EXAMPLE" not in h}
        elif name == "Meridian stack FQDN":
            hits = {h for h in hits if h not in ALLOWED_FQDN}
        elif name == "IPv4 literal":
            hits = {h for h in hits if h not in ALLOWED_IPV4 and real_ip(h)}
        elif name == "asset-id shaped":
            hits = {h for h in hits if h not in ALLOWED_ASSET_ID}
        elif name == "email address":
            hits = {h for h in hits if not h.lower().endswith(ALLOWED_EMAIL_DOMAINS)}
        if hits:
            out.append((name, sorted(hits)))
    return out


def known_identifiers(text):
    return [k for k in KNOWN_REAL_IDENTIFIERS if k in text]


def read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def tracked_text_files():
    out = subprocess.run(["git", "ls-files"], cwd=SKILL, capture_output=True, text=True, check=True)
    files = []
    for rel in (l.strip() for l in out.stdout.splitlines()):
        if not rel or rel in REPO_SWEEP_SKIP or rel.endswith("-OFL.txt"):
            continue
        if rel.lower().endswith(TEXT_SUFFIXES):
            files.append(rel)
    return files


def report(label, hits, revivals):
    if not hits and not revivals:
        return False
    print("\n%s" % label)
    for name, matches in hits:
        print("   FAIL  %-22s %s" % (name, matches[:8]))
    for k in revivals:
        print("   FAIL  removed identifier is back: %s" % k)
    return True


def main():
    explicit = [a for a in sys.argv[1:] if not a.startswith("--")]
    failed = False

    if explicit:
        # Backwards-compatible single-file mode: full pattern set, path as given.
        for path in explicit:
            if not os.path.isfile(path):
                print("NOT A FILE  %s" % path)
                failed = True
                continue
            text = read(path)
            if report(os.path.basename(path), scan_text(text, PATTERNS), known_identifiers(text)):
                failed = True
            else:
                print("\n%s\n   clean" % os.path.basename(path))
        print("")
        print("Real identifiers reached a checked file." if failed else "Clean.")
        return 1 if failed else 0

    derived = derived_public_tree()

    print("Generators (full pattern set)")
    if derived:
        print("   SKIPPED  derived public tree -- the branded-doc generators are not part of it")
    else:
        for path in GENERATORS:
            if not os.path.isfile(path):
                print("   MISSING  %s" % path)
                failed = True
                continue
            text = read(path)
            if report("   " + os.path.basename(path), scan_text(text, PATTERNS),
                      known_identifiers(text)):
                failed = True
        if not failed:
            print("   clean")

    files = tracked_text_files()
    print("\nTracked text files (%d, high-signal patterns)" % len(files))
    repo_failed = False
    for rel in files:
        text = read(os.path.join(SKILL, rel))
        if report("   " + rel, scan_text(text, REPO_WIDE), known_identifiers(text)):
            repo_failed = True
    if not repo_failed:
        print("   clean")
    failed = failed or repo_failed

    print("")
    if derived and not KNOWN_AVAILABLE:
        print("NOTE  design/known-identifiers.json is not part of a derived tree, so the")
        print("      removed-identifier regression guard did not run here. The pattern sweep did,")
        print("      and make-public.py ran both against this tree before it was published.")
    if failed:
        print("A real identifier is present in a file that ships or is published. Remove it at the")
        print("source -- for a generator, fix the generator and regenerate; never edit the PDF.")
    else:
        print("All clean. Generators safe to regenerate; tree carries no known real identifiers.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
