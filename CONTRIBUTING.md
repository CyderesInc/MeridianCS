# Contributing

Bug reports, feature requests and pull requests are all welcome.

## Before you post anything: keep your environment out of it

Meridian responses carry names, devices, departments and vulnerability data from the environment
they came from, and issues and pull requests here are public. **Never paste an API token, a stack
hostname, or inventory output.** Redact it, or describe its shape instead. Security problems go
through private reporting, as [SECURITY.md](SECURITY.md) describes, never a public issue.

## Reporting a bug or asking for a feature

Use the issue forms. For a bug, the version (`python scripts/meridian.py selfupdate` prints it as
`installedVersion`), your OS and Python version, what you asked or ran, and what happened instead
are usually enough to reproduce it.

## Pull requests

This repository is generated from Cyderes' internal repository: each release commit replaces the
tree here with a fresh derivation. So a pull request is not merged here directly.

- **It is reviewed here, then reapplied internally.** If it's accepted, the change ships in the
  next release, and the pull request is closed with a link to that release.
- **By submitting one you agree it is licensed under the Apache License 2.0**, as section 5 of the
  [LICENSE](LICENSE) provides for contributions.

To make one easy to accept:

- **Python 3 standard library only.** `scripts/meridian.py` has no pip dependencies, and that is
  deliberate. Only `report` needs anything more (a Chromium browser for PDF output).
- **Run the offline suite and the lint hooks.** CI runs both, on Ubuntu and Windows:

  ```bash
  python evals/test_connect.py
  pre-commit run --all-files
  ```

- **`SKILL.md` is behaviour, not documentation.** It is the instruction set the model follows at
  runtime, so a wording change there changes what the skill does. Say so in the pull request.
- **An unknown is never a zero.** Where the API cannot tell, the skill says it could not tell. A
  change that turns a failed check, a missing field or an unreachable connector into `0`, "none" or
  "up to date" will be asked to change.
