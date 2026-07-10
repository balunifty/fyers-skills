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

## Default path: conversational, guided auth (agent-driven)

Per `SKILL.md`'s default conversational-execution mode, the agent drives this whole
flow with the user in chat rather than telling them to go run a script alone:

1. **Check first.** Run `python scripts/fyers_login.py --check` yourself. If it says
   `OK`, you're done — report that and move on.
2. **Scaffold `.env`, don't fill it.** If `.env` doesn't exist in the project folder,
   create it with the same keys as `.env.example` (`FYERS_APP_ID`, `FYERS_SECRET_ID`,
   `FYERS_REDIRECT_URI`, plus `FYERS_PIN` only if the refresh-token flow is needed) but
   **leave every value blank**. Never write a secret value into the file yourself, and
   never ask the user to paste a secret value into chat.
3. **Wait for the user.** Ask them to open `.env` and fill in the values themselves,
   directly in the file, then tell you when they've done it (e.g. "done"). Do not
   proceed, guess values, or re-check on a timer — wait for their explicit confirmation.
4. **Run the login yourself.** Once confirmed, run `python scripts/fyers_login.py`
   (interactive: it prints the auth URL and then blocks on stdin for the `auth_code`).
   Relay the printed URL to the user in chat, have them log in in their browser and copy
   back the `auth_code` (or the full redirected URL — `extract_auth_code()` accepts
   either), and feed it to the waiting prompt to complete the exchange.
5. **Confirm success yourself.** Run `python scripts/fyers_client.py profile` and show
   the real response in chat — don't assert that auth "should" now work.

This is the default. If the user explicitly opts out (e.g. "just give me the code",
"don't run anything"), fall back to the standalone/manual path below and let them run
each step themselves.

## Standalone/manual path (opt-out only)

Everything below documents the underlying flow/fields the scripts above implement, and
is also the reference for users who explicitly want to run this themselves instead of
having the agent drive it.

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
