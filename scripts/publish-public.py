#!/usr/bin/env python3
"""Stage the public distribution for a release: derive, sync the public clone, build, verify.

    python scripts/publish-public.py --version 2.24.2 [--clone <dir>] [--allow-dirty] [--push]

Everything up to `gh release create`, and nothing beyond it. The release command is deliberately
NOT run here -- publishing is the operator's call, and it is the one step that cannot be undone.

## Why this exists

Publishing used to be one release from one repository. It is now two, and the one that matters is
the one that is easy to forget: **installed copies self-update from the PUBLIC repository**, so an
internal release alone reaches nobody. The internal release distributes the branded package to
people who install by hand; the public release is what every existing install replaces itself with.
Getting that backwards produces a release that verifies perfectly and ships to no one.

The ordering rules below are the ones that were learned the expensive way. They are code here
rather than prose in CLAUDE.md because prose is a thing a future session has to follow correctly
under time pressure, and every one of these failures is silent.

## What it does, in order

 1. Refuses a dirty internal tree, for the same reason make-package.py does: the derived tree would
    not match any commit, so nothing could later say which source produced it.
 2. Derives the public tree with make-public.py into a scratch directory. That runs the audit --
    brand assets, identifiers, measured tenant figures, dangling links -- and a non-clean audit
    stops everything here.
 3. Compares the derivation against the public clone's working tree. **If they are identical, it
    stops and says no public release is needed.** This is not an optimisation: the public tree is a
    subset, so an internal change to CLAUDE.md, design/, or a branded doc generator leaves it
    byte-identical. Cutting a public release anyway would republish the same bytes under a new tag
    and tell every install to download a version it already has.
 4. Syncs the derived tree into the clone, preserving .git, and stages it.
 5. Builds the public package from the CLONE (not the internal tree), at the version given.
 6. Verifies: the stamp matches --version, no brand asset, no .public.md source, no internal doc,
    a byte-identical rebuild, and the staged copy's own --help runs.
 7. Prints the two commands the operator runs: the push, then `gh release create`.

## The version rule

The updater validates a downloaded package's VERSION.json against the release tag it came from, so
the public package MUST carry the same number as the tag it is published under -- and the internal
and public releases should carry the same number as each other, or a user reading one cannot reason
about the other. `--version` is required for the same reason make-package.py requires it: the zip
is built before the tag exists, so deriving it from `git describe` would label it with the previous
release.
"""
import argparse
import filecmp
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
DEFAULT_CLONE = os.path.join(os.path.dirname(SKILL), "meridiancs-public")
PUBLIC_REPO = "CyderesInc/MeridianCS"
# Anything matching these must never appear in the public package. Checked on the built zip rather
# than trusted from make-public.py's audit, because this is the last look before an upload.
FORBIDDEN = ("cyderes-logo", "meridian-logo", "cyderes-report.css", "CLAUDE.md",
             ".public.md", ".public-sync.json", "design/", ".pdf")


def run(cmd, cwd, check=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        die("%s failed in %s:\n%s%s" % (" ".join(cmd), cwd, r.stdout, r.stderr))
    return r


def die(msg, code=1):
    print(msg)
    sys.exit(code)


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def tree_files(root):
    """Every file under root, relative and slash-separated, ignoring .git."""
    out = set()
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".git"]
        for n in names:
            out.add(os.path.relpath(os.path.join(base, n), root).replace(os.sep, "/"))
    return out


def identical(a, b):
    """True when two trees hold the same files with the same contents (.git excluded).

    shallow=False on purpose: a size-and-mtime comparison would call a freshly derived file
    "different" every time, which would defeat the whole no-op check.
    """
    fa, fb = tree_files(a), tree_files(b)
    if fa != fb:
        return False
    return all(filecmp.cmp(os.path.join(a, *r.split("/")),
                           os.path.join(b, *r.split("/")), shallow=False) for r in sorted(fa))


def sync_into(src, clone):
    """Replace the clone's working tree with src, keeping .git."""
    for name in os.listdir(clone):
        if name == ".git":
            continue
        p = os.path.join(clone, name)
        shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
    for rel in sorted(tree_files(src)):
        dst = os.path.join(clone, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(src, *rel.split("/")), dst)


def verify_package(zip_path, version):
    problems = []
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        try:
            stamp = json.loads(z.read("meridiancs/VERSION.json"))
        except KeyError:
            return ["no VERSION.json in the package"]
        # The updater refuses a package whose stamp does not match the tag it was published under,
        # so a mismatch here becomes an install that silently never updates again.
        if stamp.get("version") != version:
            problems.append("stamp says %r, building %r" % (stamp.get("version"), version))
        for bad in FORBIDDEN:
            hit = [n for n in names if bad in n]
            if hit:
                problems.append("must not ship: %s" % ", ".join(sorted(hit)[:4]))
        for need in ("meridiancs/SKILL.md", "meridiancs/scripts/meridian.py"):
            if need not in names:
                problems.append("missing required member: %s" % need)
        # Every install that updates from this package reads its CHANGELOG entry to say what
        # changed; without one the release reaches users with nothing to tell them.
        try:
            notes = z.read("meridiancs/CHANGELOG.md").decode("utf-8-sig")
        except KeyError:
            notes = ""
        if not re.search(r"^##\s+\[?v?%s\]?(\s|$)" % re.escape(version), notes, re.M):
            problems.append("CHANGELOG.md in the package has no `## %s` entry" % version)
    return problems


def main():
    ap = argparse.ArgumentParser(
        description="Derive, sync and build the public distribution. Stops before `gh release create`.")
    ap.add_argument("--version", required=True,
                    help="X.Y.Z for the public package and tag. Must match the internal release.")
    ap.add_argument("--clone", default=DEFAULT_CLONE,
                    help="the %s working clone (default: %s)" % (PUBLIC_REPO, DEFAULT_CLONE))
    ap.add_argument("--allow-dirty", action="store_true",
                    help="derive from an uncommitted internal tree (the derived tree then matches no commit)")
    ap.add_argument("--push", action="store_true",
                    help="also commit and push the clone; without it, the commands are printed for you to run")
    a = ap.parse_args()

    version = a.version.lstrip("v")
    if not a.allow_dirty and run(["git", "status", "--porcelain"], SKILL).stdout.strip():
        die("Internal tree is dirty; the derived tree would match no commit. Commit first, "
            "or pass --allow-dirty.", 2)
    if not os.path.isdir(os.path.join(a.clone, ".git")):
        die("Not a git clone: %s\nClone it with:\n  gh repo clone %s %s" % (a.clone, PUBLIC_REPO, a.clone), 2)
    url = run(["git", "remote", "get-url", "origin"], a.clone).stdout.strip()
    # A clone of the WRONG repo here would push the public tree over internal main. The two differ
    # only by a path on disk, so this is checked rather than assumed.
    if PUBLIC_REPO.lower() not in url.lower():
        die("%s points at %s, not %s. Refusing to sync into it." % (a.clone, url, PUBLIC_REPO), 2)

    tmp = tempfile.mkdtemp(prefix="pubtree-")
    try:
        derived = os.path.join(tmp, "tree")
        cmd = [sys.executable, os.path.join(HERE, "make-public.py"), derived]
        if a.allow_dirty:
            cmd.append("--allow-dirty")
        r = subprocess.run(cmd, cwd=SKILL, capture_output=True, text=True)
        print(r.stdout.strip())
        if r.returncode != 0:
            die("\nDerivation failed or the audit is not clean. Nothing was synced.", 1)

        if identical(derived, a.clone):
            print("\nThe public tree is byte-identical to what is already published.")
            print("No public release is needed: an internal-only change (CLAUDE.md, design/, a")
            print("branded doc generator) leaves the public subset untouched. Cutting one anyway")
            print("would republish the same bytes under a new tag.")
            return 0

        sync_into(derived, a.clone)
        run(["git", "add", "-A"], a.clone)
        changed = run(["git", "diff", "--cached", "--name-only"], a.clone).stdout.split()
        print("\nPublic tree changes (%d file(s)):" % len(changed))
        for f in changed[:40]:
            print("   " + f)

        pkg = os.path.join(a.clone, "meridiancs.v%s.skill.zip" % version)
        run([sys.executable, os.path.join(a.clone, "scripts", "make-package.py"),
             pkg, "--version", version, "--allow-dirty"], a.clone)

        problems = verify_package(pkg, version)
        rebuild = os.path.join(tmp, "rebuild.skill.zip")
        run([sys.executable, os.path.join(a.clone, "scripts", "make-package.py"),
             rebuild, "--version", version, "--allow-dirty"], a.clone)
        if sha256(pkg) != sha256(rebuild):
            problems.append("rebuild is not byte-identical")
        # The staged copy has to actually run. Without this the failure mode is a user left with a
        # skill that does not start and no way to ask it for help.
        smoke = os.path.join(tmp, "smoke")
        with zipfile.ZipFile(pkg) as z:
            z.extractall(smoke)
        if subprocess.run([sys.executable, os.path.join(smoke, "meridiancs", "scripts", "meridian.py"),
                           "--help"], capture_output=True).returncode != 0:
            problems.append("the packaged meridian.py does not run")

        print("\nPackage: %s (%d bytes)\nsha256:  %s" % (pkg, os.path.getsize(pkg), sha256(pkg)))
        if problems:
            print("\nNOT PUBLISHABLE -- %d problem(s):" % len(problems))
            for p in problems:
                print("   " + p)
            return 1
        print("Verified: stamp matches, nothing internal, deterministic rebuild, packaged script runs.")

        msg = "Release v%s" % version
        if a.push:
            run(["git", "commit", "-m", msg], a.clone)
            run(["git", "push", "origin", "HEAD"], a.clone)
            print("\nPushed %s to %s." % (msg, PUBLIC_REPO))
        else:
            print("\nNext, from %s:" % a.clone)
            print('   git commit -m "%s"' % msg)
            print("   git push origin HEAD")
        print("\nThen publish (this script never runs it):")
        print('   gh release create v%s --repo %s --target main \\' % (version, PUBLIC_REPO))
        print('     --title "..." --notes-file <file> \\')
        print('     "meridiancs.v%s.skill.zip#meridiancs.v%s.skill.zip"' % (version, version))
        print("\nThen verify what is hosted:")
        print("   gh release download v%s --repo %s --dir <tmp> --pattern '*.skill.zip'"
              % (version, PUBLIC_REPO))
        print("   ... and compare its sha256 against the one above.")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
