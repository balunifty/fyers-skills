#!/usr/bin/env python3
"""Trading utility helpers — token-free, pure functions over master record fields.

All functions here take plain scalars or symbol-master record dicts as arguments.
No network calls, no token, no FyersClient — safe to use before login and easy to
unit-test with hardcoded values.

Quick-reference for the master record fields used here:
    minLotSize   int    qty must be a multiple of this
    tickSize     float  limit/stop prices must be rounded to this increment
    expiryDate   str    epoch-seconds (empty string for cash equity)
    optType      str    'CE' / 'PE' for options, 'XX' for futures/equity

Functions
---------
Lot / qty
    validate_lot_qty(qty, lot_size)          raise ValueError if qty not a lot multiple
    round_qty_to_lot(qty, lot_size)          round DOWN to nearest valid lot (returns 0 → raises)

Price / tick
    round_price_to_tick(price, tick_size)    round to nearest valid tick increment
    validate_price_on_tick(price, tick_size) True if price is already on a tick boundary

Expiry
    expiry_date(expiry_epoch)                epoch str → datetime.date
    days_to_expiry(expiry_epoch)             int DTE (negative = already expired)
    is_expired(expiry_epoch)                 True if contract has passed its expiry date

Convenience
    order_checks(symbol, qty, price)         run all of the above from a master record in one call
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, ROUND_HALF_UP


# ---------------------------------------------------------------------------
# Lot / qty helpers
# ---------------------------------------------------------------------------

def validate_lot_qty(qty: int, lot_size: int) -> None:
    """Raise ValueError if qty is not a non-zero multiple of lot_size."""
    if lot_size <= 0:
        raise ValueError(f"lot_size must be positive, got {lot_size}")
    if qty <= 0:
        raise ValueError(f"qty must be positive, got {qty}")
    if qty % lot_size != 0:
        raise ValueError(
            f"qty {qty} is not a multiple of lot size {lot_size}; "
            f"nearest valid lots: {(qty // lot_size) * lot_size} or "
            f"{(qty // lot_size + 1) * lot_size}"
        )


def round_qty_to_lot(qty: int, lot_size: int) -> int:
    """Round qty DOWN to the nearest valid lot multiple.

    Raises ValueError if the result would be 0 (i.e. qty < lot_size).
    """
    if lot_size <= 0:
        raise ValueError(f"lot_size must be positive, got {lot_size}")
    rounded = (qty // lot_size) * lot_size
    if rounded == 0:
        raise ValueError(
            f"qty {qty} is smaller than one lot ({lot_size}); cannot round down"
        )
    return rounded


# ---------------------------------------------------------------------------
# Price / tick helpers
# ---------------------------------------------------------------------------

def round_price_to_tick(price: float, tick_size: float) -> float:
    """Round price to the nearest valid tick increment.

    Uses Decimal arithmetic to avoid floating-point drift (e.g. tick 0.05).

    >>> round_price_to_tick(805.03, 0.05)
    805.05
    >>> round_price_to_tick(100.124, 0.05)
    100.1
    """
    if tick_size <= 0:
        raise ValueError(f"tick_size must be positive, got {tick_size}")
    tick = Decimal(str(tick_size))
    p = Decimal(str(price))
    rounded = (p / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick
    return float(rounded)


def validate_price_on_tick(price: float, tick_size: float) -> bool:
    """Return True if price is already on a valid tick boundary (no rounding needed)."""
    return round_price_to_tick(price, tick_size) == round_price_to_tick(price, tick_size) and \
        abs(price - round_price_to_tick(price, tick_size)) < tick_size * 1e-9


# ---------------------------------------------------------------------------
# Expiry helpers
# ---------------------------------------------------------------------------

def expiry_date(expiry_epoch: str) -> dt.date:
    """Convert a master record's expiryDate (epoch seconds string) to a date.

    Raises ValueError for empty strings (cash equity has no expiry).
    """
    if not expiry_epoch:
        raise ValueError("expiryDate is empty — cash equity has no expiry")
    try:
        return dt.datetime.utcfromtimestamp(int(expiry_epoch)).date()
    except (ValueError, OSError) as e:
        raise ValueError(f"cannot parse expiryDate {expiry_epoch!r}: {e}") from e


def days_to_expiry(expiry_epoch: str, as_of: dt.date | None = None) -> int:
    """Return integer days to expiry (DTE).  Negative means already expired.

    as_of defaults to today (UTC date).
    """
    today = as_of or dt.date.today()
    return (expiry_date(expiry_epoch) - today).days


def is_expired(expiry_epoch: str, as_of: dt.date | None = None) -> bool:
    """Return True if the contract's expiry date is before today (or as_of)."""
    today = as_of or dt.date.today()
    return expiry_date(expiry_epoch) < today


# ---------------------------------------------------------------------------
# Convenience: validate everything from a master record in one call
# ---------------------------------------------------------------------------

def order_checks(symbol: str, qty: int, price: float | None,
                 master_record: dict) -> dict:
    """Run all pre-order checks from a master record and return a summary.

    Raises ValueError on the first hard failure.
    Returns a dict with validated/rounded values and any warnings.

    Parameters
    ----------
    symbol       : the API symbol string (for error messages)
    qty          : intended order quantity
    price        : limit/stop price (None for market orders — skips tick checks)
    master_record: the full record returned by fyers_symbols.validate_symbol()
    """
    lot = master_record.get("minLotSize", 1)
    tick = master_record.get("tickSize", 0.05)
    expiry_epoch = master_record.get("expiryDate", "")
    opt_type = master_record.get("optType", "XX")

    warnings: list[str] = []

    # --- qty ---
    validate_lot_qty(qty, lot)

    # --- price ---
    rounded_price = price
    if price is not None:
        rounded_price = round_price_to_tick(price, tick)
        if rounded_price != price:
            warnings.append(
                f"price {price} rounded to nearest tick ({tick}): {rounded_price}"
            )

    # --- expiry (only for derivatives) ---
    if expiry_epoch and opt_type != "XX" or (expiry_epoch and "FUT" in symbol):
        if is_expired(expiry_epoch):
            raise ValueError(
                f"{symbol} expired on {expiry_date(expiry_epoch)} — "
                f"pick a later expiry"
            )
        dte = days_to_expiry(expiry_epoch)
        if dte == 0:
            warnings.append(f"{symbol} expires TODAY — intraday only")
        elif dte <= 2:
            warnings.append(f"{symbol} expires in {dte} day(s)")

    return {
        "symbol": symbol,
        "qty": qty,
        "price": rounded_price,
        "lot_size": lot,
        "tick_size": tick,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# CLI smoke-test
# ---------------------------------------------------------------------------

def _cli() -> int:
    import json
    import sys

    examples = [
        ("round_price_to_tick", round_price_to_tick(805.03, 0.05)),
        ("round_price_to_tick", round_price_to_tick(100.124, 0.05)),
        ("round_qty_to_lot(31, 30)", round_qty_to_lot(31, 30)),
        ("round_qty_to_lot(75, 75)", round_qty_to_lot(75, 75)),
    ]
    for label, val in examples:
        print(f"{label:40s} -> {val}")

    # expiry checks with a known future epoch (BANKNIFTY Jun 2026 = 1782813600)
    epoch = "1782813600"
    print(f"{'expiry_date':40s} -> {expiry_date(epoch)}")
    print(f"{'days_to_expiry':40s} -> {days_to_expiry(epoch)}")
    print(f"{'is_expired':40s} -> {is_expired(epoch)}")

    # validate_lot_qty error
    try:
        validate_lot_qty(31, 30)
    except ValueError as e:
        print(f"validate_lot_qty(31,30) raised: {e}")

    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
