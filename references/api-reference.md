# Meridian API v2 — Endpoint reference

Base URL: `https://<FQDN>` + endpoint. All endpoints are case-sensitive, require
`Content-Type: application/json` and `Authorization: Bearer <token>` headers, and are limited to
60 queries/minute. Unless marked otherwise, use the **User Generated** token.

Endpoint shapes below follow Lucidum's published API v2 documentation. Response fields are named as
they arrive; confirm against your own stack, since deployments differ.

## Contents

1. [Data & metadata](#data--metadata)
2. [Change management](#change-management)
3. [System metrics](#system-metrics)
4. [Data ingestion](#data-ingestion)
5. [Connectors & profiles](#connectors--profiles)
6. [SmartLabels](#smartlabels)
7. [Response codes](#response-codes)

## Data & metadata

| Endpoint | Method | Notes |
|---|---|---|
| `/CMDB/v2/data/metadata/asset` | GET | All queryable asset fields: `{"metadata":[{fieldName, fieldDescription, dataType, displayName, fieldGroup}]}` |
| `/CMDB/v2/data/metadata/user` | GET | Same for user fields |
| `/CMDB/v2/data/cmdb` | POST | Query raw + enriched data. Body: `table` + `query` + `paging` — see query-syntax.md |
| `/CMDB/v2/data/ldg` | POST | Enriched LDG data only. **Requires Action token.** Tables: `asset`, `user` only |

There is **no field projection and no aggregation endpoint.** `fields`, `selectFields` and `columns`
on `/data/cmdb` are accepted and ignored — every page returns the full record regardless, which is
why a 100-record page is around a megabyte. Field metadata carries only
`fieldName`/`dataType`/`displayName`/`fieldGroup`/`fieldDescription`, so it cannot enumerate a
field's values either. Fewer pages and more concurrency are the only levers available.

## Change management

Per-record field change history (old value, new value, timestamp per change; a field changed N
times yields N entries).

| Endpoint | Method | Params |
|---|---|---|
| `/CMDB/v2/data/cmdb/asset/change` | GET | `?id=<asset name>` |
| `/CMDB/v2/data/cmdb/user/change` | GET | `?id=<user name>` |

Response entries:

```json
{"id": "EXAMPLE-ASSET-1", "type": "Asset", "field": "OS",
 "oldValue": "Windows 7", "newValue": "Windows XP",
 "changeTimeEpoch": 1697161296, "changeTimeUTC": "2023-10-13T01:41:36Z"}
```

## System metrics

| Endpoint | Method | Returns |
|---|---|---|
| `/CMDB/v2/system/metrics/data` | GET | `{assetCount, userCount, date, avg30DaysAssetCount, avg30DaysUserCount}`. Optional `?date=yyyy-mm-dd` |
| `/CMDB/v2/system/metrics/license` | GET | `{licenseType, expireDate, customerName}` |
| `/CMDB/v2/system/metrics/connector` | GET | Per-connector ingestion detail: status, error messages, input/output record counts, field mappings. Optional `?sort=display_name%2Cdesc`. Large response — save to a file |
| `/CMDB/v2/system/metrics/data-ingestion` | GET | `{data_ingestion_runs:[{run_type, id, status, start_date, end_date}]}` |
| `/CMDB/v2/system/metrics/data-ingestion/next` | GET | `{next_run_at, schedule_interval_value (cron), schedule_interval_type}` |
| `/CMDB/v2/system/metrics/data-ingestion/detail/<job id>` | GET | Job id like `scheduled__2023-07-27T01:05:00+00:00`. Returns `{task_list:[{task_id, docker_cmd, docker_image, status, start_date, end_date}]}` |
| `/CMDB/v2/system/metrics/action` | GET | Scheduled Actions (Spring page object): `{actions:[{actionId, actionName, actionType, actionGroup, scheduleType, scheduleCronExpression, lastRunAt, nextRunAt, status, createdBy}], totalPages, ...}` |
| `/CMDB/v2/system/metrics/action-jobs/<action_id>` | GET | Runs of one Action: `[{id, action_run_date, action_status, action_status_msg, action_result_number, action_result_log_id}]` |

On `/system/metrics/connector`, prefer `?size=2000` to fetch every run in one call rather than
paging. Do **not** narrow it with a descending `_time` sort and a small page: the returned window
then spans only a few days, so a source that last ran a month ago drops out of the response
entirely and reports as idle — which claims "hasn't ingested" when the truth is "couldn't tell".

## Data ingestion

| Endpoint | Method | Returns |
|---|---|---|
| `/CMDB/v2/system/data-ingestion/jobs` | GET | Airflow-style run list: `{dag_runs:[{dag_run_id, data_interval_start/end, run_type, state, start_date, end_date, external_trigger, note}], total_entries}` |
| `/CMDB/v2/system/data-ingestion/run` | GET | **⚠ Starts a full ingestion run from all connectors** and returns the queued dag_run. It is a GET, so treat it as state-changing regardless of method — only call with explicit user confirmation |

## Connectors & profiles

`connector_name=api` is a required constant on all of these. Find `bridge_name` via
`GET /CMDB/v2/connector`; find `profile_name` via `GET /CMDB/v2/connector/profile`.
Read-only calls are safe; POST/PUT/DELETE change the stack — confirm with the user first.

> **Treat every `/connector/profile` response as sensitive.** It can carry connector configuration
> well beyond connector health — connection targets, service-account identifiers, proxy settings and
> encrypted secrets. Don't query it directly and don't print it: `meridian.py connectors` reads it
> and extracts a field allow-list in-process, and the test suite asserts nothing credential-shaped
> survives into its output.

**The two connector endpoints answer different questions, and the obvious one is usually not the one
you want.** `/CMDB/v2/connector` is the full *catalog* of connectors the platform supports — several
hundred entries, with `status: available|dev` — and says nothing about your stack. What is actually
configured comes from `/CMDB/v2/connector/profile` (`services_list[].activity` = enabled,
`.status` = last connection test), and what actually ingested comes from
`/CMDB/v2/system/metrics/connector`.

| Endpoint | Method | Purpose |
|---|---|---|
| `/CMDB/v2/connector` | GET | Connector catalog: `{connectors:[{connector_name, bridge_name, display_name, description, group, status}]}` |
| `/CMDB/v2/connector/config?connector_name=api&bridge_name=<b>` | GET | One connector's config schema; `config.field_metadata` marks which fields are `required`/`encrypt` |
| `/CMDB/v2/connector/profile` | GET | All profiles: `{connectorProfiles:[{..., profile_name, services_list, profile_id}]}` |
| `/CMDB/v2/connector/profile?connector_name=api&bridge_name=<b>&profile_name=<p>` | GET | One profile |
| `/CMDB/v2/connector/profile` | POST | **Create profile.** Body: `{connector_name:"api", bridge_name, profile_name, url, <fields required by field_metadata>}`. Test afterwards. File-upload connectors can't be created via API |
| `/CMDB/v2/connector/profile?connector_name=api&bridge_name=<b>&profile_name=<p>` | DELETE | **Delete profile.** Returns `{removed:true}` |
| `/CMDB/v2/connector/test/async` | POST | **Start connector test.** Body `{connector_name, bridge_name, profile_name}` → `{traceId}` |
| `/CMDB/v2/connector/test/result?traceId=<t>` | GET | Test outcome: `{test_result:[{name, status OK/FAIL, message, details[]}], status:"done"}` |
| `/CMDB/v2/connector/profile/service` | PUT | **Enable/disable a service.** Body: `{connector_name:"api", bridge_name, profile_name, services_list:[{service, activity:true\|false}]}` |

Coverage names are only unique per `(connector, profile)`, not per display name — one connector can
carry several profiles pointing at different tenants. Key on the pair when merging these sources.

## SmartLabels

| Endpoint | Method | Purpose |
|---|---|---|
| `/CMDB/v2/smartlabel/search` | POST | Body `{"smartLabels": ["<Name>_SmartLabel", ...]}`. Names: spaces→underscores, append `_SmartLabel`. Empty list returns ALL. Returns `[{id, field_name, friendly_name, field_type, llmDescription, llmBusinessValue}]` |
| `/CMDB/v2/smartlabel?id=<24-hex id>` | GET | Full definition incl. `field_rules[]` (each has a `field_query_template` — a Query Builder query), status, usedByDashboards/Actions, etc. |

SmartLabel `field_name`s are queryable as fields in `/CMDB/v2/data/cmdb` queries. They are the
customer's own vocabulary, so when a user names something in terms that aren't a standard field,
check the SmartLabels before reaching for a generic field.

Resolve a label's table and type from **field metadata**, not from the SmartLabel collection names —
those are stack-specific and don't generalise. Type names also differ between the two surfaces
(`Str` → `String`, `Boolean` → `Binary`).

**Counting a label's members: Binary is `== true`; everything else is `exists`.** Never `== null` —
on a String label that matches where the label is *absent*, so it returns most of the table while
looking like a real answer. Resolve the table from the *label's* metadata, not the caller's
assumption: asking the asset table about a user label counts zero, which also looks like an answer.

## Response codes

| Code | Meaning |
|---|---|
| 200 | Success |
| 201 | Created |
| 400 | Bad request |
| 401 | Unauthorized |
| 403 | Forbidden |
| 404 | Not found |
| 400001 | Invalid numeric operator |
| 400002 | Invalid string operator |
| 400003 | Invalid Boolean operator |
| 400004 | Invalid list operator |
| 400005 | Invalid data operator |
| 400006 | Invalid page number |
| 400007 | Invalid item per page |
| 401001 | Invalid token |
| 401002 | Invalid collection name |
| 401003 | Invalid output field |

Interpretation tips: a `4000xx` code almost always means the `operator` doesn't fit the field's
`type` (or the `type` capitalization is wrong); `401001` means re-check the token; `401002` means a
bad `table` value.

**A 200 is not proof the query asked what you meant.** Several malformed queries return HTTP 200
with `totalRecords: 0` rather than an error — a quoted numeric value, a wrong-case string on a
case-sensitive operator, a misspelled field name. In an inventory tool a silent zero reads as "no
matches", which is why `meridian.py` validates field names and clause shapes locally before
issuing a request. See query-syntax.md.
