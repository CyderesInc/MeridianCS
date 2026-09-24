#!/usr/bin/env python3
"""Derive the open-source distribution tree from this repository.

    python scripts/make-public.py <target-dir> [--allow-dirty] [--force]

This repository is the INTERNAL skill: `~/.claude/skills/meridiancs` is a symlink to this working
tree, and merging to `main` deploys it. So the public variant is *derived*, never carved out of this
tree by deletion -- stripping the brand assets here would strip them from the running internal
skill, and stripping the probed API behaviour from `references/` would delete knowledge the skill
depends on to build correct queries.

Deriving instead of forking is also what keeps the two from drifting. This repo already has a
documented history of that failure: the package once shipped two merged PRs behind, and the docs
have trailed the tree by a commit. A hand-maintained public fork is that bug with a longer fuse.

## What it removes, and why

**Brand assets** (`assets/cyderes-*.svg`, `assets/meridian-logo.svg`, `assets/cyderes-report.css`).
Apache-2.0 section 6 grants no trademark rights, but the practical risk is not licensing: with the
wordmarks in a public repo, anyone can generate an authentic-looking Cyderes-branded risk report.
No code change is needed to drop them -- `_load_css`, `_logo_svg`, `_meridian_svg` and
`_font_face_css` in `meridian.py` each check `os.path.exists` and fall back to neutral styling, so
the report path degrades to unbranded output on its own. `evals/test_connect.py` asserts that
fallback, so the derived tree's report path is covered by the same suite as everything else.

The four **fonts stay**: they are SIL OFL 1.1, explicitly redistributable, and are not Cyderes
brand assets -- they are the Brand Style Guide's *public alternates* to the proprietary PX Grotesk.

**The three branded PDFs and their generators.** `make-brief.py` is customer-facing commercial
positioning and `make-impact.py` is internal stakeholder reporting; neither is part of the tool.

**`CLAUDE.md` and `design/`.** Internal engineering record -- candid about shipped defects and
parked decisions. Useful to a contributor, but that is a publishing decision to make deliberately
rather than by omission; flip `PUBLISH_INTERNAL_DOCS` if it is made the other way.

## What it refuses to do

`SANITIZE_REQUIRED` lists the reference docs that describe the vendor API in more detail than
Lucidum's own public documentation states -- endpoint corrections, undocumented response quirks,
and statistics measured against a live demo tenant. Each needs a hand-written public variant at
`references/<name>.public.md`, which is substituted for the internal one. There is deliberately no
"skip it" flag: the whole point is that an unreviewed vendor-API doc cannot reach a public repo by
someone forgetting a step. Sanitising prose is an editorial judgement per sentence, so it is not
something this script attempts to automate.

`REBRAND_REQUIRED` needs the same treatment for a different reason. README.md promises
"Cyderes-branded PDF reports", lists the brand assets in its layout tree, and links two branded
PDFs -- all three dropped here. Nothing in the prose is unsafe to publish; it is simply untrue of
the tree it would ship in, and the two dead links are what the audit actually catches. Together
the two sets are `SUBSTITUTE_REQUIRED`, and the staleness stamp covers all of it: a variant is a
reviewed derivative, so editing the internal doc invalidates the review whichever set it is in.

Output is a directory, not a zip -- it is what seeds the public repo, so it wants to be inspected
and diffed before anything is pushed.
"""
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)


def _load_pii_gate():
    """Import check-docs-pii.py by path -- the hyphen makes it unimportable by name.

    Sharing it rather than re-declaring the patterns here is deliberate: two copies of a PII
    allowlist drift, and the copy that drifts is the one that stops catching things.
    """
    path = os.path.join(HERE, "check-docs-pii.py")
    spec = importlib.util.spec_from_file_location("check_docs_pii", path)
    if spec is None or spec.loader is None:
        raise SystemExit("cannot load %s" % path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PII = _load_pii_gate()

# Decided 2026-09-23: never. CLAUDE.md and design/ hold counsel's advice, the PII gate's denylist and
# dismissed scan findings -- see design/oss-release.md. test_public_variants pins this False.
PUBLISH_INTERNAL_DOCS = False

# Markdown inline links, for the dangling-link audit below.
MD_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")

# Cyderes trademarks and Brand Style Guide styling. Not licensed for redistribution; see NOTICE.
BRAND_ASSETS = {
    "assets/cyderes-logo.svg",
    "assets/meridian-logo.svg",
    "assets/cyderes-report.css",
}

# Branded deliverables and the generators that build them.
BRANDED_DOCS = {
    "Cyderes-Meridian-Skill-Guide.pdf",
    "Cyderes-Meridian-Skill-Enhancements.pdf",
    "Cyderes-MeridianCS-Brief.pdf",
    "scripts/make-guide.py",
    "scripts/make-impact.py",
    "scripts/make-brief.py",
}

INTERNAL_DOCS = {"CLAUDE.md"}
INTERNAL_DIRS = ("design/",)

# Reference docs that must be replaced by a reviewed public variant before publication.
SANITIZE_REQUIRED = {
    "references/api-reference.md",
    "references/authentication.md",
    "references/query-syntax.md",
    "references/field-map.md",
}

# Docs needing a reviewed public variant for a DIFFERENT reason: not vendor-API detail, but
# Cyderes brand prose and links to files the public build drops. README.md promises "branded
# PDF reports", lists the brand assets in its layout tree, and links two branded PDFs that are
# not in the public tree -- so the audit's dangling-link check fails on it, which is how this
# was noticed at all rather than by anyone re-reading it. Sanitising and re-branding are
# different editorial jobs sharing one mechanism, so they are separate sets, one substitution.
REBRAND_REQUIRED = {"README.md"}

# Every doc shipped from a hand-written variant instead of its own contents. The staleness stamp
# covers the whole set: a variant is a REVIEWED derivative, so an edit to either source
# invalidates the review, whichever reason put it here.
SUBSTITUTE_REQUIRED = SANITIZE_REQUIRED | REBRAND_REQUIRED

# Must survive into the output, or the distribution is not licence-compliant.
REQUIRED_OUTPUT = (
    "LICENSE",
    "NOTICE",
    "sbom.cdx.json",
    "assets/fonts/SpaceGrotesk-OFL.txt",
    "assets/fonts/SpaceMono-OFL.txt",
    "SKILL.md",
    "scripts/meridian.py",
)



# Records the internal doc each public variant was reviewed against. A public variant is a
# *reviewed derivative*, so an edit to its source invalidates the review -- which is a thing that
# has to be enforced, because the failure is silent: the internal doc gains a new measured figure,
# the public one keeps saying something slightly false, and nothing looks broken.
SYNC_STAMP = "references/.public-sync.json"
SYNC_SCHEMA = 1

# Machinery of the derivation, not part of what is derived. The stamp records digests of internal
# docs against variants the public tree does not contain, so it cannot mean anything there -- and
# unlike the drops above, this one is unconditional: flipping PUBLISH_INTERNAL_DOCS is a decision
# to publish the engineering record, not to publish a review ledger with nothing left to review.
DERIVATION_ARTIFACTS = {SYNC_STAMP}

# Source-repo automation that cannot work in a derived tree. Dependabot there would open pull
# requests against a tree the next release commit replaces wholesale, so none could ever merge;
# updates are taken in this repo and reach the public one through the derivation.
SOURCE_ONLY = {".github/dependabot.yml"}


def doc_digest(path):
    """sha256 of a doc's content with line endings normalised.

    Normalised because git converts line endings on checkout: a raw byte hash would differ between
    a Windows working tree and a Linux CI runner, and a stamp that fails on one platform only is
    worse than no stamp.
    """
    with open(path, encoding="utf-8") as f:
        text = f.read()
    return hashlib.sha256(chr(10).join(text.splitlines()).encode("utf-8")).hexdigest()


def load_sync():
    path = os.path.join(SKILL, SYNC_STAMP)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if data.get("schema") != SYNC_SCHEMA:
        return {}
    return data.get("reviewed") or {}


def write_sync(reviewed):
    path = os.path.join(SKILL, SYNC_STAMP)
    body = {
        "schema": SYNC_SCHEMA,
        "note": ("sha256 (LF-normalised) of each internal reference doc as of the last review of "
                 "its .public.md variant. make-public.py refuses to build when these differ: the "
                 "public variant is a reviewed derivative, so a change to the source invalidates "
                 "the review. Re-read the diff, update the variant, then --resync."),
        "reviewed": dict(sorted(reviewed.items())),
    }
    with open(path, "w", encoding="utf-8", newline=chr(10)) as f:
        f.write(json.dumps(body, indent=2) + chr(10))


def stale_variants():
    """[(internal, recorded, actual)] for docs whose source moved since the variant was reviewed."""
    reviewed = load_sync()
    out = []
    for internal in sorted(SUBSTITUTE_REQUIRED):
        path = os.path.join(SKILL, internal)
        if not os.path.exists(path):
            continue
        actual = doc_digest(path)
        recorded = reviewed.get(internal)
        if recorded != actual:
            out.append((internal, recorded, actual))
    return out


def dirty_paths():
    out = subprocess.run(["git", "status", "--porcelain"], cwd=SKILL,
                         capture_output=True, text=True)
    return [l for l in out.stdout.splitlines() if l.strip()]


def tracked_files():
    out = subprocess.run(["git", "ls-files"], cwd=SKILL, capture_output=True, text=True, check=True)
    return [f.strip() for f in out.stdout.splitlines() if f.strip()]


def plan(files):
    """(keep, substitutions, dropped, missing_variants).

    `substitutions` maps an output path to the source path it is taken from, which is how a
    `.public.md` variant lands under the internal doc's name.
    """
    drop = set(BRAND_ASSETS) | set(BRANDED_DOCS) | set(DERIVATION_ARTIFACTS) | set(SOURCE_ONLY)
    if not PUBLISH_INTERNAL_DOCS:
        drop |= INTERNAL_DOCS

    keep, subs, dropped, missing = [], {}, [], []
    available = set(files)
    for f in files:
        if f in drop or (not PUBLISH_INTERNAL_DOCS and f.startswith(INTERNAL_DIRS)):
            dropped.append(f)
            continue
        if f.endswith(".public.md"):
            continue            # a source for a substitution, never shipped under its own name
        if f in SUBSTITUTE_REQUIRED:
            variant = f[:-3] + ".public.md"
            if variant in available:
                subs[f] = variant
                keep.append(f)
            else:
                missing.append((f, variant))
            continue
        keep.append(f)
    return keep, subs, dropped, missing


def copy_tree(keep, subs, target):
    for rel in keep:
        src = os.path.join(SKILL, subs.get(rel, rel))
        dst = os.path.join(target, rel)
        parent = os.path.dirname(dst)
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.copy2(src, dst)


def audit(target):
    """Post-conditions on the derived tree. Returns a list of problems (empty == publishable).

    This is the last gate before a public repository, so it re-checks the output rather than
    trusting `plan()`: a brand asset arriving by a path nobody predicted, or an identifier riding
    in on a substituted doc, both fail here. Patterns come from check-docs-pii.py so the two agree
    by construction.
    """
    problems = []

    for rel in REQUIRED_OUTPUT:
        if not os.path.exists(os.path.join(target, rel)):
            problems.append("missing required file: %s" % rel)

    for rel in sorted(BRAND_ASSETS):
        if os.path.exists(os.path.join(target, rel)):
            problems.append("brand asset survived into the output: %s" % rel)

    for root, _dirs, names in os.walk(target):
        for name in names:
            if not name.lower().endswith(PII.TEXT_SUFFIXES):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, target).replace(os.sep, "/")
            if rel in PII.SKIP_FILES or rel.endswith("-OFL.txt"):
                continue
            try:
                text = PII.read(path)
            except OSError as exc:
                problems.append("unreadable: %s (%s)" % (rel, exc))
                continue
            for label, matches in PII.scan_text(text, PII.REPO_WIDE):
                for hit in matches:
                    problems.append("%s: %s -> %s" % (rel, label, hit))
            for hit in PII.known_identifiers(text):
                problems.append("%s: removed identifier is back -> %s" % (rel, hit))
            # Measured demo-tenant aggregates. Individually not PII; together they describe one
            # customer's estate, and the .public.md reference docs deliberately replaced them with
            # illustrative round numbers. Checked here because the first sanitisation pass covered
            # the four reference docs and missed that the same figures also sit in SKILL.md, the
            # recipes, the CSF mapping and the test fixtures -- all of which ship.
            for figure, rx in PII.FIGURE_PATTERNS:
                if rx.search(text):
                    problems.append("%s: measured tenant figure -> %s" % (rel, figure))

    # Dangling relative links. Dropping a file leaves every link to it broken, and nothing else
    # here would notice: the drop lists and the prose pointing at them live in different files. This
    # is what turns "we held a doc back" into "and here is what still references it" -- including
    # SKILL.md's routing, which instructs the model to read a reference doc *before* answering, so a
    # missing target degrades an answer rather than producing an error someone would see.
    for root, _dirs, names in os.walk(target):
        for name in names:
            if not name.lower().endswith(".md"):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, target).replace(os.sep, "/")
            try:
                text = PII.read(path)
            except OSError:
                continue                      # already reported by the sweep above
            for link in MD_LINK_RE.findall(text):
                target_path = link.split("#", 1)[0]
                if not target_path or "://" in target_path or target_path.startswith("mailto:"):
                    continue
                if not os.path.exists(os.path.join(os.path.dirname(path), *target_path.split("/"))):
                    problems.append("%s: links to a file that is not in the public tree -> %s"
                                    % (rel, target_path))

    if not PII.KNOWN_AVAILABLE:
        problems.append("design/known-identifiers.json is unreadable, so the regression guard and "
                        "the tenant-figure check did not run. Derive from a full checkout.")
    return problems


def main():
    argv = sys.argv[1:]
    allow_dirty = "--allow-dirty" in argv
    force = "--force" in argv

    if "--resync" in argv:
        # Stamp the current internal docs as reviewed. Run this only after actually re-reading the
        # diff and updating the variant -- it is the button that says "I looked", so a habit of
        # pressing it to make a build pass turns the whole gate off.
        reviewed = load_sync()
        stale = stale_variants()
        if not stale:
            print("Already in sync; nothing to stamp.")
            return 0
        for internal, _recorded, actual in stale:
            reviewed[internal] = actual
            print("stamped %s" % internal)
        write_sync(reviewed)
        print("Wrote %s" % SYNC_STAMP)
        return 0

    positional = [a for a in argv if not a.startswith("--")]
    if len(positional) != 1:
        print(__doc__.strip().splitlines()[2].strip())
        return 2
    target = os.path.abspath(positional[0])

    if os.path.abspath(SKILL) == target:
        print("Refusing to write over this repository. The public tree is a separate directory.")
        return 2

    dirty = dirty_paths()
    if dirty and not allow_dirty:
        print("Working tree is dirty; the derived tree would not match any commit:")
        for line in dirty[:20]:
            print("   " + line)
        print("Commit first, or pass --allow-dirty if you know that's what you want.")
        return 2

    files = tracked_files()
    keep, subs, dropped, missing = plan(files)

    if missing:
        print("Refusing to derive a public tree: %d doc(s) have no reviewed public "
              "variant.\n" % len(missing))
        for internal, variant in missing:
            why = ("brand prose, and links to files the public build drops"
                   if internal in REBRAND_REQUIRED else
                   "vendor-API detail beyond Lucidum's public documentation")
            print("   %s  ->  needs  %s   (%s)" % (internal, variant, why))
        print("\nA reference doc here describes the vendor API beyond what Lucidum's")
        print("public documentation states -- endpoint corrections, undocumented quirks,")
        print("live-tenant statistics. README asserts a brand this distribution does not")
        print("carry. Either way: write the variant by hand and review it. There is no flag")
        print("to skip this.")
        return 1

    stale = stale_variants()
    if stale:
        print("Refusing to derive a public tree: %d internal doc(s) changed since their public "
              "variant was reviewed." % len(stale))
        print("")
        for internal, recorded, actual in stale:
            variant = internal[:-3] + ".public.md"
            state = "never reviewed" if recorded is None else "reviewed at %s" % recorded[:12]
            print("   %s  (%s, now %s)" % (internal, state, actual[:12]))
            print("       -> re-read the diff, update %s, then: make-public.py --resync" % variant)
        print("")
        print("A public variant is a reviewed derivative, so a change to its source invalidates")
        print("the review. This fails loudly because the alternative is silent: the internal doc")
        print("gains a measured figure, the public one keeps saying something slightly false, and")
        print("nothing looks broken.")
        return 1


    if os.path.exists(target) and os.listdir(target):
        if not force:
            print("Target exists and is not empty: %s\nPass --force to replace it." % target)
            return 2
        shutil.rmtree(target)
    os.makedirs(target, exist_ok=True)

    copy_tree(keep, subs, target)
    problems = audit(target)

    print("Derived public tree: %s" % target)
    print("  kept        %d files" % len(keep))
    print("  substituted %d reference doc(s): %s"
          % (len(subs), ", ".join(sorted(subs)) or "none"))
    print("  dropped     %d files" % len(dropped))
    for f in sorted(dropped):
        print("                - %s" % f)
    if dirty:
        print("  NOTE: built from a DIRTY tree")

    if problems:
        print("\nNOT PUBLISHABLE -- %d problem(s):" % len(problems))
        for p in problems:
            print("   %s" % p)
        return 1
    print("\nAudit clean: no brand assets, no unallowed identifiers, licence files present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
