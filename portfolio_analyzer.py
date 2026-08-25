"""
Fidelity portfolio analyzer.

Reads a "Portfolio_Positions_*.csv" export from Fidelity and produces:
  - a console + text summary (overview, allocation, concentration, performance,
    tax-location, observations)
  - 4 charts (asset allocation, sector breakdown, account distribution, top holdings)
  - a cleaned/enriched CSV of every position

All output is written locally next to the input file — nothing here calls out
to the network or any third-party API. Sector/asset-class classification is a
hand-maintained lookup table (SECURITY_INFO below) rather than a live data
lookup, so it stays reproducible across reruns with no API key required.

Usage:
    python portfolio_analyzer.py                      # picks the newest
                                                        # fidelity_data/Portfolio_Positions_*.csv
    python portfolio_analyzer.py path/to/export.csv    # or point at one explicitly

When you export a new CSV from Fidelity and rerun this, any symbol not yet in
SECURITY_INFO prints a warning with its description — add it to the table
(asset_class, sector) and rerun.
"""
import argparse
import csv
import glob
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

# --------------------------------------------------------------------------
# Palette (validated categorical order, light chart surface) — see the
# dataviz skill: fixed hue order, never cycled or reassigned per-filter.
# --------------------------------------------------------------------------
CAT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
SURFACE = "#fcfcfb"
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRIDLINE = "#e1e0d9"
BASELINE = "#c3c2b7"

def _plotting():
    """Import Matplotlib only when charts are rendered, not for CLI help/imports."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker

    if not getattr(_plotting, "configured", False):
        plt.rcParams.update({
            "figure.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
            "text.color": INK_PRIMARY,
            "axes.edgecolor": BASELINE,
            "axes.labelcolor": INK_SECONDARY,
            "xtick.color": INK_MUTED,
            "ytick.color": INK_MUTED,
            "font.family": "sans-serif",
            "font.size": 10,
        })
        _plotting.configured = True
    return plt, mticker

# --------------------------------------------------------------------------
# Security classification — (asset_class, sector).
#
# Sector uses a coarse taxonomy on purpose: individual stocks get a real
# GICS-style sector, but broad/multi-sector funds (S&P 500 trackers, total
# market, dividend, factor, ESG, growth ETFs) are bucketed as "Diversified /
# Multi-Sector Funds" rather than guessed at, because look-through holdings
# data isn't available offline. Style tags used for overlap detection live
# separately in OVERLAP_GROUPS below.
# --------------------------------------------------------------------------
SECURITY_INFO = {
    "SPAXX**": ("Money Market", "Cash"),
    "FDRXX**": ("Money Market", "Cash"),
    "NVDA":    ("Stock", "Technology"),
    "GOOGL":   ("Stock", "Communication Services"),
    "AMD":     ("Stock", "Technology"),
    "SWPPX":   ("Mutual Fund", "Diversified / Multi-Sector Funds"),
    "VYM":     ("ETF", "Diversified / Multi-Sector Funds"),
    "SPY":     ("ETF", "Diversified / Multi-Sector Funds"),
    "VTI":     ("ETF", "Diversified / Multi-Sector Funds"),
    "VOO":     ("ETF", "Diversified / Multi-Sector Funds"),
    "FNILX":   ("Mutual Fund", "Diversified / Multi-Sector Funds"),
    "WMT":     ("Stock", "Consumer Staples"),
    "AAPL":    ("Stock", "Technology"),
    "QQQ":     ("ETF", "Diversified / Multi-Sector Funds"),
    "CVNA":    ("Stock", "Consumer Discretionary"),
    "AMZN":    ("Stock", "Consumer Discretionary"),
    "MSFT":    ("Stock", "Technology"),
    "META":    ("Stock", "Communication Services"),
    "TSLA":    ("Stock", "Consumer Discretionary"),
    "NFLX":    ("Stock", "Communication Services"),
    "APP":     ("Stock", "Technology"),
    "FXAIX":   ("Mutual Fund", "Diversified / Multi-Sector Funds"),
    "WSHFX":   ("Mutual Fund", "Diversified / Multi-Sector Funds"),
    # CVSA appears in the Fidelity export under two different descriptions
    # ("COVISTA INC. COMMON SHARES" in the taxable account, "ADTALEM GLOBAL
    # EDU" in the 401k plan) with IDENTICAL price/day-change in both rows —
    # Fidelity treats this as one security, likely closely-held employer
    # stock (Covista Inc. sponsors the 401k plan this is also held in), with
    # a stale label on the 401k side. Sector unknown — not a public company
    # I have reliable data on. See NO_QUOTE_SYMBOLS in market_check.py.
    "CVSA":    ("Stock", "Unknown — possible employer/closely-held stock"),
    "DHS":     ("ETF", "Diversified / Multi-Sector Funds"),
    "DIV":     ("ETF", "Diversified / Multi-Sector Funds"),
    "FDMO":    ("ETF", "Diversified / Multi-Sector Funds"),
    "MGK":     ("ETF", "Diversified / Multi-Sector Funds"),
    "PRF":     ("ETF", "Diversified / Multi-Sector Funds"),
    "SCHD":    ("ETF", "Diversified / Multi-Sector Funds"),
    "SMH":     ("ETF", "Technology"),                 # concentrated semiconductor ETF
    "SPHD":    ("ETF", "Diversified / Multi-Sector Funds"),
    "SPYD":    ("ETF", "Diversified / Multi-Sector Funds"),
    "USXF":    ("ETF", "Diversified / Multi-Sector Funds"),
    "VGT":     ("ETF", "Technology"),                 # concentrated tech-sector ETF
    "VSCAX":   ("Mutual Fund", "Diversified / Multi-Sector Funds"),
    "VUG":     ("ETF", "Diversified / Multi-Sector Funds"),
    "XSHD":    ("ETF", "Diversified / Multi-Sector Funds"),   # Invesco S&P SmallCap High Div Low Vol
    "31564E540": ("Target-Date Fund", "Mixed / Target-Date"),  # Fidelity Freedom Index 2065
    "31565A760": ("Target-Date Fund", "Mixed / Target-Date"),  # Fidelity Freedom Index 2045
}

# Sectors that are inherently diversified — excluded from the ">30% sector"
# concentration flag since a high weight there isn't single-industry risk.
DIVERSIFIED_SECTORS = {"Diversified / Multi-Sector Funds", "Mixed / Target-Date", "Cash"}

# Overlap groups for "do I hold two funds doing the same job" detection.
OVERLAP_GROUPS = {
    "S&P 500 / US Large-Cap Core": ["VOO", "SPY", "SWPPX", "FNILX", "FXAIX"],
    "US Total Market": ["VTI"],
    "Large-Cap Growth / Tech-Tilted": ["QQQ", "MGK", "VUG"],
    "High-Dividend / Value": ["VYM", "SCHD", "DHS", "DIV", "SPHD", "SPYD", "XSHD", "WSHFX"],
    "Technology Sector": ["VGT", "SMH"],
    "Small-Cap": ["VSCAX"],
    "Factor / Fundamental / ESG": ["FDMO", "PRF", "USXF"],
    "Target-Date": ["31564E540", "31565A760"],
}

# Short display names for symbols whose raw ticker/CUSIP isn't self-explanatory.
FRIENDLY_NAME = {
    "SPAXX**": "SPAXX (Money Mkt)",
    "FDRXX**": "FDRXX (Money Mkt)",
    "31564E540": "FID Freedom Idx 2065",
    "31565A760": "FID Freedom Idx 2045",
}


def friendly(symbol: str) -> str:
    return FRIENDLY_NAME.get(symbol, symbol)


# High-income / high-turnover-style funds — flagged when held in a taxable
# account, since dividends/turnover there create yearly taxable events that
# tax-advantaged accounts would shelter.
HIGH_INCOME_STRATEGY = {"VYM", "SCHD", "DHS", "DIV", "SPHD", "SPYD", "XSHD", "WSHFX"}


# --------------------------------------------------------------------------
# Loading & cleaning
# --------------------------------------------------------------------------
def find_latest_csv(folder: Path) -> Path:
    candidates = sorted(glob.glob(str(folder / "Portfolio_Positions_*.csv")))
    if not candidates:
        sys.exit(f"No Portfolio_Positions_*.csv found in {folder}")
    return Path(max(candidates, key=os.path.getmtime))


def clean_money(val: str) -> float:
    if val is None:
        return float("nan")
    v = val.strip().replace("$", "").replace(",", "").replace("%", "")
    if not v:
        return float("nan")
    neg = v.startswith("(") and v.endswith(")")
    v = v.strip("()").lstrip("+")
    try:
        num = float(v)
    except ValueError:
        return float("nan")
    return -num if neg else num


HEADER_LEN = 16


def load_raw_rows(path: Path) -> tuple[list[str], list[list[str]]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    header = rows[0]
    data_rows = []
    for r in rows[1:]:
        if len(r) < HEADER_LEN:
            continue                       # footer disclaimer / blank lines
        if not r[0].strip() or not r[2].strip():
            continue
        if r[2].strip() == "Pending activity":
            continue                       # account-level pending cash, not a position
        data_rows.append(r)
    return header, data_rows


def classify_symbol(symbol: str, description: str) -> tuple[str, str, bool]:
    """Returns (asset_class, sector, was_unknown)."""
    if symbol in SECURITY_INFO:
        ac, sec = SECURITY_INFO[symbol]
        return ac, sec, False
    if symbol.endswith("**"):
        return "Money Market", "Cash", False
    if re.fullmatch(r"[0-9A-Z]{8,9}", symbol) and any(c.isdigit() for c in symbol):
        return "Fund (CUSIP, unclassified)", "Unknown", True
    return "Unknown", "Unknown", True


def build_dataframe(header: list[str], rows: list[list[str]]) -> pd.DataFrame:
    idx = {name: i for i, name in enumerate(header)}
    records = []
    unknowns = {}
    for r in rows:
        symbol = r[idx["Symbol"]].strip()
        description = r[idx["Description"]].strip()
        asset_class, sector, unknown = classify_symbol(symbol, description)
        if unknown:
            unknowns[symbol] = description
        records.append({
            "account_number": r[idx["Account number"]].strip(),
            "account_name": r[idx["Account name"]].strip(),
            "symbol": symbol,
            "description": description,
            "quantity": clean_money(r[idx["Quantity"]]),
            "last_price": clean_money(r[idx["Last price"]]),
            "current_value": clean_money(r[idx["Current value"]]),
            "total_gain_loss_dollar": clean_money(r[idx["Total gain/loss dollar"]]),
            "total_gain_loss_percent": clean_money(r[idx["Total gain/loss percent"]]),
            "cost_basis_total": clean_money(r[idx["Cost basis total"]]),
            "average_cost_basis": clean_money(r[idx["Average cost basis"]]),
            "asset_class": asset_class,
            "sector": sector,
        })
    df = pd.DataFrame(records)
    df["current_value"] = df["current_value"].fillna(0.0)
    total = df["current_value"].sum()
    df["pct_of_portfolio"] = df["current_value"] / total * 100 if total else 0.0

    if unknowns:
        print("\n⚠️  Unclassified symbols — add these to SECURITY_INFO in portfolio_analyzer.py:")
        for sym, desc in unknowns.items():
            print(f"    {sym!r}: (\"Stock|ETF|Mutual Fund|Money Market\", \"Sector\"),  # {desc}")
        print()

    return df


def account_tax_status(account_name: str) -> str:
    name = account_name.upper()
    if "ROTH" in name:
        return "Roth IRA (Tax-Advantaged)"
    if "IRA" in name:
        return "Traditional/Rollover IRA (Tax-Advantaged)"
    if "HEALTH SAVINGS" in name or "HSA" in name:
        return "HSA (Tax-Advantaged)"
    if "RETIREMENT PLAN" in name or "401" in name:
        return "401(k) / Employer Plan (Tax-Advantaged)"
    if "TOD" in name or "INDIVIDUAL" in name or "BROKERAGE" in name:
        return "Taxable Brokerage"
    return "Unknown — classify in account_tax_status()"


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------
def section(title: str) -> str:
    return f"\n{'=' * 78}\n{title}\n{'=' * 78}\n"


def fmt_money(x: float) -> str:
    return f"${x:,.2f}"


def overview(df: pd.DataFrame) -> str:
    out = [section("1. PORTFOLIO OVERVIEW")]
    total = df["current_value"].sum()
    out.append(f"Total portfolio value: {fmt_money(total)}")
    out.append(f"Unique holdings (by symbol): {df['symbol'].nunique()}")
    out.append(f"Accounts: {df['account_number'].nunique()}\n")

    by_account = (
        df.assign(tax_status=df["account_name"].apply(account_tax_status))
        .groupby(["account_name", "account_number", "tax_status"], as_index=False)["current_value"]
        .sum()
        .sort_values("current_value", ascending=False)
    )
    out.append(f"{'Account':38s} {'Tax Status':38s} {'Value':>14s}   % of Total")
    for _, r in by_account.iterrows():
        label = f"{r['account_name']} (…{r['account_number'][-4:]})"
        pct = r["current_value"] / total * 100 if total else 0
        out.append(f"{label:38s} {r['tax_status']:38s} {fmt_money(r['current_value']):>14s}   {pct:5.1f}%")
    return "\n".join(out)


def allocation_section(df: pd.DataFrame) -> tuple[str, pd.Series, pd.Series]:
    total = df["current_value"].sum()
    by_class = df.groupby("asset_class")["current_value"].sum().sort_values(ascending=False)
    by_sector = df.groupby("sector")["current_value"].sum().sort_values(ascending=False)

    out = [section("2. ASSET ALLOCATION")]
    out.append("By asset class:")
    for k, v in by_class.items():
        out.append(f"  {k:22s} {fmt_money(v):>14s}   {v/total*100:5.1f}%")
    out.append("\nBy sector:")
    for k, v in by_sector.items():
        out.append(f"  {k:34s} {fmt_money(v):>14s}   {v/total*100:5.1f}%")
    return "\n".join(out), by_class, by_sector


def concentration_section(df: pd.DataFrame) -> str:
    total = df["current_value"].sum()
    out = [section("3. CONCENTRATION RISK")]

    by_symbol = (
        df.groupby(["symbol", "description"], as_index=False)["current_value"].sum()
        .sort_values("current_value", ascending=False)
    )
    by_symbol["pct"] = by_symbol["current_value"] / total * 100
    flagged = by_symbol[by_symbol["pct"] > 10]
    out.append("Single positions > 10% of total portfolio:")
    if flagged.empty:
        out.append("  None.")
    else:
        for _, r in flagged.iterrows():
            out.append(f"  {friendly(r['symbol']):22s} {r['description'][:40]:40s} {fmt_money(r['current_value']):>12s}  {r['pct']:5.1f}%")

    by_sector = df.groupby("sector")["current_value"].sum()
    out.append("\nSectors > 30% of total portfolio (excludes diversified/cash/target-date buckets):")
    sector_flagged = False
    for sec, v in by_sector.sort_values(ascending=False).items():
        pct = v / total * 100
        if sec in DIVERSIFIED_SECTORS:
            continue
        if pct > 30:
            sector_flagged = True
            out.append(f"  {sec:34s} {fmt_money(v):>14s}   {pct:5.1f}%")
    if not sector_flagged:
        out.append("  None.")

    out.append("\nOverlapping funds (same style/objective held via multiple tickers):")
    by_symbol_val = df.groupby("symbol")["current_value"].sum()
    any_overlap = False
    for group_name, symbols in OVERLAP_GROUPS.items():
        held = [s for s in symbols if s in by_symbol_val.index]
        if len(held) > 1:
            any_overlap = True
            group_total = sum(by_symbol_val[s] for s in held)
            out.append(f"  {group_name}: {fmt_money(group_total)} ({group_total/total*100:.1f}%) across {', '.join(held)}")
    if not any_overlap:
        out.append("  None detected.")

    return "\n".join(out)


def performance_section(df: pd.DataFrame) -> str:
    out = [section("4. PERFORMANCE")]
    priced = df[df["cost_basis_total"].notna() & (df["cost_basis_total"] != 0)].copy()

    total_cost = priced["cost_basis_total"].sum()
    total_gain = priced["total_gain_loss_dollar"].sum()
    overall_return = total_gain / total_cost * 100 if total_cost else float("nan")
    out.append(f"Overall return (positions with cost-basis data, excludes cash): "
               f"{fmt_money(total_gain)} on {fmt_money(total_cost)} cost basis = {overall_return:+.1f}%\n")

    winners = priced.sort_values("total_gain_loss_dollar", ascending=False).head(5)
    losers = priced.sort_values("total_gain_loss_dollar", ascending=True).head(5)

    out.append("Top 5 winners (by $ gain):")
    for _, r in winners.iterrows():
        out.append(f"  {friendly(r['symbol']):22s} {fmt_money(r['total_gain_loss_dollar']):>12s}  ({r['total_gain_loss_percent']:+.1f}%)  [{r['account_name']}]")

    out.append("\nTop 5 losers (by $ loss):")
    for _, r in losers.iterrows():
        out.append(f"  {friendly(r['symbol']):22s} {fmt_money(r['total_gain_loss_dollar']):>12s}  ({r['total_gain_loss_percent']:+.1f}%)  [{r['account_name']}]")

    return "\n".join(out)


def tax_location_section(df: pd.DataFrame) -> str:
    out = [section("5. TAX-LOCATION CHECK")]
    d = df.copy()
    d["tax_status"] = d["account_name"].apply(account_tax_status)
    taxable = d[d["tax_status"] == "Taxable Brokerage"]

    flagged = taxable[taxable["symbol"].isin(HIGH_INCOME_STRATEGY)]
    out.append("High-dividend / high-income-style funds held in TAXABLE accounts")
    out.append("(these throw off ordinary taxable income yearly — usually more efficient in a Roth/IRA/HSA):")
    if flagged.empty:
        out.append("  None found.")
    else:
        for _, r in flagged.iterrows():
            out.append(f"  {r['symbol']:10s} {r['description'][:45]:45s} {fmt_money(r['current_value']):>12s}  [{r['account_name']}]")

    no_bonds = not any(df["description"].str.contains("BOND", case=False, na=False))
    no_reits = not any(df["description"].str.contains("REIT|REAL ESTATE", case=False, na=False, regex=True))
    if no_bonds:
        out.append("\nNo bond funds detected in this export.")
    if no_reits:
        out.append("No REIT funds detected in this export.")

    return "\n".join(out)


def observations_section(df: pd.DataFrame, by_class: pd.Series, by_sector: pd.Series) -> str:
    total = df["current_value"].sum()
    out = [section("6. OBSERVATIONS (not advice — just what the data shows)")]

    top_sector = by_sector.drop(labels=[s for s in DIVERSIFIED_SECTORS if s in by_sector.index], errors="ignore")
    if not top_sector.empty:
        name, val = top_sector.index[0], top_sector.iloc[0]
        out.append(f"- Largest single-industry sector exposure is {name} at {val/total*100:.1f}% of the portfolio "
                    f"(this is on top of whatever tech/communications weight is embedded inside the "
                    f"broad-market and dividend funds, which isn't broken out here).")

    diversified_pct = sum(by_class.get(k, 0) for k in ["ETF", "Mutual Fund"]) / total * 100
    out.append(f"- {diversified_pct:.0f}% of the portfolio sits in ETFs/mutual funds vs. individual stocks — "
                f"the concentration numbers above are somewhat softened by that, since fund positions spread "
                f"risk across many underlying companies even when the fund itself is a large % of the account.")

    n_taxable_accounts = df[df["account_name"].apply(account_tax_status) == "Taxable Brokerage"]["account_number"].nunique()
    if n_taxable_accounts:
        out.append(f"- {n_taxable_accounts} taxable brokerage account(s) present — see the tax-location section above "
                    f"for anything that might be more efficient sheltered in the Roth/IRA/HSA instead.")

    out.append("- This is a mechanical read of the export only — it doesn't know your goals, time horizon, "
                "risk tolerance, or anything outside this CSV. Treat it as a prompt for your own thinking, not a plan.")

    return "\n".join(out)


# --------------------------------------------------------------------------
# Charts
# --------------------------------------------------------------------------
def _style_ax(ax):
    ax.set_facecolor(SURFACE)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_visible(False)
    ax.spines["bottom"].set_color(BASELINE)
    ax.xaxis.grid(True, color=GRIDLINE, linewidth=0.8)
    ax.set_axisbelow(True)


def chart_allocation_bar(series: pd.Series, total: float, title: str, out_path: Path):
    plt, mticker = _plotting()
    series = series.sort_values(ascending=True)
    fig, ax = plt.subplots(figsize=(8, max(2.5, 0.5 * len(series) + 1)))
    colors = (CAT * ((len(series) // len(CAT)) + 1))[:len(series)][::-1]
    bars = ax.barh(series.index, series.values, color=colors, height=0.6)
    for bar, val in zip(bars, series.values):
        pct = val / total * 100
        ax.text(bar.get_width() + total * 0.01, bar.get_y() + bar.get_height() / 2,
                 f"{fmt_money(val)}  ({pct:.1f}%)", va="center", fontsize=9, color=INK_PRIMARY)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"${x/1000:.0f}k"))
    ax.set_xlim(0, series.values.max() * 1.35)
    ax.set_title(title, fontsize=12, color=INK_PRIMARY, loc="left", pad=12)
    _style_ax(ax)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def chart_account_distribution(df: pd.DataFrame, out_path: Path):
    plt, _ = _plotting()
    d = df.copy()
    d["tax_status"] = d["account_name"].apply(account_tax_status)
    d["label"] = d["account_name"] + " (…" + d["account_number"].str[-4:] + ")"
    by_account = d.groupby(["label", "tax_status"], as_index=False)["current_value"].sum()
    by_account = by_account.sort_values("current_value", ascending=True)

    color_map = {"Taxable Brokerage": CAT[1]}
    colors = [color_map.get(t, CAT[0]) for t in by_account["tax_status"]]

    fig, ax = plt.subplots(figsize=(8, max(2.5, 0.5 * len(by_account) + 1)))
    bars = ax.barh(by_account["label"], by_account["current_value"], color=colors, height=0.6)
    total = df["current_value"].sum()
    for bar, val in zip(bars, by_account["current_value"]):
        ax.text(bar.get_width() + total * 0.01, bar.get_y() + bar.get_height() / 2,
                 fmt_money(val), va="center", fontsize=9, color=INK_PRIMARY)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"${x/1000:.0f}k"))
    ax.set_xlim(0, by_account["current_value"].max() * 1.3)
    ax.set_title("Portfolio Value by Account", fontsize=12, color=INK_PRIMARY, loc="left", pad=12)

    handles = [plt.Rectangle((0, 0), 1, 1, color=CAT[0]), plt.Rectangle((0, 0), 1, 1, color=CAT[1])]
    ax.legend(handles, ["Tax-Advantaged", "Taxable Brokerage"], loc="lower right", frameon=False)
    _style_ax(ax)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def chart_top_holdings(df: pd.DataFrame, out_path: Path, n: int = 10):
    plt, mticker = _plotting()
    by_symbol = df.groupby(["symbol", "description"], as_index=False)["current_value"].sum()
    top = by_symbol.sort_values("current_value", ascending=False).head(n).sort_values("current_value", ascending=True)
    top["label"] = top["symbol"].apply(friendly)
    total = df["current_value"].sum()

    fig, ax = plt.subplots(figsize=(8, max(2.5, 0.4 * len(top) + 1)))
    bars = ax.barh(top["label"], top["current_value"], color=CAT[0], height=0.6)
    for bar, val in zip(bars, top["current_value"]):
        ax.text(bar.get_width() + total * 0.005, bar.get_y() + bar.get_height() / 2,
                 f"{fmt_money(val)}  ({val/total*100:.1f}%)", va="center", fontsize=9, color=INK_PRIMARY)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"${x/1000:.0f}k"))
    ax.set_xlim(0, top["current_value"].max() * 1.35)
    ax.set_title(f"Top {n} Holdings by Value", fontsize=12, color=INK_PRIMARY, loc="left", pad=12)
    _style_ax(ax)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Analyze a Fidelity Portfolio_Positions CSV export.")
    parser.add_argument("csv_path", nargs="?", default=None,
                         help="Path to Portfolio_Positions_*.csv (default: newest in fidelity_data/)")
    parser.add_argument("--out", default=None, help="Output directory (default: fidelity_data/output/)")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent
    data_dir = repo_root / "fidelity_data"
    csv_path = Path(args.csv_path) if args.csv_path else find_latest_csv(data_dir)
    out_dir = Path(args.out) if args.out else data_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    m = re.search(r"Portfolio_Positions_(.+)\.csv", csv_path.name)
    tag = m.group(1) if m else datetime.now().strftime("%Y-%m-%d")

    print(f"Loading {csv_path} ...")
    header, rows = load_raw_rows(csv_path)
    df = build_dataframe(header, rows)

    report_parts = [
        f"Fidelity Portfolio Analysis — {tag}",
        f"Source: {csv_path.name}",
        overview(df),
    ]
    alloc_text, by_class, by_sector = allocation_section(df)
    report_parts.append(alloc_text)
    report_parts.append(concentration_section(df))
    report_parts.append(performance_section(df))
    report_parts.append(tax_location_section(df))
    report_parts.append(observations_section(df, by_class, by_sector))

    report = "\n".join(report_parts) + "\n"
    print(report)

    report_path = out_dir / f"portfolio_report_{tag}.txt"
    report_path.write_text(report)

    csv_out_path = out_dir / f"positions_enriched_{tag}.csv"
    df.to_csv(csv_out_path, index=False)

    total = df["current_value"].sum()
    chart_allocation_bar(by_class, total, "Asset Allocation", out_dir / f"chart_asset_allocation_{tag}.png")
    chart_allocation_bar(by_sector, total, "Sector Breakdown", out_dir / f"chart_sector_breakdown_{tag}.png")
    chart_account_distribution(df, out_dir / f"chart_account_distribution_{tag}.png")
    chart_top_holdings(df, out_dir / f"chart_top_holdings_{tag}.png")

    print(f"\nWrote report:   {report_path}")
    print(f"Wrote data:     {csv_out_path}")
    print(f"Wrote charts:   {out_dir}/chart_*_{tag}.png")


if __name__ == "__main__":
    main()
