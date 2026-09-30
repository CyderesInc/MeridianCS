import argparse
import os
import shutil
import subprocess
import sys
import tempfile

import toolkit


def main():
    args = parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        clone, tree = os.path.join(tmp, "clone"), os.path.join(tmp, "tree")
        toolkit.run(["gh", "repo", "clone", args.repo, clone, "--", "--quiet"])
        tool("make-public.py", tree)
        replace_tree(clone, tree)
        if not toolkit.is_dirty(cwd=clone):
            refuse_unpublished_release_commit(clone, args.repo, args.version)
            print("The public tree is byte-identical to what is published; no public release is needed.")
            return
        toolkit.git("add", "-A", cwd=clone)
        # Before the build: the package's stamp records HEAD, so it must name this commit.
        toolkit.git("commit", "--quiet", "-m", release_subject(args.version), cwd=clone)
        release_commit = toolkit.git("rev-parse", "HEAD", cwd=clone)
        package = build_verified(clone, args.version, tmp)
        tool("sign-release.py", package)
        toolkit.git("push", "--quiet", "origin", "HEAD", cwd=clone)
        tool("publish-release.py", package, "--repo", args.repo, "--tag", "v" + args.version,
             "--title", args.title or "v" + args.version, "--target", release_commit)


def parse_args():
    ap = argparse.ArgumentParser(description="Publish the public meridiancs distribution.")
    ap.add_argument("--repo", required=True, help="OWNER/NAME of the public repository")
    ap.add_argument("--version", required=True, type=toolkit.release_version,
                    help="X.Y.Z; must match the internal release")
    ap.add_argument("--title", help="release title (default: vX.Y.Z)")
    return ap.parse_args()


def tool(script, *args, cwd=toolkit.ROOT):
    subprocess.run([sys.executable, os.path.join(cwd, "scripts", "tools", script), *args], cwd=cwd, check=True)


def replace_tree(clone, tree):
    for name in os.listdir(clone):
        if name != ".git":
            path = os.path.join(clone, name)
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
    shutil.copytree(tree, clone, dirs_exist_ok=True)


def refuse_unpublished_release_commit(clone, repo, version):
    sha, subject = toolkit.git("log", "-1", "--format=%h %s", cwd=clone).split(" ", 1)
    if subject == release_subject(version) and not release_exists(repo, "v" + version):
        sys.exit("%s's HEAD %s is %r, pushed by a run that failed before publishing it, so v%s has no "
                 "release." % (repo, sha, subject, version))


def release_subject(version):
    return "Release v%s" % version


def release_exists(repo, tag):
    return subprocess.run(["gh", "release", "view", tag, "--repo", repo],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def build_verified(clone, version, tmp):
    # The clone's own make-package.py, because it packages the tree it lives in.
    tool("make-package.py", "--version", version, "--out-dir", tmp, cwd=clone)
    package = os.path.join(tmp, toolkit.package_name("v" + version))
    tool("verify-package.py", package, "--version", version, "--source", clone, cwd=clone)
    return package


if __name__ == "__main__":
    main()
