#!/usr/bin/env python3
"""Fetch Global Market Data - Crude Oil, Dollar Index, VIX.

Fetches real-time data for global indicators that affect Indian markets.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
from dataclasses import dataclass
from zoneinfo import ZoneInfo

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
sys.path.insert(0, str(REPO_ROOT / "skills" / "fyers-trading" / "scripts"))

from fyers_client import FyersClient  # noqa: E402

# =============================================================================
# Configuration
# =============================================================================
MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")

# FYERS symbols for global indicators
GLOBAL_SYMBOLS = {
    # Crude Oil (MCX)
    "crude_oil": "MCX:CRUDEOIL",
    "crude_oil_mini": "MCX:CRUDEOILM",

    # USDINR (Dollar-Rupee)
    "usdinr": "NSE:USDINR",

    # India VIX
    "india_vix": "NSE:INDIA_VIX-INDEX",

    # Global indices
    "sp500": "US:SPX",
    "nasdaq": "US:IXIC",
    "dow": "US:DJI",

    # Asian markets
    "nikkei": "JP:NI225",
    "hang_seng": "HK:HSI",
    "shanghai": "CN:SHCOMP",
}

# Alternative symbols if primary not available
ALT_SYMBOLS = {
    "crude_oil": ["MCX:CRUDEOIL", "NSE:CRUDEOIL"],
    "dxy": ["NSE:DBSSINDB-EQ", "NSE:ICICIPRULI-EQ"],
}


@dataclass
class GlobalIndicator:
    name: str
    symbol: str
    price: float
    change_pct: float
    timestamp: str
    status: str  # "BULLISH", "BEARISH", "NEUTRAL"


class GlobalMarketFetcher:
    """Fetch global market indicators."""

    def __init__(self):
        self.client = FyersClient()
        self.cache = {}

    def fetch_all(self) -> dict:
        """Fetch all global indicators."""
        results = {}

        for name, symbol in GLOBAL_SYMBOLS.items():
            try:
                data = self._fetch_symbol(symbol)
                if data:
                    results[name] = data
                    self.cache[name] = data
            except Exception as e:
                print(f"Error fetching {name}: {e}", file=sys.stderr)
                # Use cache if available
                if name in self.cache:
                    results[name] = self.cache[name]

        return results

    def _fetch_symbol(self, symbol: str) -> dict | None:
        """Fetch data for a single symbol."""
        try:
            response = self.client.quotes([symbol])
            if response.get("s") != "ok":
                return None

            for item in response.get("d", []):
                v = item.get("v", {})
                ltp = v.get("lp", 0)
                prev_close = v.get("prev_close", 0)
                change_pct = ((ltp - prev_close) / prev_close * 100) if prev_close else 0

                return {
                    "symbol": symbol,
                    "ltp": ltp,
                    "prev_close": prev_close,
                    "change_pct": round(change_pct, 2),
                    "open": v.get("open", 0),
                    "high": v.get("high", 0),
                    "low": v.get("low", 0),
                    "volume": v.get("vol_traded_today", 0),
                    "timestamp": dt.datetime.now(MARKET_TIMEZONE).isoformat(),
                }
        except Exception:
            return None

    def get_crude_oil(self) -> dict | None:
        """Get crude oil data."""
        return self.cache.get("crude_oil") or self._fetch_symbol("MCX:CRUDEOIL")

    def get_usdinr(self) -> dict | None:
        """Get USDINR (Dollar-Rupee) data."""
        return self.cache.get("usdinr") or self._fetch_symbol("NSE:USDINR")

    def get_india_vix(self) -> dict | None:
        """Get India VIX."""
        return self.cache.get("india_vix")

    def get_market_sentiment(self) -> dict:
        """Analyze overall market sentiment from global indicators."""
        crude = self.get_crude_oil()
        usdinr = self.get_usdinr()
        vix = self.get_india_vix()

        sentiment = {
            "overall": "NEUTRAL",
            "factors": [],
            "risk_level": "MEDIUM",
        }

        # Crude oil analysis
        if crude:
            change = crude.get("change_pct", 0)
            if change > 2:
                sentiment["factors"].append("CRUDE_NEGATIVE")
                sentiment["risk_level"] = "HIGH"
            elif change < -2:
                sentiment["factors"].append("CRUDE_POSITIVE")
            elif change > 1:
                sentiment["factors"].append("CRUDE_SLIGHTLY_NEGATIVE")

        # USDINR analysis
        if usdinr:
            change = usdinr.get("change_pct", 0)
            if change > 0.5:  # Rupee weakening (USDINR going up)
                sentiment["factors"].append("INR_WEAKENING")
                sentiment["risk_level"] = "HIGH"
            elif change < -0.5:  # Rupee strengthening
                sentiment["factors"].append("INR_STRENGTHENING")

        # VIX analysis
        if vix:
            vix_val = vix.get("ltp", 15)
            if vix_val > 20:
                sentiment["factors"].append("VIX_HIGH")
                sentiment["risk_level"] = "HIGH"
            elif vix_val > 15:
                sentiment["factors"].append("VIX_ELEVATED")

        # Overall sentiment
        negative_count = sum(1 for f in sentiment["factors"] if "NEGATIVE" in f or "WEAKENING" in f)
        positive_count = sum(1 for f in sentiment["factors"] if "POSITIVE" in f or "STRENGTHENING" in f)

        if negative_count > positive_count:
            sentiment["overall"] = "BEARISH"
        elif positive_count > negative_count:
            sentiment["overall"] = "BULLISH"
        else:
            sentiment["overall"] = "NEUTRAL"

        return sentiment

    def format_report(self) -> str:
        """Format a report of all global indicators."""
        data = self.fetch_all()
        sentiment = self.get_market_sentiment()

        report = f"""
📊 GLOBAL MARKET INDICATORS
{'='*50}
Time: {dt.datetime.now(MARKET_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S IST')}

🛢️ CRUDE OIL:
"""
        crude = data.get("crude_oil")
        if crude:
            emoji = "🔴" if crude["change_pct"] < 0 else "🟢"
            report += f"  {emoji} Price: ${crude['ltp']:.2f} ({crude['change_pct']:+.2f}%)\n"
        else:
            report += "  ⚪ Data unavailable\n"

        report += f"\n💵 USDINR (Dollar-Rupee):\n"
        usdinr = data.get("usdinr")
        if usdinr:
            emoji = "🔴" if usdinr["change_pct"] > 0 else "🟢"  # Up = bad for India
            report += f"  {emoji} Rate: ₹{usdinr['ltp']:.2f} ({usdinr['change_pct']:+.2f}%)\n"
            if usdinr["change_pct"] > 0.5:
                report += f"  ⚠️ Rupee weakening - FII may sell\n"
            elif usdinr["change_pct"] < -0.5:
                report += f"  ✅ Rupee strengthening - Positive for markets\n"
        else:
            report += "  ⚪ Data unavailable\n"

        report += f"\n📊 INDIA VIX:\n"
        vix = data.get("india_vix")
        if vix:
            vix_val = vix['ltp']
            emoji = "🔴" if vix_val > 20 else "🟡" if vix_val > 15 else "🟢"
            report += f"  {emoji} Level: {vix_val:.2f}\n"
        else:
            report += f"  ⚪ Data unavailable\n"

        report += f"\n🌍 GLOBAL INDICES:\n"
        for idx in ["sp500", "nasdaq", "dow", "nikkei", "hang_seng"]:
            if idx in data:
                emoji = "🔴" if data[idx]["change_pct"] < 0 else "🟢"
                report += f"  {emoji} {idx.upper()}: {data[idx]['change_pct']:+.2f}%\n"

        report += f"\n{'='*50}"
        report += f"\n📈 MARKET SENTIMENT: {sentiment['overall']}"
        report += f"\n⚠️ RISK LEVEL: {sentiment['risk_level']}"
        if sentiment['factors']:
            report += f"\n📝 Factors: {', '.join(sentiment['factors'])}"
        report += f"\n{'='*50}"

        return report


# =============================================================================
# Convenience functions
# =============================================================================
def get_global_data() -> dict:
    """Get all global market data."""
    fetcher = GlobalMarketFetcher()
    return fetcher.fetch_all()

def get_crude_price() -> float | None:
    """Get current crude oil price."""
    fetcher = GlobalMarketFetcher()
    data = fetcher.get_crude_oil()
    return data["ltp"] if data else None

def get_usdinr_rate() -> float | None:
    """Get current USDINR rate."""
    fetcher = GlobalMarketFetcher()
    data = fetcher.get_usdinr()
    return data["ltp"] if data else None

def get_market_sentiment() -> dict:
    """Get overall market sentiment."""
    fetcher = GlobalMarketFetcher()
    return fetcher.get_market_sentiment()


if __name__ == "__main__":
    fetcher = GlobalMarketFetcher()
    print(fetcher.format_report())
