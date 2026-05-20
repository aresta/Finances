# Finances — Architecture

## Overview

**Finances** is a single Streamlit web application that tracks personal investment portfolios
from CSV broker order exports. It parses orders, resolves current and historical asset prices
via Yahoo Finance, and provides an interactive dashboard with 5 tabs (Overview, Assets,
Allocation, By Type, Monthly Returns).

## Core datasets

The application operates on two core datasets plus the raw orders:

### `portfolio` — DataFrame indexed by ISIN

One row per ISIN (active and closed). Built via `build_portfolio()` with FIFO lot accounting,
enriched via `enrich_portfolio()` with asset metadata from yfinance.

| Column | Source | Description |
|---|---|---|---|
| `name` | yfinance | Asset display name |
| `type` | yfinance | Stock / Bond / Other |
| `price` | yfinance | Current price per share |
| `shares_bought` | orders | Total shares purchased (positive) |
| `shares_sold` | orders | Total shares sold (positive, `abs` of sell shares) |
| `money_paid` | orders | Total cash paid for purchases (positive) |
| `money_received` | orders | Total cash received from sales (positive, `abs` of sell amounts) |
| `fifo_cost` | FIFO | Remaining cost basis of held shares |
| `realized_pnl` | FIFO | Cumulative realized P&L from partial sells (sale proceeds − FIFO cost of sold shares) |
| `first_date` | orders | Date of earliest order |
| `last_date` | orders | Date of most recent order |
| `closed` | derived | `True` when all shares sold (net shares ≈ 0) |

Derived property computed on-the-fly in `render()`: `shares_held` (= bought − sold).

### P&L calculation

All P&L metrics compute **total P&L** (unrealized + realized):

```
Total P&L = (market_value + money_received) − money_paid
Total P&L % = Total P&L / money_paid × 100
```

This works for all scenarios:
- Buy-only (`money_received = 0`, `money_paid = fifo_cost`): same as unrealized only
- Partially sold: includes realized gain/loss from the sale
- Fully closed: `market_value = 0`, `fifo_cost = 0` — pure realized P&L

The `_match_sell()` function returns the FIFO cost of shares consumed by a sell,
enabling `build_portfolio()` to accumulate `realized_pnl` per ISIN and
`build_cost_series()` to track cumulative realized P&L over time for charts.

### `hist_prices` — dict: ISIN → Series (date → stock_price)

Per-share price at each sample date (1st, 10th, 20th of each month). Portfolio value at a date
is computed by multiplying `stock_price × cumulative_shares`. Cache file: `.historical_cache.csv`
with columns `date, isin, stock_price`.

### `orders` — DataFrame

Raw parsed orders: `date, isin, amount, shares`. Needed for cumulative share computation
and Monthly Returns tab.

## Project layout

```
src/
├── finances/
│   ├── __init__.py       # Package metadata (version)
│   ├── __main__.py       # python -m finances entry
│   ├── app.py            # Streamlit dashboard, config loading, UI (5 tabs)
│   ├── common.py         # Order parsing, portfolio builder, FIFO, cache I/O, formatting
│   └── data.py           # Yahoo Finance lookups, historical prices, portfolio enrichment
├── defaults/
│   ├── finances.conf     # Default TOML config (bundled in wheel)
│   └── ordres.csv        # Sample order CSV (bundled in wheel)
```

## Module responsibilities

| Module | Purpose |
|---|---|
| `common.py` | CSV parsing (configurable delimiter, decimal separator, date format), FIFO lot accounting with realized P&L tracking, portfolio aggregation (`build_portfolio`), cumulative share matrix, cost-basis and realized P&L series (`build_cost_series` returns `(cost_df, realized_df)`), JSON cache I/O, display formatting (`format_pct`, `format_money`, `format_shares`) |
| `data.py`   | Yahoo Finance lookups (latest price, name, type from `funds_data.asset_classes`), current-price caching (`.price_cache.json`, 12 h TTL), portfolio enrichment (`enrich_portfolio`), historical price fetching with incremental CSV caching (`.historical_cache.csv`, per-ISIN delta updates) |
| `app.py`    | Loads TOML config from CWD (overridable via positional CLI arg, defaults to `finances.conf`), copies defaults from package on first run if missing, invokes the cached `load_data()` pipeline, computes value/cost DataFrames from the core datasets, renders 5 interactive Plotly and table-based tabs |

## Asset type detection

`_fetch_latest_price()` in `data.py` derives the asset type from yfinance's
`ticker.funds_data.asset_classes`. If the ISIN corresponds to a fund with
asset class breakdown, the type is `"Stock"` or `"Bond"` depending on which
class has the larger position. For equities and other non-fund instruments
where `funds_data` is unavailable, the type defaults to `"Other"`.

## Display formatting

| Function | Behavior |
|---|---|
| `format_pct(value)` | Formats as percentage, e.g. `"12.5%"`, `"−3.2%"` |
| `format_money(value)` | Formats EUR amounts. Values ≥ 1 are rounded to integer (e.g. `1 235 €`). Values < 1 show decimals (e.g. `0.45 €`) |
| `format_shares(value)` | Formats share counts with two decimals (e.g. `"9.45"`) |

## Startup sequence

When `finances` (or `streamlit run src/finances/app.py`) is launched:

1. **Config loading** — module-level code in `app.py` runs:
   - If `sys.argv` has exactly 2 elements and `argv[1]` contains `".conf"`,
     `CONFIG_FILE` is set to `argv[1]` (overriding the default `"finances.conf"`).
   - If the default path `"finances.conf"` is missing, the bundled default config
     and sample `ordres.csv` are copied from the package into CWD (first-run bootstrap).
     This bootstrap only checks for the hardcoded `"finances.conf"` path, not
     `CONFIG_FILE`.
   - The TOML file is loaded via `tomllib.load()` into a module-level `config` dict.
     Configuration defines: input file path, CSV column positions, date/number formats,
     cache TTLs, sample days.
   - **Note:** The CLI arg override runs at module level (before `main()`), but is
     fragile — it only triggers when `sys.argv` has exactly 1 extra argument. When
     launched via the `finances` console_script, `main()` rewrites `sys.argv` with
     Streamlit's own args, so the override check may see 4+ elements and not fire.

2. **Cached data pipeline** — `@st.cache_data` wraps `load_data()`. On first run (or cache miss):
   - Parse the orders CSV (configurable delimiter, decimal separator, date format)
     into a DataFrame
   - Build the portfolio DataFrame with FIFO cost basis (`build_portfolio`)
   - Enrich with asset names, types and current prices from yfinance (`enrich_portfolio`)
   - Fetch per-share historical prices at sample days from Yahoo Finance, with incremental
     CSV caching (`.historical_cache.csv`, per-ISIN delta when latest entry > threshold)

3. **Dashboard render** — `render()` computes derived value/cost DataFrames (shares × price,
   FIFO cost series) from the core datasets, then renders 5 interactive tabs with a
   date-range filter and per-asset selection sidebar.

## Cache files

All cache files are written to the current working directory.

| File | Format | Contents | Freshness |
|---|---|---|---|
| `.price_cache.json` | JSON | Per-ISIN: `{name, price, type, timestamp}` | 12 h TTL |
| `.historical_cache.csv` | CSV (`date, isin, stock_price`) | Per-share price history at sample days (1st, 10th, 20th each month) | Incremental per-ISIN delta when latest entry > threshold |

### Cache flow

```
On startup
    │
    ├─ .price_cache.json exists & valid (<12 h)?
    │   ├─ Yes → use cached prices (skip Yahoo Finance for current prices)
    │   └─ No  → fetch fresh prices, overwrite cache
    │
    ├─ .historical_cache.csv exists?
    │   ├─ Yes → load all ISIN price series
    │   │         For each ISIN:
    │   │         ├─ Not in cache → fetch full history from first order date
    │   │         ├─ In cache, most recent entry > threshold → fetch delta only
    │   │         └─ In cache and fresh → keep as-is (no API call)
    │   └─ No  → fetch all ISINs from Yahoo Finance, build initial cache
    │
    └─ Return portfolio + orders + hist_prices to render()
```

## Data flow

```
finances.conf ──► app.py (tomllib.load at module level)
                     │
ordres.csv ──► common.py ──┤ parse_orders(path, config) → orders DataFrame
              common.py ──┤ build_portfolio(orders) → portfolio DataFrame (FIFO)
                     │
              data.py ─────┤ enrich_portfolio(portfolio, config) → enriched portfolio
              data.py ─────┤ fetch_historical_prices(isins, start, end, config) → hist_prices dict
                     │
              app.py ──────┤ compute_cumulative_shares(orders, isins, dates) → shares_df
               app.py ──────┤ build_cost_series(orders, isins, dates) → (cost_df, realized_df)
               app.py ──────┤ (price_df × shares_df) → hist_df (portfolio values)
                     │
              app.py ──────┤ render() → Streamlit UI (5 tabs)
```

## Dashboard tabs

The Streamlit UI renders 5 tabs:

| Tab | Content |
|---|---|
| **Overview** | Portfolio value, cost, P&L and P&L% metric cards; Bonds/Stocks allocation breakdown (% and value, color-coded green/blue); active holdings table with daily/monthly/net P&L per ISIN (total P&L = market_value + money_received − money_paid); closed positions table |
| **Assets** | Per-asset historical line charts (Plotly) for value, P&L, or P&L% over time. Optional total overlay line. Metric selector and "Show Total" checkbox. |
| **Allocation** | Stacked area chart (Plotly) showing value or P&L time-series per asset. With "P&L %" metric, shows percentile allocation over time. |
| **By Type** | Stacked area chart (Plotly) aggregating value/cost/P&L by asset type (Stock, Bond, Other). With "P&L %" metric, shows percentile P&L per type over time. |
| **Monthly Returns** | Year × month grid table (styled HTML) with color-coded cells (green for positive, red for negative returns). Cash-flow-adjusted month-over-month return percentages, plus a YTD column. When the first month with orders has no prior month-start value, a synthetic first-month return is computed from zero starting value and all orders in that first month. |

All tabs share a date-range filter (sidebar) and per-asset selection checkboxes
grouped by type (Stocks, Bonds, Closed).

## Dependencies

- `streamlit` — web UI framework
- `pandas` — data processing and time series
- `plotly` — interactive charts (line, area, scatter)
- `yfinance` — Yahoo Finance price lookups
- `numpy` — numerical operations (transitive via pandas)

Python ≥ 3.11 required (for stdlib `tomllib`).

## Build & Install

### Build and pack
- pip install build
- python -m build --wheel

### Create venv (optional)
- python -m venv venv
- source venv/bin/activate

### Install
- pip install ..../Finances/dist/finances-0.5.0-py3-none-any.whl

### Uninstall
- pip uninstall finances

### exit venv
- deactivate
