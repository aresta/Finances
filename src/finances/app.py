"""Streamlit dashboard for personal investment portfolio tracking.

Usage::

    finances              # run from command line (after pipx install)
    python -m finances    # same as above
"""

import os
import sys
import tomllib
from datetime import datetime
from importlib.resources import files as _res_files

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from finances.common import (
    parse_orders, build_portfolio, build_cost_series,
    compute_cumulative_shares,
    format_pct, format_money, format_shares,
)
from finances.data import (
    enrich_portfolio,
    fetch_historical_prices,
)

DEFAULT_CONF_FILE = "finances.conf"
CONFIG_FILE = DEFAULT_CONF_FILE

# ---------------------------------------------------------------------------
# Entry point (for pipx console_script)
# ---------------------------------------------------------------------------

def main() -> None:
    """Launch the Streamlit dashboard.

    When installed via pipx, ``finances`` invokes this function which
    bootstraps the Streamlit CLI with the current file as the target app.
    """

    from streamlit.web import cli as _stcli

    sys.argv = ["streamlit", "run", __file__] + sys.argv[1:]
    sys.exit(_stcli.main())


# ---------------------------------------------------------------------------
# Config & page settings (run at module level — cheap, no side effects)
# ---------------------------------------------------------------------------

# Copy default files to CWD if finances.conf not found (first run after install)
if not os.path.exists("finances.conf"):
    _d = _res_files("defaults")
    with open("finances.conf", "wb") as dst:
        dst.write((_d / "finances.conf").read_bytes())
    if not os.path.exists("ordres.csv"):
        with open("ordres.csv", "wb") as dst:
            dst.write((_d / "ordres.csv").read_bytes())
        print("Created ordres.csv — replace with your own broker data.")
    print("Created finances.conf — edit it to match your broker format.")

if len(sys.argv) == 2 and '.conf' in sys.argv[1]:
    CONFIG_FILE = sys.argv[1]

# Load TOML config from current working directory
with open(CONFIG_FILE, "rb") as f:
    config = tomllib.load(f)
st.set_page_config(layout="wide", page_title=config["general"]["title"])


# ---------------------------------------------------------------------------
# Cached data pipeline — runs once, produces the two core datasets + orders
# ---------------------------------------------------------------------------


@st.cache_data(show_spinner="Loading data from Yahoo Finance...")
def load_data() -> tuple:
    """Load and compute all data needed by the dashboard.

    On first run (or cache invalidation) this fetches prices from Yahoo
    Finance and builds the full portfolio and historical price datasets.
    Subsequent Streamlit re-runs reuse the cached result.

    Returns:
        A tuple of ``(portfolio, orders, hist_prices)``.
    """
    print('orders_file',config["input"]["orders_file"])
    # Parse broker orders → DataFrame
    orders = parse_orders(config["input"]["orders_file"], config)

    # Build portfolio with FIFO cost basis (one row per ISIN)
    portfolio = build_portfolio(orders)

    # Enrich with name, type, current price from yfinance cache
    portfolio = enrich_portfolio(portfolio, config)

    # Fetch per-share historical prices at sample dates
    all_isins = list(portfolio.index)
    start_date = portfolio["first_date"].min().strftime("%Y-%m-%d")
    end_date = datetime.now().strftime("%Y-%m-%d")
    hist_prices = fetch_historical_prices(all_isins, start_date, end_date, config)

    return portfolio, orders, hist_prices


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------


def render() -> None:
    """Build and render the full dashboard UI."""
    portfolio, orders, hist_prices = load_data()

    # Derived properties
    portfolio["shares_held"] = portfolio["shares_bought"] - portfolio["shares_sold"]

    active_isins = list(portfolio[~portfolio["closed"]].sort_values(["type", "name"]).index)
    closed_isins = list(portfolio[portfolio["closed"]].sort_values("name").index)
    all_isins = list(portfolio.index)
    portfolio_min_date = portfolio['first_date'].min()

    # ---- Session state defaults ----
    # Held assets start selected, closed assets start unselected
    for isin in active_isins:
        if f"sel_{isin}" not in st.session_state:
            st.session_state[f"sel_{isin}"] = True

    for isin in closed_isins:
        if f"sel_{isin}" not in st.session_state:
            st.session_state[f"sel_{isin}"] = False

    if "metric" not in st.session_state:
        st.session_state.metric = "Value"

    # ---- Build unified date index ----
    all_dates = pd.DatetimeIndex(
        sorted(set().union(*[s.index for s in hist_prices.values()]))
    )
    all_dates = all_dates[all_dates >= portfolio_min_date]

    # ---- Compute value & cost DataFrames ----
    # Cumulative shares per ISIN per date (from orders)
    shares_df = compute_cumulative_shares(orders, all_isins, all_dates)

    # Stock price per ISIN per date (from hist_prices)
    price_df = pd.DataFrame({
        isin: hist_prices[isin] for isin in all_isins
    }).reindex(all_dates).ffill().fillna(0)

    # Portfolio value: shares × stock_price
    value_isins = [i for i in all_isins if i in shares_df.columns and i in price_df.columns]
    hist_df = pd.DataFrame(index=all_dates, columns=all_isins, dtype=float).fillna(0)


    for isin in value_isins:
        hist_df[isin] = (shares_df[isin] * price_df[isin]).round(6)
    
    # FIFO cost basis and cumulative realized P&L per ISIN per date
    cost_df, realized_df = build_cost_series(orders, all_isins, all_dates)

    # ---- Per-ISIN price anchors from hist_prices ----
    last_hist = {}
    month_start_hist = {}
    for isin, s in hist_prices.items():
        if not s.empty:
            last_hist[isin] = float(s.iloc[-1])
            ms = s[s.index.day == 1]
            if not ms.empty:
                month_start_hist[isin] = float(ms.iloc[-1])

    st.title(config["general"]["title"])

    # ---- Sidebar: date range selector ----
    st.sidebar.markdown("### Date range")
    today = pd.Timestamp.now().normalize()
    range_options = {
        "All": None,
        "Last month": today - pd.DateOffset(months=1),
        "Last 6 months": today - pd.DateOffset(months=6),
        "Last year": today - pd.DateOffset(years=1),
        "Last 2 years": today - pd.DateOffset(years=2),
    }
    chosen_range = st.sidebar.selectbox(
        "Date range", list(range_options.keys()),
        index=0, label_visibility="collapsed",
    )
    if range_options[chosen_range] is None:
        date_range = (hist_df.index.min(), hist_df.index.max())
    else:
        range_start = max(range_options[chosen_range], hist_df.index.min())
        date_range = (range_start, hist_df.index.max())

    # ---- Sidebar: asset checkboxes (grouped by type) ----
    st.sidebar.markdown("---")
    st.sidebar.markdown("### Assets")

    sel_all_col, unsel_all_col = st.sidebar.columns(2)
    if sel_all_col.button("Select All"):
        for isin in active_isins:
            st.session_state[f"sel_{isin}"] = True
        st.rerun()
    if unsel_all_col.button("Unselect All"):
        for isin in active_isins:
            st.session_state[f"sel_{isin}"] = False
        st.rerun()

    type_labels = {"Stock": "Stocks", "Bond": "Bonds"}

    # Group active ISINs by type for sidebar checkboxes
    active_port = portfolio.loc[active_isins].sort_values(["type", "name"])
    for ptype, group in active_port.groupby("type", sort=False):
        label = type_labels.get(ptype, "Other")
        st.sidebar.markdown(f"**{label}**")
        for isin in group.index:
            st.sidebar.checkbox(group.loc[isin, "name"], key=f"sel_{isin}")

    if closed_isins:
        st.sidebar.markdown("---")
        st.sidebar.markdown("**Closed**")
        for isin in closed_isins:
            st.sidebar.checkbox(
                portfolio.loc[isin, "name"] if isin in portfolio.index else isin,
                key=f"sel_{isin}"
            )

    # Collect selected ISINs (used for date clamping and all tabs)
    selected_isins = [
        isin for isin in all_isins
        if st.session_state.get(f"sel_{isin}", False)
    ]

    # Clamp date-range start to the first date with any portfolio data
    # for the selected assets, so charts don't show months of zeros
    if selected_isins:
        sel_isins = [i for i in selected_isins if i in hist_df.columns]
        if sel_isins:
            first_val_date = hist_df[sel_isins].sum(axis=1)
            first_val_date = first_val_date[first_val_date > 0].index
            if len(first_val_date) > 0:
                start_idx = first_val_date[0]
                date_range = (max(date_range[0], start_idx), date_range[1])

    # ---- Apply date-range filter to DataFrames ----
    date_mask = (hist_df.index >= date_range[0]) & (hist_df.index <= date_range[1])
    hist_filtered = hist_df.loc[date_mask]
    cost_filtered = cost_df.loc[date_mask]
    realized_filtered = realized_df.loc[date_mask]

    # ---- Reconcile metric across tab-specific radio buttons ----
    _METRIC_KEYS = ["metric_assets", "metric_allocation", "metric_by_type"]
    metric = st.session_state.get("metric", "Value")
    for key in _METRIC_KEYS:
        val = st.session_state.get(key, "Value")
        if val != metric:
            metric = val
            break

    st.session_state.metric = metric
    for key in _METRIC_KEYS:
        st.session_state[key] = metric

    def _compute_metric_values(isin: str) -> pd.Series:
        """Return the filtered value, P&L (total: unrealized + realized), or P&L % series for *isin*.

        Args:
            isin: The ISIN to compute metrics for.

        Returns:
            A ``pd.Series`` indexed by the filtered date range.
        """
        if st.session_state.metric == "Value":
            return hist_filtered[isin]
        if st.session_state.metric == "P&L":
            unrealized = hist_filtered[isin] - cost_filtered[isin]
            return unrealized + realized_filtered[isin]
        # P&L %: total P&L / money_paid × 100
        total_pnl = hist_filtered[isin] - cost_filtered[isin] + realized_filtered[isin]
        money_paid = portfolio.at[isin, "money_paid"]
        if money_paid:
            return total_pnl / money_paid * 100
        return pd.Series(float("nan"), index=total_pnl.index)

    # ---- Tabs ----
    tab_names = ["Overview", "Assets", "Allocation", "By Type",
                 "Monthly Returns"]
    tabs = st.tabs(tab_names)

    # ===== TAB 0: Overview =====
    with tabs[0]:
        if selected_isins:
            # Summary metrics (active positions only)
            active_sel = [
                i for i in selected_isins
                if i in portfolio.index and not portfolio.at[i, "closed"]
            ]
            if active_sel:
                latest_value = sum(
                    portfolio.at[i, "shares_held"] * portfolio.at[i, "price"]
                    for i in active_sel
                    if portfolio.at[i, "price"] is not None
                )
                latest_cost = sum(
                    portfolio.at[i, "money_paid"] for i in active_sel
                )
                total_received = sum(
                    portfolio.at[i, "money_received"] for i in active_sel
                )
            else:
                latest_value = latest_cost = total_received = 0.0
            total_pnl = latest_value + total_received - latest_cost
            total_pnl_pct = (total_pnl / latest_cost * 100) if latest_cost else 0.0
        else:
            latest_value = latest_cost = total_received = total_pnl = total_pnl_pct = 0.0
            active_sel = []

        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("Portfolio Value", format_money(latest_value))
        col2.metric("Realized P&L", format_money(total_received))
        col3.metric("Total Invested", format_money(latest_cost))
        col4.metric("Total P&L", format_money(total_pnl))
        col5.metric("Total P&L %", format_pct(total_pnl_pct))

        # ---- Overview tables: active holdings + closed positions ----
        active_rows = []
        closed_rows = []
        sel_port = portfolio.loc[
            portfolio.index.isin(selected_isins)
        ].sort_values(["type", "name"]) if selected_isins else portfolio.iloc[0:0]

        for isin in sel_port.index:
            row = sel_port.loc[isin]
            shares_held = row["shares_bought"] - row["shares_sold"]

            if not row["closed"]:
                # Active holding
                current_price = row["price"]
                market_value = shares_held * current_price if current_price else None
                daily_pnl_pct = (
                    (current_price - last_hist[isin]) / last_hist[isin] * 100
                    if current_price and isin in last_hist else None
                )
                monthly_pnl_pct = (
                    (current_price - month_start_hist[isin]) / month_start_hist[isin] * 100
                    if current_price and isin in month_start_hist else None
                )
                net_pnl = (market_value + row["money_received"]) - row["money_paid"] if market_value is not None else None
                net_pnl_pct = (
                    (net_pnl / row["money_paid"] * 100)
                    if row["money_paid"] and net_pnl is not None else None
                )

                active_rows.append({
                    "Type": row["type"] or "Other",
                    "Name": row["name"],
                    "ISIN": isin,
                    "Shares": format_shares(shares_held),
                    "Market Value": format_money(market_value),
                    "Daily P&L %": format_pct(daily_pnl_pct),
                    "Month P&L %": format_pct(monthly_pnl_pct),
                    "Net P&L": format_money(net_pnl),
                    "Net P&L %": format_pct(net_pnl_pct),
                })
            else:
                # Closed position — realized P&L
                net_pnl = row["money_received"] - row["money_paid"]
                net_pnl_pct = (
                    (net_pnl / row["money_paid"] * 100)
                    if row["money_paid"] else None
                )

                closed_rows.append({
                    "Type": row["type"] or "Other",
                    "Name": row["name"],
                    "ISIN": isin,
                    "Total cost": format_money(row["money_paid"]),
                    "Sold": format_money(row["money_received"]),
                    "P&L": format_money(net_pnl),
                    "P&L %": format_pct(net_pnl_pct),
                })

        def _color_column(cell_value: str) -> str:
            """Color positive values green and negative values red.

            Strips formatting (spaces, €, %) to parse the raw number.

            Args:
                cell_value: A formatted display string.

            Returns:
                CSS color rule string or ``""`` if unparseable.
            """
            if cell_value == "" or cell_value is None:
                return ""
            try:
                raw_str = (
                    cell_value.replace(" ", "").replace("\u20ac", "")
                    .replace("%", "").replace("+", "")
                )
                num = float(raw_str)
                return (
                    "color: green" if num > 0
                    else ("color: red" if num < 0 else "")
                )
            except (ValueError, AttributeError):
                return ""

        def _color_type(val: str) -> str:
            if val == "Bond":
                return "color: green; font-weight: 500;"
            if val == "Stock":
                return "color: #0066cc; font-weight: 500;"
            return ""

        # Active holdings table
        active_df = pd.DataFrame(active_rows)
        if not active_df.empty:
            # Sort: Bond group first, then Stock, then Other; alphabetical within
            type_order = {"Bond": 0, "Stock": 1, "Other": 2}
            active_df["_sort"] = active_df["Type"].map(type_order)
            active_df = active_df.sort_values(["_sort", "Name"]).drop(columns=["_sort"])

            pnl_cols = ["Daily P&L %", "Month P&L %", "Net P&L", "Net P&L %"]
            styled = active_df.style \
                .map(_color_column, subset=pnl_cols) \
                .map(_color_type, subset=["Type"]) \
                .set_properties(
                    **{"text-align": "right"},
                    subset=["Shares", "Market Value"] + pnl_cols,
                ) \
                .set_properties(
                    **{"text-align": "left"},
                    subset=["Type", "Name", "ISIN"],
                )
            st.dataframe(styled, hide_index=True)
        elif not closed_rows:
            st.info("Select at least one asset.")

        # Allocation breakdown by asset type
        if active_sel:
            bonds_value = sum(
                portfolio.at[i, "shares_held"] * portfolio.at[i, "price"]
                for i in active_sel
                if portfolio.at[i, "type"] == "Bond" and portfolio.at[i, "price"] is not None
            )
            stocks_value = sum(
                portfolio.at[i, "shares_held"] * portfolio.at[i, "price"]
                for i in active_sel
                if portfolio.at[i, "type"] == "Stock" and portfolio.at[i, "price"] is not None
            )
            bonds_pct = (bonds_value / latest_value * 100) if latest_value else 0.0
            stocks_pct = (stocks_value / latest_value * 100) if latest_value else 0.0
        else:
            bonds_pct = stocks_pct = 0.0

        col_b, col_s, _,_ = st.columns(4)
        with col_b:
            st.markdown(
                f"<span style='color:green;font-size:1.3rem;font-weight:500'>"
                f"Bonds: </span>"
                f"<span style='color:black;font-size:1.3rem;'>"
                f"{bonds_pct:.1f}%</span>",
                unsafe_allow_html=True,
            )
        with col_s:
            st.markdown(
                f"<span style='color:#0066cc;font-size:1.3rem;font-weight:500'>"
                f"Stocks: </span>"
                f"<span style='color:black;font-size:1.3rem;'>"
                f"{stocks_pct:.1f}%</span>",
                unsafe_allow_html=True,
            )

        # Closed positions table
        if closed_rows:
            st.markdown("### Closed positions")
            closed_df = pd.DataFrame(closed_rows)
            # Sort same as active
            type_order = {"Bond": 0, "Stock": 1, "Other": 2}
            closed_df["_sort"] = closed_df["Type"].map(type_order)
            closed_df = closed_df.sort_values(["_sort", "Name"]).drop(columns=["_sort"])

            styled_closed = closed_df.style \
                .map(_color_column, subset=["P&L", "P&L %"]) \
                .map(_color_type, subset=["Type"]) \
                .set_properties(
                    **{"text-align": "right"},
                    subset=["Total cost", "Sold", "P&L", "P&L %"],
                ) \
                .set_properties(
                    **{"text-align": "left"},
                    subset=["Type", "Name", "ISIN"],
                )
            st.dataframe(styled_closed, hide_index=True)

    # ===== TAB 1: Assets — single-asset line chart =====
    with tabs[1]:
        ylabel = {
            "Value": "Value (\u20ac)", "P&L": "P&L (\u20ac)", "P&L %": "P&L (%)",
        }[metric]
        show_total = st.session_state.get("show_total_assets", True)

        fig = go.Figure()
        for isin in selected_isins:
            if isin not in hist_filtered.columns:
                continue
            fig.add_trace(go.Scatter(
                x=hist_filtered.index, y=_compute_metric_values(isin),
                mode="lines", name=portfolio.at[isin, "name"] if isin in portfolio.index else isin,
                hovertemplate="%{y:,.2f}",
            ))
        # Overlay a total line (dashed black) if requested
        if selected_isins and show_total:
            sel_isins = [i for i in selected_isins if i in hist_filtered.columns]
            if sel_isins:
                if metric == "Value":
                    total_vals = hist_filtered[sel_isins].sum(axis=1)
                elif metric == "P&L":
                    total_vals = (
                        hist_filtered[sel_isins].sum(axis=1)
                        - cost_filtered[sel_isins].sum(axis=1)
                        + realized_filtered[sel_isins].sum(axis=1)
                    )
                else:
                    total_paid = sum(portfolio.at[i, "money_paid"] for i in sel_isins)
                    total_vals = (
                        hist_filtered[sel_isins].sum(axis=1)
                        - cost_filtered[sel_isins].sum(axis=1)
                        + realized_filtered[sel_isins].sum(axis=1)
                    )
                    if total_paid:
                        total_vals = total_vals / total_paid * 100
                    else:
                        total_vals = pd.Series(float("nan"), index=total_vals.index)
                fig.add_trace(go.Scatter(
                    x=hist_filtered.index, y=total_vals, mode="lines",
                    name="Total", line=dict(width=3, dash="dot", color="black"),
                    hovertemplate="%{y:,.2f}",
                ))
        fig.update_layout(
            yaxis_title=ylabel, margin=dict(l=0, r=0, t=10, b=0), height=400,
        )
        if st.session_state.metric == "P&L %":
            fig.update_layout(yaxis=dict(autorange=True))
        st.plotly_chart(fig, width="stretch")

        st.radio("Metric", ["Value", "P&L", "P&L %"], key="metric_assets")
        st.checkbox("Show Total", value=True, key="show_total_assets")

    # ===== TAB 2: Allocation — stacked area =====
    with tabs[2]:
        if selected_isins:
            sel_isins = [i for i in selected_isins if i in hist_filtered.columns]
            if sel_isins:
                metric_mode = st.session_state.metric
                if metric_mode == "P&L":
                    alloc_df = (
                        hist_filtered[sel_isins] - cost_filtered[sel_isins]
                        + realized_filtered[sel_isins]
                    ).copy()
                    alloc_df.columns = [
                        portfolio.at[i, "name"] if i in portfolio.index else i
                        for i in sel_isins
                    ]
                    y_label = "P&L (\u20ac)"
                else:
                    alloc_df = hist_filtered[sel_isins].copy()
                    alloc_df.columns = [
                        portfolio.at[i, "name"] if i in portfolio.index else i
                        for i in sel_isins
                    ]
                    y_label = "Value (\u20ac)"
                fig = px.area(
                    alloc_df, x=alloc_df.index, y=alloc_df.columns,
                    labels={"x": "", "value": y_label, "variable": "Asset"},
                    color_discrete_sequence=px.colors.qualitative.Plotly,
                )
                fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=400)
                st.plotly_chart(fig, width="stretch")
            st.radio("Metric", ["Value", "P&L"], key="metric_allocation")
        else:
            st.info("Select at least one asset.")

    # ===== TAB 3: By Type — stacked area grouped by Stock/Bond/Other =====
    with tabs[3]:
        if selected_isins:
            # Group selected ISINs by asset type
            type_isins = {}
            for isin in selected_isins:
                if isin in hist_filtered.columns:
                    t = portfolio.at[isin, "type"] if isin in portfolio.index else "Other"
                    type_isins.setdefault(t, []).append(isin)

            if type_isins:
                metric_mode = st.session_state.metric
                type_data = pd.DataFrame(index=hist_filtered.index)
                if metric_mode == "P&L %":
                    # Each type's absolute P&L contribution to total absolute P&L (%).
                    # Using absolute values keeps all percentages in [0, 100]
                    # so the stacked area chart sums to 100 % at every date.
                    pnl_by_type = pd.DataFrame(index=hist_filtered.index)
                    for atype, isins in type_isins.items():
                        vals = hist_filtered[isins].sum(axis=1)
                        costs = cost_filtered[isins].sum(axis=1)
                        realized = realized_filtered[isins].sum(axis=1)
                        pnl_by_type[atype] = vals - costs + realized
                    abs_pnl = pnl_by_type.abs()
                    total_abs = abs_pnl.sum(axis=1)
                    total_abs = total_abs.where(total_abs > 1e-9, float("nan"))
                    type_data = abs_pnl.div(total_abs, axis=0) * 100
                    y_label = "P&L contribution (%)"
                elif metric_mode == "P&L":
                    for atype, isins in type_isins.items():
                        type_data[atype] = (
                            hist_filtered[isins] - cost_filtered[isins]
                            + realized_filtered[isins]
                        ).sum(axis=1)
                    y_label = "P&L (\u20ac)"
                else:
                    for atype, isins in type_isins.items():
                        type_data[atype] = hist_filtered[isins].sum(axis=1)
                    y_label = "Value (\u20ac)"

                fig = px.area(
                    type_data, x=type_data.index, y=type_data.columns,
                    labels={"x": "", "value": y_label, "variable": "Type"},
                    color_discrete_sequence=px.colors.qualitative.Plotly,
                )
                fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=400)
                st.plotly_chart(fig, width="stretch")
            st.radio("Metric", ["Value", "P&L", "P&L %"], key="metric_by_type")
        else:
            st.info("Select at least one asset.")

    # ===== TAB 4: Monthly Returns — year × month heatmap-style grid =====
    with tabs[4]:
        if selected_isins:
            sel_isins = [i for i in selected_isins if i in hist_df.columns]
            if not sel_isins:
                st.info("Select at least one asset.")
            else:
                # Total portfolio value at all dates; trim leading zeros
                total_values = hist_df[sel_isins].sum(axis=1)
                if (total_values > 0).any():
                    total_values = total_values.loc[(total_values > 0).idxmax():]
                month_start_vals = total_values[total_values.index.day == 1]

                # Prepare order data (needed for both regular and synthetic returns)
                orders_m = orders.assign(
                    month_key=orders["date"].dt.to_period("M"),
                    is_first=orders["date"].dt.day == 1,
                )
                sel_orders = orders_m[orders_m["isin"].isin(selected_isins)]

                # Compute regular month-over-month returns
                if len(month_start_vals) >= 2:
                    # Exclude orders on the 1st of the month: they are already
                    # reflected in month_start_vals for that same month.
                    net_invest = sel_orders[~sel_orders["is_first"]] \
                        .groupby("month_key")["amount"].sum()
                    
                    prev = month_start_vals.iloc[:-1]
                    curr = month_start_vals.iloc[1:]
                    prev_periods = prev.index.to_period("M")
                    cash = pd.Series(
                        [net_invest.get(p, 0.0) for p in prev_periods],
                        index=prev.index,
                    )
                    adj_prev = prev + cash
                    safe_adj = adj_prev.replace(0, float("nan"))
                    return_pct = pd.Series(
                        (curr.values - safe_adj.values) / safe_adj.values * 100,
                        index=prev.index,
                    ).fillna(0.0)

                    return_abs = pd.Series(
                        (curr.values - safe_adj.values) ,
                        index=prev.index,
                    ).fillna(0.0)                    

                    return_abs = pd.Series(
                        (curr.values - safe_adj.values),
                        index=prev.index,
                    ).fillna(0.0)
                else:
                    return_pct = pd.Series(dtype=float)
                    return_abs = pd.Series(dtype=float)

                # Add synthetic first-month return when the first order month
                # has no month-start entry (portfolio started at zero).
                if len(month_start_vals) > 0 and len(sel_orders) > 0:
                    first_order_month = sel_orders["date"].min().to_period("M")
                    first_ms_month = month_start_vals.index[0].to_period("M")
                    if first_ms_month > first_order_month:
                        # Starting value was zero; include ALL orders in the
                        # first month (even those on the 1st).
                        first_month_cash = sel_orders[
                            sel_orders["month_key"] == first_order_month
                        ]["amount"].sum()
                        first_month_value = month_start_vals.iloc[0]
                        if first_month_cash != 0:
                            first_return_pct = (
                                (first_month_value - first_month_cash)
                                / first_month_cash * 100
                            )
                            first_return_abs = (
                                first_month_value - first_month_cash
                            )
                            synthetic_date = first_order_month.to_timestamp()
                            synthetic_pct = pd.Series(
                                [first_return_pct], index=[synthetic_date]
                            )
                            synthetic_abs = pd.Series(
                                [first_return_abs], index=[synthetic_date]
                            )
                            return_pct = pd.concat([synthetic_pct, return_pct])
                            return_abs = pd.concat([synthetic_abs, return_abs])

                if len(return_pct) == 0:
                    st.info("Not enough data for monthly returns.")
                else:
                    # Pivot into year × month grid
                    month_abbr = [
                        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
                    ]
                    returns = pd.DataFrame({
                        "year": return_pct.index.year,
                        "month": return_pct.index.month,
                        "return_pct": return_pct.values,
                    })
                    pivot = returns.pivot(
                        index="year", columns="month", values="return_pct",
                    )
                    pivot.columns = [month_abbr[m - 1] for m in pivot.columns]

                    # YTD return: same cash-flow-adjusted formula as monthly but
                    # aggregated by year.  Year-start = earliest day-1 value in
                    # each year.  Year-end = next year's start (or last available
                    # month-start for the current partial year).  Cash excludes
                    # orders on or before the year_start date — those purchases
                    # are already reflected in year_start via cumulative shares.
                    year_starts = month_start_vals.groupby(
                        month_start_vals.index.year
                    ).first()
                    if len(year_starts) > 0:
                        # Actual date of the first day-1 value per year
                        year_start_dates = month_start_vals.reset_index() \
                            .groupby(month_start_vals.index.year)["index"] \
                            .first()
                        year_prev = year_starts
                        year_curr = year_starts.shift(-1)
                        # Last (partial) year: use most recent month-start
                        year_curr.iloc[-1] = month_start_vals.iloc[-1]
                        last_ms_date = month_start_vals.index[-1]
                        # Filter: exclude orders <= year_start_date (dup) and
                        # orders beyond the last month-start
                        s = sel_orders.copy()
                        s["year_start"] = s["date"].dt.year.map(
                            year_start_dates
                        )
                        yearly_sel = s[
                            (s["date"] > s["year_start"])
                            & (s["date"] <= last_ms_date)
                        ]
                        yearly_cash = yearly_sel.groupby(
                            yearly_sel["date"].dt.year
                        )["amount"].sum()
                        year_cash = pd.Series(
                            [yearly_cash.get(y, 0.0) for y in year_prev.index],
                            index=year_prev.index,
                            dtype=float,
                        )
                        year_adj_prev = year_prev.reset_index(drop=True) \
                            + year_cash.reset_index(drop=True)
                        year_safe_adj = year_adj_prev.replace(0, float("nan"))
                        ytd = pd.Series(
                            (year_curr.values - year_safe_adj.values)
                            / year_safe_adj.values * 100,
                            index=year_prev.index,
                        ).fillna(0.0)
                    else:
                        ytd = pd.Series(dtype=float)

                    pivot["Year Total"] = ytd.reindex(pivot.index)

                    pivot = pivot.sort_index(ascending=False)

                    # Format cells as percentage strings for display
                    display = pivot.map(
                        lambda x: format_pct(x) if pd.notna(x) else ""
                    )
                    display.index = display.index.astype(str)
                    display.index.name = None

                    def _color_cell(cell: str) -> str:
                        """Color positive cells dark-green and negative cells red.

                        Args:
                            cell: A formatted return string (e.g. ``"3.5%"``).

                        Returns:
                            CSS color rule or ``""``.
                        """
                        if not cell or cell == "":
                            return ""
                        try:
                            raw = cell.replace("%", "").replace("+", "").strip()
                            num = float(raw)
                            if num > 0:
                                return "color: #008000;"
                            elif num < 0:
                                return "color: #cc0000;"
                            return ""
                        except ValueError:
                            return ""

                    styled = display.style.map(_color_cell) \
                        .set_properties(**{"text-align": "center"}) \
                        .set_table_styles([
                            {
                                "selector": "th, tr",
                                "props": [
                                    ("font-weight", "normal"), ("border", "none"),
                                ],
                            },
                            {
                                "selector": "td",
                                "props": [
                                    ("border-width", "1px 0 1px 0"),
                                    ("border-color", "#BBBBBB"),
                                ],
                            },
                            {
                                "selector": "th:last-child",
                                "props": [("font-weight", "550")],
                            },
                            {
                                "selector": "td:last-child",
                                "props": [
                                    ("background-color", "#F7F7F7"),
                                    ("font-weight", "550"),
                                ],
                            },
                        ])

                    st.markdown(styled.to_html(), unsafe_allow_html=True)
        else:
            st.info("Select at least one asset.")


if __name__ == "__main__":
    render()
