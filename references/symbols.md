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

CSV, refreshed daily, under `https://public.fyers.in/sym_details/`:

| Segment | CSV | JSON |
|---|---|---|
| NSE Capital Market | `NSE_CM.csv` | `NSE_CM_sym_master.json` |
| NSE F&O | `NSE_FO.csv` | `NSE_FO_sym_master.json` |
| NSE Currency | `NSE_CD.csv` | `NSE_CD_sym_master.json` |
| NSE Commodity | `NSE_COM.csv` | `NSE_COM_sym_master.json` |
| BSE Capital Market | `BSE_CM.csv` | `BSE_CM_sym_master.json` |
| BSE F&O | `BSE_FO.csv` | `BSE_FO_sym_master.json` |
| MCX Commodity | `MCX_COM.csv` | `MCX_COM_sym_master.json` |

### CSV column order
`Fytoken, Symbol Details, Exchange Instrument type, Minimum lot size, Tick size, ISIN,
Trading Session, Last update date, Expiry date, Symbol ticker, Exchange, Segment,
Scrip code, Underlying symbol, Underlying scrip code, Strike price, Option type,
Underlying FyToken, + 3 reserved columns`.

Use the **Symbol ticker** column as the API symbol. For multileg orders, both legs must
share the same `stream` group (JSON master field `stream`).

### Fytoken structure
`[Exchange 2d][Segment 2d][Expiry 6d YYMMDD][Exchange Token 2–6d]`.

## Resolving a name → symbol

Don't guess. Download the relevant master CSV and grep for the company/contract, or use
`/quotes` to validate a candidate symbol before trading it.
