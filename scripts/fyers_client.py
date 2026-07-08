#!/usr/bin/env python3
"""Reusable FYERS v3 REST client (stdlib only).

Loads the cached daily token (from fyers_login.py), adds the
`Authorization: <app_id>:<access_token>` header, handles 429 backoff, and exposes
the common read endpoints plus a SAFE order-placement method that defaults to
dry-run.

This intentionally has no third-party deps so it runs anywhere. For WebSocket
streaming or the official ergonomics, use the `fyers-apiv3` SDK (see references).

Quick start:
    from scripts.fyers_client import FyersClient
    fc = FyersClient()                 # uses ~/.fyers/token.json
    print(fc.profile())
    print(fc.quotes(["NSE:SBIN-EQ"]))
    candles = fc.history("NSE:SBIN-EQ", "5", "2024-01-01", "2024-01-31")

CLI smoke test (read-only):
    python scripts/fyers_client.py profile
    python scripts/fyers_client.py quotes NSE:SBIN-EQ NSE:INFY-EQ
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from .fyers_login import API_BASE, USER_AGENT, load_token  # when imported as a package
except ImportError:  # when run as a script
    from fyers_login import API_BASE, USER_AGENT, load_token

DATA_BASE = "https://api-t1.fyers.in/data"


class FyersAuthError(RuntimeError):
    """Raised when the token is missing/expired — caller should re-login."""


class FyersClient:
    def __init__(self, token: dict | None = None, max_retries: int = 3):
        tok = token or load_token()
        if not tok or not tok.get("access_token"):
            raise FyersAuthError(
                "No cached token. Run: python scripts/fyers_login.py"
            )
        self.app_id = tok["app_id"]
        self.access_token = tok["access_token"]
        self.max_retries = max_retries

    # --- core request with auth + 429 backoff ---------------------------------
    def _request(self, method: str, url: str, payload: dict | None = None) -> dict:
        headers = {
            "Authorization": f"{self.app_id}:{self.access_token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        data = json.dumps(payload).encode() if payload is not None else None
        attempt = 0
        while True:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise FyersAuthError(
                        "401 Unauthorized — token expired. Re-run fyers_login.py"
                    ) from e
                if e.code == 429 and attempt < self.max_retries:
                    retry_ms = e.headers.get("X-Retry-After-Ms")
                    retry_s = e.headers.get("Retry-After")
                    delay = (
                        float(retry_ms) / 1000.0 if retry_ms
                        else float(retry_s) if retry_s
                        else 2 ** attempt
                    )
                    time.sleep(delay)
                    attempt += 1
                    continue
                if 500 <= e.code < 600 and attempt < self.max_retries:
                    time.sleep(2 ** attempt)
                    attempt += 1
                    continue
                body = e.read().decode(errors="ignore")
                raise RuntimeError(f"HTTP {e.code} on {method} {url}: {body}") from e

    def _get(self, url: str) -> dict:
        return self._request("GET", url)

    # --- account --------------------------------------------------------------
    def profile(self) -> dict:
        return self._get(f"{API_BASE}/profile")

    def funds(self) -> dict:
        return self._get(f"{API_BASE}/funds")

    def holdings(self) -> dict:
        return self._get(f"{API_BASE}/holdings")

    def positions(self) -> dict:
        return self._get(f"{API_BASE}/positions")

    def orderbook(self) -> dict:
        return self._get(f"{API_BASE}/orders")

    def tradebook(self) -> dict:
        return self._get(f"{API_BASE}/tradebook")

    # --- market data ----------------------------------------------------------
    def quotes(self, symbols: list[str]) -> dict:
        if len(symbols) > 50:
            raise ValueError("quotes: max 50 symbols per request")
        q = urllib.parse.urlencode({"symbols": ",".join(symbols)})
        return self._get(f"{DATA_BASE}/quotes?{q}")

    def depth(self, symbol: str, ohlcv: bool = True) -> dict:
        q = urllib.parse.urlencode({"symbol": symbol, "ohlcv_flag": 1 if ohlcv else 0})
        return self._get(f"{DATA_BASE}/depth?{q}")

    def history(self, symbol: str, resolution: str, range_from: str, range_to: str,
                date_format: int = 1, cont_flag: int = 1, oi_flag: int = 0) -> dict:
        q = urllib.parse.urlencode({
            "symbol": symbol, "resolution": resolution, "date_format": date_format,
            "range_from": range_from, "range_to": range_to,
            "cont_flag": cont_flag, "oi_flag": oi_flag,
        })
        return self._get(f"{DATA_BASE}/history?{q}")

    def option_chain(self, symbol: str, strikecount: int = 5, greeks: bool = False) -> dict:
        params = {"symbol": symbol, "strikecount": strikecount}
        if greeks:
            params["greeks"] = 1
        q = urllib.parse.urlencode(params)
        return self._get(f"{DATA_BASE}/options-chain-v3?{q}")

    def market_status(self) -> dict:
        return self._get(f"{DATA_BASE}/marketStatus")

    # --- orders (SAFE: dry-run by default) ------------------------------------
    def place_order(self, order: dict, dry_run: bool = True,
                    validate_symbol: bool = True) -> dict:
        """Place a regular order. dry_run=True (default) only logs the payload.

        Set dry_run=False to actually transmit — do this only with explicit user
        consent. See references/orders.md for required fields and enum codes.

        validate_symbol=True (default) confirms the symbol exists in the daily
        symbol master BEFORE the order is built/sent, catching typos and stale
        expiries that would otherwise fail live with code -300. This runs for
        dry-run too, so a dry-run faithfully proves the symbol is real.
        """
        required = {"symbol", "qty", "type", "side", "productType"}
        missing = required - order.keys()
        if missing:
            raise ValueError(f"order missing required fields: {sorted(missing)}")

        if validate_symbol:
            try:
                from .fyers_symbols import validate_symbol as _vsym
                from .helper import validate_lot_qty, is_expired, expiry_date
            except ImportError:
                from fyers_symbols import validate_symbol as _vsym
                from helper import validate_lot_qty, is_expired, expiry_date
            rec = _vsym(order["symbol"])  # raises ValueError if not a real symbol
            lot = rec.get("minLotSize", 1)
            validate_lot_qty(order["qty"], lot)
            exp = rec.get("expiryDate", "")
            if exp and is_expired(exp):
                raise ValueError(
                    f"{order['symbol']} expired on {expiry_date(exp)} — "
                    f"pick a later expiry"
                )
        order.setdefault("limitPrice", 0)
        order.setdefault("stopPrice", 0)
        order.setdefault("disclosedQty", 0)
        order.setdefault("validity", "DAY")
        order.setdefault("offlineOrder", False)

        if dry_run:
            print("[DRY-RUN] would POST /orders/sync with:")
            print(json.dumps(order, indent=2))
            result = {"s": "dry_run", "code": 0, "message": "dry-run; nothing sent",
                      "order": order}
            try:
                from .trade_logger import log_order  # lazy import — no hard dep
            except ImportError:
                from trade_logger import log_order
            log_order(order, result, dry_run=True)
            return result

        try:
            result = self._request("POST", f"{API_BASE}/orders/sync", order)
        except Exception as exc:
            try:
                from .trade_logger import log_order
            except ImportError:
                from trade_logger import log_order
            log_order(order, str(exc), dry_run=False)
            raise
        try:
            from .trade_logger import log_order
        except ImportError:
            from trade_logger import log_order
        log_order(order, result, dry_run=False)
        return result

    def cancel_order(self, order_id: str, dry_run: bool = True) -> dict:
        if dry_run:
            print(f"[DRY-RUN] would DELETE /orders/sync id={order_id}")
            return {"s": "dry_run", "code": 0, "message": "dry-run; nothing sent"}
        return self._request("DELETE", f"{API_BASE}/orders/sync", {"id": order_id})


def _cli() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 0
    fc = FyersClient()
    cmd = sys.argv[1]
    if cmd == "profile":
        print(json.dumps(fc.profile(), indent=2))
    elif cmd == "funds":
        print(json.dumps(fc.funds(), indent=2))
    elif cmd == "positions":
        print(json.dumps(fc.positions(), indent=2))
    elif cmd == "quotes":
        print(json.dumps(fc.quotes(sys.argv[2:] or ["NSE:SBIN-EQ"]), indent=2))
    elif cmd == "status":
        print(json.dumps(fc.market_status(), indent=2))
    else:
        print(f"unknown command: {cmd}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
