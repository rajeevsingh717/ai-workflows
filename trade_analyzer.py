"""
Per-symbol trade history lookup + realized P&L summary from your Fidelity
transaction export. Answers "what did I actually pay for this last time, and
does today's price mean I'd be selling at a gain or a loss" without relying
on memory — no AI, no predictions, just your own numbers next to a live quote.

Usage:
    python trade_analyzer.py AMD    # full buy/sell history + live price for one symbol
    python trade_analyzer.py        # realized P&L summary across every symbol you've traded

Automatically merges every Accounts_History*.csv in
fidelity_data/order_transactions/ into a deduplicated master ledger (see
transaction_store.py) before analyzing — just drop a new export in that
folder any time, nothing needs to be run by hand. For the per-symbol view,
also reads the newest positions_enriched_*.csv from portfolio_analyzer.py
for Fidelity's own blended cost basis on shares bought before your earliest
recorded transaction.
"""
import argparse
import csv
import glob
import os
import sys
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
import pandas as pd
from dotenv import load_dotenv

from market_check import NO_QUOTE_SYMBOLS, fetch_quote, find_latest_enriched_csv
from portfolio_analyzer import clean_money, fmt_money, friendly
from transaction_store import sync_master


def find_latest_transactions_csv(folder: Path) -> Path:
    candidates = sorted(glob.glob(str(folder / "Accounts_History*.csv")))
    if not candidates:
        sys.exit(f"No Accounts_History*.csv found in {folder}")
    return Path(max(candidates, key=os.path.getmtime))


def load_ledger(txn_dir: Path) -> list["Txn"]:
    """Merges any new Accounts_History*.csv exports into the master ledger,
    then loads the full (deduplicated, all-time) transaction history from it.
    This is what trade_analyzer.py and telegram_listener.py use by default —
    just drop a new export in txn_dir any time and rerun."""
    master_path, added, total = sync_master(txn_dir)
    if added:
        print(f"Merged {added} new transaction(s) into {master_path.name} (now {total} total).")
    return load_transactions(master_path)


def _num0(x: float) -> float:
    return 0.0 if x != x else x  # NaN check (x != x is only true for NaN)


@dataclass
class Txn:
    date: datetime
    account: str
    account_number: str
    action: str  # BUY / SELL / DIVIDEND / OTHER
    symbol: str
    description: str
    price: float
    quantity: float  # always a positive magnitude — action tells direction
    commission: float
    fees: float
    amount: float


def load_transactions(csv_path: Path) -> list[Txn]:
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))

    header_idx = next((i for i, r in enumerate(rows) if r and r[0] == "Run Date"), None)
    if header_idx is None:
        sys.exit(f"Couldn't find the 'Run Date' header row in {csv_path}")
    header = rows[header_idx]
    idx = {name: i for i, name in enumerate(header)}

    txns = []
    for r in rows[header_idx + 1:]:
        if len(r) != len(header) or not r[0].strip():
            continue
        try:
            date = datetime.strptime(r[idx["Run Date"]], "%m/%d/%Y")
        except ValueError:
            continue  # footer / disclaimer / non-data row

        action_text = r[idx["Action"]]
        if "YOU BOUGHT" in action_text:
            action = "BUY"
        elif "YOU SOLD" in action_text:
            action = "SELL"
        elif "Dividend" in action_text:
            action = "DIVIDEND"
        else:
            action = "OTHER"

        txns.append(Txn(
            date=date,
            account=r[idx["Account"]].strip(),
            account_number=r[idx["Account Number"]].strip(),
            action=action,
            symbol=r[idx["Symbol"]].strip(),
            description=r[idx["Description"]].strip(),
            price=clean_money(r[idx["Price ($)"]]),
            quantity=abs(clean_money(r[idx["Quantity"]])),
            commission=_num0(clean_money(r[idx["Commission ($)"]])),
            fees=_num0(clean_money(r[idx["Fees ($)"]])),
            amount=clean_money(r[idx["Amount ($)"]]),
        ))
    txns.sort(key=lambda t: t.date)
    return txns


@dataclass
class Lot:
    date: datetime
    price: float
    qty: float  # remaining, mutated as sells consume it
    account: str


@dataclass
class ClosedTrade:
    account: str
    symbol: str
    buy_date: datetime
    buy_price: float
    sell_date: datetime
    sell_price: float
    qty: float
    realized_gain: float
    realized_pct: float
    holding_days: int


def fifo_match(txns: list[Txn]):
    """FIFO-matches buys to sells per (account, symbol).

    Returns (closed_trades, open_lots_by_key, unmatched_sells) — unmatched
    sells are ones that (partially or fully) drew from shares bought before
    your earliest recorded transaction, so there's no buy price on record here.
    """
    open_lots: dict[tuple[str, str], deque[Lot]] = defaultdict(deque)
    closed: list[ClosedTrade] = []
    unmatched_sells: list[Txn] = []

    for t in txns:
        key = (t.account_number, t.symbol)
        if t.action == "BUY":
            open_lots[key].append(Lot(date=t.date, price=t.price, qty=t.quantity, account=t.account))
        elif t.action == "SELL":
            remaining = t.quantity
            queue = open_lots[key]
            while remaining > 1e-6 and queue:
                lot = queue[0]
                matched_qty = min(lot.qty, remaining)
                holding_days = (t.date - lot.date).days
                realized_gain = (t.price - lot.price) * matched_qty
                realized_pct = (t.price - lot.price) / lot.price * 100 if lot.price else float("nan")
                closed.append(ClosedTrade(
                    account=t.account, symbol=t.symbol,
                    buy_date=lot.date, buy_price=lot.price,
                    sell_date=t.date, sell_price=t.price,
                    qty=matched_qty, realized_gain=realized_gain,
                    realized_pct=realized_pct, holding_days=holding_days,
                ))
                lot.qty -= matched_qty
                remaining -= matched_qty
                if lot.qty <= 1e-6:
                    queue.popleft()
            if remaining > 1e-6:
                unmatched_sells.append(t)  # rest of this sell predates the export

    return closed, open_lots, unmatched_sells


def get_live_price(symbol: str) -> float | None:
    if symbol in NO_QUOTE_SYMBOLS:
        return None
    finnhub_key = os.getenv("FINNHUB_API_KEY")
    if not finnhub_key or finnhub_key == "your-finnhub-key":
        return None
    with httpx.Client() as client:
        quote = fetch_quote(client, finnhub_key, symbol)
    return quote["c"] if quote else None


def format_symbol_compact(symbol: str, txns: list[Txn], closed: list[ClosedTrade],
                           open_lots: dict, positions_df: pd.DataFrame | None) -> str:
    """Short summary for Telegram: live price, open-lot cost range with
    best/worst case if sold now, realized total, and Fidelity's blended
    all-accounts position. Skips the full transaction-by-transaction log —
    use format_symbol_full() / print_symbol_history() for that."""
    symbol = symbol.upper()
    sym_txns = [t for t in txns if t.symbol == symbol and t.action in ("BUY", "SELL")]
    if not sym_txns:
        return f"No BUY/SELL transactions found for {symbol} in your transaction history."

    live_price = get_live_price(symbol)
    if symbol in NO_QUOTE_SYMBOLS:
        price_line = " — live price skipped (ambiguous/private ticker)"
    elif live_price:
        price_line = f" — live {fmt_money(live_price)}"
    else:
        price_line = " — live price unavailable"

    lines = [f"{friendly(symbol)} ({symbol}){price_line}"]

    open_here = [lot for (acct_num, sym), q in open_lots.items() if sym == symbol for lot in q if lot.qty > 1e-6]
    if open_here:
        prices = [lot.price for lot in open_here]
        qty_total = sum(lot.qty for lot in open_here)
        avg_cost = sum(lot.price * lot.qty for lot in open_here) / qty_total
        lines.append(f"\nOpen lots (this history): {qty_total:.2f} sh, avg cost {fmt_money(avg_cost)}, "
                      f"range {fmt_money(min(prices))}–{fmt_money(max(prices))}")
        if live_price:
            best = min(open_here, key=lambda l: l.price)   # cheapest lot = biggest gain if sold
            worst = max(open_here, key=lambda l: l.price)  # priciest lot = smallest gain/loss if sold
            best_pct = (live_price - best.price) / best.price * 100 if best.price else float("nan")
            worst_pct = (live_price - worst.price) / worst.price * 100 if worst.price else float("nan")
            lines.append(f"  Best lot:  {fmt_money(best.price)} → {best_pct:+.1f}% if sold now")
            if worst.price != best.price:
                lines.append(f"  Worst lot: {fmt_money(worst.price)} → {worst_pct:+.1f}% if sold now")
    else:
        lines.append("\nNo open lots on record — everything bought in this history has been sold.")

    sym_closed = [c for c in closed if c.symbol == symbol]
    if sym_closed:
        total_gain = sum(c.realized_gain for c in sym_closed)
        lines.append(f"\nRealized (all-time): {total_gain:+.2f} across {len(sym_closed)} closed trade(s)")

    if positions_df is not None:
        rows = positions_df[positions_df["symbol"] == symbol]
        if not rows.empty:
            total_qty = rows["quantity"].sum()
            total_cost = rows["cost_basis_total"].sum()
            blended_avg = total_cost / total_qty if total_qty else float("nan")
            lines.append(f"\nFidelity total (all accounts): {total_qty:.2f} sh @ avg {fmt_money(blended_avg)}")

    lines.append("\n(reply \"full\" after the symbol for the complete transaction log)")
    return "\n".join(lines)


def print_symbol_history(symbol: str, txns: list[Txn], closed: list[ClosedTrade],
                          open_lots: dict, positions_df: pd.DataFrame | None):
    symbol = symbol.upper()
    sym_txns = [t for t in txns if t.symbol == symbol and t.action in ("BUY", "SELL")]
    if not sym_txns:
        print(f"No BUY/SELL transactions found for {symbol} in your transaction history.")
        return

    live_price = get_live_price(symbol)
    if symbol in NO_QUOTE_SYMBOLS:
        price_line = " — live price skipped (see NO_QUOTE_SYMBOLS: ambiguous/private ticker)"
    elif live_price:
        price_line = f" — live price: {fmt_money(live_price)}"
    else:
        price_line = " — live price unavailable (market closed, or FINNHUB_API_KEY not set)"

    print(f"\n{friendly(symbol)} ({symbol}){price_line}")
    print("=" * 78)

    print("\nYour buy/sell history (your full transaction history):")
    for t in sym_txns:
        note = ""
        if live_price and t.action == "BUY":
            gain = (live_price - t.price) * t.quantity
            pct = (live_price - t.price) / t.price * 100 if t.price else float("nan")
            note = f"   → at live price: {gain:+.2f} ({pct:+.1f}%)"
        print(f"  {t.date:%m/%d/%Y}  {t.action:4s} {t.quantity:>9.4f} @ {fmt_money(t.price):>10s}  [{t.account}]{note}")

    print("\nOpen lots still held that were bought in your recorded history (oldest first):")
    any_open = False
    for (acct_num, sym), queue in open_lots.items():
        if sym != symbol:
            continue
        for lot in queue:
            if lot.qty > 1e-6:
                any_open = True
                line = f"  {lot.qty:.4f} @ {fmt_money(lot.price)}  (bought {lot.date:%m/%d/%Y}, {lot.account})"
                if live_price:
                    gain = (live_price - lot.price) * lot.qty
                    pct = (live_price - lot.price) / lot.price * 100 if lot.price else float("nan")
                    line += f"   → {gain:+.2f} ({pct:+.1f}%) if sold now"
                print(line)
    if not any_open:
        print("  None — every lot on record has since been sold.")

    sym_closed = [c for c in closed if c.symbol == symbol]
    print("\nRealized round-trips for this symbol (your full transaction history):")
    if sym_closed:
        for c in sym_closed:
            print(f"  bought {c.buy_date:%m/%d} @ {fmt_money(c.buy_price)} → sold {c.sell_date:%m/%d} @ {fmt_money(c.sell_price)}  "
                  f"qty {c.qty:.4f}  {c.realized_gain:+.2f} ({c.realized_pct:+.1f}%)  held {c.holding_days}d  [{c.account}]")
        total_gain = sum(c.realized_gain for c in sym_closed)
        print(f"  Total realized on {symbol} (all-time): {total_gain:+.2f}")
    else:
        print("  None yet.")

    if positions_df is not None:
        rows = positions_df[positions_df["symbol"] == symbol]
        if not rows.empty:
            print("\nFidelity's official position (blended — includes any shares bought before your transaction history began):")
            for _, r in rows.iterrows():
                avg_cost = r.get("average_cost_basis")
                print(f"  {r['account_name']}: {r['quantity']:.4f} shares, "
                      f"avg cost {fmt_money(avg_cost) if pd.notna(avg_cost) else 'n/a'}, "
                      f"Fidelity's last valuation {fmt_money(r['current_value'])}")


def print_overall_summary(txns: list[Txn], closed: list[ClosedTrade], unmatched_sells: list[Txn]):
    print("Realized P&L summary (your full transaction history)")
    print("=" * 78)

    if closed:
        total_gain = sum(c.realized_gain for c in closed)
        wins = [c for c in closed if c.realized_gain > 0]
        losses = [c for c in closed if c.realized_gain <= 0]
        win_rate = len(wins) / len(closed) * 100
        avg_win = sum(c.realized_gain for c in wins) / len(wins) if wins else 0.0
        avg_loss = sum(c.realized_gain for c in losses) / len(losses) if losses else 0.0
        avg_hold = sum(c.holding_days for c in closed) / len(closed)

        print(f"Closed round-trip trades: {len(closed)}")
        print(f"Total realized P&L:       {fmt_money(total_gain)}")
        print(f"Win rate:                 {win_rate:.0f}%  ({len(wins)} winners / {len(losses)} losers)")
        print(f"Avg winner:               {fmt_money(avg_win)}    Avg loser: {fmt_money(avg_loss)}")
        print(f"Avg holding period:       {avg_hold:.0f} days")

        by_symbol = defaultdict(float)
        for c in closed:
            by_symbol[c.symbol] += c.realized_gain
        print("\nRealized P&L by symbol:")
        for sym, gain in sorted(by_symbol.items(), key=lambda kv: kv[1], reverse=True):
            print(f"  {friendly(sym):22s} {gain:+10.2f}")
    else:
        print("No closed round-trip trades found in your transaction history.")

    total_fees = sum(t.commission + t.fees for t in txns if t.action in ("BUY", "SELL"))
    print(f"\nTotal commissions + fees paid: {fmt_money(total_fees)}")

    trade_counts = Counter(t.symbol for t in txns if t.action in ("BUY", "SELL"))
    print("\nMost-traded symbols (buy+sell transaction count, all-time):")
    for sym, count in trade_counts.most_common(10):
        print(f"  {friendly(sym):22s} {count}")

    dividends = [t for t in txns if t.action == "DIVIDEND"]
    if dividends:
        total_div = sum(t.amount for t in dividends if t.amount == t.amount)
        print(f"\nDividends received (all-time): {fmt_money(total_div)} across {len(dividends)} payment(s)")

    if unmatched_sells:
        print(f"\n{len(unmatched_sells)} sell(s) drew (at least partly) from shares bought before your recorded transaction history begins —")
        print("no buy price on record here, so no realized-gain figure for that portion:")
        for t in unmatched_sells:
            print(f"  {t.date:%m/%d/%Y}  SELL {t.quantity:.4f} {t.symbol} @ {fmt_money(t.price)}  [{t.account}]")


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="Look up your own buy/sell history per symbol, or a realized P&L summary.")
    parser.add_argument("symbol", nargs="?", default=None, help="Symbol to look up (e.g. AMD). Omit for the overall summary.")
    parser.add_argument("--transactions", default=None, help="Path to Accounts_History*.csv (default: newest in fidelity_data/order_transactions/)")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent
    data_dir = repo_root / "fidelity_data"
    txn_dir = data_dir / "order_transactions"

    if args.transactions:
        txn_path = Path(args.transactions)
        print(f"Loading transactions from {txn_path.name} (explicit path — master ledger not used) ...")
        txns = load_transactions(txn_path)
    else:
        txns = load_ledger(txn_dir)
    closed, open_lots, unmatched_sells = fifo_match(txns)

    if args.symbol:
        positions_df = None
        try:
            positions_path = find_latest_enriched_csv(data_dir)
            positions_df = pd.read_csv(positions_path)
        except SystemExit:
            pass
        print_symbol_history(args.symbol, txns, closed, open_lots, positions_df)
    else:
        print_overall_summary(txns, closed, unmatched_sells)


if __name__ == "__main__":
    main()
