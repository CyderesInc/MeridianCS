# Scheduling snapshot collection — per OS

Trends, alerts and entity deltas all read this stack's local snapshot history, and **there is no
backfill**: a day nobody took a snapshot is a day no trend can ever describe. So history only builds up
if something takes a snapshot on a timer. Split out of [trend-verbs.md](trend-verbs.md), which covers
what the snapshots *mean*, because "help me schedule this" never needs that and "how has X trended"
never needs this.

## The short version

```bash
python scripts/meridian.py schedule show                 # this machine's task definition + register/unregister/status commands
python scripts/meridian.py schedule show --write         # also write it under ~/.meridian/schedule/ (still not registered)
python scripts/meridian.py schedule status               # did each job run, did it work, has it stopped
```

`schedule show` prints the definition and the exact `register` command; running that command is what
starts the schedule. **Nothing registers itself.** A scheduled task is persistent configuration on the
user's machine, so it is shown first and registered only on a yes. Options: `--at HH:MM` (local time,
default 09:00), `--weekly MON` for a weekly job instead of a daily one, `--entities SCOPE` to also
capture per-entity scores (it creates the stack's salt there and then, in the session, so the scheduled
run never has to write `config.json`), and `--os windows|macos|linux|cron` to target a scheduler other
than this machine's.

The task runs one command, `schedule run --expect-stack <fqdn> --cadence daily`, which:

- takes a fresh snapshot, exactly as `digest --snapshot` does, with every defined metric;
- evaluates the alert rules, if there are any, and delivers on change as `alerts notify` does;
- writes a `started` and a `finished` line to `~/.meridian/heartbeat.<fqdn>.jsonl`, whatever happens;
- exits **0** (ok), **1** (nothing written: refused or failed) or **3** (written, with problems: a
  failed digest section, an unresolved metric, a delivery failure, or a firing rule with nowhere to
  send it). Not 2, because `die()` and argparse already use it, and the scheduler's last-result field
  has to say which of these happened.

Its flags are a contract: a registered task keeps calling them across self-updates.

## One job, one stack

`config.json` holds the *active* stack, and a scheduled job reads whatever is there when it fires. So a
`stacks switch` in a session would silently point a daily job at a different customer. Instead, each job
is pinned to the stack it was created for (`--expect-stack`). **While another stack is active, the job
refuses each run and writes nothing**, and the refusal goes in that job's heartbeat. `stacks switch`
says so when it strands a job, and `schedule status` lists every job on the machine, not only the
active stack's.

It also refuses when the stack address and the token come from different places, such as
`MERIDIAN_FQDN` in the job's environment with the token read from `config.json`. Those two resolve
independently, so the FQDN check would pass while one stack's bearer token went to another stack's host.
`schedule show` refuses a stack that comes from environment variables for the same reason: the job
won't have them.

## Alert targets and other settings in a scheduled job

Webhook and SMTP settings are environment variables only (nothing about them is written to
`~/.meridian/`), and **no scheduler inherits the shell they were exported in**. That makes the most
likely first failure of a scheduled alert a firing rule with nowhere to go. `schedule run` reports it
rather than staying quiet: the heartbeat names the reason, the run exits 3, and the change stays pending
until it is delivered.

| Scheduler | Where the variables go |
|---|---|
| Windows Task Scheduler | user environment variables (`[Environment]::SetEnvironmentVariable('<NAME>', '<value>', 'User')`), then sign out and back in so the task sees them |
| macOS `launchd`, Linux systemd and cron | `~/.meridian/schedule/alert.env`, one `NAME='value'` line per setting, `chmod 600`. The generated wrapper sources it at each start if it exists; the skill itself never reads it |

The same gap catches TLS settings. If `MERIDIAN_CA_BUNDLE` is set only in your shell, a job behind a
TLS-inspecting proxy fails certificate verification on every run, so put `"ca_bundle": "<path>"` in
`config.json` instead. `schedule show` warns when it sees one of these set only in the shell.

## The heartbeat: telling a stopped job from a quiet week

The characteristic failure of any scheduled job is that **it stops running, and its silence reads as
good news**. A digest that never ran looks exactly like a week with nothing to report. That is the same
class of wrong answer the completeness flags and trend refusals exist to prevent, one layer out.

So every run writes two lines. `finished` is written in a `finally` that also catches `die()`. A run
killed outright (by the time limit, a crash or a power cut) cannot write its own `finished`, so a
`started` with nothing after it is the evidence, and `schedule status` reports it as killed. Example
lines (illustrative figures):

```text
{"event":"started","run":"2026-08-18T09:00:03Z-4120","ts":"2026-08-18T09:00:03Z","stack":"demo.example.com","cadence":"daily"}
{"event":"finished","exit":0,"ok":true,"snapshotWritten":true,"assets":12000,"durationS":6.6,...}
{"event":"finished","exit":1,"ok":false,"refused":true,"error":"the active stack is other.example.com, but this job collects for demo.example.com, ..."}
```

`schedule status` reads it back. **`stale`** means the job has stopped: a daily job with no attempt in
3 days and 6 hours, or a weekly job with none in 15 days. A daily threshold of two days would warn every
Monday, because a job that runs only while you are logged on legitimately misses a weekend. **`lastRunOk`**
means the job ran and failed. The two fail differently and need different fixes, so neither is folded
into the other. No heartbeat at all is `scheduled: false`, which never means "ok". "Written" is judged
from the history file itself, never from a return value, so a failure after the append can't hide a
snapshot that landed.

Cross-check against the scheduler's own view:

| Scheduler | Command | Notes |
|---|---|---|
| Windows Task Scheduler | `Get-ScheduledTaskInfo -TaskName "<label>" \| Select-Object LastRunTime, LastTaskResult` | `LastTaskResult` is the exit code above; 267009 = running, 267011 = has never run |
| macOS `launchd` | `launchctl print gui/$(id -u)/<label>` | shows the last exit status |
| Linux systemd timer | `systemctl --user list-timers <label>.timer` | last and next trigger; `journalctl --user -u <label>.service` has the run's own output |
| Linux cron | none | cron keeps no run history, so the heartbeat is the only record, which is the strongest reason to prefer a systemd timer |

## Why the generated definitions look the way they do

### Windows Task Scheduler

Two defaults silently skip runs, measured while setting up a daily job on a laptop. **Neither is
reachable through `schtasks` command-line flags; both need XML**, which is why `schedule show` emits XML
rather than a one-liner:

| Setting | Default | Why it matters |
|---|---|---|
| `DisallowStartIfOnBatteries` | **true** | An unplugged laptop **skips the run entirely**. On a mobile machine this is the single most likely reason a "daily" job produces four runs a week. |
| `StartWhenAvailable` | false | A machine asleep at the trigger time never catches up. Set true and the run fires when it wakes. |

`StopIfGoingOnBatteries` is false too, or a run that starts on AC is killed mid-flight when the charger
comes out. The rest of what the XML carries, and why:

- **`InteractiveToken`**, so no password is stored. The job runs only while you are logged on, which is
  the right trade for a tool holding a bearer token.
- **The working directory is your home folder, never the skill folder.** On Windows a folder that any
  process has as its working directory can't be renamed, so a job parked in the skill folder would make
  every self-update fail.
- **`pythonw.exe`** when it sits beside your Python, so no console window flashes up each morning.
  Under `pythonw` there is no console, so the job's own output goes to `~/.meridian/schedule/run.log`.
- **`IgnoreNew`** and a 30-minute **`ExecutionTimeLimit`**. The time limit is also the line
  `schedule status` uses to call an unfinished run killed.
- **The first start is always in the future**, so registering doesn't fire a "missed" run on the spot.
- **Short `8.3` paths are expanded and the arguments use Windows quoting**, which handles a folder name
  ending in a backslash.

The file is written UTF-16, which `schtasks` requires; `Register-ScheduledTask -Xml` takes the same
document. **`schtasks` from Git Bash needs `MSYS_NO_PATHCONV=1`**, or `/create` is rewritten as a Windows
path and the error names a directory you never mentioned:

```text
ERROR: Invalid argument/option - 'C:/Program Files/Git/create'.
```

That is the same MSYS path-mangling as the leading-slash `api` endpoint documented in SKILL.md §2.
PowerShell, which the generated commands use, avoids it entirely.

### macOS: `launchd`

The job is a **LaunchAgent**, not a LaunchDaemon. A daemon runs as root at boot with no user session,
which is the wrong context for a tool holding a user-scoped bearer token. That is the same reason the
Windows job uses `InteractiveToken` rather than a stored password.

- **`--write` puts the plist under `~/.meridian/schedule/`, not `~/Library/LaunchAgents/`.** launchd
  loads everything in that folder at the next login, so writing there would register the job by itself.
  The `register` command copies it in and runs `launchctl bootstrap gui/$(id -u)`.
- **A LaunchAgent does not inherit your login shell's environment.** Nothing sources `.zshrc`, so the
  job reads credentials from `~/.meridian/config.json`, and the wrapper sources `alert.env` (above).
  Paths go in as real arguments (`exec "$@"`), never spliced into a shell string.
- **A Mac asleep at the trigger time runs the job when it wakes**: launchd coalesces the missed
  intervals into one run. A Mac that was shut down does not catch up. The heartbeat shows the gap.
- **The log goes to `~/.meridian/schedule/`, not `/tmp`**, because the job's output can carry customer
  data and `/tmp` is readable by every local user.

### Linux: a systemd user timer, or cron

A **systemd user timer** is the better fit: `Persistent=true` is the direct analogue of Windows'
`StartWhenAvailable`, so a run missed while the machine was off fires at the next boot.
`journalctl --user` captures the output natively. Two things to know:

- **A user timer stops when you log out**, unless you run `loginctl enable-linger "$USER"` once.
- `EnvironmentFile=-%h/.meridian/schedule/alert.env` is optional (the leading `-`), so a job with no
  alert targets is a valid setup. `ExecStart` escapes quotes, `%` (systemd specifiers) and `$`.

`schedule show --os cron` emits one crontab line instead, with three silent-failure traps handled:

- **Minimal `PATH` and no login shell**, so the interpreter and script paths are absolute.
- **Cron mails job output by default, and most systems have no mail server**, so output, errors
  included, simply vanishes. The line redirects its own output to `~/.meridian/schedule/cron.<fqdn>.log`.
  It does **not** set `MAILTO=""`, because that applies to every later line of your crontab and would
  silence your other jobs too.
- **`%` is crontab's newline character**, so it is escaped everywhere in the command.

Nothing edits your crontab: add the line yourself with `crontab -e`. Plain cron has no catch-up, so a
machine that was off at the time simply misses that run.

## A PDF on a schedule

`schedule run` collects history and delivers alerts. It does not render a report. A recurring PDF is
still a separate command chained by hand:

```bash
python scripts/meridian.py digest > "$HOME/meridian-reports/digest.json" && python scripts/meridian.py report --input "$HOME/meridian-reports/digest.json" --out "$HOME/meridian-reports/Weekly-Posture.pdf" --title "Weekly Meridian Posture Digest"
```

Its outputs go to a folder **outside the skill** (`~/meridian-reports` here; create it once).
Self-update replaces the skill folder and deletes anything saved inside it, and `--out` refuses a
directory that does not exist. They also carry customer PII, so pick somewhere appropriate; see the
data-handling rules. A PDF under a scheduler needs a Chromium browser on the job's `PATH`, which cron
and launchd do not share with your shell.
