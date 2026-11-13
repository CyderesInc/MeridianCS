# Launch: what to say when the result isn't the routine one

Read this when SKILL.md sends you here: a `selfupdate` result that is not silent, a `connect` state
other than `connected`, or new credentials to save. The wording below is the wording to use.

## Version check results (SKILL.md §0.5)

Cheap: the verdict is cached for a day, so most sessions answer from disk. Match the returned
`state` exactly — and for five of the six, **say nothing at all**, unless the result carries
`whatsNew` or `notice` (below). A version check is housekeeping, not an answer:

| `state` | Meaning | Your response |
|---|---|---|
| `current` | Newest release already installed | Nothing. |
| `unknown` | **The check itself failed** — GitHub unreachable, a proxy, or the anonymous rate limit | Nothing, unless it carries `notice: true` (below). **Never say "up to date"** — the check didn't answer, and reporting silence as an all-clear is the one mistake this state exists to prevent. |
| `disabled` | Self-update isn't configured, or the user switched it off | Nothing. |
| `dev` | A working tree or an unstamped copy — deliberately never overwritten | Nothing, unless they asked about versions; then quote `reasons`. |
| `ahead` | This build is newer than the latest release | Nothing. |
| `outdated` | A newer release exists | **Apply it** (below), then answer their question. |

On `outdated` only, install it and continue:

```bash
python scripts/meridian.py selfupdate --apply
```

If the result has `"applied": true`, report it in **one line** — after the §0 disclaimer, which
leads regardless, and before the rest of the welcome and the §1 greeting — filling in the versions
from the JSON:

> 🔄 **Updated to meridiancs `{toVersion}`** (from `{fromVersion}`). The new instructions take effect
> in your next session — everything works normally in this one.

**Keep that last clause.** These instructions loaded *before* the swap, so the scripts on disk are
now newer than the SKILL.md you are following — an update that changes behaviour without saying so is
indistinguishable from a bug.

**`whatsNew` on a check result is one of two exceptions to "say nothing".** It appears only in the
first session on a new version. Show it **once**, after the §0 disclaimer and before the
greeting: a `🆕 **What's new in meridiancs {version}**` line, then up to three bullets from its
newest entry, each cut to its bold lead-in. An applied update's `whatsNew` goes the same way, under
its 🔄 line. Without `whatsNew`, say nothing, and never "nothing new": its absence means there was
nothing to announce, not that nothing changed.

**`notice: true` on an `unknown` result is the other exception.** It comes only with
`cause: "tls_untrusted"`: Python on this machine doesn't trust the certificate it was shown for
GitHub, so this copy cannot update until that changes — a standing problem, not a passing outage,
and staying silent would leave the user on an old version indefinitely. `notice` is true at most
once a day. Say it in **one line**, after the §0 disclaimer, then carry on:

> ⚠️ **This copy of the skill can't check for updates** — Python doesn't trust the certificate it was shown for GitHub. {hint}

Without `notice`, an `unknown` stays silent, whatever its `cause`.

If the result has `"applied": false` — a rejected package, a failed download, anything — **say
nothing about it** and carry on. The updater leaves a working skill on every failure path, and a
self-update that didn't happen must never delay the user's question. Either way, **don't re-run the
check later in the same session.**

Asked to turn it off: `MERIDIAN_NO_AUTOUPDATE=1`, or `"autoupdate": false` in
`~/.meridian/config.json`. `selfupdate` then reports `disabled` and never reaches the network.

## Connection states other than `connected` (SKILL.md §1)

The state table in SKILL.md §1 says which template applies. Template A (`connected`) is there too.

**B — not_configured** (first run — ask for the two essentials, *don't* front-load every caveat):
> 👋 **Let's connect to your Meridian stack.** I need two things:
>
> 1. **FQDN** — your Meridian address, like `company.meridian-cyderes.com` (no `https://`, no path).
> 2. **API token** — a *User Generated* token from Meridian.
>
> Paste both and I'll validate. No token yet? Say **"walk me through it"** for the 4 clicks.

If they ask for the walkthrough, give this inline quick-path (full detail in
[authentication.md](authentication.md)):
>
> 1. **Settings → User Management**, find your account → **Edit**.
> 2. Ensure **`Api_Users`** is in **Roles** (add it, Save).
> 3. Click **Generate Token**, copy it — **Meridian shows it only once.**
> 4. Paste it here.

**C — auth_error** (only *now* introduce the role/SSO gotchas, as a short ranked list):
> ⚠️ **Reached `{fqdn}`, but the token was rejected (HTTP 401).** Usual causes:
>
> - Token expired or mistyped.
> - Account is missing the **`Api_Users`** role.
> - It's an **SSO account** — those can't use the API; use a token from a local Meridian account.
>
> Paste a fresh token and I'll retry.

**D — forbidden:**
> ⚠️ **`{fqdn}` is reachable, but this token is forbidden (HTTP 403)** — usually a *Limited* (scoped) token that doesn't allow this endpoint, or a missing role. Try a full **User Generated** token?

**E — unreachable:**
> ⚠️ **Couldn't reach `{fqdn}`** — DNS/TLS error. Check the FQDN is exact (no `https://`, no trailing path) and that the stack is reachable from this network. What's the correct address?

**F — tls_untrusted:**
> ⚠️ **`{fqdn}` answered, but Python doesn't trust the certificate it was shown.** {hint}

**G — token_unreadable:**
> ⚠️ **A token is saved for `{fqdn}`, but this Windows account can't decrypt it**, so it was never sent. That happens after a profile reset, a restore onto another machine, or a copied `~/.meridian` folder. Paste a fresh token and I'll save it again.

**H — stack_moved:**
> ⚠️ **`{fqdn}` has moved to `{domainMove.to}`.** Meridian is retiring `lucidum.cloud` addresses. Want me to move your saved stack there? It keeps your token, history and alert rules, and I'll check the new address works before changing anything.

## After you collect new credentials

1. Re-run the connection preflight to confirm you now get `connected`.
2. **Then** offer to save (its own step, so it isn't skipped):
   > Save this so you don't re-enter it next time? I'll save it under `~/.meridian/` — on Windows it's encrypted to your Windows account; on macOS and Linux it's plain text that only your user can read (outside any repo, never committed).

   On yes, save it **through the script — never write `config.json` yourself.** A hand-written file
   skips the owner-only permissions the script applies, and it drops the stack's `entity_salt`, which
   makes every earlier entity snapshot permanently incomparable. Pipe the token on stdin so it stays
   out of the command line (shell history, process listings):

   ```bash
   printf '%s\n' '<api token>' | python scripts/meridian.py stacks add --name <short name, e.g. prod> --fqdn company.meridian-cyderes.com --token -
   ```

   The first stack saved becomes active and is mirrored into `config.json`, so every helper picks it
   up. For an action token too (LDG only), send it as a second line and add `--action-token -`.

3. The **Action token** is optional — mention it only if the user asks about the LDG endpoint;
   `/CMDB/v2/data/cmdb` answers the same questions. Don't block onboarding on it.
