# Security policy

## Reporting a vulnerability

Report security problems privately, through GitHub's private vulnerability reporting:
**[Report a vulnerability](https://github.com/CyderesInc/MeridianCS/security/advisories/new)**.
Please don't open a public issue for one.

A useful report says which version you have (`python scripts/meridian.py selfupdate` prints it as
`installedVersion`), what you did, and what happened. Leave out real API tokens, stack hostnames and
inventory output; a redacted example is enough.

## What is in scope

This repository is a Claude Code skill: a client for the Meridian API. Anything the skill's own code
does is in scope, and these matter most:

- **The self-update path.** Anything that could make an install download, unpack or run code that
  is not a genuine release.
- **Credential handling.** Any way for a stack's API token to leave `~/.meridian/`, reach a host
  other than that stack, or appear in output, logs or reports.
- **Customer data.** Inventory data written somewhere the user did not ask for, or connector
  configuration surviving into output.
- **Alert delivery.** A Slack, Teams or email alert carrying more than the alert.

The Meridian platform and its API are out of scope here. Report those through your Cyderes support
contact.

## Supported versions

Only the latest release is supported. Installs from v2.24.0 onward update themselves, so a fix
ships as a new release rather than as a backport.
