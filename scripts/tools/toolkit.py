import argparse
import hashlib
import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VERSION_RE = re.compile(r"^v?(\d+\.\d+\.\d+)$")


def git(*args, cwd=ROOT):
    return run(["git", *args], cwd=cwd)


def run(cmd, cwd=ROOT):
    """stdout of `cmd`; stderr is left uncaptured so a failing command explains itself."""
    return subprocess.run(cmd, cwd=cwd, check=True, stdout=subprocess.PIPE, text=True).stdout.strip()


def tracked_files(cwd=ROOT):
    return git("ls-files", cwd=cwd).splitlines()


def is_dirty(cwd=ROOT):
    return bool(git("status", "--porcelain", cwd=cwd))


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def release_version(text):
    match = VERSION_RE.match(text)
    if not match:
        raise argparse.ArgumentTypeError("%r is not X.Y.Z" % text)
    return match.group(1)


def package_name(label):
    return "meridiancs.%s.skill.zip" % label


def read_changelog(root=ROOT):
    with open(os.path.join(root, "CHANGELOG.md"), encoding="utf-8-sig") as f:
        return f.read()


def changelog_entry(version, text):
    heading = re.compile(r"^##\s+\[?v?%s\]?(\s|$)" % re.escape(version))
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start + 1:end]).strip() + "\n"


def latest_changelog_version(text):
    match = re.search(r"^## (\d+\.\d+\.\d+)", text, re.M)
    return match.group(1) if match else None
