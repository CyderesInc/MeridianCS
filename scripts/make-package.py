"""Build the distributable skill package, meridiancs.v<VERSION>.skill.zip.

    python scripts/make-package.py --version 2.3.3 [--allow-dirty]
    python scripts/make-package.py [output.zip] [--allow-dirty]     # explicit name, one-offs

Not a skill verb -- a maintenance tool alongside make-guide.py / make-impact.py. The zip is
gitignored and distributed out of band (a GitHub Release), so it is a build artifact, not source.

The filename carries the release version, so a downloaded package says which release it is without
being opened. Nothing in the tree stores a version -- it lives in the git tag -- so the version is
either passed explicitly or read from a tag on HEAD, and this script refuses to guess. See
resolve_version() for why guessing is the dangerous option here.

Contents come from `git ls-files` and nothing else, plus exactly ONE synthesized member,
VERSION.json. That is the safety property worth keeping: an untracked file cannot be packaged by
accident, and generated risk-profile PDFs -- which carry real names, departments and exploitable
weaknesses -- are untracked by policy, so they cannot ride along in a file handed to someone else.
EXCLUDE then drops the files that maintain the skill rather than run it (contributor guidance,
evals, these generators).

The one exception is deliberate and narrow: VERSION.json is built in-process from --version and
HEAD, never read from disk, and is the only member whose name is hard-coded. It exists because the
installed skill self-updates (`meridian.py selfupdate`), and an install with no version stamp cannot
know whether it is stale -- the version lives in the git tag, which does not survive into an
extracted zip. Its content carries no timestamp on purpose: the build is deterministic and CI
asserts that, so a clock in the stamp would make every rebuild a different package.

Entries are prefixed `meridiancs/` so extracting into ~/.claude/skills/ lands SKILL.md at
~/.claude/skills/meridiancs/SKILL.md, which is what README's install steps promise.

Rebuild whenever the skill's behaviour changes -- this exists because the package silently drifted
behind two merged PRs when it was assembled by hand.
"""
import json
import os
import re
import subprocess
import sys
import zipfile

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The *archive* filename is versioned; the directory inside it is not. Extracting must still land
# SKILL.md at ~/.claude/skills/meridiancs/SKILL.md, so this prefix stays bare -- a versioned inner
# directory would break every install instruction and the skill would not load.
PREFIX = "meridiancs/"
VERSION_RE = re.compile(r"^v?(\d+\.\d+\.\d+)$")
# The version stamp the installed skill reads to decide whether it is stale. Synthesized here, absent
# from the repo -- see the module docstring for why that asymmetry is on purpose. Kept in sync with
# meridian.py's VERSION_PATH / VERSION_SCHEMA.
VERSION_MEMBER = "VERSION.json"
VERSION_SCHEMA = 1

# Repo-maintenance files: useful to a contributor, dead weight (or confusing) to someone installing
# the skill. Kept in the repo, kept out of the package.
EXCLUDE = {
    ".gitignore",
    "CLAUDE.md",                    # how to work ON the skill
    "scripts/make-guide.py",        # doc generators
    "scripts/make-impact.py",
    "scripts/make-brief.py",
    "scripts/make-package.py",      # this script
    "scripts/make-sbom.py",         # regenerates sbom.cdx.json; the SBOM itself DOES ship
    "scripts/make-public.py",       # derives the open-source tree
    "scripts/check-docs-pii.py",    # the repo-wide PII gate
    "scripts/docout.py",            # output-path guard the generators share; nothing
                                    # installed imports it, so it is maintenance too
    ".pre-commit-config.yaml",      # lint/format hooks: they run ON the skill, and an
    ".markdownlint-cli2.jsonc",     # installed copy has no repo for them to check
    "references/.public-sync.json",  # make-public.py's review stamp: which internal doc each
                                    # public variant was last reviewed against. Meaningless
                                    # away from the repo that performs the review.
}
EXCLUDE_DIRS = ("evals/", "design/", ".github/")   # smoke tests, design docs, CI config: repo, not install
# Sources for make-public.py's substitutions, never documents in their own right: a .public.md is
# a sanitised derivative of the reference doc beside it, with live-tenant figures replaced by
# illustrative round numbers. Packaging both puts a near-duplicate carrying deliberately weaker
# numbers next to the file SKILL.md actually routes to, and nothing marks which is which.
#
# Matched by SUFFIX rather than by name, because EXCLUDE is a filename list and a fifth variant
# added later would ship in silence -- which is exactly how these four reached a customer
# package. They were tracked from the day make-public.py landed and rode along in every release
# through v2.23.0, the same way the lint configs did before test_package_stamp started asserting
# the shape of a package instead of trusting the list.
EXCLUDE_SUFFIXES = (".public.md",)


def tracked_files():
    out = subprocess.run(["git", "ls-files"], cwd=SKILL, capture_output=True, text=True, check=True)
    files = [f.strip() for f in out.stdout.splitlines() if f.strip()]
    return [f for f in files if f not in EXCLUDE and not f.startswith(EXCLUDE_DIRS)
            and not f.endswith(EXCLUDE_SUFFIXES)]


def version_stamp(version):
    """The VERSION.json payload: what this package is, and which commit it came from.

    No build timestamp, no hostname, nothing else that varies run to run -- the package is
    deterministic and CI asserts a rebuild is byte-identical, so anything clock-derived here would
    break that check for a change nobody made. `dirty` appears only on an --allow-dirty build, so a
    clean release's stamp is unaffected by the key existing.
    """
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=SKILL,
                          capture_output=True, text=True).stdout.strip()
    stamp = {"schema": VERSION_SCHEMA, "version": version or None, "commit": head or None}
    if dirty_paths():
        stamp["dirty"] = True
    return json.dumps(stamp, indent=2, sort_keys=True) + "\n"


def dirty_paths():
    out = subprocess.run(["git", "status", "--porcelain"], cwd=SKILL, capture_output=True, text=True)
    return [l for l in out.stdout.splitlines() if l.strip()]


def resolve_version(explicit, required=True):
    """The version this package is labelled with, as X.Y.Z.

    Deliberately refuses to guess. No version string exists in the tree -- it lives in the git tag --
    and the documented release flow builds the zip BEFORE `gh release create` makes that tag, so
    falling back to `git describe` would label the package with the PREVIOUS release's number. A zip
    named for the wrong release is worse than no version in the name at all, and it is the exact class
    of silent staleness this filename is meant to end. Same principle as refusing a dirty tree.
    """
    if explicit:
        m = VERSION_RE.match(explicit)
        if not m:
            print("--version must look like 2.3.3 or v2.3.3 (got %r)." % explicit)
            sys.exit(1)
        return m.group(1)
    # A tag on HEAD is unambiguous -- but only while the tree matches it. With uncommitted changes the
    # tag describes a different tree than the one being packaged, so it is not a safe label either.
    got = subprocess.run(["git", "describe", "--tags", "--exact-match", "HEAD"], cwd=SKILL,
                         capture_output=True, text=True)
    m = VERSION_RE.match(got.stdout.strip()) if got.returncode == 0 else None
    if m and not dirty_paths():
        return m.group(1)
    why = ("HEAD is tagged %s but the tree has uncommitted changes, so that tag does not describe\n"
           "what would be packaged." % got.stdout.strip()) if m else \
          ("HEAD carries no release tag, and guessing from the newest tag would label this package\n"
           "with the PREVIOUS release's number.")
    if not required:
        # An explicitly-named one-off is not a release, so it is allowed to be unversioned --
        # but it is stamped `version: null`, which the installed skill reads as "no
        # provenance" and refuses to self-update from. Silently stamping a guessed number
        # would be the worse failure, for the same reason resolve_version refuses to guess.
        print("No version resolved; VERSION.json will say version=null, so this package"
              " cannot self-update.\n" + why)
        return None
    print("Pass --version (e.g. --version 2.3.3), or tag a clean HEAD before building.\n" + why)
    sys.exit(1)


def main():
    argv = sys.argv[1:]
    allow_dirty = "--allow-dirty" in argv
    version, args, i = None, [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--allow-dirty":
            i += 1
        elif a == "--version":
            version, i = (argv[i + 1] if i + 1 < len(argv) else None), i + 2
        elif a.startswith("--version="):
            version, i = a.split("=", 1)[1], i + 1
        else:
            args.append(a)
            i += 1

    if args:
        out, version = args[0], resolve_version(version, required=False)
    else:
        version = resolve_version(version)
        out = os.path.join(SKILL, "meridiancs.v%s.skill.zip" % version)
    # .gitignore matches `*.skill.zip`. A name outside that pattern stops being ignored and could be
    # committed -- which is how a package carrying whatever was on disk ends up in the repo. Enforced
    # rather than merely documented, because the failure is silent.
    if not os.path.basename(out).endswith(".skill.zip"):
        print("Output name must end in `.skill.zip` (got %r) -- .gitignore matches that pattern, and a\n"
              "package outside it becomes committable." % os.path.basename(out))
        sys.exit(1)

    dirty = dirty_paths()
    if dirty and not allow_dirty:
        print("Working tree is not clean, so the package would not match any commit:")
        for l in dirty[:20]:
            print("   " + l)
        print("Commit first, or pass --allow-dirty if you know that's what you want.")
        sys.exit(1)

    files = tracked_files()
    missing = [f for f in files if not os.path.exists(os.path.join(SKILL, f))]
    if missing:
        print("Tracked but not on disk: %s" % ", ".join(missing))
        sys.exit(1)

    # Deterministic: fixed member order and one fixed timestamp, so an unchanged tree rebuilds to an
    # identical zip and a real content change is the only thing that moves the bytes.
    stamp = (1980, 1, 1, 0, 0, 0)
    # VERSION.json is generated, so it is not in `files` -- it is written from memory alongside them
    # and sorted in by name, which keeps member order (and therefore the archive bytes) stable.
    members = sorted([(rel, None) for rel in files] + [(VERSION_MEMBER, version_stamp(version))])
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, generated in members:
            info = zipfile.ZipInfo(PREFIX + rel, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            if generated is not None:
                z.writestr(info, generated.encode("utf-8"))
                continue
            with open(os.path.join(SKILL, rel), "rb") as f:
                z.writestr(info, f.read())

    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=SKILL,
                          capture_output=True, text=True).stdout.strip()
    print("%s (v%s, %d files, %d bytes, from %s%s)"
          % (out, version or "unversioned", len(members), os.path.getsize(out), head,
             ", DIRTY tree" if dirty else ""))


main()
