"""CLI for the personal memory store."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()


def cmd_sync_notion(args):
    from memory_store.ingest.notion import sync
    sync(limit=args.limit)


def cmd_search(args):
    from memory_store.store import search
    query = " ".join(args.query)
    results = search(query, n=args.n, domain=args.domain)
    if not results:
        print("No results found.")
        return
    for r in results:
        score = r.get("score", 0)
        title = r.get("title") or "(untitled)"
        domain = r.get("domain") or ""
        date = (r.get("created_at") or "")[:10]
        print(f"\n[{score:.2f}] {title}")
        print(f"  domain={domain}  date={date}  source={r.get('source_type', '')}")
        if r.get("project"):
            print(f"  project: {r['project']}")
        if r.get("area"):
            print(f"  area: {r['area']}")
        if r.get("tags"):
            print(f"  tags: {', '.join(r['tags'])}")
        content = (r.get("content") or "")[:300].replace("\n", " ")
        if content:
            print(f"  {content}...")
        print(f"  {r.get('source_url', '')}")


def cmd_list(args):
    from memory_store.store import list_entries
    rows = list_entries(domain=args.domain, since=args.since, limit=args.limit)
    print(f"\n{len(rows)} entries\n")
    for i, r in enumerate(rows, 1):
        date = (r.get("created_at") or "")[:10]
        domain = (r.get("domain") or "").ljust(10)
        title = r.get("title") or "(untitled)"
        print(f"  [{i:>2}] {date}  [{domain}]  {title[:55]}")
        if args.full:
            content = (r.get("content") or "").strip()
            if content:
                for line in content.splitlines():
                    print(f"        {line}")
            if r.get("project"):
                print(f"        project: {r['project']}")
            if r.get("area"):
                print(f"        area: {r['area']}")
            print(f"        {r.get('source_url', '')}")
            print()


def cmd_show(args):
    from memory_store.store import list_entries
    rows = list_entries(limit=200)
    # match by index number or title substring
    query = " ".join(args.entry).lower()
    matched = None
    if query.isdigit():
        idx = int(query) - 1
        if 0 <= idx < len(rows):
            matched = rows[idx]
    else:
        for r in rows:
            if query in (r.get("title") or "").lower():
                matched = r
                break
    if not matched:
        print(f"No entry found for: {' '.join(args.entry)}")
        print("Use `list` to see entry numbers, or search by title keyword.")
        return
    print(f"\n{'='*60}")
    print(f"  {matched.get('title') or '(untitled)'}")
    print(f"{'='*60}")
    print(f"  domain : {matched.get('domain')}")
    print(f"  date   : {(matched.get('created_at') or '')[:10]}")
    print(f"  source : {matched.get('source_type')}")
    if matched.get("project"):
        print(f"  project: {matched['project']}")
    if matched.get("area"):
        print(f"  area   : {matched['area']}")
    if matched.get("tags"):
        print(f"  tags   : {', '.join(matched['tags'])}")
    print(f"  url    : {matched.get('source_url', '')}")
    print()
    content = (matched.get("content") or "").strip()
    if content:
        print(content)
    else:
        print("(no content)")


def cmd_stats(args):
    from memory_store.store import stats
    s = stats()
    print(f"\nNotes:     {s['total']}")
    print(f"Documents: {s.get('documents', 0)}\n")
    print("Notes by domain:")
    for domain, n in s["by_domain"].items():
        print(f"  {(domain or 'unset'):12s} {n}")
    print("\nNotes by source:")
    for src, n in s["by_source"].items():
        print(f"  {src:12s} {n}")
    if s.get("by_doc_type"):
        print("\nDocuments by type:")
        for dt, n in s["by_doc_type"].items():
            print(f"  {(dt or 'unset'):12s} {n}")


def cmd_auth_gdrive(args):
    from memory_store.ingest.gdrive import auth_flow, SECRETS_PATH
    if not SECRETS_PATH.exists():
        print(f"OAuth credentials not found at {SECRETS_PATH}")
        print("\nSetup:")
        print("  1. Go to console.cloud.google.com → your GCP project")
        print("  2. APIs & Services → Library → search 'Google Drive API' → Enable")
        print("  3. APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID")
        print("  4. Application type: Desktop app → Create → Download JSON")
        print(f"  5. Save to: {SECRETS_PATH}")
        return
    print("Opening browser for Google authentication...")
    auth_flow()


def cmd_ingest(args):
    from memory_store.ingest.document import process_file

    sources = args.sources
    force = args.force
    doc_type_hint = args.type

    results = {"processed": 0, "skipped": 0, "failed": 0}

    for source in sources:
        print(f"\nProcessing: {source}")

        # Handle --force by deleting existing record first
        if force:
            _force_delete(source)

        try:
            result = process_file(source, doc_type_hint=doc_type_hint)

            if result.get("skipped"):
                results["skipped"] += 1
                continue

            results["processed"] += 1
            _print_result(result)

        except Exception as e:
            print(f"  ✗ Failed: {e}")
            results["failed"] += 1

    print(f"\nDone — {results['processed']} processed, "
          f"{results['skipped']} skipped, {results['failed']} failed")


def _force_delete(source: str):
    """Remove existing document record so it gets reprocessed."""
    import re
    from pathlib import Path
    from memory_store.store import _db

    is_drive = source.startswith("http") or re.match(r"^[a-zA-Z0-9_-]{25,}$", source)
    if is_drive:
        from memory_store.ingest.gdrive import extract_file_id
        source_id = extract_file_id(source)
    else:
        source_id = str(Path(source).expanduser().resolve())

    with _db() as conn:
        conn.execute("DELETE FROM documents WHERE source_id = ?", (source_id,))
        conn.execute("DELETE FROM memory_entries WHERE source_id = ?", (f"doc::{source_id}",))


def _print_result(result: dict):
    doc_type = result.get("doc_type", "other")
    confidence = result.get("confidence", "")
    fields = result.get("fields") or {}

    print(f"\n  {'─'*50}")
    print(f"  Type       : {doc_type}  (confidence: {confidence})")

    if not fields:
        print("  (no structured fields extracted)")
        return

    # Print fields in a readable format
    _LABELS = {
        # lease
        "landlord": "Landlord", "tenant": "Tenant",
        "property_address": "Property", "monthly_rent": "Rent/month",
        "security_deposit": "Deposit", "lease_start": "Start",
        "lease_end": "End", "notice_period_days": "Notice",
        "utilities_included": "Utilities", "pet_policy": "Pets",
        "key_clauses": "Key clauses",
        # visa
        "holder_name": "Holder", "visa_category": "Category",
        "nationality": "Nationality", "passport_number": "Passport #",
        "issue_date": "Issued", "expiry_date": "Expires",
        "issuing_authority": "Issued by", "entry_type": "Entry",
        "restrictions": "Restrictions",
        # medical
        "provider": "Provider", "doctor": "Doctor",
        "visit_date": "Visit date", "diagnosis": "Diagnosis",
        "medications": "Medications", "test_results": "Test results",
        "follow_up_date": "Follow-up", "notes": "Notes",
        # insurance
        "insurer": "Insurer", "policy_number": "Policy #",
        "coverage_type": "Coverage", "premium": "Premium",
        "coverage_start": "Start", "coverage_end": "End",
        "insured_items": "Covers", "deductible": "Deductible",
        # tax
        "tax_year": "Tax year", "filing_status": "Filing status",
        "gross_income": "Gross income", "total_deductions": "Deductions",
        "tax_owed": "Tax owed", "refund_amount": "Refund",
        "filing_date": "Filed",
        # id
        "doc_type": "Doc type", "id_number": "ID number",
        "date_of_birth": "DOB", "address": "Address",
    }

    for key, val in fields.items():
        if val is None:
            continue
        label = _LABELS.get(key, key).ljust(14)
        if isinstance(val, list):
            val = ", ".join(str(v) for v in val) if val else "—"
        if isinstance(val, (int, float)) and key in ("monthly_rent", "security_deposit",
                                                       "gross_income", "total_deductions",
                                                       "tax_owed", "refund_amount"):
            val = f"${val:,.0f}"
        print(f"  {label} : {val}")


def cmd_docs(args):
    from memory_store.store import list_documents
    rows = list_documents(doc_type=args.type, limit=args.limit)
    if not rows:
        print("No documents found.")
        return
    print(f"\n{len(rows)} document(s)\n")
    for i, r in enumerate(rows, 1):
        date = (r.get("ingested_at") or "")[:10]
        dtype = (r.get("doc_type") or "?").ljust(10)
        conf = (r.get("confidence") or "").ljust(6)
        src = r.get("source_type", "")
        fname = r.get("filename") or "(unknown)"
        print(f"  [{i:>2}] {date}  [{dtype}] [{conf}] [{src}]  {fname[:50]}")


def cmd_doc_show(args):
    from memory_store.store import list_documents, find_document
    query = " ".join(args.doc).lower()
    matched = None

    if query.isdigit():
        rows = list_documents(limit=200)
        idx = int(query) - 1
        if 0 <= idx < len(rows):
            matched = rows[idx]
    else:
        matched = find_document(query)

    if not matched:
        print(f"No document found for: {' '.join(args.doc)}")
        print("Use `docs` to see document numbers.")
        return

    fields = matched.get("fields") or {}
    print(f"\n{'='*60}")
    print(f"  {matched.get('filename')}")
    print(f"{'='*60}")
    print(f"  type       : {matched.get('doc_type')}")
    print(f"  confidence : {matched.get('confidence')}")
    print(f"  source     : {matched.get('source_type')}")
    print(f"  ingested   : {(matched.get('ingested_at') or '')[:10]}")
    print(f"  url        : {matched.get('file_url', '')}")
    print()
    if fields:
        for key, val in fields.items():
            if val is None:
                continue
            if isinstance(val, list):
                print(f"  {key}:")
                for item in val:
                    print(f"    - {item}")
            else:
                print(f"  {key}: {val}")
    else:
        print("  (no structured fields extracted)")


def main():
    p = argparse.ArgumentParser(description="Personal memory store")
    sub = p.add_subparsers(dest="cmd", required=True)

    # sync-notion
    s = sub.add_parser("sync-notion", help="Pull notes from Notion Notes [PT]")
    s.add_argument("--limit", type=int, default=None, help="Max notes to sync (for testing)")
    s.set_defaults(func=cmd_sync_notion)

    # search
    s = sub.add_parser("search", help="Semantic search across all notes")
    s.add_argument("query", nargs="+", help="Search query")
    s.add_argument("--n", type=int, default=5, help="Number of results (default 5)")
    s.add_argument("--domain", default=None, help="Filter by domain")
    s.set_defaults(func=cmd_search)

    # list
    s = sub.add_parser("list", help="List entries")
    s.add_argument("--domain", default=None, help="Filter by domain")
    s.add_argument("--since", default=None, help="From date e.g. 2026-01-01")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--full", action="store_true", help="Show full content of each entry")
    s.set_defaults(func=cmd_list)

    # show
    s = sub.add_parser("show", help="Show full content of one entry")
    s.add_argument("entry", nargs="+", help="Entry number (from list) or title keyword")
    s.set_defaults(func=cmd_show)

    # stats
    s = sub.add_parser("stats", help="Show store statistics")
    s.set_defaults(func=cmd_stats)

    # auth-gdrive
    s = sub.add_parser("auth-gdrive", help="Authenticate with Google Drive (one-time)")
    s.set_defaults(func=cmd_auth_gdrive)

    # ingest
    s = sub.add_parser("ingest", help="Process one or more files (local path or Drive URL)")
    s.add_argument("sources", nargs="+", help="File paths or Google Drive URLs")
    s.add_argument("--type", default=None,
                   choices=["lease", "visa", "medical", "insurance", "tax", "id", "other"],
                   help="Skip classification and use this doc type")
    s.add_argument("--force", action="store_true", help="Reprocess even if already ingested")
    s.set_defaults(func=cmd_ingest)

    # docs
    s = sub.add_parser("docs", help="List ingested documents")
    s.add_argument("--type", default=None,
                   choices=["lease", "visa", "medical", "insurance", "tax", "id", "other"],
                   help="Filter by document type")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(func=cmd_docs)

    # doc-show
    s = sub.add_parser("doc-show", help="Show extracted fields for one document")
    s.add_argument("doc", nargs="+", help="Document number (from docs) or filename keyword")
    s.set_defaults(func=cmd_doc_show)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
