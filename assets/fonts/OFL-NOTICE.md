# Bundled fonts — license notice

This skill bundles the following fonts, embedded into generated reports so they render on-brand on
any machine. Both are licensed under the **SIL Open Font License, Version 1.1**, which permits
bundling, embedding, and redistribution.

| Font | Use | License | Full text |
|------|-----|---------|-----------|
| Space Grotesk (Regular / Medium / Bold) | Cyderes brand-alternate to PX Grotesk (body + headings) | SIL OFL 1.1 | [SpaceGrotesk-OFL.txt](SpaceGrotesk-OFL.txt) |
| Space Mono (Regular) | Cyderes brand-alternate to PX Grotesk Mono (data / monospace) | SIL OFL 1.1 | [SpaceMono-OFL.txt](SpaceMono-OFL.txt) |

- Space Grotesk © 2020 The Space Grotesk Project Authors — <https://github.com/floriankarsten/space-grotesk>
- Space Mono © 2016 The Space Mono Project Authors — <https://github.com/googlefonts/spacemono>

Both fonts are redistributed **unmodified**.

## What the OFL actually requires here

**Clause 2 requires the copyright notice and the license to travel with the font in every copy** —
which is why the full text of each license now sits beside the binaries as `SpaceGrotesk-OFL.txt`
and `SpaceMono-OFL.txt`, rather than only being linked. Those files are tracked, so
`make-package.py` includes them in the distributed zip automatically; a package that ships the
`.ttf` files without them would not be compliant. `evals/test_connect.py` asserts this rather than
trusting it, since the fonts and the license texts are separate files and nothing else would notice
them drifting apart.

**Clause 3 — Reserved Font Names — imposes no obligation on this project.** Neither upstream
copyright line declares a reserved name (both read simply "Copyright <year> The <Family> Project
Authors", with no "with Reserved Font Name" clause), so there is no name to protect and no
restriction on derivatives. An earlier version of this notice said reserved names "must be
respected", which was boilerplate rather than a reading of these two licenses. Should either
upstream add a reserved name in a future release, re-check this when bumping the font.

**The OFL reaches the font files only.** It is not copyleft over this project's source, and clause 5
explicitly excludes documents created with the fonts — so a generated PDF report carries no OFL
obligation despite embedding the faces.

## Trademark note

Per the Cyderes Brand Style Guide (v01-26), Space Grotesk / Space Mono are the approved alternates
to the licensed **PX Grotesk** family for teams without a PX Grotesk license. PX Grotesk itself is
proprietary, is not bundled, and is only named as a CSS fallback.

The fonts are OFL and freely redistributable. **Cyderes brand assets are not** — see the trademark
section of the top-level [NOTICE](../../NOTICE), and `scripts/make-public.py` for how they are
excluded from public distributions.
