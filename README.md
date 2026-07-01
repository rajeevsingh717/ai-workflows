# AI Workflows

A collection of personal AI automation tools running locally on Mac.

---

## Projects

| Project | Entry point | What it does |
|---|---|---|
| [Memory Store](#memory-store) | `memory_store/cli.py` | Personal knowledge base — sync notes from Notion, search semantically |
| [Photo Organizer](#photo-organizer) | `photo_organize.py` | Classify, album, and back up Apple Photos using local AI |
| [Deep Research](#deep-research) | `main.py` | Multi-iteration internet research reports via LangGraph |

---

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
```

Required keys per project:

```
# Memory Store
NOTION_TOKEN=secret_...

# Photo Organizer
GCS_BUCKET=your-bucket-name
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json

# Deep Research
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-6   # optional, this is the default
```

---

## Memory Store

A local personal knowledge base. Pulls notes from Notion, stores them in SQLite + a local vector database, and lets you search semantically across everything.

**Local-first — no data leaves your machine.**

### How it works

```
Notion Notes [PT]
       ↓
  notion.py          ← fetches pages + block content via Notion API
       ↓
  store.py           ← writes to SQLite (structured) + ChromaDB (vectors)
       ↓
  cli.py             ← search, list, show
```

### Prerequisites

**Notion integration token:**

1. Go to [notion.so/profile/integrations](https://www.notion.so/profile/integrations)
2. Click **New integration** → name it `memory-store` → Save
3. Copy the `secret_...` token → paste into `.env` as `NOTION_TOKEN=secret_...`
4. In Notion, open **Notes [PT]** → `...` menu → **Connect to** → select `memory-store`

### Commands

#### Sync from Notion

Pull all notes from the **Notes [PT]** database into the local store. Safe to re-run — already-synced notes are updated, not duplicated.

```bash
python memory_store/cli.py sync-notion

# Test with a small batch first
python memory_store/cli.py sync-notion --limit 5
```

#### Search

Semantic search — finds relevant notes even if the exact words don't match.

```bash
python memory_store/cli.py search "sql interview preparation"
python memory_store/cli.py search "german grammar exercises"
python memory_store/cli.py search "back pain treatment"

# Filter by domain
python memory_store/cli.py search "study plan" --domain work
python memory_store/cli.py search "diet food" --domain personal

# Return more results
python memory_store/cli.py search "career goals" --n 10
```

**Domains:** `work` · `study` · `personal` · `health` · `ideas` · `general`

#### List

Browse all entries as a numbered index.

```bash
# All entries (most recent first)
python memory_store/cli.py list

# Filter by domain
python memory_store/cli.py list --domain study
python memory_store/cli.py list --domain work

# Filter by date
python memory_store/cli.py list --since 2026-01-01

# Increase the limit
python memory_store/cli.py list --limit 100

# Show full content inline (good for piping to less)
python memory_store/cli.py list --full
python memory_store/cli.py list --domain personal --full | less
```

#### Show

Display the full content of a single entry.

```bash
# By number from the list
python memory_store/cli.py show 8

# By title keyword (case-insensitive, first match wins)
python memory_store/cli.py show "faang"
python memory_store/cli.py show "food for anu"
python memory_store/cli.py show "german class"
python memory_store/cli.py show "meta interview"
```

#### Stats

Overview of what's in the store.

```bash
python memory_store/cli.py stats
```

Example output:
```
Total entries: 22

By domain:
  study        10
  work          5
  personal      5
  ideas         1
  general       1

By source:
  notion       22
```

### Data model

Each entry in `memory_store/memory.db`:

| Field | Description |
|---|---|
| `id` | UUID (local) |
| `source_type` | `notion` — more sources coming |
| `source_id` | Notion page ID (used for dedup on re-sync) |
| `title` | Page title |
| `content` | Full extracted text from the page body |
| `tags` | Multi-select tags from Notion |
| `domain` | Inferred: `work`, `study`, `personal`, `health`, `ideas`, `general` |
| `project` | Linked Notion project name (if set) |
| `area` | Linked Notion area/resource name (if set) |
| `source_url` | Notion page URL |
| `created_at` | Original creation time in Notion |
| `updated_at` | Last edited time in Notion |
| `ingested_at` | When it was pulled into this store |
| `metadata` | JSON blob with raw Notion metadata |

Vectors live in `memory_store/chroma/` (local ChromaDB, `all-MiniLM-L6-v2` model, downloads once ~80MB).

### File layout

```
memory_store/
├── cli.py          ← entry point for all commands
├── store.py        ← SQLite + ChromaDB read/write API
├── ingest/
│   └── notion.py   ← Notion API ingestion pipeline
├── memory.db       ← SQLite database (created on first sync)
└── chroma/         ← vector store (created on first sync)
```

### Planned sources

- Voice memos / recordings (iPhone `.m4a` → Whisper transcript)
- PDF files
- Word documents (`.docx`)
- Images (via Ollama vision — already used in Photo Organizer)

---

## Photo Organizer

A CLI tool to classify, organize, and archive Apple Photos using a local Ollama vision model and Google Cloud Storage (GCS).

Photos are never deleted automatically — all deletions are done manually in Photos.app so you stay in full control.

### Prerequisites

**Ollama** — install from [ollama.com](https://ollama.com), then:
```bash
ollama pull llama3.2-vision
ollama serve   # run in a separate terminal before using vision commands
```

**GCP service account** at `~/.config/gcp/photo-archiver-sa.json` with `roles/storage.objectAdmin` on your bucket.

**Photos.app access** — grant Terminal full access in **System Settings → Privacy & Security → Photos**.

### Commands

#### `download` — Pull iCloud originals to Mac

```bash
python photo_organize.py download
```

Downloads all iCloud-only photos locally. Required before running `vision` on iCloud photos. Check disk space first — 1000 photos can be several GB.

#### `vision` — Classify photos with AI

```bash
# Full run (use caffeinate to prevent sleep)
caffeinate -i python photo_organize.py vision --out rajeev_vision_labels.json

# Test on a small batch first
python photo_organize.py vision --limit 50 --out rajeev_vision_labels.json

# Resume a previous run (already-classified photos are skipped)
python photo_organize.py vision --out rajeev_vision_labels.json
```

Options:

| Flag | Default | Description |
|---|---|---|
| `--out` | `vision_labels.json` | Output JSON file |
| `--limit N` | none | Only classify first N photos |
| `--model` | `llama3.2-vision` | Ollama model to use |
| `--library` | system default | Photos library path |

Labels: `selfie` · `people` · `group` · `landscape` · `nature` · `food` · `object` · `document` · `animal` · `vehicle` · `building` · `other`

#### `vision-albums` — Create albums in Photos.app

```bash
python photo_organize.py vision-albums rajeev_vision_labels.json
```

Creates `Vision-Selfie`, `Vision-People`, etc. albums. Safe to re-run.

#### `backup` — Upload to GCS

```bash
python photo_organize.py backup rajeev_vision_labels.json
```

Uploads to `gs://<GCS_BUCKET>/Vision-<Label>/`. Already-uploaded files are skipped.

GCS layout:
```
gs://rajeev-iphone-photo-archive/
├── metadata/catalog/vision_labels_2026-06-22.json
├── Vision-Selfie/
├── Vision-People/
├── Vision-Landscape/
└── ...
```

#### `plan` / `execute` — Rule-based classification (optional)

Quick pass using metadata rules (blur, darkness, face tags) without AI:

```bash
python photo_organize.py plan --out plan.json
python photo_organize.py execute plan.json
```

### Full workflow

```bash
# 1. Start Ollama (separate terminal)
ollama serve

# 2. Download iCloud-only photos
python photo_organize.py download

# 3. Classify with AI (auto-saves every 10, resume-safe)
caffeinate -i python photo_organize.py vision --out rajeev_vision_labels.json

# 4. Create Vision-* albums in Photos.app
python photo_organize.py vision-albums rajeev_vision_labels.json

# 5. Review albums in Photos.app — delete what you don't want

# 6. Back up to GCS
python photo_organize.py backup rajeev_vision_labels.json
```

### Tips

**Check progress mid-run:**
```bash
python -c "
import json
from collections import Counter
d = json.load(open('vision_labels.json'))
print(f'Classified: {len(d)}')
for label, count in Counter(v.get('label','?') for v in d.values()).most_common():
    print(f'  {label:15s} {count}')
"
```

**Check local vs iCloud counts:**
```bash
source .venv/bin/activate
python -c "
import osxphotos
db = osxphotos.PhotosDB()
photos = db.photos(images=True, movies=False)
print(f'Local: {sum(1 for p in photos if not p.ismissing)}')
print(f'iCloud only: {sum(1 for p in photos if p.ismissing)}')
print(f'Total: {len(photos)}')
"
```

---

## Deep Research

A LangGraph workflow that researches any topic iteratively — generates sub-questions, searches the web and Reddit, synthesizes findings, reflects on gaps, and loops until it has enough to write a full report.

### Prerequisites

`ANTHROPIC_API_KEY` in `.env`. Uses `claude-sonnet-4-6` by default (override with `ANTHROPIC_MODEL`).

### Usage

```bash
python main.py "your research topic"

# More thorough (more iterations = more sources)
python main.py "impact of sleep on athletic performance" --max-iterations 3

# Save report to file
python main.py "best practices for data pipeline monitoring" --out Armpit Lymph Node Hard Swelling, Cancer, and Mammogram.md
```

### How it works

```
plan → search_web ─┐
                   ├→ analyze → reflect ─→ (loop back or) summarize
     search_reddit ┘
```

Each iteration:
1. **Plan** — Claude generates 3–5 focused sub-questions
2. **Search** — Tavily (web) + DuckDuckGo (Reddit) run in parallel
3. **Analyze** — Claude synthesizes findings into bullet points with citations
4. **Reflect** — Claude decides if gaps remain; if yes, loops with new sub-questions
5. **Summarize** — Final structured markdown report with executive summary, key findings, community perspectives, and numbered sources

### Output format

```markdown
# Research Report: <topic>

## Executive Summary
## Key Findings
## Detailed Analysis
## Community Perspectives (Reddit)
## Open Questions / Limitations
## Sources
```
