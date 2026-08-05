# AI Workflows

A collection of personal AI automation tools running locally on Mac.

---

## Projects

| Project | Entry point | What it does |
|---|---|---|
| [Memory Store](#memory-store) | `memory_store/cli.py` | Personal knowledge base — sync notes from Notion, search semantically |
| [Photo Organizer](#photo-organizer) | `photo_organize.py` | Classify, album, and back up Apple Photos using local AI |
| [Deep Research](#deep-research) | `main.py` | Multi-iteration internet research reports via LangGraph |
| [Portfolio Analyzer](#portfolio-analyzer) | `portfolio_analyzer.py` / `market_check.py` / `trade_analyzer.py` | Analyze a Fidelity CSV export; live market check via Claude; look up your own past buy/sell prices |

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

# Portfolio Analyzer (market_check.py only — portfolio_analyzer.py needs nothing)
FINNHUB_API_KEY=...                 # free at finnhub.io/register
TELEGRAM_BOT_TOKEN=...              # optional — pushes the daily summary to Telegram
TELEGRAM_CHAT_ID=...                # optional — see "Telegram push" below
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

---

## Portfolio Analyzer

Four scripts that work together to analyze a Fidelity account export:

- `portfolio_analyzer.py` — one-shot analysis of a CSV export: allocation, concentration risk, performance, tax-location check, charts
- `market_check.py` — optional daily add-on that marks current holdings to live market prices and asks Claude to flag anything worth a second look
- `trade_analyzer.py` — no AI, just your own buy/sell history per symbol next to a live quote, so you're never guessing what price you paid last time
- `telegram_listener.py` — remote control for `trade_analyzer.py` from your phone via Telegram

All four are informational only — **none of them ever place a trade or tell you to buy/sell anything.** Everything under `fidelity_data/` (your actual account data, reports, charts) is gitignored and never leaves your machine; no data is uploaded anywhere.

### Prerequisites

**A Fidelity CSV export.** From Fidelity.com: Accounts & Trade → Portfolio → Positions → **Download** (or **Export**). Save it into `fidelity_data/` in this repo — the filename pattern `Portfolio_Positions_<date>.csv` (Fidelity's default) is what both scripts look for.

**Finnhub API key** (for `market_check.py` only) — free at [finnhub.io/register](https://finnhub.io/register), free tier is 60 calls/min which comfortably covers a few dozen holdings. Add it to `.env` as `FINNHUB_API_KEY`.

**Anthropic API key** (for `market_check.py` only) — same `ANTHROPIC_API_KEY` used by Deep Research.

**Telegram bot** (optional, for pushing the daily summary to your phone instead of only a local file):
1. Message [@BotFather](https://t.me/BotFather) in Telegram → `/newbot` → copy the token it gives you into `.env` as `TELEGRAM_BOT_TOKEN`
2. Generate one update so Telegram has something to show: message your new bot directly (any text works), or — for a channel instead of a DM — add the bot as a channel admin (needs "Post Messages" permission) and post any message there
3. Look up your `chat_id`:
   ```bash
   python3 -c "
   from dotenv import load_dotenv; load_dotenv()
   import os, httpx
   token = os.getenv('TELEGRAM_BOT_TOKEN')
   print(httpx.get(f'https://api.telegram.org/bot{token}/getUpdates').json())
   "
   ```
   The `chat.id` field in the response is what goes in `.env` as `TELEGRAM_CHAT_ID`.

If both vars are set, `market_check.py` pushes the header + Claude's summary (not the full raw-data dump) to that chat every run. If unset, it silently skips Telegram and just writes the local report file as before.

### `portfolio_analyzer.py` — one-shot CSV analysis

```bash
python portfolio_analyzer.py                      # auto-picks the newest
                                                    # fidelity_data/Portfolio_Positions_*.csv
python portfolio_analyzer.py path/to/export.csv    # or point at one explicitly
```

Produces, all written to `fidelity_data/output/`:

- **Overview** — total value, breakdown by account (with tax status: taxable brokerage vs. Roth/Traditional IRA/HSA/401(k)), unique holdings
- **Asset allocation** — by class (stock/ETF/mutual fund/money market/target-date) and by sector, as horizontal bar charts
- **Concentration risk** — single positions >10% of the portfolio, sectors >30% (excluding inherently-diversified buckets like broad index funds), and overlapping funds doing the same job (e.g. holding both VOO and SPY)
- **Performance** — gain/loss % per position, top winners/losers, overall portfolio return
- **Tax-location check** — flags high-dividend/high-income funds sitting in a taxable account that would usually be more efficient in a Roth/IRA/HSA
- **Charts** — asset allocation, sector breakdown, account distribution, top 10 holdings (PNGs)
- **Observations** — plain data-driven notes, explicitly framed as not-advice

Sector/asset-class classification is a hand-maintained table (`SECURITY_INFO`) in the script itself — no live data lookup, no API key needed to run this one. If a new export has a symbol the table doesn't recognize, the script prints exactly what to add.

### `market_check.py` — live morning check

```bash
python market_check.py
```

Runs a mark-to-market pass against your current holdings and an LLM summary:

1. Loads the newest `positions_enriched_*.csv` (output of `portfolio_analyzer.py` — run that first)
2. Checks Finnhub's market-status endpoint; if the market's closed (weekend, holiday, after-hours) it prints that and exits without calling the LLM, so you never get a summary based on stale prices
3. Pulls a live quote per holding, recomputes portfolio value/allocation % with today's prices
4. Re-runs the same concentration/overlap/tax-location rules from `portfolio_analyzer.py` against the live numbers
5. Sends the day's movers plus any already-flagged positions to Claude, which writes 4-8 bullet observations — hard-blocked by its system prompt from ever saying "buy," "sell," or naming a share count or price target
6. Saves the report to `fidelity_data/output/market_check_<date>_<time>.txt`
7. If `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are set, pushes the summary to Telegram too — otherwise this step is a silent no-op

### Keeping transaction history over time

Fidelity's transaction export only covers whatever date range you picked when downloading it — it's not a full history. To avoid silently losing older trades once they age out of your next export, `trade_analyzer.py` (and `telegram_listener.py`, through it) auto-merges every `Accounts_History*.csv` in `fidelity_data/order_transactions/` into a single deduplicated `master_transactions.csv` on every run, via `transaction_store.py`.

**Workflow:** whenever you have a new export, just drop the CSV into `fidelity_data/order_transactions/` — no renaming, no manual merge step. The next time you run `trade_analyzer.py` or message the Telegram bot, new transactions are automatically folded into the master ledger (deduplicated by date/account/action/symbol/price/quantity/amount, so re-uploading overlapping date ranges is harmless — already-seen transactions are recognized and skipped, never double-counted). Nothing already in the ledger is ever removed just because a later export doesn't happen to include it.

```bash
python merge_transactions.py   # optional — merges and reports what was added,
                                # useful right after uploading a new export if
                                # you want to see the count before running anything else
```

### `trade_analyzer.py` — "what did I actually pay for this last time"

```bash
python trade_analyzer.py AMD    # full buy/sell history + live price for one symbol
python trade_analyzer.py        # realized P&L summary across every symbol you've traded
```

No AI involved — this is a pure lookup tool for the very common problem of forgetting what price you paid last time you bought or sold something, so you can't tell if selling today would lock in a gain or a loss.

Reads the accumulated `master_transactions.csv` (see "Keeping transaction history over time" above — export from Fidelity: Accounts & Trade → Activity & Orders → **Download**), FIFO-matches your buys to sells **per account per symbol** (tax lots are account-specific), and:

- **Per symbol** (`python trade_analyzer.py AMD`) — every buy/sell in the export with date/price/account, a live quote next to each one showing the gain/loss if that lot were sold right now, which lots are still open, and which round-trips already closed with their realized gain/loss and holding period. Since the export only covers a fixed date range, any shares bought before that window won't have an exact price on record here — those are called out explicitly and backed up by Fidelity's own blended average cost basis (from `portfolio_analyzer.py`'s enriched CSV) so nothing is silently missing, just less precise.
- **No symbol** (`python trade_analyzer.py`) — realized P&L total, win rate, avg winner/loser, total fees paid, dividends received, and your most-traded symbols, so you can see honestly whether frequent trading in a name has actually been costing you or not.

`CVSA` is deliberately skipped for live quotes here too, for the same reason as in `market_check.py` — see `NO_QUOTE_SYMBOLS`.

### `telegram_listener.py` — remote control from your phone

`market_check.py`'s Telegram integration is one-way (script → your phone). This adds the other direction: message your bot and get `trade_analyzer.py` results back, without touching the laptop.

```bash
python telegram_listener.py   # foreground, for testing
./setup_telegram_listener.sh  # installs as an always-on launchd daemon
```

Long-polls Telegram for new messages and replies with the same output `trade_analyzer.py` prints locally:

| Message | Reply |
|---|---|
| `AMD` (or any symbol) | That symbol's full buy/sell history + live price |
| `summary` | Realized P&L summary across everything you've traded |
| `help` | Lists these commands |

**Security:** only messages from your own `TELEGRAM_CHAT_ID` (set in `.env`) are ever acted on — anything from a different chat is logged locally and silently ignored, never replied to. Symbol input is validated against a strict pattern before being used, and every command maps to a fixed, pre-written function — there's no way to get it to run arbitrary code.

**Reality check on "remote":** this is a local listener, not a cloud service. Your Mac still needs to be powered on, awake, and connected to the internet for a message to do anything — Telegram polling doesn't need any inbound ports opened, but it does need the process running. If you want something that works even with the Mac fully off, that requires actually hosting the scripts (and your Fidelity data) somewhere else, which is a different, bigger project — this keeps everything local by design, same as every other script in this repo.

Installed via `setup_telegram_listener.sh` as `com.rajeevsingh.telegramlistener` — unlike the scheduled `market_check.py` job, this one uses `RunAtLoad` + `KeepAlive` so it starts immediately and relaunches itself if it ever crashes:

```bash
launchctl list | grep telegramlistener                                      # confirm it's running
tail -f fidelity_data/output/telegram_listener.log                          # watch activity
launchctl unload ~/Library/LaunchAgents/com.rajeevsingh.telegramlistener.plist   # stop it
```

### Running it automatically at market open

`setup_launchd.sh` installs a macOS `launchd` job (`com.rajeevsingh.marketcheck.plist`) that runs `market_check.py` three times on weekdays — 9:35am (open), 12:30pm (midday), and 3:45pm (close), local time:

```bash
./setup_launchd.sh
```

```bash
launchctl list | grep marketcheck                     # confirm it's loaded
launchctl start com.rajeevsingh.marketcheck            # trigger a run right now, for testing
tail -f fidelity_data/output/market_check.log          # watch stdout
launchctl unload ~/Library/LaunchAgents/com.rajeevsingh.marketcheck.plist   # stop it
```
