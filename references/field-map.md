# Meridian field cheat-sheet (typical deployment)

A cache of field names, value conventions, and response quirks, so you don't have to re-pull and
re-grep `/CMDB/v2/data/metadata/{asset,user}` every session. **Deployments add custom fields, Tags,
and SmartLabels**, so if an expected field is missing or a query returns 0 unexpectedly, fall back to
the metadata endpoint to confirm the exact name/case. Field names are case-sensitive.

> **The counts in this document are illustrative round numbers**, not measurements of any particular
> deployment. They are chosen so the *relationships* between them hold — a naive figure against a
> correct one, a numerator against the right denominator — because that relationship is the whole
> lesson in each case. Treat them as a worked example on a hypothetical stack of ~15,000 active
> compute assets and ~10,000 identities. Where an exact value is itself the finding (a `0`, or two
> queries returning an identical count), that is called out.

## Risk conventions (both asset and user)

- **`Risk_Score`** (Float) — the master risk metric. **Unbounded, NOT 0–10** — real values range
  from single digits into the thousands. Never assume a 0–10 or 0–100 scale. Send comparison values
  as **unquoted numbers**.
- **`Risk_Level`** (String) — the tier. Commonly `1-low`, `2-medium` (often low-frequency),
  `3-high`. Map to severity colour: `3-high` → 🔴, `2-*` → 🟠/🟡, `1-low` → 🟢.
- **`Risk_STD`** (Float) — "Risk Ranking", a 0–100 percentile (100 = riskiest). Good for "top X%".
- **`RiskReason1/2/3`** (String) — the top 3 human-readable risk factors ("Top Factor 1/2/3").
- **`Risk_Reasons`** (List) — full list of risk factors.
- **`RiskFactor1/2/3`** (String) — "Top Variable 1/2/3" (the driving data variables).

## User (table `user`) — common fields

- Identity: `Owner_Name` (the key/username), `displayName`, `Owner_Email` (List),
  `Owner_Job_Title`, `Owner_Department`, `Owner_Manager`, `Owner_ID`, `Owner_Groups`,
  `Owner_Sources`, `Source_User_Name` (List), `Role_Name`.
- Threats/data: `Threat_List` (List — see quirk below), `Data_Classification`, `Data_Type`,
  `Data_Risk`, `Files` (List of {File_Name...}).
- Posture: `Is_MFA_Configured` (List of objects — see quirk), `Count_No_MFA`,
  `Non_Compliance` (List), `Count_Non_Compliance`, `Owner_Status` (List of per-source objects).
- Associations: `High_Risk_Asset` (List of asset names), `Count_High_Risk_Asset`, `Asset_Name`
  (List), `Count_Asset`.

## Asset (table `asset`) — common fields

- Identity: `Asset_Name` (the key), `Host_Name` (List), `FQDN` (List), `IP_Address` (List),
  `MAC_Address` (List), `sourcetype` (List, **lowercase field name**), `Lucidum_Asset_Type`,
  `Lucidum_OS_Type`, `OS`, `Is_Cloud_Device`, `Is_Public`, `Is_Encrypted`.
- Vulnerabilities: `KEV` (List of CVE IDs actively exploited), `Count_KEV`,
  `Critical_CVE`/`Count_Critical_Severity_Vuln`, `High_CVE`/`Count_High_Severity_Vuln`,
  `Medium_CVE`/`Count_Medium_Severity_Vuln`, `Low_CVE`, `CVE` (all), `Count_CVE` (see cap quirk),
  `High_EPSS`/`Count_High_EPSS_Vuln`, `Fixed_CVE`, `Count_Mitigated_Vuln`.
  Weighted risk contributors: `Risk:_Critical_KEVs`, `Risk:_Critical_Vulns` (Integer).
- Associations: `High_Risk_User` (List), `Count_High_Risk_User`, `High_Risk_App`,
  `Count_High_Risk_App`, `Critical_Risk_App`.
- `lucidum_vuln_risk` (Float) — "Meridian Verified Risk".

## Response quirks (these bite every time — handle them up front)

- **NEVER derive a coverage gap by subtracting two asset counts.** Meridian creates **one asset node
  per data source**, linked by `SAME_AS`, so one physical laptop can exist as an MDM record, an
  identity-provider device record, a network record *and* an EDR record. `all macOS − has_edr` counts
  the non-EDR *representations of covered machines* as uncovered machines. On real data this
  overstates gaps by multiples, not percentages — an EDR gap by around **3.5×** (roughly 440 claimed
  against roughly 125 actual) and an MDM gap by around **14×** (roughly 280 against 20). On the
  illustrative stack above, the same mistake claims **25,200** uncovered assets where the answer is
  **9,000**. The raw `/CMDB/v2/data/cmdb` endpoint does **not** traverse `SAME_AS`; the console does,
  which is why the console and a hand-rolled subtraction disagree.
  - **Anchor coverage questions to a deduplicated denominator**, normally the
    `Online_Compute_SmartLabel == Binary Yes` population ("active compute resources"), then filter
    `sourcetype not in <agent>` inside that same query. One query, no arithmetic.
  - **Sanity-check against a type that should be fully covered.** If every asset typed `laptop` has
    the agent, a double-digit percentage fleet gap is impossible — that contradiction is the tell.
  - **If the user's console shows a different number, the console is right.** Say so, correct the
    figure, and reconcile by composition (group the surplus records by `sourcetype` combination) rather
    than defending the derived one.
- **A missing count on an agent-less asset means unknown, never zero.** Records with neither an MDM
  nor an EDR source return `Count_CVE: null` because nothing is reporting vulnerabilities — they are
  the *least* assessed machines, not the cleanest. Never let a null read as a healthy 0.
- **A coverage percentage over a population that mostly lacks the field is the same bug one layer up.**
  Suppose `Is_Encrypted` is populated on **8,500** of the **15,000** active compute assets. Then
  `count(Is_Encrypted == true) / 15,000` books **6,500 unknown** assets as unencrypted — about
  **12%** — where the population that actually carries the field is about **21%**. Both are
  arithmetically correct and only one answers the question asked. Distinguish the two question
  shapes: "does signal Y exist?" is safe over the whole population, "what does signal Y say?" must
  divide by the sub-population that has it, and state the unknown remainder either way.
- **`sourcetype` `in` / `not in` are reliable and complementary**, so the pair is a free self-check.
  On the illustrative stack, `in <edr_a>,<edr_b>` returns **6,000** and `not in` the same list
  returns **9,000** — together exactly the **15,000** active compute assets, partitioned with nothing
  left over. That the two sum to the total is the property worth checking; the individual numbers are
  not. (The naive subtraction over the whole asset table claims **25,200**, a ~2.8× overstatement.)
  The operators were never the bug; the subtraction was.
- **A `sourcetype` value that matches nothing is not a zero.** Values are exact and unintuitive —
  a stack typically carries names like `crowdstrike_host` and `sentinelone_agent`, *not*
  `crowdstrike` / `sentinelone`. A plausible-but-wrong value returns 0 records, which in a coverage
  answer reads as "no EDR anywhere". Confirm the value with
  `summary --table asset --by sourcetype` before building a percentage on it.
- **OS strings vary in case by source** — agent-sourced records may read `macOS 26.5.1` where
  identity- or network-sourced records read `MACOS 26.5.1`. `match` is case-insensitive so it catches
  both; `==` would not.
- **`Agent_Status` / `Is_Managed_Asset` / `Not_Managed` can be entirely unpopulated** (0 records)
  even though the fields exist in metadata. Confirm a field has data before building a coverage
  answer on it, and don't infer "nothing is managed" from an empty field.
- **A field literally named for "which sources are missing" can itself be one of the unpopulated
  ones** (0 records) even though it exists in metadata. It reads like the obvious way to answer
  "what's missing an agent?" and answers nothing — use the deduplicated `sourcetype not in <agent>`
  approach above instead of reaching for a field just because its name matches the question.
- **`Is_MFA_Configured` is an `Embed_List` of per-source objects**
  (`[{ "Source": "<idp>_user", "Status": "yes|no" }, ...]`) — readable in a record, but **not
  queryable**: `exists`, `match` and `not match` all return HTTP 400 on it, same as `Vuln_List`.
  Read it out of a fetched record; never build a filter on it.
- **`Count_No_MFA` is populated only where it is `>= 1`, so `== 0` is always empty.** On the
  illustrative stack of 10,000 identities: `exists` and `>= 1` both return **2,600**, `== 0` returns
  **`0`** — that zero is the behaviour, not an illustration — and `== null` returns **7,400**. Two
  consequences, both of which have bitten:
  - **"MFA is on" is not expressible on this API.** `== 0` is a tautology (the field's presence
    *means* a source reports MFA off), and `== null` is "no MFA signal at all", not "MFA is on" —
    treating those 7,400 as compliant is the unknown-as-passing error. What you *can* say is
    "2,600 identities have at least one source reporting MFA off, and 7,400 have no MFA
    telemetry" — a floor, not a total.
  - **Gating a percentage on `exists` here makes the denominator the failing set**, so the
    answer is a permanent 0.0%: arithmetically correct, reads as catastrophic, means nothing.
    Before gating any metric on `exists`, check that the field is populated on the *passing*
    records too. `Is_Encrypted` is — of the 8,500 populated records, both the true and the false
    cases are present — and `Count_No_MFA` is not.
- **`Owner_Status` is a List of per-source objects** (`{sourcetype, Status, Is_Disabled,
  Lucidum_Owner_Status}`) — sources often disagree (Enabled in the directory, Deprovisioned in the
  IdP). Use `Lucidum_Owner_Status`, or a reconciled-status SmartLabel if the deployment defines one,
  for the settled value.
- **`exists` on a dotted sub-field of a per-source embedded list (`Owner_Status.<field>`) undercounts
  and must never be used as a coverage denominator.** `match`/`==` resolve the dotted path correctly,
  but `exists` does not — it only recognises the sub-field as present when none of the record's
  source entries carry it null, so a sub-field only a few connectors ever report gets treated as
  absent everywhere else. On the illustrative stack this looked like `exists` returning a few dozen
  identities against several times as many — roughly 6× — for `== Binary 1` on the same sub-field.
  Querying the *parent* field's own `exists` is no substitute: it is true for essentially the whole
  identity population, since almost everyone has *some* embedded entry, populated or not. There is no
  reliable single-call "has this sub-field" filter for these paths — count coverage the way a genuine
  List field's coverage is counted, with an OR query over discovered values, never `exists`.
- **`Threat_List` mixes two kinds of entry**: DLP/behavioural threats prefixed with a severity in
  square brackets, e.g. `[Critical]<description of the DLP rule that fired>`, and credential leaks
  of the form `Leaked Password <masked>: <source>`. Split on the `Leaked Password` prefix to
  separate "exfiltration/behavioural" from "leaked creds" — they tell very different stories.
- **`Count_CVE` appears capped at 1000** — treat it as "≥1000"; the per-severity counts
  (`Count_Critical_Severity_Vuln`, etc.) are itemized and more precise.
- **`Vuln_List` is an `Embed_List` of per-CVE objects** carrying `CVE`, `Score` (CVSS), `epss`,
  `epss_percentile`, `Is_Fixable`, `Is_KEV`, `lucidum_vuln_risk` (Meridian's own composite) and
  `Name` (a long description). It arrives with the record, so reading it costs nothing extra —
  `profile` uses it for severity, exploitability and fixability findings that KEV counts alone
  cannot express. Two cautions: **`exists` on it returns HTTP 400** (it is not a queryable scalar —
  filter on `Count_CVE` / `Count_KEV` instead), and it is **unbounded** — well over a hundred
  entries on a single host is entirely possible — so anything rendering it needs a disclosed cap.
- **`Is_Fixable: 0` means no patch exists**, which is worse than an unpatched fixable flaw, not
  better — it can only be closed by upgrading or replacing the software. Values come back as
  floats (`1.0` / `0.0`), so compare on the string form or via a float cast, never on truthiness.
- **`epss_percentile` is a percentile (0–1), not a probability** — `epss` is the probability.
  "Top 10% of exploit probability" is `epss_percentile >= 0.9`; reading the raw `epss` the same
  way would flag almost nothing, since most CVEs sit far below 0.9 there.
- **The `Risk_Level` tier count is not guaranteed.** Test the tier *word* (`high`, `critical`),
  never a bare `"3" in level`: that fires on any label whose text merely contains a 3, and tier 3
  of 5 is the middle of the scale, not the top. For a defensible ranking quote `Risk_STD`, which is
  an explicit 0–100 percentile.
- **Change-history values are arrays.** `/CMDB/v2/data/cmdb/{asset,user}/change?id=<name>` returns
  entries whose `oldValue`/`newValue` are Lists — join them for display. A record that oscillates
  the same field every day (a department flipping between two values, a `Risk_Level` flipping
  between `3-high` and `1-low`) is an **identity-resolution / dedup conflict** between sources, not
  a real change — flag it rather than reporting the movement.
- Records are wide (300+ fields, nested `Details[]` per source). Save raw responses to files and
  extract only what the question needs. They contain personal data — see the privacy notes in the
  README before saving or sharing anything derived from them.

## Fast recipes

See [recipes.md](recipes.md) for the full question→command map. Quick pointers:

- **Top-N by any numeric field** (riskiest users/assets, most KEVs, etc.) — use the helper, which
  auto-finds the threshold and sorts client-side:

  ```bash
  python scripts/meridian.py top --table user  --field Risk_Score --top 5
  python scripts/meridian.py top --table asset --field Count_KEV  --top 10 --select Asset_Name,IP_Address,Count_KEV
  python scripts/meridian.py top --table user  --field Risk_Score --top 5 --where "Owner_Department == String Legal"
  ```

  Output is JSON: `{ matchedAtThreshold, totalInTail, top: [ ... ] }`. Format `top` per §4 of SKILL.md.
- **Profile / investigate one entity** (risk profile + blast radius) — use the helper; it does the
  whole workflow (identity linkage, threats, non-compliance, linked assets + shared users, and a
  change-log stability check) in one call:

  ```bash
  python scripts/meridian.py profile --name AEXAMPLE                    # user (default)
  python scripts/meridian.py profile --name I-0EXAMPLE0000000 --type asset
  ```

- **Count only** — for "how many", read `totalRecords` from a `recordsPerPage:1` response.
