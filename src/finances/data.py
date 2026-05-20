"""Price fetching and portfolio enrichment from Yahoo Finance."""

import os
import sys
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

from finances.common import (
    load_cache, save_cache, is_cache_valid,
)


def _fetch_latest_price(isin: str) -> tuple[str, float | None, str]:
    """Retrieve the most recent available price, asset name and type for an ISIN.

    Tries descending look-back periods (5d → 1mo → 3mo) to handle thinly-
    traded securities.

    Args:
        isin: The ISIN identifier.

    Returns:
        A ``(name, price, type)`` tuple. *price* is ``None`` if no data was found.
    """
    ticker = yf.Ticker(isin)
    info = ticker.info
    name = info.get("longName") or info.get("shortName") or isin
    asset_class = ticker.funds_data.asset_classes
    if asset_class:
        type = 'Stock' if asset_class.get('stockPosition') > asset_class.get('bondPosition') else 'Bond'
    else:
        type = 'Other'
    # Try increasing windows until we find trading data
    for period in ("5d", "1mo", "3mo"):
        hist = ticker.history(period=period)
        if not hist.empty:
            return name, float(hist["Close"].iloc[-1]), type
    return name, None, type


def resolve_prices(
    isins: list[str],
    config: dict,
    force_refresh: bool = False,
) -> dict:
    """Look up current prices and types for a list of ISINs, using a local JSON cache.

    Args:
        isins: ISIN identifiers to resolve.
        config: Application configuration (cache path, TTL).
        force_refresh: If ``True``, skip the cache and fetch fresh prices.

    Returns:
        Dict mapping ISIN → ``{"name": str, "price": float|None, "type": str, "timestamp": str}``.
    """
    cache = load_cache(config["cache"]["price"])
    prices = {}
    price_ttl = timedelta(hours=config["cache"]["price_ttl_hours"])

    for isin in isins:
        # Use cached entry if still valid and refresh not forced
        if not force_refresh and isin in cache and is_cache_valid(cache[isin], price_ttl):
            prices[isin] = cache[isin]
            continue

        try:
            name, price, ptype = _fetch_latest_price(isin)
            entry = {"name": name, "price": price, "type": ptype, "timestamp": datetime.now().isoformat()}
        except Exception as error:
            print(f"Warning: could not resolve {isin}: {error}", file=sys.stderr)
            entry = {"name": f"Unknown ({isin})", "price": None, "type": None, "timestamp": datetime.now().isoformat()}

        cache[isin] = entry
        prices[isin] = entry

    save_cache(cache, config["cache"]["price"])
    return prices


def enrich_portfolio(portfolio: pd.DataFrame, config: dict) -> pd.DataFrame:
    """Enrich a portfolio DataFrame with name, type and current price.

    Resolves all ISINs via the yfinance price cache (TTL-gated) and merges
    the results into the portfolio.

    Args:
        portfolio: DataFrame from :func:`~finances.common.build_portfolio`,
                   indexed by ISIN.
        config: Application configuration.

    Returns:
        The same DataFrame with added columns: ``name``, ``type``, ``price``.
    """
    prices = resolve_prices(list(portfolio.index), config)
    price_df = pd.DataFrame.from_dict(prices, orient="index")

    portfolio["name"] = portfolio.index.map(
        lambda i: price_df.at[i, "name"] if i in price_df.index else i
    )
    portfolio["type"] = portfolio.index.map(
        lambda i: price_df.at[i, "type"] if i in price_df.index else "Other"
    )
    portfolio["price"] = portfolio.index.map(
        lambda i: price_df.at[i, "price"] if i in price_df.index else None
    )

    return portfolio


# ---------------------------------------------------------------------------
# Historical price fetching (per-share prices at sample dates)
# ---------------------------------------------------------------------------

# Days of the month at which historical prices are sampled
SAMPLE_DAYS: list[int] = [1, 10, 20]


def _sample_date_range(
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
) -> pd.DatetimeIndex:
    """Generate sample dates within ``[start, end]`` for the configured sample days.

    Enumerates month starts, then selects the specified calendar days that
    fall within the date range. Invalid days (e.g. Feb 30) are skipped.

    Args:
        start: Earliest date (inclusive).
        end: Latest date (inclusive).

    Returns:
        A sorted ``DatetimeIndex`` of unique sample dates.
    """
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    month_starts = pd.date_range(
        start=start_ts.replace(day=1), end=end_ts, freq="MS",
    )
    candidates: set[pd.Timestamp] = set()
    for ms in month_starts:
        max_day = ms.days_in_month
        for day in SAMPLE_DAYS:
            if day <= max_day:
                candidate = ms.replace(day=day)
                if start_ts <= candidate <= end_ts:
                    candidates.add(candidate)
    return pd.DatetimeIndex(sorted(candidates))


def _fetch_historical(
    isin: str,
    start: str,
    end: str,
) -> pd.Series:
    """Fetch historical per-share prices for an ISIN, resampled to sample dates.

    Args:
        isin: The ISIN identifier.
        start: Start date string (YYYY-MM-DD).
        end: End date string (YYYY-MM-DD).

    Returns:
        A ``pd.Series`` of per-share prices indexed by sample dates.
        Returns an empty Series named after the ISIN if no data is found.
    """
    ticker = yf.Ticker(isin)
    hist = ticker.history(start=start, end=end)
    if hist.empty:
        return pd.Series(dtype=float, name=isin)

    # Extract Close prices, normalize index to date-only
    close_series = hist["Close"].copy()
    close_series.name = isin
    close_series.index = pd.to_datetime(close_series.index.date)

    # Reindex to target sample dates, forward-filling gaps
    target_dates = _sample_date_range(start, end)
    combined = close_series.reindex(target_dates.union(close_series.index)).ffill()
    return combined.reindex(target_dates)


def fetch_historical_prices(
    isins: list[str],
    start_date: str,
    end_date: str,
    config: dict,
) -> dict:
    """Retrieve historical per-share prices for multiple ISINs with incremental CSV caching.

    Only fetches from Yahoo Finance when:
    - The cache file does not exist (full fetch for all ISINs).
    - An ISIN is not in the cache at all (fetch from *start_date*).
    - An ISIN's most recent cached entry is older than the configured
      threshold (fetch delta from the last cached date onward).
    - An ISIN's earliest cached entry is later than *start_date*
      (fetch backward delta and prepend to existing data).
    To force a full rebuild, delete ``.historical_cache.csv`` manually.

    Cache format: ``date, isin, stock_price`` — per-share price at each
    sample date (1st, 10th, 20th of each month).

    Args:
        isins: ISIN identifiers to ensure are in the cache.
        start_date: Earliest date for ISINs not yet in the cache (YYYY-MM-DD).
        end_date: Latest date to fetch to (YYYY-MM-DD).
        config: Application configuration.

    Returns:
        Dict mapping ISIN → ``pd.Series`` of per-share prices indexed by sample dates.
    """
    prices = {}
    stale_days = config["cache"]["historical_max_age_days"]
    stale_threshold = pd.Timestamp.now().normalize() - pd.Timedelta(days=stale_days)
    cache_path = config["cache"]["historical"]
    to_fetch_full: dict[str, tuple[str, str]] = {}   # isin → (start, end)
    to_fetch_delta: dict[str, tuple[str, str]] = {}   # isin → (last_date+1d, end)
    to_fetch_backward: dict[str, tuple[str, str]] = {}  # isin → (start, cache_start-1d)

    if os.path.exists(cache_path):
        # Load all existing cached data (migrate old `close` column name)
        cached = pd.read_csv(cache_path, parse_dates=["date"])
        if "close" in cached.columns:
            cached = cached.rename(columns={"close": "stock_price"})
        cached["date"] = pd.to_datetime(cached["date"])
        for isin, group in cached.groupby("isin"):
            price_series = group.set_index("date")["stock_price"]
            price_series.index = pd.to_datetime(price_series.index)
            prices[isin] = price_series

        # Determine per-ISIN fetch windows
        req_start = pd.Timestamp(start_date)
        for isin in isins:
            if isin not in prices:
                to_fetch_full[isin] = (start_date, end_date)
            else:
                if prices[isin].index.max() < stale_threshold:
                    delta_start = (prices[isin].index.max() + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
                    to_fetch_delta[isin] = (delta_start, end_date)
                if prices[isin].index.min() > req_start:
                    backward_end = (prices[isin].index.min() - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
                    to_fetch_backward[isin] = (start_date, backward_end)
    else:
        # No cache — full fetch for every ISIN
        to_fetch_full = {isin: (start_date, end_date) for isin in isins}

    # Fetch full history for brand-new ISINs
    for isin, (start, end) in to_fetch_full.items():
        try:
            price_series = _fetch_historical(isin, start, end)
            prices[isin] = price_series
        except Exception as error:
            print(f"Warning: could not fetch historical data for {isin}: {error}", file=sys.stderr)
            prices[isin] = pd.Series(dtype=float, name=isin)

    # Fetch recent delta for stale ISINs, merge with existing data
    for isin, (start, end) in to_fetch_delta.items():
        try:
            new_series = _fetch_historical(isin, start, end)
            existing = prices[isin]
            combined = pd.concat([existing, new_series])
            combined = combined[~combined.index.duplicated(keep="last")]
            prices[isin] = combined.sort_index()
        except Exception as error:
            print(f"Warning: could not fetch delta for {isin}: {error}", file=sys.stderr)

    # Fetch backward delta for ISINs whose cache starts too late, prepend
    for isin, (start, end) in to_fetch_backward.items():
        try:
            older = _fetch_historical(isin, start, end)
            existing = prices[isin]
            combined = pd.concat([older, existing])
            combined = combined[~combined.index.duplicated(keep="last")]
            prices[isin] = combined.sort_index()
        except Exception as error:
            print(f"Warning: could not fetch backward data for {isin}: {error}", file=sys.stderr)

    # Persist to CSV cache (long format: date, isin, stock_price)
    cache_rows = {
        isin: pd.DataFrame({"date": s.index, "isin": isin, "stock_price": s.values})
        for isin, s in prices.items() if not s.empty
    }
    if cache_rows:
        pd.concat(cache_rows.values(), ignore_index=True).to_csv(cache_path, index=False)

    return prices
