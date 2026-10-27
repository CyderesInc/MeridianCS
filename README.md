# Meridian (Lucidum) API v2 — Claude Code Skill

A [Claude Code](https://claude.com/claude-code) skill that connects to a **Meridian**
(formerly Lucidum) stack via API v2, translates natural-language questions into the correct
API queries, and returns written answers — including PDF reports.

> Meridian is the Cyderes rebrand of Lucidum, so the API paths still use `/CMDB/v2/...` and the
> public docs live at lucidum.io.

## What it does

Ask in plain language. The skill picks the endpoint, builds the query, pages through the results
within the API's 60-requests-a-minute limit, and answers in prose with severity cues.

| | Ask things like | Verbs behind it |
|---|---|---|
| **Answer** | "how many servers are in AWS?", "what's missing CrowdStrike?", "break assets down by risk level", "certificates expiring in the next 30 days" | `list`, `summary`, `top` |
| **Investigate** | "investigate &lt;person&gt;", "compare these two laptops" | `profile`, `compare` |
| **Use your own terms** | "what's in PCI scope?", "do we have HR data?" | `labels`, `hr` |
| **Track over time** | "start tracking KEV exposure", "what got worse since Friday?" | `metrics`, `snapshot`, `trend`, `schedule` |
| **Alert** | "tell me when a connector breaks" | `alerts` |
| **Report and export** | "make this a PDF", "send me this every week", "export every match to a spreadsheet" | `report`, `digest`, `--format csv` |

An investigation answers in one step: asking about a person or asset returns the profile *plus* the
findings and recommended actions, ordered most severe first, in about three seconds. The PDF is
for when you want a file to send on, not a prerequisite for seeing what to do.

**It says what it doesn't know.** For a security inventory, a confident wrong answer is the expensive
failure, so the skill is built to show the limits of each answer:

- **Data coverage first.** A session opens with which connectors are enabled, which are failing, and
  how many records each last ingested. Ask "connector status?" to see it again.
- **Every answer is dated and sourced.** It names the stack it came from and how current the data is
  (the latest Meridian rebuild), so production is never mistaken for staging and yesterday's figure
  is never presented as today's.
- **Gaps are stated, not hidden.** A breakdown that couldn't place every record says how many it
  missed. Something that wasn't measured is reported as unknown, never as zero or "no change". A trend
  across a change in connector health says so.
- **Read-only by default.** State-changing endpoints (an ingestion trigger, connector profile changes)
  need explicit confirmation.

## Install

### Prerequisites

| | |
|---|---|
| **Claude Code** | CLI, desktop app, web, or an IDE extension. Claude Cowork is not supported: it has no copy of the skill's scripts to run. |
| **Python 3.9 or newer** | the only hard requirement — standard library only, nothing to `pip install` |
| **Chrome or Edge** | only for turning reports into PDF; without it you still get HTML |

Gemini CLI, GitHub Copilot and OpenAI Codex can run the skill too. The steps below are for Claude
Code; [Other agents](#other-agents-gemini-copilot-codex) covers where those put it and what they need.

> **The command differs by platform.** On **macOS and Linux** it is `python3`. On **Windows** it is
> `python`. macOS has shipped no `python` at all since 12.3, so a bare `python` there fails with
> "command not found" — that is a missing alias, not missing Python. Commands below are written for
> each platform; elsewhere in the docs they are written `python` for brevity.

### How you get the files

The skill is a single self-contained folder — no build step and nothing to `pip install`. Download
the packaged zip from [Releases](https://github.com/CyderesInc/MeridianCS/releases): that is the
install that keeps itself up to date. Cloning the repository lands the same files, but a clone never
updates itself (see [Staying up to date](#staying-up-to-date)), so you would have to `git pull` for
every release.

### Install on macOS — step by step

**1. Check for Python 3.**

```bash
python3 --version
```

If that prints a version (3.9 or newer), skip to step 2. Otherwise install it with
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

Or double-click the zip in Finder and drag the resulting `meridiancs` folder into `~/.claude/skills/`.

Or clone it, if you would rather update with `git pull` yourself. A clone never updates itself:

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

If that prints a version (3.9 or newer), skip to step 2. If it opens the Microsoft Store instead, that
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

Or clone it, if you would rather update with `git pull` yourself. A clone never updates itself:

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

### Other agents: Gemini, Copilot, Codex

`SKILL.md` follows the open [Agent Skills](https://agentskills.io) format, so the same package also
runs under agents other than Claude Code. On 2026-10-15, Gemini CLI, GitHub Copilot CLI and OpenAI
Codex CLI each found the skill with its full description. That check proves the skill is found, not
that a session works: what each agent needs before the skill can reach your stack is below.

**Where it goes.** Extract the same package into `~/.agents/skills/`
(`%USERPROFILE%\.agents\skills\` on Windows), the user-level folder all three read. The zip holds a
`meridiancs` folder, so the skill lands at `~/.agents/skills/meridiancs`. On macOS or Linux:

```bash
mkdir -p ~/.agents/skills
unzip ~/Downloads/meridiancs.v*.skill.zip -d ~/.agents/skills/
```

On Windows, in PowerShell (unblock the zip first, as in step 2 above):

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.agents\skills"
Get-ChildItem "$env:USERPROFILE\Downloads\meridiancs.*.skill.zip" |
  Expand-Archive -DestinationPath "$env:USERPROFILE\.agents\skills" -Force
```

| Agent | Reads the skill from | Before the first session |
|---|---|---|
| **Gemini CLI** | `~/.agents/skills` or `~/.gemini/skills` | Nothing. `gemini skills list` should show `meridiancs`. |
| **GitHub Copilot CLI** | `~/.agents/skills`, `~/.copilot/skills`, and `~/.claude/skills` as an "Inherited" skill | Nothing. If Claude Code already has the skill, Copilot CLI uses that copy and needs no second install. `copilot skill list` should show it. |
| **GitHub Copilot in VS Code** (agent mode) | `~/.copilot/skills`, per VS Code's documentation (not tested here) | Type `/skills` in the chat panel to see what loaded. |
| **OpenAI Codex**: CLI, IDE extension, ChatGPT desktop app | `~/.agents/skills` | Open the sandbox, below. Without it, Codex can't reach your stack or save your credentials. |
| **ChatGPT on the web** | — | Not supported. ChatGPT's web chat runs no standalone skills. |

**Codex: open the sandbox.** Codex's default `workspace-write` sandbox allows no network access and
no writes outside the project you opened. The skill needs both: it calls your stack, and it keeps
credentials, caches and history in `~/.meridian`. Add this to `~/.codex/config.toml`, which the CLI,
the IDE extension and the ChatGPT desktop app share:

```toml
sandbox_mode = "workspace-write"

[sandbox_workspace_write]
network_access = true
writable_roots = ["~/.meridian"]

# Windows only: without a Windows sandbox mode, Codex runs every command read-only.
[windows]
sandbox = "unelevated"
```

Tested on Windows with Codex CLI 0.163.0 on 2026-10-15; not yet on macOS or Linux.

- **Create `~/.meridian` before the first session** (`mkdir ~/.meridian`). A writable root that
  doesn't exist yet stays read-only, so the skill can't create the folder to save your credentials.
  Without the root, `connect` reports your stack as unreachable, but the real cause is that it
  can't write to `~/.meridian`.
- **`~` works.** Codex expands it to your account's home folder.
- **On Windows, the `[windows]` table is required.** Without it, Codex falls back to read-only with
  no network, whatever `sandbox_mode` says. `"elevated"` is the other value, and it needs a one-time
  setup as an administrator.
- **`network_access` is not per skill.** It opens outbound network to every command Codex runs in
  that sandbox.
- **Permission profiles replace these settings.** If your `config.toml` sets `default_permissions`,
  don't add the block above, because the two systems don't combine. Add `"~/.meridian" = "write"`
  under your profile's `[permissions.<name>.filesystem]` table and `enabled = true` under
  `[permissions.<name>.network]`.
- **Self-update doesn't work under Codex on Windows.** An update is staged in the folder that holds
  the skill. Making `~/.agents/skills` writable gets the staging folder created, but Python makes
  that folder private to your account, so the sandbox can't write the download into it. The
  update fails without a word, and the skill stays on its version. Upgrade by hand (see
  [Staying up to date](#staying-up-to-date)). On macOS and Linux, adding `~/.agents/skills` as a
  writable root should be enough, but that's untested. The root makes every skill in that folder
  writable to Codex, which is the trade.

**Your credential files have less protection under these agents.** In Claude Code, a
`permissions.deny` rule keeps the agent's file tools out of `~/.meridian` (see
[Recommended hardening](#recommended-hardening)). This README documents no equivalent for the agents
above, so there only `SKILL.md`'s instruction never to read those files keeps the agent out of them.
They hold the token for every saved stack: encrypted to your Windows account on Windows, plain text
on macOS and Linux. What each vendor offers, as of 2026-10-15:

- **Gemini CLI** has a policy engine whose `deny` rules can match a tool call's arguments. Save the
  rule below as `~/.gemini/policies/meridian.toml`; Gemini loads it at startup without a flag.
  Tested with Gemini CLI 0.64.0 on Windows on 2026-10-15, it refused `read_file` on either file and
  any shell command naming one (`cat ~/.meridian/config.json`, a `Get-Content` of the full path),
  while the skill's own `meridian.py` commands still ran. It does not catch a command that reaches
  the files some other way: `cat ~/.meridian/*.json` printed both.

  ```toml
  [[rule]]
  toolName = "*"
  argsPattern = '\.meridian[/\\]+(config|stacks)\.json'
  decision = "deny"
  priority = 900
  ```

- **GitHub Copilot** documents no way to deny a path, in the CLI or in VS Code. Copilot CLI starts
  with access to the directory you launched it in and asks before going further, so decline any
  request to read `~/.meridian`.
- **Codex** applies its filesystem rules to every command in its sandbox, the skill's own script
  included. A rule denying `~/.meridian` would stop the skill from reading its own credentials, so
  Codex has no way to keep the agent out while letting the skill in.

**Two smaller differences.**

- **Environment variables** set in Claude Code's `settings.json` don't reach other agents. Set any
  `MERIDIAN_*` variable in your shell profile, bearing in mind that a desktop app started from the
  Dock or the Start menu doesn't read one.
- **The welcome message says "Meridian for Claude"** whichever agent shows it. The greeting is part
  of the skill's instructions, so it is left as it is.

### Getting an API token

In Meridian, as a user with admin rights over the account you want to use:

1. **Settings → User Management**, find the account, click **Edit**.
2. Under **Roles**, ensure **`Api_Users`** is assigned, then Save.
3. Click **Generate Token**, copy it, then **Confirm**.

Two things catch people out. Meridian shows the token **once** — copy it before leaving the page. And
**SSO accounts cannot use the API at all**; the token must come from a local Meridian account holding
the `Api_Users` role.

You will also be asked for your **FQDN** — e.g. `company.meridian-cyderes.com`, with no `https://` and
no trailing path.

## Connect

On first use the skill asks for your **Meridian FQDN** and a **User Generated API token** — see
[Getting an API token](#getting-an-api-token) — validates them, and then offers to save them. Saved
credentials live **outside the skill folder**, in `~/.meridian/` (`%USERPROFILE%\.meridian\` on
Windows), and are never committed. Let the skill save them, or use `stacks add` below, rather than
writing the files by hand: the script creates them owner-only and keeps the per-stack `entity_salt`
that entity snapshots depend on.

- **On Windows, saved tokens are encrypted** to your Windows account, so a copy of the files is useless
  on another machine or account. Existing installs are converted the first time they connect. On macOS
  and Linux they are plain text that only your user can read.
- **Environment variables win over saved settings.** `MERIDIAN_FQDN`, `MERIDIAN_API_TOKEN` and
  `MERIDIAN_ACTION_TOKEN` are never written to disk, which makes them the safer choice for a
  production token. A saved token is only ever sent to the address it was saved with: if
  `MERIDIAN_FQDN` names a different stack, set `MERIDIAN_API_TOKEN` for it as well.
- **The action token is optional.** It is only needed for the LDG endpoint, and the standard one
  answers the same questions.

### Multiple stacks

Keep several stacks and switch between them — switching rewrites only `config.json`, so the other
stacks' credentials are never overwritten. All named stacks live in `~/.meridian/stacks.json`; an
existing single-stack `config.json` is migrated in automatically on first use.

```bash
printf '%s\n' '<token>' | python scripts/meridian.py stacks add --name prod --fqdn acme.meridian-cyderes.com --token -
python scripts/meridian.py stacks switch prod        # activate + validate
python scripts/meridian.py stacks list               # tokens redacted to last 4
```

`--token -` reads the token from stdin, which keeps it out of shell history and process listings;
omitting `--token` uses `MERIDIAN_API_TOKEN` instead. `--action-token -` does the same for the
action token, read as the next line. A stack with a self-signed certificate takes `--insecure-tls`,
stored per stack and carried across switches. Each stack needs a token generated **on that stack** —
tokens are not portable between stacks.

`python scripts/meridian.py stacks rm <name>` removes one, with its caches. Its snapshot history,
metrics and alert rules stay on disk unless you add `--purge`, which deletes them for good. If it was
the active stack, another saved stack becomes active and the output says which; if none remain, its
credentials are removed from `config.json`.

### Moving to meridian-cyderes.com

Meridian is retiring `lucidum.cloud` addresses on **2026-12-05** in favour of `meridian-cyderes.com`;
each stack keeps its name, so `company.lucidum.cloud` becomes `company.meridian-cyderes.com`. When a
saved stack is still on an old address, `connect` says so and offers the move:

```bash
python scripts/meridian.py stacks move prod --fqdn acme.meridian-cyderes.com
```

The move keeps the stack's token, history, metrics and alert rules, and checks that the new address
answers for that token before it changes anything. If the new address isn't live yet, it reports
`moved: false` and leaves the stack as it was, so it is safe to try. Moving with `stacks rm` and
`stacks add` instead would leave the history under the old address, where nothing reads it.

The move accepts only the stack's own `meridian-cyderes.com` address, because it sends the saved
token there. A stack that genuinely moved somewhere else takes `--any-fqdn`. The new address is
always checked with certificate verification on, and an `insecure_tls` setting stays behind.

### TLS

Connections to your stack are made with **certificate verification enabled**. If your stack presents
a self-signed or internally-issued certificate, opt out with `MERIDIAN_INSECURE_TLS=1` or by adding
`"insecure_tls": true` to `config.json` — but only where you trust the network path, because the API
token travels over that connection. `connect` and `check` both report `tlsVerified`, so an opt-out
saved once stays visible instead of becoming an invisible default.

**Behind a TLS-inspecting proxy** (Zscaler and the like), don't turn verification off: give Python
the proxy's root certificate instead. Python uses its own list of trusted certificates, not your
operating system's, which is why `gh` and `curl` can work while the skill reports
`CERTIFICATE_VERIFY_FAILED`.

- **On macOS this is automatic.** The skill also trusts the certificates in the system keychains,
  where a managed Mac's proxy root is normally installed. It reads them with `/usr/bin/security` and
  never reads your login keychain. `MERIDIAN_NO_KEYCHAIN=1` turns this off.
- **Anywhere else, or a root that isn't in the System keychain:** save the root as a PEM file and set
  `MERIDIAN_CA_BUNDLE` to its path, or add `"ca_bundle": "<path>"` to `config.json`. For Claude
  Code, the `env` block of `~/.claude/settings.json` is the place for the variable. The file adds to
  Python's trusted certificates rather than replacing them.

Both apply to every verified connection: your stack, update checks, and alert webhooks and email.
`connect` shows what was added as `tlsTrust`. A certificate that still isn't trusted is reported as
`tls_untrusted`, with these instructions.

### Recommended hardening

`config.json` and `stacks.json` hold the tokens for *every* saved stack. Encrypted or not, anything
running as you can still use them. The scripts read these files in-process and never need an agent
to open them, so denying agent read access costs no functionality. Add to `~/.claude/settings.json`
(macOS) or `%USERPROFILE%\.claude\settings.json` (Windows):

```json
{
  "permissions": {
    "deny": ["Read(~/.meridian/**)", "Grep(~/.meridian/**)", "Glob(~/.meridian/**)"]
  }
}
```

Use `connect` or `stacks list` to inspect configuration — both redact tokens to the last 4. This
blocks the agent's file tools, not a shell command reading the file directly;
`sandbox.credentials.files` covers that path if you run sandboxed. Under Gemini, Copilot or Codex
this rule doesn't apply; see [Other agents](#other-agents-gemini-copilot-codex) for what does.

## Using it

Most of the time you just ask, and the skill runs the right command. The commands are shown here so
you know what exists; `python scripts/meridian.py <verb> --help` documents every flag.

### Tracking over time

Meridian keeps no history, so the skill records its own: a **snapshot** is a dated copy of the
stack's totals, risk breakdown, top lists and your **metrics** (named counts you choose to track),
appended to a local file per stack. `trend` compares snapshots.

```bash
python scripts/meridian.py metrics add --name kev-exposed --where "Count_KEV >= Integer 1"
python scripts/meridian.py snapshot
python scripts/meridian.py trend --since 2026-10-01
```

History only grows when something takes a snapshot, so say "keep tracking this" and the skill sets
up a daily or weekly job for Windows, macOS or Linux. It shows you the definition and registers
nothing without a yes.

```bash
python scripts/meridian.py schedule show      # the job definition, and the commands to register it
python scripts/meridian.py schedule status    # did each job run, did it work, has it stopped
python scripts/meridian.py schedule rm        # retire a job; prints the command that removes the task
```

A job collects for one stack and writes nothing while another is active. Every run is recorded, and
the next session tells you when a job has stopped or keeps failing. Details:
[references/trend-verbs.md](references/trend-verbs.md) and
[references/scheduling.md](references/scheduling.md).

### Alerts

Rules are evaluated against the recorded history: a connector entering the failing set, or a
tracked metric crossing a threshold. Each rule is `firing`, `clear` or `unevaluable`, and an
untestable rule is never reported as passing.

```bash
python scripts/meridian.py alerts add --name connectors --if coverage-regressed
python scripts/meridian.py alerts add --name kev-ceiling --if above --metric kev-exposed --value 500
python scripts/meridian.py alerts eval
python scripts/meridian.py alerts notify      # Slack, Teams or email, only when a verdict changes
```

Delivery targets come from environment variables only (see
[Configuration reference](#configuration-reference)), and the Meridian token is never sent to them.
A scheduled job needs those variables in its own environment; see
[references/scheduling.md](references/scheduling.md).

### Reports and exports

`meridian.py report` renders a PDF from any verb's JSON output, including trend charts and a
blast-radius graph for a profile. Several profiles render as one document. It needs a Chromium
browser (Chrome or Edge) for the PDF, and falls back to HTML with `--html`. This distribution carries
no Cyderes brand assets, so reports render in the neutral default theme and name no brand (see
[Licensing](#licensing)).

```bash
python scripts/meridian.py profile --name "<person>" > p.json
python scripts/meridian.py report --input p.json --out profile.pdf
python scripts/meridian.py digest > d.json && python scripts/meridian.py report --input d.json --out digest.pdf
python scripts/meridian.py list --where "Count_KEV >= Integer 1" --all --format csv --out kev.csv
```

`digest` is the periodic posture review in one call. `--format csv` (on `list`, `top` and `trend`)
writes rows to a spreadsheet-ready file and prints only a summary, which keeps large results out of
the conversation. Details: [references/reports.md](references/reports.md).

## Configuration reference

Environment variables override `config.json`. Set them for Claude Code in the `env` block of
`~/.claude/settings.json`.

| Setting | What it does |
|---|---|
| `MERIDIAN_FQDN`, `MERIDIAN_API_TOKEN`, `MERIDIAN_ACTION_TOKEN` | The stack and its tokens, for this session only, never written to disk |
| `MERIDIAN_INSECURE_TLS=1`, or `"insecure_tls": true` | Skip certificate verification for a self-signed stack ([TLS](#tls)) |
| `MERIDIAN_CA_BUNDLE`, or `"ca_bundle": "<path>"` | Extra root certificates, for a TLS-inspecting proxy ([TLS](#tls)) |
| `MERIDIAN_NO_KEYCHAIN=1` | macOS: don't read the system keychains for root certificates |
| `MERIDIAN_NO_AUTOUPDATE=1`, or `"autoupdate": false` | Turn off self-update ([Staying up to date](#staying-up-to-date)) |
| `MERIDIAN_ALERT_SLACK_WEBHOOK` | Slack incoming-webhook URL for `alerts notify` |
| `MERIDIAN_ALERT_TEAMS_WEBHOOK` | Teams Workflows webhook URL for `alerts notify` |
| `MERIDIAN_ALERT_EMAIL_SMTP_HOST` and `MERIDIAN_ALERT_EMAIL_TO` | Email delivery for `alerts notify`; optionally `_SMTP_PORT`, `_FROM`, `_SMTP_USER`, `_SMTP_PASS` and `_STARTTLS` with the same `MERIDIAN_ALERT_EMAIL` prefix |
| `MERIDIAN_NO_CACHE=1` | Turn off the aggregate cache, so every figure is measured fresh |
| `MERIDIAN_PRETTY_JSON=1` | Indent the JSON output, for reading it by hand |

## Staying up to date

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

What the feature does, since the guarantees are the reason it can be left on:

| | |
|---|---|
| **How often** | Once per session, and the answer is cached for a day — most sessions never touch the network |
| **Where from** | The configured public repository's latest release asset, over HTTPS, `github.com` only |
| **What it needs** | Nothing. No `git`, no GitHub account, no credentials — the release is public |
| **What it touches** | Only the skill folder. Your credentials and history in `~/.meridian/` are never read or written by an update |

- **The update takes full effect in your *next* session.** The scripts are live immediately, but the
  skill's instructions were loaded into the running session before the swap, so the message says so
  rather than leaving you to wonder why something changed.
- **You're told what changed.** The first session on a new version lists that release's
  highlights from [CHANGELOG.md](CHANGELOG.md), and asking "what's new in the Meridian skill?"
  reads the whole of it. A fresh install says nothing, because nothing is new to it.
- **A failed check never claims success.** If GitHub is unreachable or the anonymous rate limit is
  hit, the skill carries on with the version you have and says nothing. It will not report "up to
  date" when it could not actually tell. The one failure it mentions, at most once a day, is a
  certificate Python doesn't trust: that won't clear up on its own, so it says how to fix it (see
  [TLS](#tls)).
- **A broken download can't break your install.** The new version is staged in a temporary folder,
  verified, and test-run before anything is replaced; the working copy is kept until the new one is
  in place, and restored if the swap fails.
- **Only a signed release installs** (from v2.28.0). The signature is checked before the download
  is opened, so a release not signed by a Cyderes release key is never unpacked or run. You can
  check one yourself; see [Verifying a release](#verifying-a-release).
- **A checkout is never overwritten.** If your skill folder is a git clone or a symlink to one — how a
  maintainer works on it — updates are declined and the reason is reported.
- **Turning it off.** Set `MERIDIAN_NO_AUTOUPDATE=1`, or add `"autoupdate": false` to
  `~/.meridian/config.json`. The check then reports `disabled` for that reason instead.

### Verifying a release

Every release from v2.28.0 carries a signature beside the package
(`meridiancs.v<X.Y.Z>.skill.zip.sig`). The keys below are the ones the skill trusts.

Save these lines as a file named `allowed_signers`:

<!-- allowed-signers:begin -->
```text
meridiancs-release namespaces="meridiancs-release" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPYtuETdYl+SD4h3N/1f8gOT3biRfmoMJpg++ty7EWrA meridiancs-release-vault-v1
```
<!-- allowed-signers:end -->

Then, in the folder holding the package, its `.sig` and that file (put the release's version in place
of `X.Y.Z`):

```bash
gh release download vX.Y.Z --repo CyderesInc/MeridianCS --pattern 'meridiancs.v*'
ssh-keygen -Y verify -f allowed_signers -I meridiancs-release -n meridiancs-release -s meridiancs.vX.Y.Z.skill.zip.sig < meridiancs.vX.Y.Z.skill.zip
```

`Good "meridiancs-release" signature` means the package is exactly what a release key signed.
PowerShell has no `<` redirect, so on Windows run the second line in Git Bash or wrap it in
`cmd /c "..."`.

### Releases

Tagged [releases](https://github.com/CyderesInc/MeridianCS/releases) carry the packaged
skill; each asset is built from that tag's tree and is never re-uploaded afterwards, so a given
version always means the same bytes. Patch versions carry fixes and refreshed documentation; new
capability comes with a minor bump. [CHANGELOG.md](CHANGELOG.md) says which is which. Copies from
v2.23.0 or earlier do not upgrade themselves (see [Staying up to date](#staying-up-to-date)): replace
the folder contents — there are no configuration changes, and your saved credentials in
`~/.meridian/` are untouched.

## Layout

```text
SKILL.md              the skill's runtime instructions: connection, query translation, presentation, safety
CHANGELOG.md          what changed in each release; the skill reads it after an update
CONTRIBUTING.md       how to propose a change
SECURITY.md           how to report a vulnerability
LICENSE, NOTICE       Apache-2.0, attribution and trademark notice
sbom.cdx.json         CycloneDX bill of materials
references/           what the skill reads on demand
  authentication.md   tokens, roles and the SSO restriction
  query-syntax.md     the query DSL, operators and nesting
  api-reference.md    endpoints and response shapes
  field-map.md        field names and the risk model (varies per stack)
  recipes.md          worked queries for common questions
  scripts.md          why each verb behaves as it does
  trend-verbs.md      snapshots, metrics, trends and alerts
  scheduling.md       scheduled collection on Windows, macOS and Linux
  reports.md          PDF reports: layout, blast-radius graph and trend charts
  internals.md        transport, rate limiting, TLS, caching and self-update
  welcome.md          the launch greeting
scripts/
  meridian.py         the CLI, standard library only; every verb is listed below
  tools/              packaging, signing, SBOM and docs-check tooling (never shipped in the package)
assets/report-base.css  the report layout; a theme supplies only its colours, fonts and logos
assets/fonts/         Space Grotesk and Space Mono (SIL OFL, licence texts alongside)
evals/                the automated test suite, plus prompt-level checks in evals.json
```

`scripts/meridian.py` verbs (`--help` on any of them lists its flags):

| Area | Verbs |
|---|---|
| Connection | `connect` (run first every session), `asof` (how current the data is), `check` (what the token can do), `stacks` |
| Questions | `list`, `summary`, `top`, `profile`, `compare`, `labels`, `hr`, `connectors`, `refresh-fields`, `api` (raw calls, paced under the rate limit) |
| History | `snapshot`, `snapshots`, `trend`, `metrics`, `alerts`, `schedule`, `digest` |
| Output | `report` |
| Maintenance | `selfupdate` |

## Licensing

Licensed under the **Apache License, Version 2.0** — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

- **The skill has no third-party dependencies.** `scripts/meridian.py` imports only the Python 3
  standard library, so it runs anywhere with nothing to `pip install`. The repository's release
  tooling in `scripts/tools/` uses `hvac` for Vault, hash-locked in its `uv.lock`, and never ships.
- **The four bundled fonts are SIL OFL 1.1**, redistributed unmodified with their full licence texts
  alongside them (`assets/fonts/SpaceGrotesk-OFL.txt`, `SpaceMono-OFL.txt`), as OFL section 2
  requires. The OFL covers the font files only — it is not copyleft over this source, and clause 5
  exempts documents the fonts are embedded in, so a generated report carries no OFL obligation.
- **[sbom.cdx.json](sbom.cdx.json)** is a CycloneDX 1.6 bill of materials, regenerated by
  `python scripts/tools/make-sbom.py` and kept honest by `--check` in the test suite. GitHub's automatic
  dependency-graph export comes back empty for this repo — there is no manifest to read — so the
  SBOM is built explicitly.
- **Trademarks are not licensed.** Cyderes owns the *Cyderes*, *Meridian* and *Lucidum* marks —
  Lucidum was acquired by merger in October 2025 — and Apache-2.0 section 6 grants no rights in
  trade names or marks, so redistributing this software conveys no right to use any of them. Cyderes
  brand assets (wordmark artwork, Brand Style Guide palette and typography) are **not part of this
  distribution**, for a further reason: with the wordmarks present, anyone could generate an
  authentic-looking Cyderes-branded risk report. The report path falls back to neutral, unbranded
  styling on its own, and asserts no brand in text either.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) describes how to propose a change, and
[SECURITY.md](SECURITY.md) how to report a vulnerability privately. Run the offline test suite before
opening a pull request:

```bash
uv run --locked --project scripts/tools --group release pytest evals
```

`python scripts/tools/make-package.py --version <X.Y.Z>` builds the package zip from tracked files;
releases themselves are built, signed and published by the maintainers' release workflow.
