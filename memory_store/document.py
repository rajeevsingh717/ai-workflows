"""Classify and extract structured fields from documents using Claude."""
import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Supported file extensions
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".bmp"}
PDF_EXT = ".pdf"

# ── Extraction schemas per document type ─────────────────────────────────────

_SCHEMAS = {
    "lease": {
        "landlord": "string — landlord or property management company name",
        "tenant": "string — tenant name(s)",
        "property_address": "string — full rental property address",
        "monthly_rent": "number — monthly rent amount in USD",
        "security_deposit": "number — security deposit amount in USD",
        "lease_start": "string — lease start date YYYY-MM-DD",
        "lease_end": "string — lease end date YYYY-MM-DD",
        "notice_period_days": "number — days notice required to terminate",
        "utilities_included": "array of strings — utilities covered by landlord",
        "pet_policy": "string — pet rules",
        "key_clauses": "array of strings — other important terms or restrictions",
    },
    "visa": {
        "holder_name": "string — full name of visa/document holder",
        "doc_type": "string — e.g. H1B Visa, Green Card, Passport, EAD",
        "visa_category": "string — visa classification code e.g. H-1B, F-1, L-1",
        "nationality": "string — holder's nationality/country of citizenship",
        "passport_number": "string — passport number if present",
        "issue_date": "string — issue date YYYY-MM-DD",
        "expiry_date": "string — expiration date YYYY-MM-DD",
        "issuing_authority": "string — issuing country, embassy or agency",
        "entry_type": "string — Single, Multiple, etc.",
        "restrictions": "string — any restrictions or special conditions",
    },
    "medical": {
        "provider": "string — hospital, clinic or healthcare provider name",
        "doctor": "string — treating doctor or physician name",
        "visit_date": "string — date of visit or report YYYY-MM-DD",
        "diagnosis": "string — diagnosis or medical findings",
        "medications": "array of strings — prescribed medications and dosages",
        "test_results": "array of strings — lab or test results with values",
        "follow_up_date": "string — follow-up appointment date YYYY-MM-DD if any",
        "notes": "string — additional notes or instructions",
    },
    "insurance": {
        "insurer": "string — insurance company name",
        "policy_number": "string — policy or certificate number",
        "coverage_type": "string — type of insurance e.g. health, auto, renters",
        "premium": "string — premium amount and frequency e.g. $150/month",
        "coverage_start": "string — coverage start date YYYY-MM-DD",
        "coverage_end": "string — coverage end date YYYY-MM-DD",
        "insured_items": "array of strings — what is covered",
        "deductible": "string — deductible amount",
    },
    "tax": {
        "tax_year": "number — tax year e.g. 2025",
        "filing_status": "string — Single, Married Filing Jointly, etc.",
        "gross_income": "number — total gross income in USD",
        "total_deductions": "number — total deductions in USD",
        "tax_owed": "number — total tax owed in USD",
        "refund_amount": "number — refund amount in USD if applicable",
        "filing_date": "string — date filed YYYY-MM-DD",
    },
    "id": {
        "holder_name": "string — full name",
        "doc_type": "string — Passport, Driver License, State ID, etc.",
        "id_number": "string — document ID or number",
        "date_of_birth": "string — date of birth YYYY-MM-DD",
        "nationality": "string — nationality or issuing country",
        "issue_date": "string — issue date YYYY-MM-DD",
        "expiry_date": "string — expiry date YYYY-MM-DD",
        "issuing_authority": "string — issuing agency or country",
        "address": "string — address on document if present",
    },
}

_CLASSIFY_PROMPT = """\
You are a document classifier. Read the beginning of this document and identify its type.

Document text:
{text}

Classify as one of: lease, visa, medical, insurance, tax, id, other

Reply with JSON only — no prose, no markdown:
{{"doc_type": "...", "confidence": "high|medium|low", "reason": "one short sentence"}}"""

_EXTRACT_PROMPT = """\
You are a document data extractor. Extract structured information from this {doc_type} document.

Return ONLY a JSON object with these fields (use null for any field not found):
{schema}

Document text:
{text}

Return ONLY the JSON object. No explanation, no markdown."""


def _claude(prompt: str) -> str:
    """Call Claude claude-sonnet-4-6 and return the text response."""
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic package not installed. Run: pip install anthropic")

    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    msg = client.messages.create(
        model=model,
        max_tokens=2048,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def _parse_json(text: str) -> dict:
    """Parse JSON from Claude response, stripping markdown fences if present."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return json.loads(text)


def classify(text: str) -> tuple[str, str, str]:
    """
    Classify a document from its text.
    Returns (doc_type, confidence, reason).
    """
    sample = text[:1200]
    response = _claude(_CLASSIFY_PROMPT.format(text=sample))
    data = _parse_json(response)
    return (
        data.get("doc_type", "other"),
        data.get("confidence", "low"),
        data.get("reason", ""),
    )


def extract_fields(doc_type: str, text: str) -> dict:
    """Extract structured fields for a known document type."""
    if doc_type not in _SCHEMAS:
        return {}

    schema_lines = "\n".join(
        f'  "{k}": {v}' for k, v in _SCHEMAS[doc_type].items()
    )
    schema_str = "{\n" + schema_lines + "\n}"

    response = _claude(_EXTRACT_PROMPT.format(
        doc_type=doc_type,
        schema=schema_str,
        text=text[:6000],
    ))
    return _parse_json(response)


def process_file(source: str, doc_type_hint: str = None) -> dict:
    """
    Main entry point. Accepts a local file path or Google Drive URL.
    Returns a result dict with doc_type, confidence, fields, entry_id, doc_id.
    """
    from memory_store.store import upsert, upsert_document, document_exists

    # ── 1. Resolve source ────────────────────────────────────────────────────
    is_drive = source.startswith("http") or re.match(r"^[a-zA-Z0-9_-]{25,}$", source)

    if is_drive:
        from memory_store.ingest.gdrive import download_file, extract_file_id
        print(f"  ↳ downloading from Google Drive...")
        local_path, filename, file_id = download_file(source)
        source_id = file_id
        source_type = "gdrive"
        file_url = f"https://drive.google.com/file/d/{file_id}/view"
    else:
        local_path = str(Path(source).expanduser().resolve())
        filename = Path(local_path).name
        source_id = local_path
        source_type = "local"
        file_url = f"file://{local_path}"

    # ── 2. Dedup check ───────────────────────────────────────────────────────
    if document_exists(source_id):
        print(f"  ↳ already processed — skipping (use --force to reprocess)")
        return {"skipped": True, "source_id": source_id}

    # ── 3. Extract text ──────────────────────────────────────────────────────
    suffix = Path(local_path).suffix.lower()
    if suffix not in IMAGE_EXTS | {PDF_EXT}:
        raise ValueError(f"Unsupported document type: {suffix or '(no extension)'}")
    file_type = "image" if suffix in IMAGE_EXTS else "pdf"

    if file_type == "pdf":
        from memory_store.ingest.pdf import extract_pdf_text, is_scanned
        print(f"  ↳ extracting text from PDF...")
        text = extract_pdf_text(local_path)
        if is_scanned(text):
            print(f"  ↳ scanned PDF detected — using vision OCR...")
            from memory_store.ingest.image_doc import extract_image_text
            text = extract_image_text(local_path)
    else:
        print(f"  ↳ extracting text from image via vision...")
        from memory_store.ingest.image_doc import extract_image_text
        text = extract_image_text(local_path)

    if not text.strip():
        print(f"  ↳ ⚠ no text extracted")
        text = f"[No text extracted from {filename}]"

    # ── 4. Classify ──────────────────────────────────────────────────────────
    if doc_type_hint:
        doc_type = doc_type_hint
        confidence = "high"
        reason = "manually specified"
    else:
        print(f"  ↳ classifying document type...")
        doc_type, confidence, reason = classify(text)

    print(f"  ↳ type: {doc_type}  confidence: {confidence}  ({reason})")

    # ── 5. Extract fields ────────────────────────────────────────────────────
    fields = {}
    if doc_type != "other" and confidence in ("high", "medium"):
        print(f"  ↳ extracting {doc_type} fields...")
        fields = extract_fields(doc_type, text)
    else:
        print(f"  ↳ skipping field extraction (type=other or low confidence)")

    # ── 6. Store in memory_entries (searchable) ───────────────────────────────
    entry_id = upsert({
        "source_type": source_type,
        "source_id": f"doc::{source_id}",
        "title": filename,
        "content": text,
        "tags": [doc_type],
        "domain": _doc_type_to_domain(doc_type),
        "source_url": file_url,
        "created_at": None,
        "updated_at": None,
        "metadata": {"doc_type": doc_type, "confidence": confidence, "fields": fields},
    })

    # ── 7. Store in documents (structured) ───────────────────────────────────
    doc_id = upsert_document({
        "entry_id": entry_id,
        "source_type": source_type,
        "source_id": source_id,
        "filename": filename,
        "file_type": file_type,
        "doc_type": doc_type,
        "confidence": confidence,
        "fields": fields,
        "file_url": file_url,
    })

    return {
        "skipped": False,
        "filename": filename,
        "doc_type": doc_type,
        "confidence": confidence,
        "fields": fields,
        "entry_id": entry_id,
        "doc_id": doc_id,
    }


def _doc_type_to_domain(doc_type: str) -> str:
    return {
        "lease": "personal",
        "visa": "personal",
        "medical": "health",
        "insurance": "personal",
        "tax": "personal",
        "id": "personal",
    }.get(doc_type, "general")
