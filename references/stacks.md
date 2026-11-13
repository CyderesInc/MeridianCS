# Managing multiple stacks

Read this before `stacks add`, `switch`, `rm` or `move` (SKILL.md §1 sends you here).

Users can keep credentials for several stacks and switch the active one **without re-entering
anything or overwriting the others**. All named stacks live in `~/.meridian/stacks.json`; the
**active** stack is mirrored into `config.json` (the single credential set every helper reads), so
switching only rewrites `config.json` — the other stacks stay intact in the registry.

Trigger on "switch to my prod stack", "use `<name>`", "add another stack", "what stacks do I have":

```bash
python scripts/meridian.py stacks list                                                  # saved stacks + which is active
printf '%s\n' '<token>' | python scripts/meridian.py stacks add --name prod --fqdn acme.meridian-cyderes.com --token -  # others untouched
python scripts/meridian.py stacks switch prod                                           # activate + validate
python scripts/meridian.py stacks rm prod
python scripts/meridian.py stacks move prod --fqdn acme.meridian-cyderes.com           # new address, same token + history
```

- `switch` mirrors the chosen stack into `config.json` **and** runs the §1 preflight, returning the
  same `state` (`connected` / `auth_error` / …) — report it with the §1 templates. A switch changes
  which connectors feed the answers, so re-run `connectors` (§1.5) and re-state the coverage for the
  stack you just moved to.
- Each stack needs a token generated **on that stack** (tokens are per-stack — you can't reuse one).
- `add` never touches other stacks; the **first** stack added becomes active automatically.
- `rm` of the active stack makes another saved stack active: **say which** (`active`), since the next
  answer comes from it. `rm` keeps the stack's history, metrics and alert rules (`keptOnDisk`); add
  `--purge` only when the user asks to delete them, after saying it can't be undone. Relay a
  `scheduleWarning` with its `unregister` command.
- `move` changes a saved stack's address and keeps its token, entity salt, history, metrics and
  alert rules (`carried`). It checks the new address with the saved token first; `moved: false`
  means nothing changed, so report its `state` with the §1 templates. A scheduled job is pinned to the
  old address, so the move retires it: relay the `scheduleWarning` and its `unregister` command, and
  offer `schedule show` for the new address. Never move a stack with `rm` + `add`: the history stays
  under the old address, where nothing reads it.
- Env vars (`MERIDIAN_*`) still override `config.json`; `switch` warns if they'd mask the change.
- An existing single-stack `config.json` is migrated into the registry automatically on first use,
  so nothing is lost when the feature is first exercised.
