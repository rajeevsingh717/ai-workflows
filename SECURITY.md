# Security Policy

## Supported version

Security fixes are applied to the latest commit on the `main` branch.

## Reporting a vulnerability

Do not open a public issue for exposed credentials, personal-data leakage, command injection, or another security-sensitive problem. Use GitHub's private vulnerability reporting feature for this repository, if enabled, or contact the repository owner privately through their GitHub profile.

Include:

- the affected file and workflow;
- reproduction steps that do not expose real personal data;
- the potential impact;
- a suggested remediation, if known.

## Credential exposure

If a token, API key, OAuth credential, service-account key, Telegram bot token, or financial export is exposed:

1. Revoke or rotate it at the provider immediately.
2. Remove it from the current tree.
3. Inspect the complete Git history and CI logs.
4. Rewrite history when necessary; deleting the current file alone is insufficient.
5. Notify affected users or providers when personal data may have been accessed.

## Sensitive local data

This project intentionally excludes `.env`, service-account credentials, photo catalogs, Memory Store databases, generated research reports, and `fidelity_data/`. Contributors should use synthetic fixtures in tests, examples, issues, and pull requests.
