# Scheduling a recurring snapshot or report — per OS

How to drive `digest`/`snapshot`/`report` on a timer. Split out of [trend-verbs.md](trend-verbs.md)
(which covers the snapshot/trend/metrics/entity-delta semantics themselves) because a "how has X
trended" question never needs this, and "help me schedule a snapshot" never needs that.

The skill deliberately does **not** schedule anything itself — that belongs to the OS or to Claude
Code, and a daemon inside a CLI would be the wrong place for it. `digest` exists so the scheduled thing
is a single command:

```bash
python scripts/meridian.py digest > digest.json &&   python scripts/meridian.py report --input digest.json --out "Weekly-Posture.pdf" --title "Weekly Meridian Posture Digest"
```

Drive that from Windows Task Scheduler, macOS `launchd`, Linux `cron`/`systemd`, or Claude Code's own
scheduling — all four are supported today; none of it is skill code, just the OS running the command
above on a timer. Note the output carries customer PII, so a scheduled job must write somewhere
appropriate — see the data-handling rules.

## Windows Task Scheduler: two defaults that silently skip runs

Measured while setting up a daily job on a laptop (`PCSystemType 2`). **Neither is reachable through
`schtasks` command-line flags — both need XML**, which is the whole reason to register a task this way
rather than with a one-liner:

| Setting | Default | Why it matters |
|---|---|---|
| `DisallowStartIfOnBatteries` | **true** | An unplugged laptop **skips the run entirely**. On a mobile machine this is the single most likely reason a "daily" job produces four runs a week. |
| `StartWhenAvailable` | false | A machine asleep at the trigger time never catches up. Set true and the run fires when it wakes. |

Also set `StopIfGoingOnBatteries` false, or a run that starts on AC is killed mid-flight when the
charger comes out — which for `digest --snapshot` means a partial snapshot rather than no snapshot.

Register from XML (UTF-16 — `schtasks` requires Unicode; `Register-ScheduledTask -Xml` in PowerShell
takes the same document):

```xml
<Settings>
  <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
  <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
  <StartWhenAvailable>true</StartWhenAvailable>
  <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
  <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
  <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
  <DeleteExpiredTaskAfter>P1D</DeleteExpiredTaskAfter>
</Settings>
```

Use `InteractiveToken` for the principal so no password is stored; the job then only runs while the
user is logged on, which is the right trade for a tool holding a bearer token. Give a temporary job an
`<EndBoundary>` on its trigger plus `DeleteExpiredTaskAfter`, so scaffolding expires instead of firing
at a path that no longer exists.

**`schtasks` from Git Bash needs `MSYS_NO_PATHCONV=1`.** Otherwise `/create` is rewritten as a Windows
path and the error names a directory you never mentioned:

```text
ERROR: Invalid argument/option - 'C:/Program Files/Git/create'.
```

Same MSYS path-mangling as the leading-slash `api` endpoint documented in SKILL.md §2. PowerShell
avoids it entirely.

## macOS: `launchd`

Use a **LaunchAgent** (`~/Library/LaunchAgents/com.cyderes.meridian.digest.plist`), not a
LaunchDaemon — a daemon runs as root at boot with no user session, which is the wrong context for a
tool holding a user-scoped bearer token, the same reason the Windows section above runs the task under
`InteractiveToken` rather than storing a password.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.cyderes.meridian.digest</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-c</string>
    <string>cd /path/to/meridiancs &amp;&amp; python3 scripts/meridian.py digest &gt; digest.json &amp;&amp; python3 scripts/meridian.py report --input digest.json --out Weekly-Posture.pdf --title "Weekly Meridian Posture Digest"</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>/tmp/meridian-digest.log</string>
  <key>StandardErrorPath</key><string>/tmp/meridian-digest.log</string>
</dict>
</plist>
```

Two things this format changes versus the Windows job above:

- **A LaunchAgent does not inherit your login shell's environment** — nothing sources `.zshrc`/
  `.bash_profile`, so `MERIDIAN_API_TOKEN`/`MERIDIAN_FQDN` set there are invisible to the job. Either
  add an `EnvironmentVariables` dict to the plist, or rely on `~/.meridian/config.json` instead: since
  credentials resolve **env vars → config.json** in that order (CLAUDE.md), a job with no env vars set
  simply falls through to whatever `connect`/`stacks switch` already wrote there, which needs no plist
  changes at all.
- **`launchd` has no equivalent of Windows' `StartWhenAvailable`.** A Mac asleep at 9:00 simply misses
  that day's run — there's no built-in catch-up on wake. The heartbeat log below is the only way to
  notice.

Load/unload with `launchctl load ~/Library/LaunchAgents/com.cyderes.meridian.digest.plist` /
`launchctl unload ...`; `launchctl list com.cyderes.meridian.digest` reports `LastExitStatus` (0 =
success) as the cross-check equivalent to `Get-ScheduledTaskInfo` below.

## Linux: `cron` or a `systemd` user timer

Plain `cron` works but carries two of its own silent-failure traps:

- **Minimal `PATH` and no login shell** — same environment gap as `launchd`: a crontab line runs
  without `.bashrc`/`.profile` sourced, so use an absolute interpreter path (`/usr/bin/python3`) and
  either an explicit `MERIDIAN_*` line in the crontab or the same `config.json` fallback described
  above.
- **Cron mails job output by default, and most systems have no MTA configured** — output (including
  errors) then vanishes with no delivery failure either, which is an even quieter version of "silence
  reads as good news" than a skipped run. Set `MAILTO=""` and redirect explicitly instead of relying on
  mail:

  ```cron
  0 9 * * * cd /path/to/meridiancs && /usr/bin/python3 scripts/meridian.py digest > digest.json && /usr/bin/python3 scripts/meridian.py report --input digest.json --out Weekly-Posture.pdf --title "Weekly Meridian Posture Digest" >> ~/meridian-digest.log 2>&1
  ```

Plain `cron` also has no wake-catch-up, same gap as `launchd`. Where that matters — a laptop that's
often asleep — a **systemd user timer** is the better fit, because `Persistent=true` is a direct
analogue of Windows' `StartWhenAvailable`: a missed run fires as soon as the system is next up.

`~/.config/systemd/user/meridian-digest.service`:

```ini
[Service]
Type=oneshot
WorkingDirectory=/path/to/meridiancs
ExecStart=/bin/bash -c 'python3 scripts/meridian.py digest > digest.json && python3 scripts/meridian.py report --input digest.json --out Weekly-Posture.pdf --title "Weekly Meridian Posture Digest"'
```

`~/.config/systemd/user/meridian-digest.timer`:

```ini
[Timer]
OnCalendar=*-*-* 09:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable with `systemctl --user enable --now meridian-digest.timer` (a **user** unit — same
no-stored-password, runs-as-you reasoning as the LaunchAgent and `InteractiveToken` above). `journalctl
--user -u meridian-digest.service` captures stdout/stderr natively, closing cron's silent-mail gap
without a manual redirect. `systemctl --user status meridian-digest.timer` shows the last and next
trigger time as the cross-check equivalent to `Get-ScheduledTaskInfo` below.

## A scheduled job needs a heartbeat, not just output

The characteristic failure of any scheduled job here is that **it stops running, and its silence reads
as good news** — the same class of wrong answer the completeness flags and trend refusals exist to
prevent, one layer out. A digest that never ran looks exactly like a week with nothing to report.

So have the wrapper append one line per run — timestamp, exit code, and a summary — *including on
failure*, and never let an exception swallow the line:

```text
2026-08-18T09:00:03  ok=True   exit=0   assets=34364 sectionsUnavailable=0   6.6s
2026-08-19T09:00:04  ok=False  exit=1   error=HTTP 401: token rejected
```

Seven lines at the end of a week means it ran seven times. Four lines is a finding, and one you can
only make if the absence was recorded somewhere. Cross-check against the scheduler's own view, so a
log line written by a job that then crashed cannot fool you:

| Scheduler | Command | Notes |
|---|---|---|
| Windows Task Scheduler | `Get-ScheduledTaskInfo -TaskName "<name>" \| Select-Object LastRunTime, LastTaskResult` | `LastTaskResult` 0 = success, 267009 = running, 267011 = has-never-run |
| macOS `launchd` | `launchctl list <label>` | `LastExitStatus` 0 = success |
| Linux `systemd` timer | `systemctl --user status <name>.timer` | shows last and next trigger; `journalctl --user -u <name>.service` has the run's own output |
| Linux `cron` | none built in | cron itself has no run history — the wrapper's log line is the *only* record, which is the strongest reason to prefer a systemd timer on Linux when one is available |
