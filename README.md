# Meridian (Lucidum) API v2 — Claude Code Skill

A [Claude Code](https://claude.com/claude-code) skill that connects to a **Meridian**
(formerly Lucidum) stack via API v2, translates natural-language questions into the correct
API queries, and returns written answers — including PDF reports.

> Meridian is the Cyderes rebrand of Lucidum, so the API paths still use `/CMDB/v2/...` and the
> public docs live at lucidum.io.

## What it does

Ask plain questions ("who are the 10 riskiest users?", "which assets are missing MFA?",
"investigate <person>", "break assets down by risk level") and the skill picks the right
endpoint, builds the query, paginates within the rate limit, and answers in prose with severity
cues. It can also produce a PDF report of any result.

An investigation answers in one step: asking about a person or asset returns the profile *plus* the
findings and recommended actions, ordered most severe first, in about three seconds — the PDF is for
when you want a file to send on, not a prerequisite for seeing what to do.

At the start of a session it also reports **data coverage** — which connectors are enabled, which are
failing authentication, and how many records each last ingested — so you know what's behind an answer
before you act on it. Ask "connector status?" at any point to see it again.

## Install

### Prerequisites

| | |
|---|---|
| **Claude Code** | CLI, desktop app, web, or an IDE extension |
| **Python 3** | the only hard requirement — standard library only, nothing to `pip install` |
| **Chrome or Edge** | only for turning reports into PDF; without it you still get HTML |

> **The command differs by platform.** On **macOS and Linux** it is `python3`. On **Windows** it is
> `python`. macOS has shipped no `python` at all since 12.3, so a bare `python` there fails with
> "command not found" — that is a missing alias, not missing Python.

### How you get the files

The skill is a single self-contained folder — no build step and nothing to `pip install`. Download
the packaged zip from [Releases](https://github.com/CyderesInc/MeridianCS/releases), or clone the
repository; both land the same tree.

### Install on macOS — step by step

**1. Check for Python 3.**

```bash
python3 --version
```

If that prints a version (3.8 or newer), skip to step 2. Otherwise install it with
[the python.org installer](https://www.python.org/downloads/macos/) or, if you use Homebrew:

```bash
brew install python
```

**2. Put the skill in your Claude skills folder.** The folder name *is* the skill's identifier, so it
must end up exactly at `~/.claude/skills/meridiancs`.

```bash
mkdir -p ~/.claude/skills
gh release download --repo CyderesInc/MeridianCS --pattern '*.skill.zip' --dir ~/Downloads
unzip ~/Downloads/meridiancs.v*.skill.zip -d ~/.claude/skills/
```

Or clone it directly:

```bash
git clone https://github.com/CyderesInc/MeridianCS.git ~/.claude/skills/meridiancs
```

**3. Confirm the layout.** This must print a path, not an error:

```bash
ls ~/.claude/skills/meridiancs/SKILL.md
```

**4. Restart Claude Code.** Skills are registered at startup, so open a new session in any project —
the skill works from any directory, not just its own folder.

**5. Generate a Meridian API token** — see [Getting an API token](#getting-an-api-token) below.

**6. Connect.** Ask Claude anything about your inventory, or say "connect to my Meridian stack", and
paste the FQDN and token when prompted.

**7. Verify it worked.**

```bash
cd ~/.claude/skills/meridiancs && python3 scripts/meridian.py connect
```

A healthy stack returns `"state": "connected"` with your asset and user counts.

### Install on Windows — step by step

**1. Check for Python 3.** In PowerShell:

```powershell
python --version
```

If that prints a version (3.8 or newer), skip to step 2. If it opens the Microsoft Store instead, that
is Windows' placeholder stub and Python is *not* installed. Install it with:

```powershell
winget install Python.Python.3.13
```

Or use [the python.org installer](https://www.python.org/downloads/windows/) — and **tick "Add
python.exe to PATH"** on the first screen. Missing that checkbox is the most common cause of
"python is not recognized" afterwards. Close and reopen PowerShell before re-checking.

**2. Put the skill in your Claude skills folder.** The folder name *is* the skill's identifier, so it
must end up exactly at `%USERPROFILE%\.claude\skills\meridiancs`.

If you downloaded the zip in a browser, **right-click it → Properties → tick Unblock** if that
checkbox appears (Windows marks files that arrived from elsewhere, and PowerShell will refuse to
expand them cleanly otherwise). Then:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.claude\skills"
gh release download --repo CyderesInc/MeridianCS --pattern '*.skill.zip' --dir "$env:USERPROFILE\Downloads"
# The package filename carries its version -- use the one you actually downloaded.
Get-ChildItem "$env:USERPROFILE\Downloads\meridiancs.*.skill.zip" |
  Expand-Archive -DestinationPath "$env:USERPROFILE\.claude\skills" -Force
```

Or clone it directly:

```powershell
git clone https://github.com/CyderesInc/MeridianCS.git "$env:USERPROFILE\.claude\skills\meridiancs"
```

**3. Confirm the layout.** This must print the file, not an error:

```powershell
Get-Item "$env:USERPROFILE\.claude\skills\meridiancs\SKILL.md"
```

**4. Restart Claude Code.** Skills are registered at startup, so open a new session in any project —
the skill works from any directory, not just its own folder.

**5. Generate a Meridian API token** — see [Getting an API token](#getting-an-api-token) below.

**6. Connect.** Ask Claude anything about your inventory, or say "connect to my Meridian stack", and
paste the FQDN and token when prompted.

**7. Verify it worked.**

```powershell
cd "$env:USERPROFILE\.claude\skills\meridiancs"; python scripts\meridian.py connect
```

A healthy stack returns `"state": "connected"` with your asset and user counts. Edge ships with
Windows, so PDF reports work without installing anything else.

### Getting an API token

In Meridian, as a user with admin rights over the account you want to use:

1. **Settings → User Management**, find the account, click **Edit**.
2. Under **Roles**, ensure **`Api_Users`** is assigned, then Save.
3. Click **Generate Token**, copy it, then **Confirm**.

Two things catch people out. Meridian shows the token **once** — copy it before leaving the page. And
**SSO accounts cannot use the API at all**; the token must come from a local Meridian account holding
the `Api_Users` role.

You will also be asked for your **FQDN** — e.g. `company.lucidum.cloud`, with no `https://` and no
trailing path.

### Staying up to date

**Installs from v2.24.0 onward update themselves** from this repository's release feed — checked
once per session, cached for a day, no GitHub account needed.

Copies installed from v2.23.0 or earlier cannot self-update, and never will: self-update only works
from a build that already carries the feed address, and every release through v2.23.0 shipped
without one. Those report `disabled` and need one manual upgrade to reach a version that updates
itself:

```bash
gh release download --repo CyderesInc/MeridianCS --pattern '*.skill.zip'
```

Your credentials and history in `~/.meridian/` sit outside the skill folder and are untouched by a
swap. To see the current state for yourself:

```bash
python scripts/meridian.py selfupdate            # what's available, or why nothing is
python scripts/meridian.py selfupdate --apply     # install it
```

What the feature does, since the guarantees are the
reason it can be left on:

| | |
|---|---|
| **How often** | Once per session, and the answer is cached for a day — most sessions never touch the network |
| **Where from** | The configured public repository's latest release asset, over HTTPS, `github.com` only |
| **What it needs** | Nothing. No `git`, no GitHub account, no credentials — the release is public |
| **What it touches** | Only the skill folder. Your credentials and history in `~/.meridian/` are never read or written by an update |

- **The update takes full effect in your *next* session.** The scripts are live immediately, but the
  skill's instructions were loaded into the running session before the swap, so the message says so
  rather than leaving you to wonder why something changed.
- **A failed check is silent, and never claims success.** If GitHub is unreachable, a corporate proxy
  is in the way, or the anonymous rate limit is hit, the skill carries on with the version you have
  and says nothing. It will not report "up to date" when it could not actually tell.
- **A broken download can't break your install.** The new version is staged in a temporary folder,
  verified, and test-run before anything is replaced; the working copy is kept until the new one is
  in place, and restored if the swap fails.
- **A checkout is never overwritten.** If your skill folder is a git clone or a symlink to one — how a
  maintainer works on it — updates are declined and the reason is reported.
- **Turning it off.** Set `MERIDIAN_NO_AUTOUPDATE=1`, or add `"autoupdate": false` to
  `~/.meridian/config.json`. The check then reports `disabled` for that reason instead.

### Releases

Tagged [releases](https://github.com/CyderesInc/MeridianCS/releases) carry the packaged skill; each
asset is built from that tag's tree and is never re-uploaded afterwards, so a given version always
means the same bytes. Copies from v2.23.0 or earlier do not upgrade themselves (see
[Staying up to date](#staying-up-to-date)): replace the folder contents — there are
no configuration changes, and your saved credentials in `~/.meridian/` are untouched. Patch versions
cover refreshed documentation; the skill's behaviour changes only on a minor bump.

Rebuild the package with `python scripts/make-package.py --version <X.Y.Z>` after any behaviour
change — it's a build artifact, gitignored, and nothing in the repo will flag it as stale.

## Connect

On first use the skill prompts for your **Meridian FQDN** and a **User Generated API token** — see
[Getting an API token](#getting-an-api-token). Credentials are stored **outside this repo** at
`~/.meridian/config.json` on macOS or `%USERPROFILE%\.meridian\config.json` on Windows (or the
`MERIDIAN_FQDN` / `MERIDIAN_API_TOKEN` / `MERIDIAN_ACTION_TOKEN` environment variables) — never commit
them.

```json
{ "fqdn": "company.lucidum.cloud", "api_token": "…", "action_token": "…(optional, LDG only)" }
```

### Multiple stacks

Keep several stacks and switch between them — switching rewrites only `config.json`, so the other
stacks' credentials are never overwritten. All named stacks live in `~/.meridian/stacks.json`; an
existing single-stack `config.json` is migrated in automatically on first use.

```bash
python scripts/meridian.py stacks add --name prod --fqdn acme.lucidum.cloud --token '<token>'
python scripts/meridian.py stacks switch prod        # activate + validate
python scripts/meridian.py stacks list               # tokens redacted to last 4
```

To keep the token out of shell history and process listings, pass `--token -` and paste it on
stdin, or omit `--token` and set `MERIDIAN_API_TOKEN` for the one command. A stack with a
self-signed certificate takes `--insecure-tls`, stored per stack and carried across switches.

`python scripts/meridian.py stacks rm <name>` removes one. Each stack needs a token generated **on
that stack** — tokens are not portable between stacks.

### TLS

Connections to your stack are made with **certificate verification enabled**. If your stack presents
a self-signed or internally-issued certificate, opt out with `MERIDIAN_INSECURE_TLS=1` or by adding
`"insecure_tls": true` to `config.json` — but only where you trust the network path, because the API
token travels over that connection. `connect` and `check` both report `tlsVerified`, so an opt-out
saved once stays visible instead of becoming an invisible default.

### Recommended hardening

`config.json` and `stacks.json` hold plaintext tokens for *every* saved stack, so a single leak now
exposes each one. The scripts read these files in-process and never need an agent to open them —
so denying agent read access costs no functionality. Add to `~/.claude/settings.json` (macOS) or
`%USERPROFILE%\.claude\settings.json` (Windows):

```json
{
  "permissions": {
    "deny": ["Read(~/.meridian/**)", "Grep(~/.meridian/**)", "Glob(~/.meridian/**)"]
  }
}
```

Use `connect` or `stacks list` to inspect configuration — both redact tokens to the last 4. For
production tokens, prefer the `MERIDIAN_*` environment variables (session-scoped, never written to
disk) over saving them in the registry. Note this blocks the agent's file tools, not a shell command
reading the file directly; `sandbox.credentials.files` covers that path if you run sandboxed.

## Layout

```text
SKILL.md                     workflow + connection + query translation + presentation + safety
references/                  authentication, query-syntax, api-reference, field-map, recipes
scripts/
  meridian.py                the CLI — every verb, plus the PDF report generator
                               connect          onboarding preflight (run first every session)
                               connectors       data coverage: enabled / succeeding / ingesting
                               top              top-N by a numeric field
                               profile          full risk + blast-radius profile of a user/asset
                               list             filtered list / count
                               summary          group-by / posture (exact counts) + stack metrics
                               compare          two entities side by side
                               stacks           manage + switch between saved stacks
                               check            token capability preflight
                               refresh-fields   cache this stack's real field names
                               report           PDF/HTML from any verb's JSON
                               api              raw API caller (self-paces under 60/min)
  make-package.py            rebuilds meridiancs.v<version>.skill.zip from tracked files only
  make-sbom.py               regenerates sbom.cdx.json
assets/
  fonts/                     Space Grotesk + Space Mono (SIL OFL, see the licence texts alongside)
evals/                       smoke-test prompts
```

## Reports

`meridian.py report` renders a PDF from any verb's JSON output. Needs Python 3 + a Chromium browser
(Chrome/Edge) for HTML→PDF; falls back to HTML with `--html`.

This distribution carries no Cyderes brand assets, so reports render in neutral styling — the report
path falls back on its own when the artwork and stylesheet are absent, and names no brand in the
footer or masthead. See [Licensing](#licensing).

## Requirements

See [Prerequisites](#prerequisites) above — Claude Code, Python 3 (`python3` on macOS/Linux, `python`
on Windows), and a Chromium browser only for PDF reports.

> Versions through v2.1 also shipped PowerShell helpers (`scripts/meridian-*.ps1`) as a fallback for
> Windows without Python. They were removed in v2.2: they covered every verb except `report`, ran
> about 2× slower (one process per API call), and their failure modes were silent — the PowerShell
> array-unwrap bug returns an empty result rather than an error, which in an inventory tool reads as
> "nothing matches". Saved credentials in `~/.meridian/` are unaffected by the upgrade.

## Licensing

Licensed under the **Apache License, Version 2.0** — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

- **No third-party source code, and no dependency manifest.** Every import resolves to the Python 3
  standard library; there is nothing to `pip install` and nothing to audit for licence conflicts.
- **The four bundled fonts are SIL OFL 1.1**, redistributed unmodified with their full licence texts
  alongside them (`assets/fonts/SpaceGrotesk-OFL.txt`, `SpaceMono-OFL.txt`), as OFL section 2
  requires. The OFL covers the font files only — it is not copyleft over this source, and clause 5
  exempts documents the fonts are embedded in, so a generated report carries no OFL obligation.
- **[sbom.cdx.json](sbom.cdx.json)** is a CycloneDX 1.6 bill of materials, regenerated by
  `python scripts/make-sbom.py` and kept honest by `--check` in the test suite. GitHub's automatic
  dependency-graph export comes back empty for this repo — there is no manifest to read — so the
  SBOM is built explicitly.
- **Trademarks are not licensed.** Cyderes owns the *Cyderes*, *Meridian* and *Lucidum* marks —
  Lucidum was acquired by merger in October 2025 — and Apache-2.0 section 6 grants no rights in
  trade names or marks, so redistributing this software conveys no right to use any of them. Cyderes
  brand assets (wordmark artwork, Brand Style Guide palette and typography) are **not part of this
  distribution**, for a further reason: with the wordmarks present, anyone could generate an
  authentic-looking Cyderes-branded risk report. The report path falls back to neutral, unbranded
  styling on its own, and asserts no brand in text either.

## Notes

- Read-only by default. State-changing endpoints (ingestion trigger, connector profile
  create/delete/service toggle) require explicit confirmation.
