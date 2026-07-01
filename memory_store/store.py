"""Core memory store: SQLite for structured data, ChromaDB for semantic search."""
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import chromadb
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

STORE_DIR = Path(__file__).parent
DB_PATH = STORE_DIR / "memory.db"
CHROMA_PATH = STORE_DIR / "chroma"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id              TEXT PRIMARY KEY,
    entry_id        TEXT,            -- FK → memory_entries (for full-text search)
    source_type     TEXT NOT NULL,   -- local | gdrive
    source_id       TEXT UNIQUE,     -- absolute path or Drive file ID
    filename        TEXT,
    file_type       TEXT,            -- pdf | image
    doc_type        TEXT,            -- lease | visa | medical | insurance | tax | id | other
    confidence      TEXT,            -- high | medium | low
    fields          TEXT,            -- JSON extracted fields
    file_url        TEXT,
    ingested_at     TEXT
);

CREATE TABLE IF NOT EXISTS memory_entries (
    id           TEXT PRIMARY KEY,
    source_type  TEXT NOT NULL,
    source_id    TEXT UNIQUE,
    title        TEXT,
    content      TEXT,
    tags         TEXT,       -- JSON array
    domain       TEXT,
    project      TEXT,
    area         TEXT,
    source_url   TEXT,
    created_at   TEXT,       -- ISO8601 from source
    updated_at   TEXT,       -- ISO8601 from source
    ingested_at  TEXT,       -- ISO8601 when pulled into store
    metadata     TEXT        -- JSON blob for source-specific extras
);
"""


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    conn.commit()
    return conn


def _chroma():
    client = chromadb.PersistentClient(path=str(CHROMA_PATH))
    return client.get_or_create_collection(
        "memory",
        embedding_function=DefaultEmbeddingFunction(),
    )


def upsert(entry: dict) -> str:
    """Add or update an entry by source_id. Returns the entry id."""
    now = datetime.now(timezone.utc).isoformat()

    # Reuse existing id if updating by source_id
    eid = entry.get("id")
    if not eid:
        with _db() as conn:
            row = conn.execute(
                "SELECT id FROM memory_entries WHERE source_id = ?",
                (entry.get("source_id"),),
            ).fetchone()
            eid = row["id"] if row else str(uuid.uuid4())

    with _db() as conn:
        conn.execute(
            """
            INSERT INTO memory_entries
                (id, source_type, source_id, title, content, tags, domain,
                 project, area, source_url, created_at, updated_at, ingested_at, metadata)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source_id) DO UPDATE SET
                title=excluded.title,
                content=excluded.content,
                tags=excluded.tags,
                domain=excluded.domain,
                project=excluded.project,
                area=excluded.area,
                source_url=excluded.source_url,
                updated_at=excluded.updated_at,
                ingested_at=excluded.ingested_at,
                metadata=excluded.metadata
            """,
            (
                eid,
                entry.get("source_type", "manual"),
                entry.get("source_id"),
                entry.get("title"),
                entry.get("content"),
                json.dumps(entry.get("tags") or []),
                entry.get("domain"),
                entry.get("project"),
                entry.get("area"),
                entry.get("source_url"),
                entry.get("created_at"),
                entry.get("updated_at"),
                now,
                json.dumps(entry.get("metadata") or {}),
            ),
        )

    content = (entry.get("content") or entry.get("title") or "").strip()
    if content:
        _chroma().upsert(
            ids=[eid],
            documents=[content],
            metadatas=[{
                "source_type": entry.get("source_type", "manual"),
                "title": entry.get("title") or "",
                "domain": entry.get("domain") or "",
                "tags": ",".join(entry.get("tags") or []),
            }],
        )
    return eid


def search(query: str, n: int = 10, domain: str = None) -> list[dict]:
    """Semantic search. Returns entries ranked by relevance."""
    col = _chroma()
    total = col.count()
    if total == 0:
        return []

    where = {"domain": domain} if domain else None
    results = col.query(
        query_texts=[query],
        n_results=min(n, total),
        where=where,
    )
    ids = results["ids"][0]
    distances = results["distances"][0]

    with _db() as conn:
        rows = []
        for eid, dist in zip(ids, distances):
            row = conn.execute(
                "SELECT * FROM memory_entries WHERE id = ?", (eid,)
            ).fetchone()
            if row:
                d = dict(row)
                # ChromaDB cosine distance is in [0,2]; map to similarity [0,1]
                d["score"] = round(max(0.0, 1 - dist / 2), 3)
                d["tags"] = json.loads(d["tags"] or "[]")
                rows.append(d)
    return rows


def list_entries(domain: str = None, since: str = None, limit: int = 50) -> list[dict]:
    """List entries ordered by creation date descending."""
    with _db() as conn:
        q = "SELECT * FROM memory_entries WHERE 1=1"
        params: list = []
        if domain:
            q += " AND domain = ?"
            params.append(domain)
        if since:
            q += " AND created_at >= ?"
            params.append(since)
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(q, params).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["tags"] = json.loads(d["tags"] or "[]")
        result.append(d)
    return result


def upsert_document(doc: dict) -> str:
    """Add or update a document record. Returns the document id."""
    now = datetime.now(timezone.utc).isoformat()
    did = doc.get("id")
    if not did:
        with _db() as conn:
            row = conn.execute(
                "SELECT id FROM documents WHERE source_id = ?", (doc.get("source_id"),)
            ).fetchone()
            did = row["id"] if row else str(uuid.uuid4())

    with _db() as conn:
        conn.execute(
            """
            INSERT INTO documents
                (id, entry_id, source_type, source_id, filename, file_type,
                 doc_type, confidence, fields, file_url, ingested_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source_id) DO UPDATE SET
                entry_id=excluded.entry_id,
                filename=excluded.filename,
                file_type=excluded.file_type,
                doc_type=excluded.doc_type,
                confidence=excluded.confidence,
                fields=excluded.fields,
                file_url=excluded.file_url,
                ingested_at=excluded.ingested_at
            """,
            (
                did,
                doc.get("entry_id"),
                doc.get("source_type"),
                doc.get("source_id"),
                doc.get("filename"),
                doc.get("file_type"),
                doc.get("doc_type"),
                doc.get("confidence"),
                json.dumps(doc.get("fields") or {}),
                doc.get("file_url"),
                now,
            ),
        )
    return did


def document_exists(source_id: str) -> bool:
    with _db() as conn:
        row = conn.execute(
            "SELECT id FROM documents WHERE source_id = ?", (source_id,)
        ).fetchone()
    return row is not None


def list_documents(doc_type: str = None, limit: int = 50) -> list[dict]:
    with _db() as conn:
        q = "SELECT * FROM documents WHERE 1=1"
        params: list = []
        if doc_type:
            q += " AND doc_type = ?"
            params.append(doc_type)
        q += " ORDER BY ingested_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(q, params).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["fields"] = json.loads(d["fields"] or "{}")
        result.append(d)
    return result


def find_document(query: str) -> dict | None:
    """Find a document by filename keyword."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM documents WHERE filename LIKE ? ORDER BY ingested_at DESC LIMIT 1",
            (f"%{query}%",),
        ).fetchall()
    if not rows:
        return None
    d = dict(rows[0])
    d["fields"] = json.loads(d["fields"] or "{}")
    return d


def stats() -> dict:
    """Return basic counts."""
    with _db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0]
        by_domain = conn.execute(
            "SELECT domain, COUNT(*) as n FROM memory_entries GROUP BY domain ORDER BY n DESC"
        ).fetchall()
        by_source = conn.execute(
            "SELECT source_type, COUNT(*) as n FROM memory_entries GROUP BY source_type ORDER BY n DESC"
        ).fetchall()
    with _db() as conn:
        doc_total = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        by_doc_type = conn.execute(
            "SELECT doc_type, COUNT(*) as n FROM documents GROUP BY doc_type ORDER BY n DESC"
        ).fetchall()
    return {
        "total": total,
        "by_domain": {r["domain"]: r["n"] for r in by_domain},
        "by_source": {r["source_type"]: r["n"] for r in by_source},
        "documents": doc_total,
        "by_doc_type": {r["doc_type"]: r["n"] for r in by_doc_type},
    }
