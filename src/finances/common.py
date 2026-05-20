"""Shared utilities: order parsing, FIFO accounting, caching, formatting, portfolio builder."""

import json
import os
from datetime import datetime, timedelta

import pandas as pd

# Epsilon constants for float comparisons
SHARE_EPS = 1e-6
VALUE_EPS = 1e-9


def _match_sell(lots: list[list[float]], shares_to_sell: float) -> float:
    """Consume shares from the FIFO lot queue, returning the cost of sold shares.

    Matches a sell order against the oldest buy lots first (FIFO).
    Modifies *lots* directly — mutates its elements and may shorten the list.

    Args:
        lots: Mutable list of ``[shares, cost]`` pairs representing remaining buy lots.
        shares_to_sell: Number of shares being sold (positive value).

    Returns:
        FIFO cost basis of the shares that were sold.
    """
    cost_of_sold = 0.0
    remaining = shares_to_sell
    while remaining > SHARE_EPS and lots:
        first_lot = lots[0]
        if first_lot[0] <= remaining + SHARE_EPS:
            remaining -= first_lot[0]
            cost_of_sold += first_lot[1]
            lots.pop(0)
        else:
            cost_per_share = first_lot[1] / first_lot[0]
            cost_of_sold += cost_per_share * remaining
            first_lot[0] -= remaining
            first_lot[1] -= cost_per_share * remaining
            remaining = 0
    return cost_of_sold


def _make_converter(*, strip_currency: bool = False, decimal_separator: str = "."):
    """Create a CSV column value converter for the given decimal format.

    Handles European number format (comma as decimal separator) and optional
    currency suffix stripping.

    Args:
        strip_currency: Whether to strip currency suffixes (" EUR", " €").
        decimal_separator: The decimal separator character ("." or ",").

    Returns:
        A converter function suitable for ``pd.read_csv(converters=...)``.
    """
    def _convert(value: str) -> float:
        value = value.strip()
        if strip_currency:
            value = value.replace(" EUR", "").replace(" €", "")
        if decimal_separator == ",":
            value = value.replace(".", "").replace(",", ".")
        return float(value)
    return _convert


# ---------------------------------------------------------------------------
# Order parsing
# ---------------------------------------------------------------------------


def parse_orders(path: str, config: dict) -> pd.DataFrame:
    """Parse a broker order CSV file and return a sorted DataFrame.

    Handles semicolon-delimited files with European number format
    (comma as decimal separator, optional currency suffix on amounts).

    Args:
        path: Path to the orders CSV file.
        config: Configuration dict with ``csv`` (delimiter, decimal_separator),
                ``input.columns`` (column indices), and ``date_format``.

    Returns:
        DataFrame with columns: ``date``, ``isin``, ``amount``, ``shares``,
        sorted chronologically by date. Positive shares = buy, negative = sell.
    """
    col_conf = config["input"]["csv"]
    dec_sep = col_conf["decimal_separator"]
    date_fmt = col_conf["date_format"]

    _parse_amount = _make_converter(strip_currency=True, decimal_separator=dec_sep)
    _parse_shares = _make_converter(strip_currency=False, decimal_separator=dec_sep)

    # Build the DataFrame
    df = pd.read_csv(
        path,
        sep=col_conf["delimiter"],
        header=0,
        encoding="utf-8",
        converters={
            col_conf["amount"]: _parse_amount,
            col_conf["shares"]: _parse_shares,
        },
    )

    # Identify refund columns and switch to negative vals
    subsc_colnum = col_conf.get("subsc")
    if subsc_colnum is not None and len(df.columns) > subsc_colnum:
        amnt_col = df.columns[col_conf["amount"]]
        sh_col = df.columns[col_conf["shares"]]
        refund_pattern = 'refund|reembolso|neg'
        condition = df.iloc[:, subsc_colnum].str.contains(refund_pattern, case=False, na=False)
        df.loc[condition, amnt_col] *= -1
        df.loc[condition, sh_col] *= -1

    # Keep only the 4 columns we need, rename them
    df = df.iloc[:, [col_conf["date"], col_conf["isin"], col_conf["amount"], col_conf["shares"]]]
    df.columns = ["date", "isin", "amount", "shares"]
    # Parse dates and sort chronologically (required by FIFO functions)
    df["date"] = pd.to_datetime(df["date"], format=date_fmt)
    df = df.sort_values("date")
    return df


# ---------------------------------------------------------------------------
# Portfolio construction (FIFO-based, includes closed positions)
# ---------------------------------------------------------------------------


def build_portfolio(orders: pd.DataFrame) -> pd.DataFrame:
    """Build a portfolio DataFrame from parsed orders with FIFO lot accounting.

    Aggregates order data per ISIN using vectorised groupby, then applies
    FIFO lot matching to compute the remaining cost basis for each ISIN.
    Includes both active and closed positions.

    Args:
        orders: DataFrame from :func:`parse_orders`, sorted by date.

    Returns:
        DataFrame indexed by ISIN with columns: ``shares_bought``,
        ``shares_sold``, ``money_paid``, ``money_received``, ``fifo_cost``,
        ``realized_pnl``, ``first_date``, ``last_date``, ``closed``.
        All share and money fields are stored as positive numbers.
    """
    # Separate buy and sell orders
    buys = orders[orders["shares"] > 0]
    sells = orders[orders["shares"] < 0]

    # Aggregate buys per ISIN (vectorised)
    bought = buys.groupby("isin").agg(
        shares_bought=("shares", "sum"),
        money_paid=("amount", "sum"),
    )

    # Aggregate sells per ISIN (shares/amounts are negative in CSV → abs)
    sold = sells.groupby("isin").agg(
        shares_sold=("shares", lambda s: abs(s.sum())),
        money_received=("amount", lambda a: abs(a.sum())),
    )

    # First and last operation dates
    dates = orders.groupby("isin").agg(
        first_date=("date", "min"),
        last_date=("date", "max"),
    )

    # Merge into portfolio DataFrame
    portfolio = bought.join(sold, how="outer").join(dates, how="outer").fillna(0)
    portfolio = portfolio.astype({
        "shares_bought": float, "money_paid": float,
        "shares_sold": float, "money_received": float,
    })

    # FIFO cost basis for remaining shares and realized P&L from sells
    portfolio["fifo_cost"] = 0.0
    portfolio["realized_pnl"] = 0.0
    for isin in portfolio.index:
        isin_orders = orders[orders["isin"] == isin]
        lots = []
        cum_realized = 0.0
        for order in isin_orders.itertuples(index=False):
            if order.shares > 0:
                lots.append([order.shares, order.amount])
            else:
                cost_of_sold = _match_sell(lots, abs(order.shares))
                cum_realized += abs(order.amount) - cost_of_sold
        portfolio.at[isin, "fifo_cost"] = sum(lot[1] for lot in lots)
        portfolio.at[isin, "realized_pnl"] = cum_realized

    # Closed flag: no remaining shares
    portfolio["closed"] = (
        (portfolio["shares_bought"] - portfolio["shares_sold"]).abs() < SHARE_EPS
    )

    portfolio.index.name = "isin"
    return portfolio


def compute_cumulative_shares(
    orders: pd.DataFrame,
    isins: list[str],
    dates: pd.DatetimeIndex,
) -> pd.DataFrame:
    """Compute cumulative shares held per ISIN at each sample date.

    Pivots the orders into a date × ISIN matrix of share changes,
    cumulatively sums them, and forward-fills to the requested dates.

    Args:
        orders: DataFrame from :func:`parse_orders`, sorted by date.
        isins: ISINs to include.
        dates: Sample dates at which to evaluate share holdings.

    Returns:
        DataFrame indexed by *dates*, columns = ISINs, values = cumulative shares.
    """
    orders_sub = orders[orders["isin"].isin(isins)]
    if orders_sub.empty:
        return pd.DataFrame(0, index=dates, columns=isins)

    orders_pivot = orders_sub.pivot_table(
        index="date", columns="isin", values="shares", aggfunc="sum",
    )
    orders_pivot = orders_pivot.reindex(columns=isins, fill_value=0)
    orders_pivot = orders_pivot.fillna(0).cumsum()

    shares_extended = orders_pivot.reindex(
        dates.union(orders_pivot.index)
    ).ffill().reindex(dates).fillna(0)

    return shares_extended


def build_cost_series(
    orders: pd.DataFrame,
    isins: list[str],
    dates: pd.DatetimeIndex,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Compute FIFO cost basis and cumulative realized P&L for all ISINs at each sample date.

    Performs a single chronological pass through the orders, tracking
    the remaining FIFO lot cost and cumulative realized P&L after each
    trade. Then forward-fills both metrics to the requested sample dates.

    Args:
        orders: DataFrame from :func:`parse_orders`, sorted by date.
        isins: ISINs to include.
        dates: Sample dates at which to evaluate cost basis.

    Returns:
        Tuple of ``(cost_df, realized_df)``:
        - *cost_df* — DataFrame indexed by *dates*, columns = ISINs, values = FIFO cost basis.
        - *realized_df* — DataFrame indexed by *dates*, columns = ISINs, values = cumulative realized P&L.
    """
    records = []
    lots = {}
    cum_realized = {}
    for isin in isins:
        lots[isin] = []
        cum_realized[isin] = 0.0

    for order in orders.sort_values("date").itertuples(index=False):
        isin = order.isin
        if isin not in isins:
            continue

        if order.shares > 0:
            lots[isin].append([order.shares, order.amount])
        else:
            cost_of_sold = _match_sell(lots[isin], abs(order.shares))
            cum_realized[isin] += abs(order.amount) - cost_of_sold

        records.append({
            "date": order.date,
            "isin": isin,
            "cost": sum(lot[1] for lot in lots[isin]),
            "realized_pnl": cum_realized[isin],
        })

    # Build result DataFrames: date × ISIN
    cost_result = pd.DataFrame(index=dates, columns=isins, dtype=float)
    realized_result = pd.DataFrame(index=dates, columns=isins, dtype=float)

    if records:
        cost_records = pd.DataFrame(records)
        for isin in isins:
            isin_rec = cost_records[cost_records["isin"] == isin]
            if isin_rec.empty:
                cost_result[isin] = 0.0
                realized_result[isin] = 0.0
                continue

            last_per_date = isin_rec.drop_duplicates("date", keep="last")
            all_dates = dates.union(last_per_date["date"])

            # FIFO cost basis
            cost_series = last_per_date.set_index("date")["cost"]
            cost_result[isin] = (
                cost_series.reindex(all_dates).ffill().reindex(dates).fillna(0)
            )

            # Cumulative realized P&L
            realized_series = last_per_date.set_index("date")["realized_pnl"]
            realized_result[isin] = (
                realized_series.reindex(all_dates).ffill().reindex(dates).fillna(0)
            )
    else:
        cost_result[:] = 0.0
        realized_result[:] = 0.0

    return cost_result, realized_result


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------


def load_cache(path: str) -> dict:
    """Load a JSON cache file.

    Args:
        path: Path to the JSON cache file.

    Returns:
        The deserialised dictionary, or ``{}`` if the file does not exist.
    """
    if os.path.exists(path):
        with open(path, encoding="utf-8") as cache_file:
            return json.load(cache_file)
    return {}


def save_cache(cache: dict, path: str) -> None:
    """Persist a dictionary to a JSON file with indentation.

    Args:
        cache: The dictionary to serialise.
        path: Destination file path (overwritten if it exists).
    """
    with open(path, "w", encoding="utf-8") as cache_file:
        json.dump(cache, cache_file, indent=2)


def is_cache_valid(entry: dict, ttl: timedelta) -> bool:
    """Check whether a cached price entry is still within its TTL.

    Args:
        entry: A cache entry dict with a ``"timestamp"`` ISO-format string.
        ttl: Maximum allowed age as a timedelta.

    Returns:
        ``True`` if the entry has not expired, ``False`` otherwise.
    """
    cached_time = datetime.fromisoformat(entry["timestamp"])
    return datetime.now() - cached_time < ttl


# ---------------------------------------------------------------------------
# Display formatting (European-style: space thousand separator, € suffix)
# ---------------------------------------------------------------------------


def format_pct(value: float | None) -> str:
    """Format a percentage with up to 2 decimals, dropping trailing zeros.

    Args:
        value: Percentage value, ``None``, or ``NaN``.

    Returns:
        String like ``"12.34%"`` or ``"5.6%"``, or ``""`` for ``None``/``NaN``.
    """
    if value is None:
        return ""
    if isinstance(value, float) and value != value:  # NaN check
        return ""
    formatted = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{formatted}%"


def format_money(value: float | None) -> str:
    """Format a EUR amount with thousands separator.

    Args:
        value: EUR amount, ``None``, or ``NaN``.

    Returns:
        String like ``"1 234 €"`` or ``"0.50 €"``, or ``""`` for ``None``/``NaN``.
    """
    if value is None:
        return ""
    if isinstance(value, float) and value != value:  # NaN check
        return ""
    if abs(value) >= 1 or value == 0:
        return f"{int(round(value)):,}".replace(",", " ") + " €"
    return f"{value:,.2f}".replace(",", " ") + " €"


def format_shares(value: float | None) -> str:
    """Format a share count, dropping trailing zeros.

    Args:
        value: Share count, ``None``, or ``NaN``.

    Returns:
        String like ``"9.45"``, or ``""`` for ``None``/``NaN``.
    """
    if value is None:
        return ""
    if isinstance(value, float) and value != value:  # NaN check
        return ""
    return f"{value:.2f}".rstrip("0").rstrip(".")