import argparse
import json
import os
import zipfile

import toolkit

# Unversioned, so extracting into ~/.claude/skills/ lands SKILL.md where the install steps say.
PREFIX = "meridiancs/"
# Kept in sync with meridian.py's VERSION_PATH / VERSION_SCHEMA.
VERSION_MEMBER = "VERSION.json"
VERSION_SCHEMA = 1
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

EXCLUDE = {
    ".gitignore",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    ".pre-commit-config.yaml",
    ".markdownlint-cli2.jsonc",
    "references/.public-sync.json",
}
# Directories and a suffix rather than names, so a new tool or variant cannot ship by being forgotten.
EXCLUDE_DIRS = ("evals/", "design/", ".github/", "scripts/tools/")
EXCLUDE_SUFFIXES = (".public.md",)


def main():
    args = parse_args()
    label = "v%s" % args.version if args.version else args.label
    out = os.path.join(args.out_dir, toolkit.package_name(label))
    members = packaged_files()
    write_zip(out, members, version_stamp(args.version))
    print("%s (%d files, %d bytes)" % (out, len(members) + 1, os.path.getsize(out)))


def parse_args():
    ap = argparse.ArgumentParser(description="Build meridiancs.<label>.skill.zip from the tracked files.")
    name = ap.add_mutually_exclusive_group(required=True)
    name.add_argument("--version", type=toolkit.release_version, help="release X.Y.Z (a leading v is fine)")
    name.add_argument("--label", help="non-release label for the filename; the stamp says version null")
    ap.add_argument("--out-dir", default=".", help="where to write the package (default: here)")
    return ap.parse_args()


def packaged_files():
    # Tracked files only: generated report PDFs carry customer data and are untracked by policy.
    return [f for f in toolkit.tracked_files() if f not in EXCLUDE
            and not f.startswith(EXCLUDE_DIRS) and not f.endswith(EXCLUDE_SUFFIXES)]


def version_stamp(version):
    # No timestamp: verify-package.py rebuilds and requires identical bytes.
    stamp = {"schema": VERSION_SCHEMA, "version": version, "commit": toolkit.git("rev-parse", "HEAD")}
    if toolkit.is_dirty():
        stamp["dirty"] = True
    return json.dumps(stamp, indent=2, sort_keys=True) + "\n"


def write_zip(out, files, stamp):
    members = sorted([(rel, None) for rel in files] + [(VERSION_MEMBER, stamp)])
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, generated in members:
            info = zipfile.ZipInfo(PREFIX + rel, date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, generated.encode("utf-8") if generated else read_bytes(rel))


def read_bytes(rel):
    with open(os.path.join(toolkit.ROOT, rel), "rb") as f:
        return f.read()


if __name__ == "__main__":
    main()
