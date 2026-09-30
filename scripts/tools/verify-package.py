import argparse
import json
import os
import subprocess
import sys
import tempfile
import zipfile

import toolkit

PREFIX = "meridiancs/"
REQUIRED = (
    "SKILL.md",
    "LICENSE",
    "NOTICE",
    "sbom.cdx.json",
    "references/welcome.md",
    "scripts/meridian.py",
    "assets/fonts/SpaceGrotesk-OFL.txt",
    "assets/fonts/SpaceMono-OFL.txt",
)
REPO_ONLY_DIRS = ("design/", "evals/", ".github/", "scripts/tools/")
# selfupdate refuses to apply over a directory holding CLAUDE.md (install_markers), so an install
# from a package carrying one could never update again.
REPO_ONLY_NAMES = ("CLAUDE.md",)
REPO_ONLY_SUFFIXES = (".public.md", ".public-sync.json")


def main():
    args = parse_args()
    with zipfile.ZipFile(args.package) as z:
        names = [n[len(PREFIX):] for n in z.namelist()]
        stamp = json.loads(z.read(PREFIX + "VERSION.json"))
        changelog = z.read(PREFIX + "CHANGELOG.md").decode("utf-8-sig")
        eol = eol_problems({n[len(PREFIX):]: z.read(n) for n in z.namelist()})
    problems = (member_problems(names) + eol
                + stamp_problems(stamp, args.version, args.source)
                + changelog_problems(changelog, args.version)
                + rebuild_problems(args.package, stamp, args.source)
                + smoke_problems(args.package))
    if problems:
        print("NOT PUBLISHABLE: %s" % args.package)
        for p in problems:
            print("   " + p)
        sys.exit(1)
    print("Verified %s (commit %s, sha256 %s)" % (args.package, stamp["commit"][:7], toolkit.sha256(args.package)))


def parse_args():
    ap = argparse.ArgumentParser(description="Verify a built meridiancs package.")
    ap.add_argument("package")
    ap.add_argument("--version", type=toolkit.release_version,
                    help="the release this package must be stamped as (omit for a non-release build)")
    ap.add_argument("--source", default=toolkit.ROOT, help="the checkout it was built from (default: this repo)")
    return ap.parse_args()


def member_problems(names):
    problems = ["missing: %s" % r for r in REQUIRED if r not in names]
    problems += ["must not ship: %s" % n for n in names
                 if n.startswith(REPO_ONLY_DIRS) or os.path.basename(n) in REPO_ONLY_NAMES
                 or n.endswith(REPO_ONLY_SUFFIXES)]
    stray = [n for n in names if n.startswith("scripts/") and n != "scripts/meridian.py"]
    return problems + ["scripts/ ships only meridian.py, not %s" % n for n in stray]


def eol_problems(members):
    """Every text member LF-only, and SKILL.md's front matter closed by a bare `---` line. The second
    is the check that matters: a CRLF SKILL.md loads everywhere except where a parser splits on LF,
    so nothing on Windows noticed v2.27.4 shipping one."""
    problems = ["%s has CR line endings; packages ship text with LF" % n
                for n, data in sorted(members.items()) if toolkit.is_text(data) and b"\r" in data]
    lines = members.get("SKILL.md", b"").split(b"\n")
    if lines[0] != b"---" or b"---" not in lines[1:]:
        problems.append("SKILL.md's front matter is not opened and closed by bare `---` lines")
    return problems


def stamp_problems(stamp, version, source):
    problems = []
    if version and stamp.get("version") != version:
        problems.append("stamp says version %r, expected %r" % (stamp.get("version"), version))
    head = toolkit.git("rev-parse", "HEAD", cwd=source)
    if stamp.get("commit") != head:
        problems.append("stamp names commit %s, %s is at %s" % (stamp.get("commit"), source, head))
    if stamp.get("dirty"):
        problems.append("built from a dirty tree, so it matches no commit")
    return problems


def changelog_problems(changelog, version):
    if version and toolkit.changelog_entry(version, changelog) is None:
        return ["CHANGELOG.md has no `## %s` entry; installs would announce nothing" % version]
    return []


def rebuild_problems(package, stamp, source):
    name = ["--version", stamp["version"]] if stamp.get("version") else ["--label", "rebuild"]
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(source, "scripts", "tools", "make-package.py")
        subprocess.run([sys.executable, script, *name, "--out-dir", tmp], cwd=source, check=True,
                       stdout=subprocess.DEVNULL)
        rebuilt = os.path.join(tmp, os.listdir(tmp)[0])
        if toolkit.sha256(rebuilt) != toolkit.sha256(package):
            return ["a rebuild from %s is not byte-identical" % source]
    return []


def smoke_problems(package):
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(package) as z:
            z.extractall(tmp)
        script = os.path.join(tmp, "meridiancs", "scripts", "meridian.py")
        if subprocess.run([sys.executable, script, "--help"], capture_output=True).returncode:
            return ["the packaged meridian.py does not start"]
    return []


if __name__ == "__main__":
    main()
