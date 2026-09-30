import argparse
import json
import os
import sys
import tempfile

import toolkit

CANDIDATES_KEPT = 5


def main():
    args = parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        create_release(args, notes_file(args, tmp))
        problems = hosted_mismatches(args, tmp)
    if problems:
        sys.exit("PUBLISHED, BUT THE HOSTED ASSETS DO NOT MATCH:\n   " + "\n   ".join(problems))
    print("Published %s on %s; hosted assets match what was built." % (args.tag, args.repo))
    if args.candidate:
        prune_candidates(args.repo)


def parse_args():
    ap = argparse.ArgumentParser(description="Publish a signed meridiancs package as a GitHub release.")
    ap.add_argument("package")
    ap.add_argument("--repo", required=True, help="OWNER/NAME to publish on")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--title", help="release title (default: the tag)")
    ap.add_argument("--target", help="commit to create the tag at; omit when the tag already exists")
    ap.add_argument("--candidate", action="store_true", help="a prerelease signed with the dev key")
    return ap.parse_args()


def notes_file(args, tmp):
    if args.candidate:
        notes = ("Release candidate built from %s and signed with the dev key. Installs refuse it; it is "
                 "for checking a build before the release is tagged.\n" % args.target)
    else:
        version = toolkit.release_version(args.tag)
        notes = toolkit.changelog_entry(version, toolkit.read_changelog())
        if notes is None:
            sys.exit("CHANGELOG.md has no `## %s` entry." % version)
    path = os.path.join(tmp, "notes.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(notes)
    return path


def create_release(args, notes):
    where = ["--target", args.target] if args.target else ["--verify-tag"]
    kind = ["--prerelease"] if args.candidate else []
    toolkit.run(["gh", "release", "create", args.tag, "--repo", args.repo, *where, *kind,
                 "--title", args.title or args.tag, "--notes-file", notes,
                 args.package, args.package + ".sig"])


def hosted_mismatches(args, tmp):
    hosted = os.path.join(tmp, "hosted")
    toolkit.run(["gh", "release", "download", args.tag, "--repo", args.repo, "--dir", hosted,
                 "--pattern", "meridiancs.*"])
    problems = []
    for built in (args.package, args.package + ".sig"):
        got = os.path.join(hosted, os.path.basename(built))
        if not os.path.isfile(got):
            problems.append("%s is not on the release" % os.path.basename(built))
        elif toolkit.sha256(got) != toolkit.sha256(built):
            problems.append("hosted %s does not match the built one" % os.path.basename(built))
    return problems


def prune_candidates(repo):
    releases = json.loads(toolkit.run(["gh", "release", "list", "--repo", repo, "--limit", "100",
                                       "--json", "tagName,isPrerelease,createdAt"]))
    candidates = sorted((r for r in releases if r["isPrerelease"] and "-rc." in r["tagName"]),
                        key=lambda r: r["createdAt"], reverse=True)
    for old in candidates[CANDIDATES_KEPT:]:
        toolkit.run(["gh", "release", "delete", old["tagName"], "--repo", repo, "--cleanup-tag", "--yes"])


if __name__ == "__main__":
    main()
