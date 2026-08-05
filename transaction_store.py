"""
Maintains fidelity_data/order_transactions/master_transactions.csv — a
deduplicated, ever-growing ledger built from every Accounts_History*.csv
you've ever dropped in that folder.

Fidelity's export only covers a rolling window (whatever date range you
picked at export time), so relying on "the newest export file" alone would
silently lose older transactions once they age out of a future export. This
module instead unions every export you've ever provided, keyed by the
transaction's own content — safe to re-run any time, already-seen
transactions are recognized and never duplicated, and nothing already in the
ledger is ever removed just because a later export happens not to include it.

sync_master() is called automatically by trade_analyzer.py on every run —
you don't need to run anything by hand, just drop a new export CSV into
fidelity_data/order_transactions/ whenever you have one.
"""
import csv
import glob
from datetime import datetime
from pathlib import Path

MASTER_FILENAME = "master_transactions.csv"

FIELDS = ["Run Date", "Account", "Account Number", "Action", "Symbol", "Description",
          "Type", "Price ($)", "Quantity", "Commission ($)", "Fees ($)",
          "Accrued Interest ($)", "Amount ($)", "Settlement Date"]

# Only these fields go into the dedup fingerprint — deliberately excludes
# Description/Type/Settlement Date, which are more likely to drift slightly
# between two exports of the same real transaction (see the CVSA labeling
# inconsistency found in portfolio_analyzer.py) without it actually being a
# different trade.
DEDUP_FIELDS = ["Run Date", "Account Number", "Action", "Symbol", "Price ($)", "Quantity", "Amount ($)"]


def _normalize(value: str) -> str:
    v = (value or "").strip()
    cleaned = v.replace("$", "").replace(",", "").strip('"')
    try:
        return f"{float(cleaned):.6f}"
    except ValueError:
        return v


def _row_key(row: dict) -> tuple:
    return tuple(_normalize(row.get(f, "")) for f in DEDUP_FIELDS)


def _read_raw_export(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    header_idx = next((i for i, r in enumerate(rows) if r and r[0] == "Run Date"), None)
    if header_idx is None:
        return []
    header = rows[header_idx]
    out = []
    for r in rows[header_idx + 1:]:
        if len(r) != len(header) or not r[0].strip():
            continue
        out.append(dict(zip(header, r)))
    return out


def _sort_key(row: dict) -> datetime:
    try:
        return datetime.strptime(row.get("Run Date", ""), "%m/%d/%Y")
    except ValueError:
        return datetime.min


def sync_master(txn_dir: Path) -> tuple[Path, int, int]:
    """Merges every Accounts_History*.csv in txn_dir into master_transactions.csv.

    Returns (master_path, new_rows_added, total_rows_in_master).
    """
    master_path = txn_dir / MASTER_FILENAME

    seen_keys: set[tuple] = set()
    existing_rows: list[dict] = []
    if master_path.exists():
        with open(master_path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                existing_rows.append(row)
                seen_keys.add(_row_key(row))

    new_rows: list[dict] = []
    for export_path in sorted(glob.glob(str(txn_dir / "Accounts_History*.csv"))):
        for row in _read_raw_export(Path(export_path)):
            key = _row_key(row)
            if key not in seen_keys:
                seen_keys.add(key)
                new_rows.append(row)

    all_rows = sorted(existing_rows + new_rows, key=_sort_key)

    txn_dir.mkdir(parents=True, exist_ok=True)
    with open(master_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for row in all_rows:
            writer.writerow({k: row.get(k, "") for k in FIELDS})

    return master_path, len(new_rows), len(all_rows)
