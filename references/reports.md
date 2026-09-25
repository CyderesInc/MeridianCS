# Branded PDF reports

`report` — how the branded PDF, the blast-radius graph and the trend charts are produced, and what to
pass them. Verb flags are in [scripts.md](scripts.md); the trend inputs it renders come from
[trend-verbs.md](trend-verbs.md).

Report generation is the only feature needing more than stdlib Python: it requires a Chromium browser
for print-to-PDF and falls back to writing branded HTML when none is found.

## Usage

- **Every report states what kind of data it shows**, in the masthead under the title, read from the
  input's `dataCurrency`: *Data as of 2026-09-24 10:12 UTC (latest Meridian rebuild)* for an LDG read,
  *Historical · local snapshots, 2026-09-01 to 2026-09-24* for a trend, or *Data currency could not
  be confirmed: …* when the stamp was unreadable. An input with no `dataCurrency` (written before
  v2.27.0, or `api` output) prints *Data currency not recorded in this input*, never nothing, since
  a report with no line reads as current. Historical parts inside a current report are labelled where
  they appear: the digest's "Now vs 30-day average (Historical)" column, and a profile's change-log
  instability callout. No emoji in the PDF, because the word carries the meaning.

- **Branded PDF reports** (`meridian.py report`) — the deliverable format for anything shareable.
  Pipe any verb's JSON in and get a **Cyderes-branded PDF** built to the official Brand Style Guide
  (v01-26): a black masthead pairing the **official Cyderes wordmark** (`assets/cyderes-logo.svg`,
  Cybervolt lime) with the **official Meridian wordmark** (`assets/meridian-logo.svg`, white) —
  both authentic vectors, on black per the brand rule — over a Light Gray `#F9F2ED` ground with
  black body text and the
  **Space Grotesk / Space Mono** brand-alternate fonts — bundled in `assets/fonts/` (OFL-licensed)
  and embedded as data URIs so reports look right on any machine. Severity dots + score bars use
  semantic colors kept separate from the brand green (green is accent-only per the guide). Design in
  `assets/cyderes-report.css`. Usage:

  ```bash
  python meridian.py top --table asset --field Risk_Score --top 10 --select "Asset_Name,Risk_Score,Risk_Level,IP_Address,OS,Count_KEV" > <scratch>/top.json
  python meridian.py report --input <scratch>/top.json --out "<out-dir>/Cyderes-Top10-Assets.pdf" --title "Top 10 Riskiest Assets" --date 2026-07-22
  ```

  `<scratch>` is the session scratchpad and `<out-dir>` the user's own folder — never the skill
  folder, which self-update replaces (deleting anything saved in it).

  It renders the branded HTML then converts to PDF via headless Chrome/Edge (auto-detected). If no
  browser is found it writes the branded `.html` instead; pass `--html` to force HTML. Handles the
  `top`, `list`, `summary`, and **`profile`** shapes. Report generation is the one feature that needs
  more than stdlib Python — a Chromium browser, for reliable print-to-PDF.
  - **`--input` takes several files, which renders one document with several subjects** — the normal
    use is an identity plus each of its linked assets, so a blast-radius answer and the per-asset
    exploitability detail arrive as one thing to hand over:

    ```bash
    python meridian.py profile --type user  --name AEXAMPLE > <scratch>/u.json
    python meridian.py profile --type asset --name 13837M3          > <scratch>/a1.json
    python meridian.py report --input <scratch>/u.json <scratch>/a1.json --out "<out-dir>/Cyderes-Profile.pdf" \
      --title "Identity Risk, Blast Radius & Linked Assets"
    ```

    Each subject keeps its own banner, stat row, findings and recommendations, and **nothing is
    aggregated across subjects** — a "total KEVs" spanning an identity and its own linked assets
    counts the same finding twice, so the top-level stat row is deliberately empty. Multiple files
    are **profiles only**: a `top` beside a profile is refused by name rather than rendered into
    something that looks deliberate. Everything else still takes one file, and no `--input` at all
    still reads stdin.
  - The stacked layout is denser than the single-subject one (measured: a user plus three linked
    assets went from 10 pages to 5, nothing removed) — asset bodies flow in two columns, the
    blast-radius graph floats beside the short sections, and uninformative "None." sections are
    dropped. **All of it is CSS scoped to `.subj` / `.pbody` / `.graphbox` / `.assetbox`**, classes
    only a multi-subject document emits, so a single-profile report is byte-identical to before the
    feature existed apart from two grouping wrappers. Don't unscope those rules to "tidy up" — that
    silently restyles every existing single-asset report.
  - **Don't try to shave the ~3s.** It is ~2.5s of Chrome startup, measured on a *trivial* page, and
    startup flags don't move it (`--no-first-run --disable-extensions --disable-sync` and friends
    measured 2557ms against 2551ms). HTML assembly is ~8ms including the 478KB of embedded font data
    URIs. Caching the browser-path lookup was tried and reverted: the search reads ~250ms in isolation
    but ~205ms of that is `import shutil`, already paid by the time `report` runs, so end-to-end it
    changed nothing (3000ms vs 3005ms). Only a persistent browser process would help, and across
    separate CLI invocations that means a daemon.
  - **Blast-radius network graph (user profiles):** when the input is a *user* `profile`, the report
    embeds a **force-directed blast-radius graph** — the user at the center with every linked identity
    account, threat (leaked creds + DLP alerts), linked asset (risk/KEV/encryption), and posture gap
    (MFA, non-compliance) as its own node; shared assets add asset→other-user edges so the true blast
    radius is visible. Layout is a deterministic Fruchterman-Reingold spring simulation seeded from the
    name (same profile → same picture), rendered as static SVG so it prints reliably. Node color =
    severity (red high/critical/threat, amber attention, green low, gray identity); **node size =
    risk score** (the user and each asset scale with their score; identity/threat/posture nodes,
    which have no score, sit at a baseline size). Generated by
    `_blast_radius_svg()` in `scripts/meridian.py`; asset profiles skip the graph. So the default way
    to give someone a blast-radius picture is just:

    ```bash
    python meridian.py profile --name "<person>" > <scratch>/prof.json
    python meridian.py report --input <scratch>/prof.json --out "<out-dir>/Cyderes-BlastRadius.pdf" --title "Risk & Blast-Radius Profile" --date 2026-07-23
    ```
