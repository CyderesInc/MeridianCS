import argparse
import json
import re
import sys

import toolkit


def main():
    args = parse_args()
    args.command(args)


def parse_args():
    ap = argparse.ArgumentParser(description="Release facts the workflow needs.")
    sub = ap.add_subparsers(required=True)
    check = sub.add_parser("check-tag", help="exit 1 unless the tag is vX.Y.Z and on main")
    check.add_argument("tag")
    check.set_defaults(command=check_tag)
    title = sub.add_parser("title", help="the release title: the annotated tag's subject after the version")
    title.add_argument("tag")
    title.set_defaults(command=print_title)
    candidate = sub.add_parser("candidate-tag", help="the prerelease tag for a build of HEAD")
    candidate.add_argument("--run", required=True, help="a number unique to this build, e.g. the CI run")
    candidate.set_defaults(command=print_candidate_tag)
    return ap.parse_args()


def check_tag(args):
    if not re.fullmatch(r"v\d+\.\d+\.\d+", args.tag):
        sys.exit("%s is not vX.Y.Z" % args.tag)
    if "origin/main" not in toolkit.git("branch", "--remotes", "--contains", args.tag).split():
        sys.exit("%s is not on main" % args.tag)


def print_title(args):
    # Through the API rather than local git: actions/checkout rewrites an annotated tag as a
    # lightweight one, which would drop the subject.
    ref = json.loads(toolkit.run(["gh", "api", "repos/{owner}/{repo}/git/ref/tags/%s" % args.tag]))
    subject = ""
    if ref["object"]["type"] == "tag":
        tag = json.loads(toolkit.run(["gh", "api", "repos/{owner}/{repo}/git/tags/%s" % ref["object"]["sha"]]))
        subject = (tag["message"].strip().splitlines() or [""])[0]
    print("%s: %s" % (args.tag, subject) if subject else args.tag)


def print_candidate_tag(args):
    print("v%s-rc.%s-%s" % (next_version(), args.run, toolkit.git("rev-parse", "--short=7", "HEAD")))


def next_version():
    version = toolkit.latest_changelog_version(toolkit.read_changelog())
    if not toolkit.git("tag", "--list", "v" + version):
        return version
    major, minor, patch = version.split(".")
    return "%s.%s.%d" % (major, minor, int(patch) + 1)


if __name__ == "__main__":
    main()
