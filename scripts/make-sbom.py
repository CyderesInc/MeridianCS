#!/usr/bin/env python3
"""Regenerate sbom.cdx.json -- the CycloneDX software bill of materials.

    python scripts/make-sbom.py            # write sbom.cdx.json
    python scripts/make-sbom.py --check    # exit 1 if the committed file is stale

Not a skill verb -- a maintenance tool, alongside make-package.py. The SBOM itself ships (it is
not in make-package.py's EXCLUDE), because the question it answers is asked of the distributed
artifact, not of the repo.

Why a generator and not a hand-written JSON: the interesting content is four font hashes, and a
hand-maintained hash is a hash that is eventually wrong. `--check` in CI is what makes the
committed file trustworthy rather than merely present -- the same reason make-package.py's
determinism is asserted rather than assumed.

Two deliberate choices, both inherited from VERSION.json's design:

  * **No timestamp.** CycloneDX allows `metadata.timestamp`; including it would make every
    regeneration differ from the committed file for a change nobody made, which is exactly what
    would train people to ignore `--check`.
  * **A derived serial number.** `serialNumber` is a deterministic UUIDv5 over the component
    inventory, so it is stable while the inventory is stable and changes when the inventory does.
    A random UUID would have the same staleness problem as a hand-copied hash.

The inventory is short because the software is: no third-party source code at all, Python standard
library only. That is the SBOM's most useful single fact, so it is stated explicitly in the
top-level component description rather than left to be inferred from an empty list.
"""
import hashlib
import json
import os
import sys
import uuid

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(SKILL, "sbom.cdx.json")

# (family, file, SPDX licence id, copyright, upstream, licence text file)
FONTS = [
    ("Space Grotesk", "SpaceGrotesk-Regular.ttf"),
    ("Space Grotesk", "SpaceGrotesk-Medium.ttf"),
    ("Space Grotesk", "SpaceGrotesk-Bold.ttf"),
    ("Space Mono",    "SpaceMono-Regular.ttf"),
]
FONT_META = {
    "Space Grotesk": ("Copyright 2020 The Space Grotesk Project Authors",
                      "https://github.com/floriankarsten/space-grotesk",
                      "assets/fonts/SpaceGrotesk-OFL.txt"),
    "Space Mono":    ("Copyright 2016 The Space Mono Project Authors",
                      "https://github.com/googlefonts/spacemono",
                      "assets/fonts/SpaceMono-OFL.txt"),
}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def font_components():
    out = []
    for family, filename in FONTS:
        copyright_, upstream, licence_file = FONT_META[family]
        rel = "assets/fonts/" + filename
        path = os.path.join(SKILL, "assets", "fonts", filename)
        if not os.path.exists(path):
            raise SystemExit("missing bundled font: %s" % rel)
        out.append({
            "type": "file",
            "bom-ref": rel,
            "name": rel,
            "description": "%s font binary, embedded as a data URI in generated PDF reports."
                           % family,
            "copyright": copyright_,
            "licenses": [{"license": {"id": "OFL-1.1",
                                      "url": "https://openfontlicense.org"}}],
            "hashes": [{"alg": "SHA-256", "content": sha256(path)}],
            "externalReferences": [
                {"type": "vcs", "url": upstream},
                {"type": "license", "url": upstream, "comment":
                 "Full licence text redistributed with this software at %s" % licence_file},
            ],
        })
    return out


def prerequisites():
    """Declared, not redistributed -- so `scope` carries the real information here."""
    return [
        {
            "type": "platform",
            "bom-ref": "prereq/python3",
            "name": "Python",
            "version": ">=3.8",
            "scope": "required",
            "description": "Interpreter for scripts/meridian.py. Standard library only -- this "
                           "software installs no Python packages and has no dependency manifest.",
            "licenses": [{"license": {"id": "PSF-2.0"}}],
            "externalReferences": [{"type": "website", "url": "https://www.python.org/"}],
        },
        {
            "type": "application",
            "bom-ref": "prereq/chromium",
            "name": "Chromium-based browser",
            "scope": "optional",
            "description": "Google Chrome or Microsoft Edge, invoked as an external process for "
                           "HTML-to-PDF rendering in the `report` verb. Never bundled; without "
                           "one, reports fall back to HTML output.",
            "licenses": [{"license": {"name": "NOASSERTION -- vendor's own terms; not "
                                              "redistributed by this project"}}],
        },
    ]


def document():
    components = font_components() + prerequisites()
    root = {
        "type": "application",
        "bom-ref": "meridiancs",
        "name": "meridiancs",
        "description": "Claude Code skill providing a natural-language interface to a Meridian "
                       "(formerly Lucidum) stack via its API v2. Contains no third-party source "
                       "code; the implementation depends only on the Python 3 standard library. "
                       "The only redistributed third-party artifacts are the four SIL OFL 1.1 "
                       "font binaries listed in this document.",
        "licenses": [{"license": {"id": "Apache-2.0"}}],
        "copyright": "Copyright 2026 Cyderes",
        "supplier": {"name": "Cyderes"},
    }
    body = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            # No `timestamp` on purpose -- see module docstring.
            "component": root,
            "supplier": {"name": "Cyderes"},
            "licenses": [{"license": {"id": "Apache-2.0"}}],
        },
        "components": components,
    }
    # Deterministic serial: stable while the inventory is stable.
    fingerprint = hashlib.sha256(
        json.dumps(components, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    serial = uuid.uuid5(uuid.NAMESPACE_URL, "urn:meridiancs:sbom:" + fingerprint)
    body["serialNumber"] = "urn:uuid:%s" % serial
    return body


def render():
    return json.dumps(document(), indent=2, sort_keys=False) + "\n"


def main(argv):
    text = render()
    if "--check" in argv:
        if not os.path.exists(OUT):
            print("sbom.cdx.json is missing; run: python scripts/make-sbom.py")
            return 1
        with open(OUT, encoding="utf-8") as f:
            current = f.read()
        if current != text:
            print("sbom.cdx.json is stale (a bundled component changed).")
            print("Regenerate it with: python scripts/make-sbom.py")
            return 1
        print("sbom.cdx.json is current.")
        return 0
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print("wrote %s (%d components)" % (OUT, len(document()["components"])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
