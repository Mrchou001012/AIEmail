# AI Sales Agent

A reference implementation for a bounded AI-assisted sales inbox. The AI layer
extracts intent and drafts responses; deterministic application code controls
pricing, attachments, approvals, rate limits, and delivery.

This public repository contains fictional demo data only. Company identities,
mailboxes, infrastructure names, customer data, product identifiers, commercial
rules, and deployment coordinates belong in ignored local configuration or a
private secret/configuration store.

## What is included

- FastAPI administration API and review pages
- PostgreSQL-backed customers, messages, cases, jobs, and outbox records
- IMAP ingestion and SMTP/file delivery adapters
- Product-list, quotation, follow-up, forwarding, and COA workflows
- Human-review gates, safe mode, allowlists, pacing, and audit events
- Optional retrieval from a policy-classified document source
- A stub AI provider for deterministic local development

The tracked catalog, signatures, document policy, email addresses, and PDF are
demonstration fixtures. Replace them through local configuration before using
the application for real business activity.

## Local demo with Docker

Requirements: Docker Desktop with Compose.

```powershell
Copy-Item .env.example .env
docker compose build
docker compose run --rm migrate
docker compose up -d --wait
```

The default API is available at `http://localhost:8000`. Interactive API docs
are enabled only in demo mode. The sample administration credentials are stored
in `.env.example` and must not be used outside local development.

To seed and run the file-delivery demo:

```powershell
./scripts/demo.ps1
```

Generated messages are written under `runtime/demo_outbox/` and are ignored by
Git.

## Native Python development

Python 3.12 or newer is required.

```powershell
python -m venv .venv
./.venv/Scripts/python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Run the checks with:

```powershell
./.venv/Scripts/python -m ruff check app tests scripts
./.venv/Scripts/python -m mypy app
./.venv/Scripts/python -m pytest
```

PostgreSQL integration tests require an explicitly configured test database.
Never point tests at a production database.

## Configuration

Settings are loaded from environment variables and an ignored `.env` file.
Start from `.env.example`, which intentionally contains only safe demo values.

Important safety defaults include:

- file delivery instead of SMTP;
- safe mode enabled;
- automatic sending disabled;
- demo-only recipient allowlist;
- external document retrieval disabled;
- attachment and quotation auto-send disabled.

Keep production values out of tracked files. In particular, configure these
locally:

- database and provider credentials;
- sender mailbox and internal domains;
- public application URL;
- company content directory;
- product catalog and aliases (`PRODUCT_CATALOG_PATH` and
  `PRODUCT_ALIASES_PATH`);
- document-source mount and classification policy;
- COA catalog and approved document root;
- recipient allowlists and all automatic-send switches.

## Public-repository data policy

Do not commit any of the following:

- real domains, email addresses, employee names, signatures, or logos;
- server names, IP addresses, service paths, mount points, or share names;
- customer or mailbox exports, `.eml` files, logs, databases, or credentials;
- proprietary product codes, pricing rules, stock data, or commercial terms;
- internal rollout checklists or operational URLs;
- real catalogs, certificates, quotations, CRM workbooks, or NAS indexes.

Use reserved domains such as `example.com`, generic paths such as
`/mnt/company-data`, and clearly fictional identifiers such as `DEMO-P100` in
tracked examples.

Before publishing a change, run a tracked-file scan appropriate to your
organization, for example:

```powershell
rg -n -i --hidden --glob '!.git/**' `
  'real-domain|employee-name|server-name|share-name|proprietary-prefix' .
```

This scan is only a guardrail. Review binary assets such as PDFs and office
documents separately.

## Private deployment notes

Production topology, company mappings, and operating procedures are deliberately
not documented in this public README. Keep them in a private runbook or an
ignored local AI handoff file. A public source repository cannot conceal the
application's executable behavior; use a private repository if the workflow
logic itself is confidential.
