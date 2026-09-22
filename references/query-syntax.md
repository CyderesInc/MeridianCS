# Meridian API v2 — Query syntax for /CMDB/v2/data/cmdb and /CMDB/v2/data/ldg

Sources: <https://lucidum.io/docs/endpoints-for-assets-and-users/> ,
<https://lucidum.io/docs/endpoints-for-ldg-data-only/> ,
<https://lucidum.io/docs/operators-and-data-types/> , <https://lucidum.io/docs/pagination/> ,
<https://lucidum.io/docs/examples-for-lucidum-api-v2/>

> **Record counts in this document are illustrative round numbers**, chosen to show the shape of a
> result and the relationship between two figures. They are not measurements of any particular
> deployment, and no stack should be expected to reproduce them. Where a specific value *is* the
> point — a `0`, or two queries returning the identical count — that is called out as such.

## Contents

1. [The two query endpoints](#the-two-query-endpoints)
2. [Field metadata (always check first)](#field-metadata)
3. [Request body anatomy](#request-body-anatomy)
4. [AND / OR nesting](#and--or-nesting)
5. [Operators by data type](#operators-by-data-type)
6. [Datetime value syntax](#datetime-value-syntax)
7. [Pagination and response shape](#pagination-and-response-shape)
8. [Worked examples](#worked-examples)
9. [Common field names](#common-field-names)

## The two query endpoints

| | `/CMDB/v2/data/cmdb` | `/CMDB/v2/data/ldg` |
|---|---|---|
| Method | POST | POST |
| Data | All fields, raw + enriched | Enriched Lucidum Data Group only (focused records) |
| Token | User Generated | **Action token** |
| Tables | `asset`, `asset_history`, `asset_ip`, `user`, `user_history`, `user_ip` | `asset`, `user` |

Special tables: `asset_ip` has only `Asset_Name` + `IP_Address` (ML asset↔IP mapping);
`user_ip` has only `Owner_Name` + `IP_Address`. `*_history` tables hold prior time periods.

Default to `/CMDB/v2/data/cmdb` — it answers everything and uses the standard token.

## Field metadata

`GET /CMDB/v2/data/metadata/asset` and `GET /CMDB/v2/data/metadata/user` return every queryable
field:

```json
{"metadata": [
  {"fieldName": "File_Bucket", "fieldDescription": "File bucket name",
   "dataType": "List", "displayName": "Cloud Bucket", "fieldGroup": "Data"}
]}
```

- `fieldName` — the exact, case-sensitive string to put in `searchFieldName`
  (`Asset_Type`, not `asset_type`; `sourcetype`, not `Sourcetype`).
- `dataType` — decides which operators are legal (see table below).
- `displayName`/`fieldGroup` — how the field appears in the UI; useful for matching what the
  user said to the real field. SmartLabels appear with fieldGroup "Smart Labels" and Tags in a
  Tags group — both are queryable like any other field.

The response is thousands of lines: save it to a file and search it; don't print it.

**Check the metadata rather than assuming a type.** A field assumed to be a `String` when it is
actually a `List` gets the wrong operator, and the wrong operator on this API is far more likely to
return a confident zero than an error. `meridian.py` caches the metadata per stack and validates
every field name before issuing a request, precisely because a typo previously came back as
`totalRecords: 0` — indistinguishable from a real zero.

## Request body anatomy

```json
{
  "table": "asset",
  "query": [
    [
      {
        "searchFieldName": "Asset_Type",
        "operator": "==",
        "type": "String",
        "value": "Servers"
      }
    ]
  ],
  "paging": { "page": 0, "recordsPerPage": 100 }
}
```

Each criterion object has exactly four keys:

- `searchFieldName` — exact field name from metadata.
- `operator` — from the operators table below.
- `type` — one of `Binary`, `Datetime`, `Float`, `Integer`, `List`, `String` —
  **initial capital**, and it must match the field's `dataType` from metadata.
- `value` — the comparison value. Its JSON type must match the field's data type:
  - **`Float` / `Integer` → send an UNQUOTED JSON number**: `"value": 50`, NOT `"value": "50"`.
    This is the single most common mistake — a quoted numeric value silently returns **zero
    records** (HTTP 200, `totalRecords: 0`) instead of erroring, so it looks like "no matches"
    when the query is actually malformed. The unquoted form returns the real matches; the quoted
    form returns exactly `0`, every time.
  - `String` / `List` / `Binary` → quoted string (Binary examples also accept `1`/`0` unquoted).
  - `exists` / `empty` → value is ignored; use `null`.
  - `in` / `not in` → a JSON array (e.g. `["macOS"]`) or a comma-separated string.

## AND / OR nesting

`query` is an array of arrays of criterion objects:

- **Inner arrays are ANDed together** (every inner array must match).
- **Objects inside one inner array are ORed** (any one of them matches).

```text
"query": [ [ {A} ], [ {B} ] ]      →  A AND B
"query": [ [ {A}, {B} ] ]          →  A OR B
"query": [ [ {A}, {B} ], [ {C} ] ] →  (A OR B) AND C
```

Any number of AND groups and OR alternatives is allowed. There is no NOT wrapper — use the
negative operators (`!=`, `not match`, `not in`).

**Over- or under-nesting returns 0 records with no error**, so an unexpected empty result is more
often a nesting mistake than a genuine absence. Dump the generated JSON body before concluding
anything about the data.

## Operators by data type

**Binary** (fields hold 0/1; official examples pass `"value": "True"` / `"Yes"` / `1`):

| operator | meaning |
|---|---|
| `==` / `!=` | is / is not |
| `exists` / `empty` | field has a value / has none |

**Datetime**:

| operator | meaning |
|---|---|
| `==`, `>=`, `>`, `<=`, `<` | compare to a specific date-time |
| `within past` / `not within past` | ⛔ **do not use** — see below |
| `within` / `not within` | ⛔ **do not use** — see below |
| `within future` / `not within future` | ⛔ **do not use** — see below |
| `exists` / `empty` | |

> ⛔ **The windowed Datetime operators do not filter by the window, and one half of them fails
> silently.** Tested against a live deployment:
>
> | spelling | result |
> |---|---|
> | `within past` / `not within past` | HTTP 400 `Invalid operator` |
> | `within` / `not within` / `within future` / `not within future` | HTTP 200, **window ignored** |
>
> The failure that matters is the second row. `Expired_Datetime within future Datetime <N>, days`
> returns **the same count for every N** — 1 day, 30 days, 90 days and 3650 days all come back
> identical, because the result is simply every record where the field exists. Illustratively: if
> around 50 certificates carry an expiry date at all, all four windows answer ~50, while the true
> 90-day figure via an absolute range is a shade under that. So it yields a plausible number,
> slightly wrong, for a question it never asked — and "50 certificates expire in the next 24 hours"
> is exactly the kind of confident wrong answer to avoid.
>
> The negations are worse, because their wrong answer is the *reassuring* one.
> `Last_Discovered_Datetime not within Datetime 30, days` returns **`0`** — that zero is the
> measured behaviour, not an illustration — which reads as "nothing is stale". The absolute form,
> `< Datetime -30d`, returns a substantial non-zero count on the same data. Nobody investigates a
> zero.
>
> **Don't "restore" these operators.** The long form is the one that errors; the short form is the
> one that lies, and the short form is the tempting fallback. `meridian.py` refuses all six
> spellings in `--where` rather than passing them through, and the refusal names the working
> alternative.

**Float / Integer**: `==`, `!=`, `>=`, `>`, `<=`, `<`, `exists`, `empty`.

**List** (comma-separated multi-value fields like `IP_Address`, `sourcetype`):

| operator | meaning |
|---|---|
| `match` / `not match` | substring or regex, case-INsensitive; multiple terms comma-separated |
| `in` / `not in` | exact match, case-SENSITIVE; value may be an array |
| `length gt` / `length lt` / `length eq` | count of list entries vs a number |
| `exists` / `empty` | |

**String**: `match` / `not match` (substring/regex, case-insensitive), `==` / `!=`
(exact, case-sensitive), `exists` / `empty`.

Rules of thumb: when the user's phrasing is fuzzy ("windows boxes"), use `match`; reserve `==`
for values you've verified exactly. `==`, `!=`, `in`, `not in` are case-sensitive — a wrong-case
value silently returns 0 records.

`not match` and `not in` are the only negation this DSL has, since there is no NOT wrapper. A
clause parser that splits on a fixed number of tokens will silently lose both, because they are the
only two-word operators — match operators longest-first against the full operator table instead.

## Datetime value syntax

Use **absolute comparisons** — they're the only Datetime filter this API honours (the windowed
operators are refused, above). Always include an explicit time component: a bare `2026-08-17` and
`2026-08-17 00:00:00` do **not** select the same records, so a date-only value lands at an
unspecified point inside the day.

```json
[[{"searchFieldName": "Expired_Datetime", "operator": ">=",
   "type": "Datetime", "value": "2026-08-17 00:00:00"},
  {"searchFieldName": "Expired_Datetime", "operator": "<=",
   "type": "Datetime", "value": "2026-11-15 23:59:59"}]]
```

### Relative values in `--where` (resolved client-side)

`meridian.py` expands a relative value in a Datetime slot to an absolute timestamp before the request
goes out, so a time-window question needs no date arithmetic by hand. This is the only working route
to a time window on this API.

| value | means |
|---|---|
| `+90d` / `-30d` | 90 days ahead / 30 days back |
| `+2w` / `-1w` | weeks |
| `today` | the current day |

Lower bounds (`>=`, `>`) open at **00:00:00** and upper bounds (`<=`, `<`) close at **23:59:59**, so a
pair of clauses describes an inclusive range in the terms the question was asked. Relative values are
rejected on `==` / `!=`, where a whole-day window has no meaning.

```bash
# certificates expiring in the next 90 days — matches a hand-built absolute range exactly
meridian.py list --table asset \
  --where "Expired_Datetime >= Datetime today" --where "Expired_Datetime <= Datetime +90d"
```

The `days` / `weeks` / `month` units appear only in the windowed-operator examples, which don't
filter. In an absolute value, send an ordinary timestamp.

## Pagination and response shape

Always include:

```json
"paging": { "page": 0, "recordsPerPage": 100 }
```

`page` is 0-indexed. `recordsPerPage` defaults to 20, max 100 (>100 clamps to 100, ≤0 resets
to 20). Query responses look like this (illustrative counts):

```json
{
  "totalRecords": 500,
  "data": [ { ...full record, 300+ fields... } ],
  "totalPages": 25,
  "recordsPerPage": 20,
  "page": 0
}
```

Loop pattern: `page = 0`; POST; append `data`; continue while `page < totalPages`, incrementing
and pacing against the 60/min limit. For "how many" questions, `totalRecords` from page 0 is the
whole answer — don't paginate.

A 100-record page is roughly a megabyte and takes a couple of seconds, since there is no field
projection (see api-reference.md). Serial paging dominates the runtime of anything multi-page, so
`meridian.py` fetches independent pages concurrently. The 60/min budget still applies — fanning out
spends it faster, it doesn't raise it.

### There is no server-side sort — finding the max/top-N

The query API has no `sort`/`order` parameter, so you can't ask it for "the highest X" directly.
To find the riskiest user, the most-vulnerable asset, the oldest cert, etc., use a
**threshold-and-narrow** approach instead of pulling all records:

1. Don't assume a field's scale — many are unbounded. `Risk_Score`, for example, is NOT 0–10 and
   not 0–100; real values run from single digits into the thousands. Sample the range first with an
   `exists` query on page 0 and inspect the values.
2. Filter with a `>=` threshold and pick one high enough that the result set is small
   (tens of records, 1–2 pages), then sort those client-side to get the exact top item.
3. If the threshold returns 0, it may be too high OR the value may be wrongly quoted — for
   numeric fields double-check `value` is an unquoted number before lowering the threshold.

This finds the true maximum while fetching only the tail rather than the whole table. `meridian.py
top` implements it, including remembering a workable threshold between runs, so prefer it over
hand-rolled threshold queries.

## Worked examples

**Simple equality** — assets with restricted data:

```json
{"table":"asset","query":[[{"searchFieldName":"Data_Classification","operator":"==","type":"String","value":"Restricted"}]],"paging":{"page":0,"recordsPerPage":100}}
```

**AND** — cloud devices running Windows Server 2019:

```json
{"table":"asset","query":[
  [{"searchFieldName":"Is_Cloud_Device","operator":"==","type":"Binary","value":"Yes"}],
  [{"searchFieldName":"OS","operator":"==","type":"String","value":"Windows Server 2019"}]
],"paging":{"page":0,"recordsPerPage":100}}
```

**OR** — assets seen by the GCP inventory or AWS EC2 connector:

```json
{"table":"asset","query":[[
  {"searchFieldName":"sourcetype","operator":"match","type":"List","value":"gcp_inventory"},
  {"searchFieldName":"sourcetype","operator":"match","type":"List","value":"aws_ec2"}
]],"paging":{"page":0,"recordsPerPage":100}}
```

**(A OR B) AND C** — Windows Server 2019/2020 not covered by an EDR agent:

```json
{"table":"asset","query":[
  [{"searchFieldName":"OS","operator":"==","type":"String","value":"Windows Server 2020"},
   {"searchFieldName":"OS","operator":"==","type":"String","value":"Windows Server 2019"}],
  [{"searchFieldName":"sourcetype","operator":"not match","type":"List","value":"sentinel"}]
],"paging":{"page":0,"recordsPerPage":100}}
```

> Before building a coverage percentage on a pattern like the one above, read the coverage section of
> field-map.md. Subtracting two asset counts to get a "gap" overstates it badly, because the platform
> creates one asset node per data source.

**Endpoint-protection gaps** — agent missing OR out of date:

```json
{"table":"asset","query":[[
  {"searchFieldName":"EP_Not_Updated","operator":"==","type":"Binary","value":"True"},
  {"searchFieldName":"EP_Not_Installed","operator":"==","type":"Binary","value":"True"}
]],"paging":{"page":0,"recordsPerPage":100}}
```

**Numeric threshold** — highest-risk users (note the UNQUOTED number in `value`):

```json
{"table":"user","query":[[{"searchFieldName":"Risk_Score","operator":">=","type":"Float","value":800}]],"paging":{"page":0,"recordsPerPage":100}}
```

Then sort the returned records by `Risk_Score` descending client-side and take the top one — the
API won't sort for you. Quoting the `800` returns `0` records rather than an error.

**Datetime window** — certificates expiring in the next 90 days. The windowed operators don't
filter, so use an absolute range:

```json
{"table":"asset","query":[
  [{"searchFieldName":"Lucidum_Asset_Type","operator":"match","type":"String","value":"Certificate"}],
  [{"searchFieldName":"Expired_Datetime","operator":">=","type":"Datetime","value":"2026-08-17 00:00:00"}],
  [{"searchFieldName":"Expired_Datetime","operator":"<=","type":"Datetime","value":"2026-11-15 23:59:59"}]
],"paging":{"page":0,"recordsPerPage":100}}
```

**`in` with array value** — macOS assets:

```json
{"table":"asset","query":[[{"searchFieldName":"Lucidum_OS_Type","operator":"in","type":"List","value":["macOS"]}]],"paging":{"page":0,"recordsPerPage":100}}
```

**asset_ip lookup** — which asset owns an IP:

```json
{"table":"asset_ip","query":[[{"searchFieldName":"IP_Address","operator":"match","type":"List","value":"10.0.0.42"}]],"paging":{"page":0,"recordsPerPage":20}}
```

**exists** — assets with any public-facing open port:

```json
{"table":"asset","query":[[{"searchFieldName":"EXT_Open_Ports","operator":"exists","type":"List","value":null}]],"paging":{"page":0,"recordsPerPage":100}}
```

## Common field names

Verified in official docs (always confirm in metadata — deployments add custom fields, Tags, and
SmartLabels): `Asset_Name`, `Asset_Type`, `Lucidum_Asset_Type`, `Lucidum_OS_Type`, `OS`,
`Is_Cloud_Device`, `Is_Public`, `Is_Encrypted`, `Data_Classification`, `sourcetype` (lowercase),
`IP_Address`, `MAC_Address`, `FQDN`, `Host_Name`, `Open_Port_List`, `EXT_Open_Ports`, `Services`,
`First_Discovered_Datetime`, `Last_Discovered_Datetime`, `Expired_Datetime`, `Risk_Score`, `High_CVE`,
`EP_Not_Updated`, `EP_Not_Installed`, `Location_Country_ISO_Code`, `Owner_Name`, `Owner_Manager`,
`Owner_Email`, `Data_Type`, `Source_User_Name`. SmartLabel fields end in `_SmartLabel`.

**A field in the official docs is not necessarily a field on your stack.** `First_Time_Seen` is
documented but does not resolve on every deployment, where `First_Discovered_Datetime` is the one
that does. Treat that as the reminder the caveat above is making: this list is doc-derived, and only
`refresh-fields` knows what a given stack actually has.
