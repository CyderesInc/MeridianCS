What changed in each release of the meridiancs skill, newest first. The skill reads this file to
tell you what's new in the first session after it updates. Ask "what's new in the Meridian skill?"
at any time for the full list. To get started, see [Install](README.md#install) and
[Connect](README.md#connect) in the README.

## 2.27.3 - 2026-09-25

- **"Top" questions and counts are faster on stacks with large records.** A count no longer
  downloads a record, and a top-10 that took about 25 seconds now takes about 9, with the same
  results.
- **The raw API command is safer.** It refuses the connector-runs endpoint, which can carry each
  connector's settings, and paths written to slip past that check, and it asks before starting a
  full data ingestion run.
- **Commands that read a file refuse your saved-credentials folder**, so a report or API request
  can never pick up your saved tokens.
- **An alert that fails to send is sent again next time.** Before, a change that reached nobody
  was treated as delivered.

## 2.27.2 - 2026-09-25

- **Breakdowns now count the records that have no value for the field.** Before, those records
  were in no group and nothing said so, so a breakdown could look complete while part of the
  population was missing. The answer now says how many.

## 2.27.1 - 2026-09-24

- **Percentages from an earlier answer are no longer reused after Meridian rebuilds.** If one part
  of a share was re-run and the other wasn't, the skill now re-runs both or marks the figure as out
  of date, instead of quoting it as current.

## 2.27.0 - 2026-09-24

- **Every answer now says which data it describes**: the time of Meridian's latest rebuild, for
  example "Data as of 2026-09-24 10:12 UTC". If that time can't be read, the answer says so instead
  of implying the data is current.
- **If Meridian rebuilds mid-conversation, the skill tells you** before its next answer, says which
  earlier figures may have changed, and offers to re-run them. It never combines numbers from two
  rebuilds.
- **Figures from the past are labelled "Historical" wherever they appear.** That covers trends,
  alerts, snapshot history, the 30-day averages and a user's change history, so a past value is
  never mistaken for today's.
- **Trends are more accurate on stacks that rebuild several times a day.** Same-day rebuilds are
  separate points, and a snapshot just after midnight no longer draws a false flat line. Older
  history is unchanged.
- **Reports show the same "as of" or "Historical" line under the title.**

## 2.26.1 - 2026-09-24

- **Questions about HR systems now find your HR data.** Ask about Dayforce, BambooHR, ADP, Workday,
  UKG, HiBob or Sage People, or about managers and employees, and the skill checks those
  connectors directly instead of sometimes saying you have no HR data. If an HR connector is set up
  but not delivering, it says so and why.
- A field that appears on your stack after the skill first looked, for example when you enable a
  new connector, is no longer reported as missing.
- An update from a deeply nested folder on Windows now says why it can't install, instead of
  failing with a file-not-found error.

## 2.26.0 - 2026-09-23

- **Teams alerts now arrive.** They were sent in a format the standard Teams Workflows webhook
  accepts and then silently drops, so no alert ever reached the channel. They now arrive as a card.
- **Keep reports and exports out of the skill's folder:** updates replace it and delete anything
  saved there. If you scheduled a report from the old examples, point it at `~/meridian-reports`.
  The skill now warns if one would land there.
- **A raw API call that could change your stack now needs `--allow-write`.** The skill adds it only
  after you confirm that specific change. Queries are unaffected.
- **Update downloads can't be redirected away from GitHub.** Every redirect on the way to a
  release is checked, not only the first address, and a switch to unencrypted `http` is refused.
- **Reports can't be hijacked by a file in the current folder.** To build a PDF, the skill now runs
  only a browser installed as a real program, never a script that happens to sit in the working
  folder.
- **Digest reports escape the values they show**, so text from your stack can no longer inject
  markup into one.
- **The connector summary at the start of each session takes up less of the conversation.** Each
  warning is stated once, and every connector it affects points back to it.

## 2.25.0 - 2026-09-23

- **The skill now tells you what changed when it updates.** The first session on a new version
  lists that release's highlights from this file, and "what's new?" reads the whole of it. A fresh
  install says nothing, because nothing is new to it.
- **This changelog ships with every release.** It is the single source for each release's notes,
  and a release cannot be built without an entry here.

## 2.24.3 - 2026-09-22

- **Self-update now works on Windows when the skill's folder is in use.** Windows refuses to
  replace a folder that any program has open as its working directory, and the assistant's own
  shell often does. Every update on such a machine used to fail silently. The update now swaps
  the folder's contents instead, and puts every file back if anything goes wrong.

## 2.24.2 - 2026-09-22

- **Connector error messages no longer carry connector credentials.** Hosts, service accounts,
  URLs with passwords in them, and bearer tokens quoted inside a connector's error text are
  replaced with `[redacted]` before the message reaches the chat, the cache, or a report.
- **Saving credentials goes through the skill's own writer**, which keeps the file owner-only and
  preserves the key that keeps historical snapshots comparable. Both tokens can be supplied on
  standard input, so they never appear on a command line.
- **Exports can't overwrite the wrong file.** `report --out` must end in `.pdf` or `.html`, and
  `--format csv --out` must end in `.csv`. Both are checked before any work is done.
- **Local caches stay private on shared machines**, and alert delivery refuses unencrypted
  webhooks and unencrypted mail logins, and escapes text that could ping a whole Slack channel.

## 2.24.1 - 2026-09-22

- Documentation-only release. No change to the skill.

## 2.24.0 - 2026-09-22

- **The skill keeps itself up to date.** Once per session it checks for a newer release, cached
  for a day, and installs it. A failed check changes nothing and never claims to be current.
- **The source is published** under the Apache 2.0 licence.
