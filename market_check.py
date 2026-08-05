"""
Morning portfolio check: marks current holdings to live market prices via
Finnhub, re-runs the same concentration / overlap / tax-location rules from
portfolio_analyzer.py against today's prices, and asks Claude to summarize
what moved and which already-flagged positions are worth a second look.

This is strictly informational. It never places trades, never emits a
"buy X shares" instruction, and every output is explicitly framed as an
observation for you to evaluate — not financial advice. See the system
prompt in build_llm_prompt() for the exact guardrails.

Usage:
    python market_check.py                 # uses the newest enriched CSV from
                                             # fidelity_data/output/ (run
                                             # portfolio_analyzer.py first)
    python market_check.py --positions path/to/positions_enriched_X.csv

Requires FINNHUB_API_KEY and ANTHROPIC_API_KEY in .env.
Designed to be run once a day around market open (see setup_launchd.sh).
"""
import argparse
import glob
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
from dotenv import load_dotenv

from portfolio_analyzer import (
    DIVERSIFIED_SECTORS,
    HIGH_INCOME_STRATEGY,
    OVERLAP_GROUPS,
    account_tax_status,
    fmt_money,
    friendly,
)

FINNHUB_BASE = "https://finnhub.io/api/v1"

# Symbols never sent to Finnhub even if their asset_class would normally
# qualify — e.g. CVSA appears in the Fidelity export as both "COVISTA INC.
# COMMON SHARES" (taxable account) and "ADTALEM GLOBAL EDU" (401k plan) with
# identical price/day-change in both rows, meaning Fidelity treats it as one
# security (likely closely-held employer stock) with a stale description on
# one side. Querying a public ticker "CVSA" on Finnhub risks pulling an
# unrelated company's quote, so this is priced from the CSV only.
NO_QUOTE_SYMBOLS = {"CVSA"}


def find_latest_enriched_csv(data_dir: Path) -> Path:
    candidates = sorted(glob.glob(str(data_dir / "output" / "positions_enriched_*.csv")))
    if not candidates:
        sys.exit(
            f"No positions_enriched_*.csv found in {data_dir / 'output'}.\n"
            f"Run 'python portfolio_analyzer.py' first."
        )
    return Path(max(candidates, key=os.path.getmtime))


def market_is_open(client: httpx.Client, api_key: str) -> tuple[bool, str]:
    try:
        r = client.get(f"{FINNHUB_BASE}/stock/market-status",
                        params={"exchange": "US", "token": api_key}, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("isOpen"):
            return True, "open"
        if data.get("holiday"):
            return False, f"closed — holiday: {data['holiday']}"
        return False, f"closed — session: {data.get('session', 'unknown')}"
    except Exception as e:
        print(f"  ! Could not check market status ({e}) — proceeding anyway.")
        return True, "unknown (status check failed)"


def fetch_quote(client: httpx.Client, api_key: str, symbol: str) -> dict | None:
    try:
        r = client.get(f"{FINNHUB_BASE}/quote", params={"symbol": symbol, "token": api_key}, timeout=10)
        r.raise_for_status()
        data = r.json()
        # Finnhub returns all-zero payload for symbols it can't price (e.g. many mutual funds)
        if not data or data.get("c") in (0, None):
            return None
        return data
    except Exception as e:
        print(f"  ! quote failed for {symbol}: {e}")
        return None


def load_positions(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    # Group by symbol ALONE — description/sector/asset_class can vary across
    # accounts for the same underlying security (stale or inconsistent
    # per-account labels in Fidelity's export), and grouping on those too
    # silently splits one real holding into two fake ones. See NO_QUOTE_SYMBOLS.
    grouped = df.groupby("symbol", as_index=False).agg(
        description=("description", "first"),
        asset_class=("asset_class", "first"),
        sector=("sector", "first"),
        quantity=("quantity", "sum"),
        cost_basis_total=("cost_basis_total", "sum"),
        last_known_value=("current_value", "sum"),
    )
    # which taxable accounts hold each symbol — needed for the tax-location re-check
    taxable_holders = (
        df.assign(tax_status=df["account_name"].apply(account_tax_status))
        .query("tax_status == 'Taxable Brokerage'")
        .groupby("symbol")["account_name"].apply(lambda s: sorted(set(s)))
    )
    grouped["taxable_accounts"] = grouped["symbol"].map(taxable_holders).apply(lambda x: x if isinstance(x, list) else [])
    return grouped


def mark_to_market(client: httpx.Client, api_key: str, positions: pd.DataFrame) -> pd.DataFrame:
    quotable_classes = {"Stock", "ETF", "Mutual Fund"}
    rows = []
    for _, row in positions.iterrows():
        symbol = row["symbol"]
        quote = None
        if row["asset_class"] in quotable_classes and symbol not in NO_QUOTE_SYMBOLS:
            quote = fetch_quote(client, api_key, symbol)

        r = row.to_dict()
        if quote:
            live_price = quote["c"]
            r["live_price"] = live_price
            r["day_change_pct"] = quote.get("dp")
            r["day_change_dollar_per_share"] = quote.get("d")
            r["current_value_live"] = live_price * row["quantity"] if pd.notna(row["quantity"]) else row["last_known_value"]
            r["quote_available"] = True
        else:
            r["live_price"] = None
            r["day_change_pct"] = None
            r["day_change_dollar_per_share"] = None
            r["current_value_live"] = row["last_known_value"]
            r["quote_available"] = False
        rows.append(r)

    out = pd.DataFrame(rows)
    total = out["current_value_live"].sum()
    out["pct_of_portfolio_live"] = out["current_value_live"] / total * 100 if total else 0.0
    out["total_gain_loss_pct_live"] = (
        (out["current_value_live"] - out["cost_basis_total"]) / out["cost_basis_total"] * 100
    ).where(out["cost_basis_total"] > 0)
    return out.sort_values("current_value_live", ascending=False)


def compute_flags(df: pd.DataFrame) -> dict:
    total = df["current_value_live"].sum()
    over_10pct = df[df["pct_of_portfolio_live"] > 10]["symbol"].tolist()

    by_symbol_val = df.set_index("symbol")["current_value_live"]
    overlap_hits = {}
    for group_name, symbols in OVERLAP_GROUPS.items():
        held = [s for s in symbols if s in by_symbol_val.index]
        if len(held) > 1:
            overlap_hits[group_name] = held

    tax_flags = df[
        df["symbol"].isin(HIGH_INCOME_STRATEGY) & (df["taxable_accounts"].apply(len) > 0)
    ]["symbol"].tolist()

    return {"over_10pct": over_10pct, "overlap_hits": overlap_hits, "tax_flags": tax_flags, "total": total}


def build_llm_prompt(df: pd.DataFrame, flags: dict, market_note: str) -> tuple[str, str]:
    total = flags["total"]
    movers = df[df["day_change_pct"].notna()].copy()
    top_up = movers.sort_values("day_change_pct", ascending=False).head(5)
    top_down = movers.sort_values("day_change_pct", ascending=True).head(5)

    def fmt_row(r):
        return (f"  {friendly(r['symbol']):20s} {r['sector']:32s} "
                f"value={fmt_money(r['current_value_live']):>12s} "
                f"({r['pct_of_portfolio_live']:.1f}% of portfolio)  "
                f"day={r['day_change_pct']:+.2f}%  "
                f"total_return={r['total_gain_loss_pct_live']:+.1f}%")

    lines = [
        f"Portfolio total (live-priced): {fmt_money(total)}",
        f"Market status: {market_note}",
        "",
        "Today's biggest gainers among current holdings:",
        *[fmt_row(r) for _, r in top_up.iterrows()],
        "",
        "Today's biggest decliners among current holdings:",
        *[fmt_row(r) for _, r in top_down.iterrows()],
        "",
        f"Positions currently >10% of total portfolio: {', '.join(friendly(s) for s in flags['over_10pct']) or 'none'}",
        "",
        "Overlapping funds doing the same job (same style/objective held via multiple tickers):",
    ]
    if flags["overlap_hits"]:
        for group, symbols in flags["overlap_hits"].items():
            lines.append(f"  {group}: {', '.join(friendly(s) for s in symbols)}")
    else:
        lines.append("  none")

    lines.append("")
    lines.append("High-dividend/high-income funds sitting in a TAXABLE account:")
    lines.append("  " + (", ".join(friendly(s) for s in flags["tax_flags"]) or "none"))

    lines.append("")
    lines.append("Full current holdings snapshot:")
    for _, r in df.iterrows():
        if pd.isna(r["day_change_pct"]):
            lines.append(f"  {friendly(r['symbol']):20s} {r['sector']:32s} "
                          f"value={fmt_money(r['current_value_live']):>12s} "
                          f"({r['pct_of_portfolio_live']:.1f}%)  [no live quote — mutual fund NAV or unpriced]")
        else:
            lines.append(fmt_row(r))

    user_prompt = "\n".join(lines)

    system_prompt = """You are a portfolio observation assistant for a personal investor.
You are NOT a licensed financial advisor and must not give investment advice.

Your job: given today's live price moves and this person's existing holdings —
including which positions already carry a rule-based flag (over 10% of the
portfolio, overlapping funds doing the same job, or a high-dividend fund
sitting in a taxable account) — write a short "things to consider today" note.

Hard rules:
- Never say "buy", "sell", "add to", "trim", or give a share count, dollar
  amount, or price target as an instruction. Describe conditions and let the
  reader draw their own conclusion (e.g. "X is both up sharply today and
  already your largest position — some investors would treat that as a
  prompt to revisit position sizing" is fine; "sell 10 shares of X" is not).
- Only call out a position if there's something concrete tying today's move
  to an existing flag (concentration, overlap, or tax-location) or an
  unusually large single-day move (>5%). Don't manufacture a note for every
  holding — silence on a boring position is correct.
- Keep it to 4-8 bullet points, plain language, no jargon without explanation.
- End with one sentence reminding the reader this is a mechanical read of
  today's price data only, not advice, and doesn't know their goals or plans.
"""
    return system_prompt, user_prompt


def send_telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return  # optional feature — silently skip if not configured
    # Telegram caps messages at 4096 chars; the summary is always short, but
    # guard against a future prompt change making it long.
    text = text[:4000]
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
        r.raise_for_status()
        print("Sent to Telegram.")
    except Exception as e:
        print(f"  ! Telegram send failed: {e}")


def call_claude(system_prompt: str, user_prompt: str) -> str:
    import anthropic
    client = anthropic.Anthropic()
    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    resp = client.messages.create(
        model=model,
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return resp.content[0].text


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="Live morning check against current Fidelity holdings.")
    parser.add_argument("--positions", default=None, help="Path to positions_enriched_*.csv (default: newest)")
    args = parser.parse_args()

    finnhub_key = os.getenv("FINNHUB_API_KEY")
    if not finnhub_key or finnhub_key == "your-finnhub-key":
        sys.exit("FINNHUB_API_KEY not set in .env — get a free key at https://finnhub.io/register")

    repo_root = Path(__file__).resolve().parent
    data_dir = repo_root / "fidelity_data"
    positions_path = Path(args.positions) if args.positions else find_latest_enriched_csv(data_dir)

    now_et = datetime.now(ZoneInfo("America/New_York"))
    print(f"[{now_et:%Y-%m-%d %H:%M %Z}] Loading positions from {positions_path.name} ...")

    with httpx.Client() as client:
        is_open, market_note = market_is_open(client, finnhub_key)
        print(f"Market status: {market_note}")
        if not is_open:
            print("Market is not open — skipping live check to avoid a stale/misleading LLM read.")
            return

        positions = load_positions(positions_path)
        # money-market/CUSIP-only positions have no public quote — mark_to_market
        # falls back to last known value for those automatically.
        print(f"Fetching live quotes for {len(positions)} holdings...")
        priced = mark_to_market(client, finnhub_key, positions)

    flags = compute_flags(priced)
    system_prompt, user_prompt = build_llm_prompt(priced, flags, market_note)

    print("Asking Claude to summarize...")
    summary = call_claude(system_prompt, user_prompt)

    header = f"Morning Portfolio Check — {now_et:%Y-%m-%d %H:%M %Z}\n{'=' * 78}\n"
    report = header + summary + "\n\n" + "-" * 78 + "\n" + "RAW DATA USED\n" + "-" * 78 + "\n" + user_prompt + "\n"

    print("\n" + report)

    out_dir = data_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"market_check_{now_et:%Y-%m-%d_%H%M}.txt"
    out_path.write_text(report)
    print(f"\nSaved: {out_path}")

    send_telegram(header + summary)


if __name__ == "__main__":
    main()
