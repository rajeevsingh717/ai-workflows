"""
Merges every Accounts_History*.csv in fidelity_data/order_transactions/ into
a single deduplicated master_transactions.csv. Safe to re-run any time —
already-seen transactions are recognized by content and never duplicated.

trade_analyzer.py (and telegram_listener.py, through it) calls this
automatically on every run, so you don't normally need to run this by hand —
it's here mainly so you can see exactly what got added right after dropping
in a new export.

Usage:
    python merge_transactions.py
"""
from pathlib import Path

from transaction_store import sync_master


def main():
    txn_dir = Path(__file__).resolve().parent / "fidelity_data" / "order_transactions"
    master_path, added, total = sync_master(txn_dir)
    print(f"Master transaction ledger: {master_path}")
    print(f"New transactions added this run: {added}")
    print(f"Total transactions in master: {total}")


if __name__ == "__main__":
    main()
