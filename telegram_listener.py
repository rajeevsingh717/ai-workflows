"""
Telegram remote control for trade_analyzer.py.

Long-polls Telegram for messages from your configured TELEGRAM_CHAT_ID and
replies with the same output trade_analyzer.py would print locally — lets
you look up your own buy/sell history from your phone without touching the
laptop. Requires the Mac to stay on/awake; this is a local listener, not a
cloud service (your Fidelity data still never leaves this machine).

Commands (message text, case-insensitive):
    <SYMBOL>   e.g. "AMD"   -> that symbol's buy/sell history + live price
    summary                 -> realized P&L summary across everything
    help                    -> lists these commands

Only messages from TELEGRAM_CHAT_ID in .env are processed; anything from a
different chat_id is logged locally and silently ignored — never acted on,
never replied to.

Run via setup_telegram_listener.sh to install as a persistent launchd daemon,
or directly with `python telegram_listener.py` for foreground testing.
"""
import contextlib
import io
import os
import re
import sys
import time
from pathlib import Path

import httpx
import pandas as pd
from dotenv import load_dotenv

from market_check import find_latest_enriched_csv, send_telegram
from trade_analyzer import (
    fifo_match,
    format_symbol_compact,
    load_ledger,
    print_overall_summary,
    print_symbol_history,
)

REPO_ROOT = Path(__file__).resolve().parent
DATA_DIR = REPO_ROOT / "fidelity_data"
TXN_DIR = DATA_DIR / "order_transactions"
OFFSET_FILE = DATA_DIR / "output" / "telegram_listener_offset.txt"

SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,10}(\*\*)?$")

HELP_TEXT = (
    "Commands:\n"
    "  <SYMBOL>       e.g. AMD  — compact summary: live price, open-lot cost range, realized total\n"
    "  <SYMBOL> full  e.g. AMD full — full transaction-by-transaction log\n"
    "  summary        — realized P&L summary across everything\n"
    "  help           — this message"
)


def _capture(fn, *args, **kwargs) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args, **kwargs)
    return buf.getvalue()


def run_symbol_lookup(symbol: str, full: bool = False) -> str:
    txns = load_ledger(TXN_DIR)
    closed, open_lots, _ = fifo_match(txns)
    positions_df = None
    try:
        positions_path = find_latest_enriched_csv(DATA_DIR)
        positions_df = pd.read_csv(positions_path)
    except SystemExit:
        pass
    if full:
        return _capture(print_symbol_history, symbol, txns, closed, open_lots, positions_df)
    return format_symbol_compact(symbol, txns, closed, open_lots, positions_df)


def run_summary() -> str:
    txns = load_ledger(TXN_DIR)
    closed, open_lots, unmatched = fifo_match(txns)
    return _capture(print_overall_summary, txns, closed, unmatched)


def handle_message(text: str) -> str:
    text = text.strip()
    if not text:
        return HELP_TEXT
    lowered = text.lower()
    if lowered in ("help", "/help", "start", "/start"):
        return HELP_TEXT
    if lowered in ("summary", "/summary"):
        try:
            return run_summary()
        except SystemExit as e:
            return f"Error: {e}"

    candidate = text.upper().lstrip("/")
    if candidate.startswith("TRADE "):
        candidate = candidate[len("TRADE "):].strip()

    full = False
    if candidate.endswith(" FULL"):
        full = True
        candidate = candidate[:-len(" FULL")].strip()

    if SYMBOL_RE.match(candidate):
        try:
            return run_symbol_lookup(candidate, full=full)
        except SystemExit as e:
            return f"Error: {e}"

    return f"Didn't recognize {text!r}.\n\n{HELP_TEXT}"


def load_offset() -> int:
    if OFFSET_FILE.exists():
        try:
            return int(OFFSET_FILE.read_text().strip())
        except ValueError:
            return 0
    return 0


def save_offset(offset: int) -> None:
    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_FILE.write_text(str(offset))


def send_chunked(text: str) -> None:
    # Telegram caps messages at 4096 chars; chunk on line boundaries so a
    # long symbol history doesn't get cut mid-line.
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3800:
            send_telegram(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        send_telegram(chunk)


def poll_loop():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    allowed_chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not allowed_chat_id:
        sys.exit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID must be set in .env")

    offset = load_offset()
    print(f"Listening for Telegram commands (allowed chat_id={allowed_chat_id}) ...", flush=True)

    with httpx.Client(timeout=40) as client:
        while True:
            try:
                r = client.get(
                    f"https://api.telegram.org/bot{token}/getUpdates",
                    params={"offset": offset, "timeout": 30},
                )
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                print(f"  ! poll error: {e}", flush=True)
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                save_offset(offset)

                msg = update.get("message") or {}
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "")

                if chat_id != str(allowed_chat_id):
                    print(f"  ! ignored message from unauthorized chat_id={chat_id}", flush=True)
                    continue
                if not text:
                    continue

                print(f"  <- {text!r}", flush=True)
                reply = handle_message(text)
                send_chunked(reply)


def main():
    load_dotenv()
    poll_loop()


if __name__ == "__main__":
    main()
