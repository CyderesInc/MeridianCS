# Meridian skill — smoke tests

`evals.json` holds representative prompts for each query shape the skill supports, with the
expected behavior. They're **qualitative** checks (the outputs depend on live stack data), not
automated pass/fail assertions.

## How to run

For each prompt: start a session with the `meridiancs` skill enabled and a configured stack, paste
the prompt, and check three things:

1. **Routing** — did the skill pick the right verb/script (top / list / summary / profile / compare
   / check / api)?
2. **Correctness** — does the answer match a manual query? (Cross-check a couple with
   `meridian.py api` directly.)
3. **Presentation** — is it formatted per SKILL.md §4 (headline first, severity dots, magnitude
   bars, callouts), and are caveats surfaced (client-side sort, truncation, oscillation)?

## Onboarding tests (13–18)

These check the connection experience (SKILL.md §1). The `[bracketed]` text in each prompt is a
**precondition to arrange first**, not something to paste. Set up the state, then send a connect-style
prompt and confirm the reply matches the right `state` + message template:

| Eval | Set up this state | Expect |
|---|---|---|
| 13 | Valid `~/.meridian/config.json` (or env vars) | `connected` → template A greeting with counts; no re-prompt |
| 14 | No config file **and** no `MERIDIAN_*` env vars | `not_configured` → template B; essentials only, no role/SSO wall |
| 15 | Config present but token wrong/expired | `auth_error` → template C; surfaces Api_Users role + SSO |
| 16 | Config with a non-resolvable FQDN | `unreachable` → template E; re-prompts for FQDN |
| 17 | Provide fresh creds mid-session | re-validates, offers save with plain-text warning, redacts token |
| 18 | Any state | inline 4-step token quick-path, links authentication.md |

## Data-coverage tests (19–22)

These check SKILL.md §1.5 — the first question of a session must tell the user which connectors feed
the stack, so no answer lands without its provenance.

| Eval | Set up this state | Expect |
|---|---|---|
| 19 | Valid config; ask any question cold | one `connect --with-connectors` call, then the coverage summary (tally + named connectors + failing callout) *and* the answer; not repeated on the next question |
| 20 | Any connected state | `connectors` verb, names + pass/fail + records, grouped failures, no raw JSON, no direct `/connector/profile` call |
| 21 | A failing endpoint-protection or identity connector | the answer is caveated with that connector's state instead of being presented as complete |
| 22 | Token that 403s on the connector endpoints | still `connected` and still answering; the missing half reported in one line |

Preview the raw coverage payload without a session:

```bash
python scripts/meridian.py connectors
```

Quick way to preview the raw state the skill keys off, without a full session:

```bash
python scripts/meridian.py connect
```

Temporarily point `MERIDIAN_FQDN` / `MERIDIAN_API_TOKEN` at bad values to see `unreachable` /
`auth_error` without touching your saved config. Confirm the token is never echoed beyond `tokenLast4`.

### Automated part (`test_connect.py`)

The deterministic slice is covered by an automated test — the state classification
(401/403/other/network → `state`), the `not_configured` path, and the §1.5 connector rollup (driven by
a fixture) all run fully offline, no stack needed:

```bash
python evals/test_connect.py            # offline; CI-safe; exit 0 = pass
python evals/test_connect.py --live      # also assert connected + coverage + redaction on your stack
```

It shares `classify_connect_error()` and `summarize_connectors()` with `meridian.py`, so a regression
in state mapping, in a health verdict, or in the credential-field filtering fails the test. The
remaining checks (message templates, staged asks, how the coverage summary reads) stay qualitative.

## What to watch for (regressions seen during development)

- Onboarding: role/SSO caveats belong in `auth_error` (template C), **not** in the first-run ask
  (template B). The full token must never be echoed — only the last 4. The save prompt must state
  the config is plain text.
- Numeric filters must send **unquoted** values (quoted → 0 results).
- Query array nesting: AND = separate inner arrays, OR = objects in one array. If a query returns an
  empty response, dump the generated body first — over/under-nesting looks like "no matches".
- State-changing endpoints (ingestion run, connector profile create/delete/service toggle) must
  ask for confirmation.
- `/CMDB/v2/connector/profile` can carry sensitive connector configuration (connection targets,
  service-account identifiers, an encrypted secret). Only the `connectors` verb may read it, and its
  output must stay free of those fields — the offline test asserts this on a fixture and the `--live`
  run asserts it on real data.
- On a **real customer stack**, run `meridian.py refresh-fields` first — field names differ from
  the demo cheat-sheet.
