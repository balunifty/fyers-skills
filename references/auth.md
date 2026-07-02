# FYERS v3 Authentication (OAuth)

Base host: `https://api-t1.fyers.in`. Auth is a two-step OAuth flow that yields a
daily `access_token`. **The access token expires (end of trading day); regenerate it
each day.** Errors `-8`, `-15`, `-16`, `-17` (or HTTP 401) mean the token is invalid/
expired — re-login, don't retry.

## App credentials (from https://myapi.fyers.in/dashboard/)

- `client_id` (a.k.a. `app_id`) — format `XXXXXXXXX-100` (e.g. `SPXXXXE7-100`).
- `secret_id` (app secret).
- `redirect_uri` — must match what's registered on the app.

Store these in environment variables (see `.env.example`), never in code:
`FYERS_APP_ID`, `FYERS_SECRET_ID`, `FYERS_REDIRECT_URI`.

## appIdHash

```
appIdHash = SHA256( "<app_id>:<secret_id>" )   # hex digest
```
Example: `SHA256("SPXXXXE7-100:secret")` → `7c7120d2b500...`.

## Step 1 — Generate auth code (GET)

```
GET https://api-t1.fyers.in/api/v3/generate-authcode
      ?client_id=<APP_ID>
      &redirect_uri=<REDIRECT_URI>
      &response_type=code
      &state=<random_string>
```
The user opens this URL, logs in, and is redirected to `redirect_uri?auth_code=<CODE>&state=<...>`.
Copy the `auth_code` from the redirected URL.

## Step 2 — Exchange auth code for token (POST)

```
POST https://api-t1.fyers.in/api/v3/validate-authcode
Content-Type: application/json

{
  "grant_type": "authorization_code",
  "appIdHash": "<sha256(app_id:secret_id)>",
  "code": "<auth_code>"
}
```
Response:
```json
{ "s": "ok", "code": 200, "message": "",
  "access_token": "eyJ0eXAi...", "refresh_token": "eyJ0eXAi..." }
```

## Authorization header (every REST call)

```
Authorization: <app_id>:<access_token>
```
e.g. `Authorization: SPXXXXE7-100:eyJ0eXAi...`. The Python SDK builds this from
`client_id` + `token`.

## Refresh token (POST) — limited

```
POST https://api-t1.fyers.in/api/v3/validate-refresh-token
{
  "grant_type": "refresh_token",
  "appIdHash": "<sha256(app_id:secret_id)>",
  "refresh_token": "<refresh_token>",
  "pin": "<user_pin>"
}
```
Returns a new `access_token` only (no new refresh_token). Refresh token validity is
**15 days**. **Caveat:** the docs note the refresh-token flow may be discontinued —
treat daily re-login (Step 1+2) as the reliable path and verify refresh against the
live docs before depending on it.

## Python SDK

```python
from fyers_apiv3 import fyersModel

session = fyersModel.SessionModel(
    client_id=APP_ID, secret_key=SECRET_ID, redirect_uri=REDIRECT_URI,
    response_type="code", grant_type="authorization_code",
)
print(session.generate_authcode())          # open this URL, log in, copy auth_code

session.set_token(AUTH_CODE)
resp = session.generate_token()
access_token = resp["access_token"]

fyers = fyersModel.FyersModel(client_id=APP_ID, token=access_token, is_async=False, log_path="")
print(fyers.get_profile())
```

The skill automates Steps 1–2 and caches the token in `scripts/fyers_login.py`.

## Permission templates (set on the app)

- **Basic**: Profile, Logout.
- **Transactions Info**: Orders, Positions, Trades, Holdings, Funds, Market Status.
- **Order Placement**: place/modify/cancel/exit/convert.
- **Market Data**: Historical, Market Depth, Quotes.

Make sure the app has the permissions the strategy needs, or calls return permission
errors (HTTP 403).

## Pitfall: bare 403 from `urllib`'s default User-Agent

If you build requests with stdlib `urllib` instead of `requests`/the SDK, FYERS' edge
returns a **403 with no useful body** for the default `Python-urllib/x.y` User-Agent —
before your `appIdHash`/credentials are even checked. This is easy to misdiagnose as a
bad `app_id`/`secret_id`/`redirect_uri` or a permissions issue (the 403 above). Always
set a browser-like `User-Agent` header on raw `urllib` requests to `api-t1.fyers.in`
(`scripts/fyers_login.py`'s `USER_AGENT` constant does this); the official `fyers-apiv3`
SDK is unaffected because it's built on `requests`, which sends a different default UA.
Also make sure any custom HTTP wrapper surfaces the response body on `HTTPError` —
swallowing it hides which of these two causes you're looking at.
