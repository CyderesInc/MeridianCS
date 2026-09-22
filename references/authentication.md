# Meridian API v2 — Authentication

Source: <https://lucidum.io/docs/authentication-in-api-v2/> and <https://lucidum.io/docs/api-token-management/>

Every API v2 request must carry two headers:

```text
Content-Type: application/json
Authorization: Bearer <token>
```

Endpoints are case-sensitive. SSO accounts are not supported for API access — the token must be
generated from a local Meridian account that has the **Api_Users** role.

## Token types

| Type | Works with | Where to create |
|---|---|---|
| **User Generated** | Every endpoint EXCEPT `/CMDB/v2/data/ldg` | Settings > User Management > Edit User |
| **Action** | `/CMDB/v2/data/ldg` only | Actions page > action tile > Token icon |
| **Limited** | Admin-selected subset of APIs, with expiration date | Settings > API Token Management (Admin only) |

All three are supplied the same way, as the `Bearer` value in the header.

## Generating a User Generated token (standard — covers almost everything)

1. Go to **Settings > User Management**, find the account, click **Edit**.
2. In the **Roles** field, make sure **Api_Users** is assigned (click it and the right-arrow, Save).
3. In the Edit User page, click **Generate ClientID/Secret**. Copy the Client ID and Secret
   (double-click each field) and store them safely.
4. Click **Generate Token**. Copy the Token value — **Meridian will not display it again.**
5. Click **Confirm**.

## Generating an Action token (only needed for /CMDB/v2/data/ldg)

1. Ensure the account has the **Api_Users** role (same as above).
2. Go to the **Actions** page (left menu > Actions icon).
3. Click the tile of any **enabled** action.
4. Click the **Token** icon (upper left) to open Manage Action Tokens.
5. Click **Add Token** (plus icon), name it.
6. Click **View External API Script** (code icon) on the new token; the Action Token is inside
   the script. Copy and store it safely.

## Managing tokens

**Settings > API Token Management** lists all tokens with Tag, Type, Created By/On, Last
Modified By/On, and Expires On. Tokens are displayed once at creation only. Admins can create
**Limited** tokens (tag + expire date + allow-list of APIs) and can edit only Limited tokens;
users can delete only tokens they created.

## Handling the token

The token is a bearer credential with read access to the whole inventory, which includes personal
data about the people and devices in it. Treat it accordingly:

- Prefer the `MERIDIAN_API_TOKEN` environment variable over a file on disk for production tokens.
- If you do save it, it lands in `~/.meridian/config.json` in plaintext, alongside every other
  saved stack's token — so one leak exposes all of them. The README's "Recommended hardening"
  section has a `permissions.deny` rule that blocks agent file access to that directory without
  costing any functionality, since the scripts read it in-process.
- Keep TLS verification on. The token travels over that connection.

## Header examples

Python:

```python
headers = {
    'Content-Type': 'application/json',
    'Authorization': 'Bearer <token>'
}
```

cURL:

```text
--header 'Content-Type: application/json' \
--header 'Authorization: Bearer <token>'
```

## Troubleshooting

- `401` / `401001 Invalid token` — token wrong, expired, or the account lost the Api_Users role.
- `403` on `/CMDB/v2/data/ldg` with a User Generated token — expected; that endpoint needs an
  Action token.
- Rate limit: 60 queries per minute across the API.
