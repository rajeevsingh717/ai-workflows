# Contributing

Thank you for improving AI Workflows.

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

## Pull-request guidelines

- Keep each change focused on one workflow or shared concern.
- Add or update tests for parsing, deduplication, matching, or scheduling logic.
- Use synthetic data only. Never commit real photos, notes, documents, account numbers, holdings, transactions, generated personal reports, or credentials.
- Update `README.md` and `.env.example` when adding commands or configuration.
- Preserve the safety boundaries: no automatic photo deletion and no automatic trade execution.
- Clearly label AI-generated portfolio commentary as informational, not advice.

## Before submitting

```bash
python -m unittest discover -s tests -v
python -m compileall -q \
  *.py memory_store
git diff --check
```

Run a secret scanner against your branch before opening a pull request.
