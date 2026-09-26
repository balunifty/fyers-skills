#!/usr/bin/env python3
"""FYERS v3 OAuth login helper.

Runs the two-step OAuth flow and caches the daily access_token so other scripts
(fyers_client.py, example_strategy.py) can reuse it. Secrets come from the
environment only — nothing is hardcoded or committed.

Environment variables (see .env.example):
    FYERS_APP_ID        e.g. "SPXXXXE7-100"  (a.k.a. client_id)
    FYERS_SECRET_ID     app secret
    FYERS_REDIRECT_URI  registered redirect URL

Usage:
    python scripts/fyers_login.py            # run the OAuth flow, cache token
    python scripts/fyers_login.py --check    # exit 0 if a valid cached token exists
    python scripts/fyers_login.py --print-token   # print cached token to stdout

The token is cached at  ~/.fyers/token.json  (mode 600).

This uses only the standard library (urllib) so it works without the SDK. If the
`fyers-apiv3` package is installed, your generated strategy code can pass the
cached token straight into fyersModel.FyersModel(...).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

API_BASE = "https://api-t1.fyers.in/api/v3"
TOKEN_PATH = Path(os.path.expanduser("~/.fyers/token.json"))

# FYERS' edge rejects requests carrying urllib's default User-Agent
# ("Python-urllib/x.y") with a bare 403 before credentials are even checked.
# Send a browser-like one on every request. (The official fyers-apiv3 SDK
# avoids this because it's built on `requests`, which has a different default.)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _load_dotenv() -> None:
    """Load simple KEY=value entries without replacing existing environment values."""
    env_path = Path(__file__).resolve().parents[3] / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip("\"'")
        if key and not os.environ.get(key):
            os.environ[key] = value


_load_dotenv()


def _env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(
            f"ERROR: {name} is not set. Export FYERS_APP_ID, FYERS_SECRET_ID and "
            f"FYERS_REDIRECT_URI (see .env.example) before logging in."
        )
    return val


def app_id_hash(app_id: str, secret_id: str) -> str:
    """appIdHash = SHA256("<app_id>:<secret_id>") hex digest."""
    return hashlib.sha256(f"{app_id}:{secret_id}".encode()).hexdigest()


def _post(url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="ignore")
        sys.exit(f"ERROR: HTTP {e.code} on POST {url}: {detail}")


def generate_authcode_url(app_id: str, redirect_uri: str, state: str = "fyers_skill") -> str:
    q = urllib.parse.urlencode(
        {
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
        }
    )
    return f"{API_BASE}/generate-authcode?{q}"


def extract_auth_code(pasted: str) -> str:
    """Accept either the raw auth_code or the full redirected URL and pull the code."""
    pasted = pasted.strip()
    if "auth_code=" in pasted:
        parsed = urllib.parse.urlparse(pasted)
        qs = urllib.parse.parse_qs(parsed.query)
        if "auth_code" in qs:
            return qs["auth_code"][0]
    return pasted


def save_token(app_id: str, access_token: str, refresh_token: str | None) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "app_id": app_id,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "created_at": int(time.time()),
    }
    TOKEN_PATH.write_text(json.dumps(data, indent=2))
    os.chmod(TOKEN_PATH, 0o600)


def load_token() -> dict | None:
    if not TOKEN_PATH.exists():
        return None
    try:
        return json.loads(TOKEN_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def token_is_valid(tok: dict | None) -> bool:
    """Best-effort liveness check: call /profile with the cached token.

    FYERS tokens expire end-of-day; there is no documented TTL, so we verify by
    a cheap authenticated request rather than guessing an expiry time.
    """
    if not tok or not tok.get("access_token") or not tok.get("app_id"):
        return False
    req = urllib.request.Request(
        f"{API_BASE}/profile",
        headers={
            "Authorization": f"{tok['app_id']}:{tok['access_token']}",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
        return body.get("s") == "ok"
    except urllib.error.HTTPError:
        return False
    except Exception:
        return False


def refresh_access_token(tok: dict) -> dict | None:
    """Attempt to refresh an expired access_token using the cached refresh_token.

    POST https://api-t1.fyers.in/api/v3/validate-refresh-token
    {
      "grant_type": "refresh_token",
      "appIdHash": "<sha256(app_id:secret_id)>",
      "refresh_token": "<refresh_token>",
      "pin": "<user_pin>"
    }

    Returns the updated token dict on success, or None on failure.
    The refresh token is valid for 15 days; after that, full re-login is required.
    """
    refresh_token = tok.get("refresh_token")
    if not refresh_token:
        return None

    app_id = tok.get("app_id") or os.environ.get("FYERS_APP_ID", "")
    secret_id = os.environ.get("FYERS_SECRET_ID", "")
    pin = os.environ.get("FYERS_PIN", "")

    if not app_id or not secret_id:
        return None

    if not pin:
        print("WARNING: FYERS_PIN not set — cannot refresh token. "
              "Set FYERS_PIN in .env for auto-refresh.", file=sys.stderr)
        return None

    payload = {
        "grant_type": "refresh_token",
        "appIdHash": app_id_hash(app_id, secret_id),
        "refresh_token": refresh_token,
        "pin": pin,
    }

    try:
        resp = _post(f"{API_BASE}/validate-refresh-token", payload)
    except SystemExit:
        return None

    if resp.get("s") != "ok" or "access_token" not in resp:
        return None

    # Update the cached token with the new access_token
    tok["access_token"] = resp["access_token"]
    # Some responses include a new refresh_token
    if "refresh_token" in resp:
        tok["refresh_token"] = resp["refresh_token"]
    tok["refreshed_at"] = int(time.time())

    save_token(tok["app_id"], tok["access_token"], tok.get("refresh_token"))
    print(f"OK: token refreshed via refresh_token (cached to {TOKEN_PATH})")
    return tok


def auto_refresh_token() -> dict | None:
    """Try to get a valid token: load cache -> refresh if expired -> return.

    Returns a valid token dict, or None if full re-login is needed.
    """
    tok = load_token()
    if not tok:
        return None

    # If cached token is still valid, return it
    if token_is_valid(tok):
        return tok

    # Token expired — try refresh
    print("Cached token expired, attempting refresh...")
    refreshed = refresh_access_token(tok)
    if refreshed and token_is_valid(refreshed):
        return refreshed

    print("Refresh failed — full re-login required.", file=sys.stderr)
    return None


def interactive_login() -> dict:
    app_id = _env("FYERS_APP_ID")
    secret_id = _env("FYERS_SECRET_ID")
    redirect_uri = _env("FYERS_REDIRECT_URI")

    url = generate_authcode_url(app_id, redirect_uri)
    print("\n1) Open this URL in your browser and log in to FYERS:\n")
    print("   " + url + "\n")
    if webbrowser.open(url, new=2):
        print("The authorization page was opened in your default browser.")
    else:
        print("Could not open the browser automatically; copy the URL above manually.")
    print("2) After login you'll be redirected to your redirect_uri with ?auth_code=...")
    pasted = input("3) Paste the auth_code (or the full redirected URL) here:\n> ")
    auth_code = extract_auth_code(pasted)

    resp = _post(
        f"{API_BASE}/validate-authcode",
        {
            "grant_type": "authorization_code",
            "appIdHash": app_id_hash(app_id, secret_id),
            "code": auth_code,
        },
    )
    if resp.get("s") != "ok" or "access_token" not in resp:
        sys.exit(f"ERROR: token exchange failed: {resp}")

    save_token(app_id, resp["access_token"], resp.get("refresh_token"))
    print(f"\nOK: access_token cached to {TOKEN_PATH}")
    return resp


def main() -> int:
    ap = argparse.ArgumentParser(description="FYERS v3 OAuth login helper")
    ap.add_argument("--check", action="store_true", help="exit 0 if cached token is valid")
    ap.add_argument("--print-token", action="store_true", help="print cached access_token")
    ap.add_argument("--refresh", action="store_true", help="force token refresh and exit")
    args = ap.parse_args()

    if args.print_token:
        tok = load_token()
        if not tok:
            sys.exit("ERROR: no cached token. Run: python scripts/fyers_login.py")
        print(tok["access_token"])
        return 0

    if args.refresh:
        tok = auto_refresh_token()
        if tok:
            print("OK: token refreshed successfully")
            return 0
        print("FAILED: could not refresh token — full re-login required", file=sys.stderr)
        return 1

    if args.check:
        tok = auto_refresh_token()
        if tok and token_is_valid(tok):
            print("OK: cached token is valid")
            return 0
        print("INVALID: no valid cached token — run: python scripts/fyers_login.py")
        return 1

    interactive_login()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
