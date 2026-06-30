# FYERS v3 Symbol Format & Master Files

Every tradable instrument is identified by a symbol string `EXCHANGE:...`. Getting
this exactly right is the most common source of `-300 Invalid symbol` errors.

## Construction rules

| Segment | Format | Examples |
|---|---|---|
| Equity | `{Ex}:{Symbol}-{Series}` | `NSE:SBIN-EQ`, `NSE:MODIRUBBER-BE`, `BSE:SBIN-A` |
| Equity Futures | `{Ex}:{Underlying}{YY}{MMM}FUT` | `NSE:NIFTY20OCTFUT`, `BSE:SENSEX23AUGFUT` |
| Equity Options (Monthly) | `{Ex}:{Underlying}{YY}{MMM}{Strike}{CE/PE}` | `NSE:NIFTY20OCT11000CE`, `NSE:BANKNIFTY20NOV25000PE` |
| Equity Options (Weekly) | `{Ex}:{Underlying}{YY}{M}{dd}{Strike}{CE/PE}` | `NSE:NIFTY2010811000CE`, `NSE:NIFTY20O0811000CE`, `NSE:NIFTY20D1025000CE` |
| Currency Futures | `{Ex}:{Pair}{YY}{MMM}FUT` | `NSE:USDINR20OCTFUT` |
| Currency Options | monthly/weekly as above | `NSE:USDINR20OCT75CE`, `NSE:USDINR20O0875CE` |
| Commodity Futures | `{Ex}:{Commodity}{YY}{MMM}FUT` | `MCX:CRUDEOIL20OCTFUT`, `MCX:GOLD20DECFUT` |
| Commodity Options (Monthly) | `{Ex}:{Commodity}{YY}{MMM}{Strike}{CE/PE}` | `MCX:CRUDEOIL20OCT4000CE` |
| Index (for option chain / data) | `{Ex}:{Name}-INDEX` | `NSE:NIFTY50-INDEX`, `NSE:NIFTYBANK-INDEX` |

## Placeholders

- `{Ex}` = `NSE` | `BSE` | `MCX`
- `{YY}` = last two digits of year (`20`, `25`)
- `{MMM}` (monthly) = three-letter uppercase month: `JAN`…`DEC`
- `{M}` (weekly, **single char month code**): Jan=`1` Feb=`2` Mar=`3` Apr=`4` May=`5`
  Jun=`6` Jul=`7` Aug=`8` Sep=`9` **Oct=`O` (letter O), Nov=`N`, Dec=`D`**
- `{dd}` (weekly) = two-digit expiry day (`01`, `08`, `25`)
- `{Strike}` = strike price (integer, no decimals)
- `{Opt_Type}` = `CE` (call) | `PE` (put)

**Weekly vs monthly is the #1 gotcha.** Weekly uses the 1-char month code + 2-digit
day (e.g. `NSE:NIFTY20D1025000CE` → year 20, Dec=`D`, day `10`, strike 25000, PUT…CALL);
monthly uses the 3-letter month and no day (`NSE:NIFTY20OCT11000CE`). Note Oct/Nov/Dec
collapse to letters `O`/`N`/`D` in weekly form. When unsure of the exact expiry encoding
for a contract, **look it up in the symbol master file** rather than constructing it by
hand.

Symbols with special characters (e.g. `M&M`) must be URL-encoded in raw REST query
strings: `M%26M`.

## Symbol master files (authoritative source of valid symbols)

**Refreshed every trading day.** The master files are the canonical list of every
tradable instrument and the only reliable way to map a name to an exact symbol. There
are two formats under `https://public.fyers.in/sym_details/`:

- **JSON (preferred)** — `https://public.fyers.in/sym_details/<MASTER>_sym_master.json`.
  A dict keyed by the API symbol ticker, with each instrument's lot size, tick size,
  expiry, strike, etc. as named fields. No column-position parsing.
- **CSV** — `https://public.fyers.in/sym_details/<MASTER>.csv`. Same data, positional.

| Segment | Master code | JSON URL |
|---|---|---|
| NSE Capital Market (equity/index) | `NSE_CM`  | `…/NSE_CM_sym_master.json` |
| NSE Equity Derivatives (F&O)       | `NSE_FO`  | `…/NSE_FO_sym_master.json` |
| NSE Currency Derivatives            | `NSE_CD`  | `…/NSE_CD_sym_master.json` |
| NSE Commodity                       | `NSE_COM` | `…/NSE_COM_sym_master.json` |
| BSE Capital Market                  | `BSE_CM`  | `…/BSE_CM_sym_master.json` |
| BSE Equity Derivatives              | `BSE_FO`  | `…/BSE_FO_sym_master.json` |
| MCX Commodity                       | `MCX_COM` | `…/MCX_COM_sym_master.json` |

### JSON record fields (the ones you'll use)
Each value in the dict (keyed by `symTicker`, e.g. `"NSE:SBIN-EQ"`) includes:

| Field | Meaning |
|---|---|
| `symTicker` | the API symbol string (use this for orders/quotes/history) |
| `exSymName` / `symDetails` | human-readable name (e.g. `STATE BANK OF INDIA`, `30 Jun 26 65400 CE`) |
| `minLotSize` | order quantity must be a multiple of this (F&O lot) |
| `tickSize` | price increment (round limit/stop prices to this) |
| `optType` | `CE` / `PE` for options, `XX` for futures & cash |
| `strikePrice` | strike for options; `-1` for non-options |
| `expiryDate` | **epoch seconds** (empty for cash); convert to a date before displaying |
| `exchange` / `segment` | numeric IDs (10=NSE 11=MCX 12=BSE; segment 10=CM 11=FO 12=CD 20=COM) |
| `isin` | ISIN for equities |
| `fyToken` | unique instrument token (also used by some WebSocket calls) |
| `stream` | multileg group — both legs of a multileg order must share the same `stream` |

### CSV column order (if you use the CSV instead)
`Fytoken, Symbol Details, Exchange Instrument type, Minimum lot size, Tick size, ISIN,
Trading Session, Last update date, Expiry date, Symbol ticker, Exchange, Segment,
Scrip code, Underlying symbol, Underlying scrip code, Strike price, Option type,
Underlying FyToken, + 3 reserved columns`. Use the **Symbol ticker** column as the API symbol.

### Fytoken structure
`[Exchange 2d][Segment 2d][Expiry 6d YYMMDD][Exchange Token 2–6d]`.

## Resolving a name → symbol

**Don't guess — look it up.** Use the bundled helper, which downloads + caches the
relevant master for the day and searches it:

```bash
python scripts/fyers_symbols.py search NSE_CM SBIN                 # equity
python scripts/fyers_symbols.py search NSE_FO BANKNIFTY --opt CE --expiry 2026-06-30
python scripts/fyers_symbols.py info  NSE_CM NSE:SBIN-EQ           # full record (lot, tick, isin…)
python scripts/fyers_symbols.py refresh NSE_FO                     # force re-download
```

It caches to `~/.fyers/sym_master/<MASTER>.json` and re-fetches when the cached copy is
stale (its newest `lastUpdate` is before today). As a final check before trading a
constructed symbol, validate it with `/quotes` (an invalid symbol returns `-300`).
