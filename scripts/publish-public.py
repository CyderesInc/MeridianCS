#!/usr/bin/env python3
"""Stage the public distribution for a release: derive, sync the public clone, commit, build, verify.

    python scripts/publish-public.py --version 2.24.2 [--clone <dir>] [--allow-dirty] [--push]

Everything up to `git push` and `gh release create`, and nothing beyond it. The release commit is
made in the clone, locally; the push only with --push, and the release command never -- publishing
is the operator's call, and it is the one step that cannot be undone.

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
 2. Refuses a clone whose HEAD is not exactly its upstream after a fetch. The release commit is
    made on top of HEAD, so a HEAD that is behind would put it on a stale parent (the push is then
    rejected, and the rebase that fixes it changes the commit the stamp names), and a HEAD that is
    ahead is an earlier run's release commit that was never pushed.
 3. Derives the public tree with make-public.py into a scratch directory. That runs the audit --
    brand assets, identifiers, measured tenant figures, dangling links -- and a non-clean audit
    stops everything here.
 4. Compares the derivation against what is published: the clone's tracked files, on a clean clone
    whose HEAD is its upstream. **If they are identical, it stops and says no public release is
    needed.** This is not an optimisation: the public tree is a subset, so an internal change to
    CLAUDE.md, design/, or a branded doc generator leaves it byte-identical. Cutting a public
    release anyway would republish the same bytes under a new tag and tell every install to
    download a version it already has. Tracked files only, because the previous release's package
    sits in the clone (ignored by .gitignore) and would otherwise make every tree look changed.
 5. Syncs the derived tree into the clone, preserving .git, stages it, and makes the release
    commit -- locally. Never pushed without --push.
 6. Builds the public package from the CLONE (not the internal tree), at the version given, from
    that clean commit -- without --allow-dirty.
 7. Verifies: the stamp matches --version, names the release commit and is not marked dirty, no
    brand asset, no .public.md source, no internal doc, a byte-identical rebuild, and the staged
    copy's own --help runs. Any failure from the commit onward undoes the commit (soft reset), so
    the clone is left synced and staged and a rerun starts from the published tip again.
 8. Prints the two commands the operator runs: the push, then `gh release create` targeting the
    release commit by sha.

## Why the commit is made here, before the build

Through v2.25.0 this script built the package from the synced-but-uncommitted clone with
--allow-dirty and printed a `git commit` for the operator to run afterwards. So every public
package's VERSION.json recorded the *previous* public commit plus `"dirty": true`: the v2.24.2
asset's stamp names 030d07c, the v2.24.0 seed, where the release commit is e93e47f. The version
field was right, so installs updated, but `selfupdate` reported the wrong `installedCommit` and
the stamp's provenance pointed at a tree the package was not built from. The commit is what makes
the stamp true, so it has to exist before make-package.py reads HEAD.

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


def published_matches(derived, clone):
    """True when the derived tree is byte-identical to what the clone has published.

    Only meaningful once check_clone_tip() has confirmed HEAD is the upstream: then a clean clone's
    tracked files ARE the published tree. Tracked rather than every file on disk, because the last
    release's package is built into the clone and ignored by .gitignore -- walking the directory
    counted it as a difference, so this check could never fire once a release had been built.
    shallow=False on purpose: a size-and-mtime comparison would call a freshly derived file
    "different" every time, which would defeat the whole no-op check.
    """
    if run(["git", "status", "--porcelain"], clone).stdout.strip():
        return False
    tracked = set(run(["git", "ls-files", "-z"], clone).stdout.split("\0")) - {""}
    if tracked != tree_files(derived):
        return False
    return all(filecmp.cmp(os.path.join(derived, *r.split("/")),
                           os.path.join(clone, *r.split("/")), shallow=False) for r in sorted(tracked))


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


def check_clone_tip(clone):
    """Refuse unless the clone's HEAD is exactly its upstream, after a fetch. Returns HEAD's sha."""
    if run(["git", "fetch", "--quiet", "origin"], clone, check=False).returncode != 0:
        die("Could not fetch origin in %s. The release commit has to sit on the published tip,\n"
            "which cannot be confirmed without a fetch." % clone, 2)
    up = run(["git", "rev-parse", "--abbrev-ref", "@{u}"], clone, check=False)
    if up.returncode != 0:
        die("%s has no upstream branch; check out the branch that tracks origin/main." % clone, 2)
    counts = run(["git", "rev-list", "--left-right", "--count", "HEAD...@{u}"], clone).stdout.split()
    ahead, behind = int(counts[0]), int(counts[1])
    if ahead:
        die("%s is %d commit(s) ahead of %s -- most likely an earlier run's release commit that\n"
            "was never pushed. Push it and publish that release, or drop it with\n"
            "  git -C %s reset --hard %s\n"
            "Building on top of it would put two release commits in one push."
            % (clone, ahead, up.stdout.strip(), clone, up.stdout.strip()), 2)
    if behind:
        die("%s is %d commit(s) behind %s. Update it first:\n  git -C %s pull --ff-only"
            % (clone, behind, up.stdout.strip(), clone), 2)
    return run(["git", "rev-parse", "HEAD"], clone).stdout.strip()


def print_no_op():
    print("\nThe public tree is byte-identical to what is already published.")
    print("No public release is needed: an internal-only change (CLAUDE.md, design/, a")
    print("branded doc generator) leaves the public subset untouched. Cutting one anyway")
    print("would republish the same bytes under a new tag.")


def verify_package(zip_path, version, commit):
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
        # selfupdate reports this as installedCommit, and it is the package's only provenance once
        # extracted. Every public package through v2.25.0 named the previous release's commit and
        # carried dirty: true, because the build ran before the commit existed.
        if stamp.get("commit") != commit:
            problems.append("stamp names commit %r, the release commit is %r" % (stamp.get("commit"), commit))
        if "dirty" in stamp:
            problems.append("stamp is marked dirty: the package matches no commit")
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


def build_and_verify(clone, tmp, version, commit):
    """Build from the clean release commit, rebuild, smoke-test. Returns (package path, problems)."""
    pkg = os.path.join(clone, "meridiancs.v%s.skill.zip" % version)
    # No --allow-dirty: the release commit exists, so a dirty clone here is a bug, and make-package.py
    # refusing it is the right outcome rather than a stamp that quietly says so.
    run([sys.executable, os.path.join(clone, "scripts", "make-package.py"),
         pkg, "--version", version], clone)

    problems = verify_package(pkg, version, commit)
    rebuild = os.path.join(tmp, "rebuild.skill.zip")
    run([sys.executable, os.path.join(clone, "scripts", "make-package.py"),
         rebuild, "--version", version], clone)
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
    return pkg, problems


def main():
    ap = argparse.ArgumentParser(
        description="Derive, sync, commit (locally) and build the public distribution. "
                    "Stops before `git push` and `gh release create`.")
    ap.add_argument("--version", required=True,
                    help="X.Y.Z for the public package and tag. Must match the internal release.")
    ap.add_argument("--clone", default=DEFAULT_CLONE,
                    help="the %s working clone (default: %s)" % (PUBLIC_REPO, DEFAULT_CLONE))
    ap.add_argument("--allow-dirty", action="store_true",
                    help="derive from an uncommitted internal tree (the derived tree then matches no commit)")
    ap.add_argument("--push", action="store_true",
                    help="also push the release commit; without it, the push command is printed for you to run")
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
    base = check_clone_tip(a.clone)

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

        if published_matches(derived, a.clone):
            print_no_op()
            return 0

        sync_into(derived, a.clone)
        run(["git", "add", "-A"], a.clone)
        changed = run(["git", "diff", "--cached", "--name-only"], a.clone).stdout.split()
        if not changed:
            # Reachable when a clone left dirty by an earlier run already differed from HEAD only
            # in ways the derivation undoes: the sync has restored the published tree exactly.
            print_no_op()
            return 0
        print("\nPublic tree changes (%d file(s)):" % len(changed))
        for f in changed[:40]:
            print("   " + f)

        msg = "Release v%s" % version
        run(["git", "commit", "--quiet", "-m", msg], a.clone)
        release = run(["git", "rev-parse", "HEAD"], a.clone).stdout.strip()
        ok = False
        try:
            pkg, problems = build_and_verify(a.clone, tmp, version, release)
            print("\nPackage: %s (%d bytes)\nsha256:  %s\ncommit:  %s"
                  % (pkg, os.path.getsize(pkg), sha256(pkg), release))
            if problems:
                print("\nNOT PUBLISHABLE -- %d problem(s):" % len(problems))
                for p in problems:
                    print("   " + p)
                return 1
            ok = True
        finally:
            if not ok:
                # Soft, so the synced tree stays staged for inspection -- but HEAD goes back to the
                # published tip, so a rerun is not refused as "ahead" by a commit nobody should push.
                run(["git", "reset", "--soft", "--quiet", base], a.clone, check=False)
                print("\nUndid the local release commit; %s is back at %s with the sync staged."
                      % (a.clone, base[:7]))
        print("Verified: stamp matches the release commit, nothing internal, deterministic rebuild, "
              "packaged script runs.")

        if a.push:
            run(["git", "push", "origin", "HEAD"], a.clone)
            print("\nPushed %s (%s) to %s." % (msg, release[:7], PUBLIC_REPO))
        else:
            print("\nCommitted %s locally as %s. Next, from %s:" % (msg, release[:7], a.clone))
            print("   git push origin HEAD")
        # Targeting the sha rather than main: the stamp names this commit, so the tag must too, and
        # `main` would tag whatever the branch points at by the time the command is run.
        print("\nThen publish (this script never runs it):")
        print('   gh release create v%s --repo %s --target %s \\' % (version, PUBLIC_REPO, release))
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
