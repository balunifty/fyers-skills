#!/usr/bin/env python3
"""Option chain helpers — token-free, operate on response dicts from fyers_client.option_chain().

No network calls, no token required. Pass the raw API response dict and these
functions extract, filter, and compute what you need.

Functions
---------
    parse_chain(resp)              split response into spot/calls/puts/expiry_data/vix/OI
    atm_strike(legs, spot)         nearest strike to spot from a calls or puts dict
    filter_expiry(expiry_data, flag)  filter expiryData[] by "W" weekly or "M" monthly
    pcr(resp)                      put/call ratio (putOi / callOi)
    straddle_cost(calls, puts, strike)  CE ltp + PE ltp at a given strike
    max_pain(calls, puts)          strike where total option writers' loss is minimised

Usage
-----
    from fyers_client import FyersClient
    from option_chain import parse_chain, atm_strike, filter_expiry, pcr, straddle_cost

    fc = FyersClient()
    resp = fc.option_chain("NSE:NIFTY50-INDEX", strikecount=10, greeks=True)
    chain = parse_chain(resp)

    atm = atm_strike(chain["calls"], chain["spot"])
    print("ATM:", atm, "  PCR:", round(pcr(resp), 2))
    print("Straddle cost:", straddle_cost(chain["calls"], chain["puts"], atm))

CLI
---
    python scripts/option_chain.py demo     # run smoke-test on a mock response
"""
from __future__ import annotations


def parse_chain(resp: dict) -> dict:
    """Split an option_chain response into structured dicts.

    Returns:
        {
          "spot":        float,                   # underlying LTP
          "calls":       {strike_price: leg, ...}, # CE legs keyed by int strike
          "puts":        {strike_price: leg, ...}, # PE legs keyed by int strike
          "expiry_data": [...],                   # list of available expiries
          "call_oi":     int,
          "put_oi":      int,
          "india_vix":   float,
        }

    Each leg contains all optionsChain fields: ltp, bid, ask, oi, oich, oichp,
    prev_oi, volume, fyToken, symbol, and greeks{} when greeks=1 was requested.
    """
    data = resp.get("data", resp)
    calls: dict = {}
    puts: dict = {}
    spot: float = 0.0

    for leg in data.get("optionsChain", []):
        ot = leg.get("option_type", "")
        if ot == "CE":
            calls[leg["strike_price"]] = leg
        elif ot == "PE":
            puts[leg["strike_price"]] = leg
        elif ot == "" and leg.get("ltp"):
            spot = float(leg["ltp"])

    vix = data.get("indiavixData", {})
    return {
        "spot":        spot,
        "calls":       calls,
        "puts":        puts,
        "expiry_data": data.get("expiryData", []),
        "call_oi":     data.get("callOi", 0),
        "put_oi":      data.get("putOi", 0),
        "india_vix":   float(vix.get("ltp", 0)),
    }


def atm_strike(legs: dict, spot: float) -> int:
    """Return the strike price nearest to spot from a calls or puts dict."""
    if not legs:
        raise ValueError("legs dict is empty — fetch option chain first")
    return min(legs.keys(), key=lambda s: abs(s - spot))


def filter_expiry(expiry_data: list, flag: str) -> list:
    """Return expiry entries matching flag: 'W' (weekly) or 'M' (monthly).

    Each entry: {"date": "DD-MM-YYYY", "expiry": "<epoch_str>", "expiry_flag": "W"|"M"}.
    Pass entry["expiry"] as the `timestamp` param to option_chain() to select that expiry.
    """
    flag = flag.upper()
    if flag not in ("W", "M"):
        raise ValueError(f"flag must be 'W' or 'M', got {flag!r}")
    return [e for e in expiry_data if e.get("expiry_flag") == flag]


def pcr(resp: dict) -> float:
    """Put/Call Ratio: putOi / callOi.

    PCR > 1.2 → bearish sentiment · PCR < 0.8 → bullish · 0.8–1.2 → neutral.
    Returns float('inf') when callOi is 0.
    """
    data = resp.get("data", resp)
    call_oi = data.get("callOi", 0)
    put_oi  = data.get("putOi", 0)
    return float("inf") if call_oi == 0 else put_oi / call_oi


def straddle_cost(calls: dict, puts: dict, strike: int) -> float:
    """CE ltp + PE ltp at the given strike. Raises KeyError if strike not in chain."""
    ce = calls.get(strike)
    pe = puts.get(strike)
    if ce is None or pe is None:
        raise KeyError(
            f"strike {strike} not found in chain; "
            f"available: {sorted(set(calls) | set(puts))}"
        )
    return float(ce["ltp"]) + float(pe["ltp"])


def max_pain(calls: dict, puts: dict) -> int:
    """Strike where total option writers' loss (open interest × ITM amount) is minimised.

    Max pain theory: the underlying tends to close near this strike at expiry, as it
    maximises losses for option buyers (and minimises them for sellers/writers).

    Returns the strike with the lowest total writer loss.
    """
    strikes = sorted(set(calls) | set(puts))
    if not strikes:
        raise ValueError("calls and puts dicts are both empty")

    min_loss = float("inf")
    pain_strike = strikes[0]

    for expiry_strike in strikes:
        total_loss = 0.0
        for s in strikes:
            ce = calls.get(s)
            pe = puts.get(s)
            # Call writers lose when expiry_strike > strike
            if ce and expiry_strike > s:
                total_loss += float(ce.get("oi", 0)) * (expiry_strike - s)
            # Put writers lose when expiry_strike < strike
            if pe and expiry_strike < s:
                total_loss += float(pe.get("oi", 0)) * (s - expiry_strike)
        if total_loss < min_loss:
            min_loss = total_loss
            pain_strike = expiry_strike

    return pain_strike


# ---------------------------------------------------------------------------
# CLI demo
# ---------------------------------------------------------------------------

def _demo() -> int:
    import json

    mock = {
        "data": {
            "callOi": 138681800,
            "putOi":  108234555,
            "expiryData": [
                {"date": "24-03-2026", "expiry": "1774346400", "expiry_flag": "W"},
                {"date": "30-03-2026", "expiry": "1774864800", "expiry_flag": "M"},
            ],
            "indiavixData": {"ltp": 22.81},
            "optionsChain": [
                {"option_type": "",    "strike_price": -1,    "ltp": 23114.5},
                {"option_type": "CE", "strike_price": 23050, "ltp": 266.0,  "oi": 726700,  "bid": 265.0, "ask": 267.0, "volume": 15000, "oich": 3055,   "oichp": 0.42,  "prev_oi": 723645,  "fyToken": "A", "symbol": "NSE:NIFTY2632423050CE", "greeks": {"delta":  0.56, "gamma": 0.0007, "theta": -28.52, "vega": 9.51, "iv": 23.74}},
                {"option_type": "PE", "strike_price": 23050, "ltp": 192.8,  "oi": 1453010, "bid": 193.0, "ask": 194.0, "volume": 82000, "oich": 495430, "oichp": 51.74, "prev_oi": 957580,  "fyToken": "B", "symbol": "NSE:NIFTY2632423050PE", "greeks": {"delta": -0.44, "gamma": 0.0007, "theta": -28.48, "vega": 9.51, "iv": 23.70}},
                {"option_type": "CE", "strike_price": 23100, "ltp": 237.35, "oi": 2531555, "bid": 236.0, "ask": 238.0, "volume": 75000, "oich": 930995, "oichp": 58.17, "prev_oi": 1600560, "fyToken": "C", "symbol": "NSE:NIFTY2632423100CE", "greeks": {"delta":  0.52, "gamma": 0.0007, "theta": -28.48, "vega": 9.59, "iv": 23.51}},
                {"option_type": "PE", "strike_price": 23100, "ltp": 214.5,  "oi": 3207750, "bid": 213.0, "ask": 215.0, "volume": 175000,"oich": 1437210,"oichp": 81.17, "prev_oi": 1770540, "fyToken": "D", "symbol": "NSE:NIFTY2632423100PE", "greeks": {"delta": -0.48, "gamma": 0.0007, "theta": -28.48, "vega": 9.59, "iv": 23.51}},
            ],
        }
    }

    c = parse_chain(mock)
    atm = atm_strike(c["calls"], c["spot"])
    print(f"spot:        {c['spot']}")
    print(f"india_vix:   {c['india_vix']}")
    print(f"atm_strike:  {atm}")
    print(f"PCR:         {pcr(mock):.3f}")
    print(f"straddle:    {straddle_cost(c['calls'], c['puts'], atm):.2f}")
    print(f"max_pain:    {max_pain(c['calls'], c['puts'])}")
    print(f"weekly exp:  {filter_expiry(c['expiry_data'], 'W')}")
    print(f"monthly exp: {filter_expiry(c['expiry_data'], 'M')}")

    atm_ce = c["calls"][atm]
    atm_pe = c["puts"][atm]
    if "greeks" in atm_ce:
        print(f"\nATM greeks ({atm}):")
        print(f"  CE  delta={atm_ce['greeks']['delta']}  theta={atm_ce['greeks']['theta']}  iv={atm_ce['greeks']['iv']}%")
        print(f"  PE  delta={atm_pe['greeks']['delta']}  theta={atm_pe['greeks']['theta']}  iv={atm_pe['greeks']['iv']}%")
    return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        raise SystemExit(_demo())
    print(__doc__)
