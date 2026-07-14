# AI Sales Agent MVP

A runnable, bounded sales-email workflow. Claude may extract facts and draft language; deterministic application code controls recipients, prices, state transitions, send eligibility, retries, and audit records.

## Safe demo defaults

The repository runs without external credentials:

- `AI_PROVIDER=stub`
- `MAIL_TRANSPORT=file` (writes RFC-compliant messages to `runtime/demo_outbox/`)
- `DINGTALK_TRANSPORT=log`
- `SAFE_MODE=true`
- `AUTO_SEND_ENABLED=false`
- SMTP recipients are blocked unless they are on `RECIPIENT_ALLOWLIST`

No real secrets are included. Copy `.env.example` to `.env` and fill it **locally** when you are ready. Do not paste keys, mailbox passwords, or webhook URLs into chat.

## Components

Docker Compose starts:

- `db`: PostgreSQL 16
- `migrate`: one-shot Alembic schema migration
- `api`: FastAPI on port 8000
- `worker`: PostgreSQL-backed job and outbox processor using `FOR UPDATE SKIP LOCKED`
- `imap`: read-only Gmail Inbox/Sent UID poller with bounded history batches

The schema covers customers, contacts, products, versioned price policies, cases, inbound/outbound emails, quotes, handoffs, jobs, outbox records, AI invocations, mailbox cursors, and append-only audit events. An inbound email can create at most one terminal handoff, while non-email handoffs remain unrestricted.

## Start the demo

PowerShell:

```powershell
cd D:\Downloads\ai-sales-agent
.\scripts\bootstrap.ps1
.\scripts\demo.ps1
```

Or manually:

```powershell
Copy-Item .env.example .env
docker compose build
docker compose run --rm migrate
docker compose up -d
```

Open:

- API docs: <http://localhost:8000/docs>
- Health: <http://localhost:8000/health> (HTTP 503 when PostgreSQL is unavailable)
- Read-only operations dashboard: <http://localhost:8000/dashboard> (uses the admin username/password)
- Protected status: `GET /admin/status`

Default demo administration credentials are `admin` / `change-me-locally`. Change them in your local `.env` before exposing the service beyond localhost.

Queue an outreach message:

```powershell
$pair = 'admin:change-me-locally'
$token = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($pair))
$headers = @{ Authorization = "Basic $token" }
Invoke-RestMethod -Method Post -Uri 'http://localhost:8000/admin/demo/outreach' -Headers $headers -ContentType 'application/json' -Body '{"recipient":"internal@example.com","quantity":100}'
```

The demo script reports whether outreach was newly queued or already present, waits with a bounded timeout, and prints the generated file path. The worker freezes a quote and writes the message to `runtime/demo_outbox/`; its stable RFC Message-ID is reused on retries.

## Demo inbound `.eml`

1. Queue demo outreach and open the generated `.eml` in `runtime/demo_outbox/`.
2. Copy its `Message-ID` into the `In-Reply-To` and `References` headers in `assets/demo_counteroffer.eml` or `assets/demo_sample_request.eml`.
3. Upload the fixture to `POST /admin/demo/inbound` in Swagger UI.
4. A safe counteroffer is deterministically bounded above the hard floor. Risky intents create specific handoffs: `SAMPLE_REQUEST`, `ORDER_COMMITMENT`, `SHIPPING_REQUEST`, `TECHNICAL_REQUEST`, or `COMPLAINT`. Below-floor, nonstandard, attachment-dependent, suppressed, or low-confidence cases also create a handoff and no autonomous commitment.

Duplicate raw messages do not create duplicate email-processing jobs. If a recovered worker processes the same inbound email again, the unique source-email constraint reuses the first handoff, audit event, and notification job.

## Excel templates

Generated templates are committed at:

- `assets/import_templates/customer_list_template.xlsx`
- `assets/import_templates/price_list_template.xlsx`

They are regenerated at API startup and via `POST /admin/templates/regenerate`.

Import endpoints accept `.xlsx` or UTF-8 `.csv`, use dry-run by default, and use `?apply=true` for transactional apply:

- `POST /admin/imports/customers`
- `POST /admin/imports/prices`

Apply the price list before the customer list. Customer import creates one active customer-product case per row. Queue its first quotation with `POST /admin/cases/{case_id}/outreach` and a JSON body such as `{"quantity":100}`.

Invalid email, missing product, overlapping active price policy, invalid currency/date, or floor above standard price is returned as a row-level error and blocks apply.

## Local approved content

Review and replace these before production use:

- `config/content/company_profile.md`
- `config/content/approved_product_text.yaml`
- `config/content/compliance_whitelist.yaml`
- `config/content/email_signature.txt`
- `config/content/email_signature.html`

The renderer inserts approved product copy and exact Decimal pricing. Validators reject altered approved text, unexpected monetary values, and unsupported commitments.

## Anthropic integration

Set locally:

```dotenv
AI_PROVIDER=anthropic
ANTHROPIC_MODEL=claude-opus-4-8
ANTHROPIC_API_KEY=...
```

The implementation uses the official `anthropic.AsyncAnthropic` SDK and typed Pydantic structured output via `messages.parse`. Inbound message text is wrapped as untrusted email data and cannot authorize prices, recipients, sending, or policy changes. Refusal, truncation, schema failure, or API failure results in human handoff.

Keep `MAIL_TRANSPORT=file`, `SAFE_MODE=true`, and `AUTO_SEND_ENABLED=false` while testing real Claude.

## Gmail and DingTalk activation

Gmail IMAP/SMTP uses a Workspace mailbox plus app password where Workspace policy permits. Gmail OAuth/Gmail API is the recommended production upgrade.

Local `.env` fields:

```dotenv
GMAIL_ADDRESS=...
GMAIL_APP_PASSWORD=...
IMAP_SYNC_ENABLED=false
IMAP_FOLDER=INBOX
IMAP_SENT_FOLDER=[Gmail]/Sent Mail
IMAP_BATCH_SIZE=100
MAIL_FROM=...
MAIL_TRANSPORT=file
DINGTALK_TRANSPORT=log
DINGTALK_WEBHOOK_URL=...
```

### Existing Gmail/OpenClaw history

Gmail synchronization is explicitly disabled until `IMAP_SYNC_ENABLED=true`. On the first enabled run, the poller snapshots the current highest UID independently for Sent and Inbox, imports at most `IMAP_BATCH_SIZE` messages per folder per cycle, and marks everything up to that snapshot as history. Historical Inbox messages are never queued for autonomous reply.

Sent is synchronized before Inbox. Threads are matched by Message-ID/In-Reply-To/References, then conservatively by contact and subject. A historical thread whose latest message is inbound becomes `WAITING_HUMAN`; one whose latest message is outbound becomes `PAUSED`. Any matched historical outbound message blocks the initial-outreach workflow, preventing the application from treating an OpenClaw contact as new.

Monitor and rerun reconciliation through:

- `GET /admin/history/status`
- `POST /admin/history/reconcile`

Messages that cannot be matched remain unassigned and are reported by the status endpoint. Importing customer data automatically retries reconciliation. Keep file mail, safe mode, and disabled auto-send throughout history migration.

Safe activation sequence:

1. Enable Anthropic while leaving file mail and log DingTalk.
2. Set Gmail credentials while leaving `IMAP_SYNC_ENABLED=false`.
3. Set `IMAP_SYNC_ENABLED=true` and monitor `/admin/history/status` until both folders report `history_complete=true`.
4. Review `WAITING_HUMAN`, `PAUSED`, and unmatched history before enabling any live sending.
5. Set DingTalk webhook and `DINGTALK_TRANSPORT=webhook`.
6. Set an internal `RECIPIENT_ALLOWLIST` and keep `SAFE_MODE=true`.
7. Only then set `MAIL_TRANSPORT=smtp` and `AUTO_SEND_ENABLED=true` for internal tests.
8. Canary a small set of approved customers/products after verifying no duplicate messages, floor violations, or unapproved claims.

Emergency stop: set `AUTO_SEND_ENABLED=false`. Inbound ingestion, drafting, and handoff processing continue.

## Verification

Docker Desktop must be running with Linux containers. The verification script fails early with a clear message when the daemon is unavailable.

```powershell
.\scripts\verify.ps1
```

Verification has three layers:

1. **Static/unit:** Compose configuration, Ruff, Python compilation, and tests that do not require a database.
2. **PostgreSQL integration:** migration upgrade/downgrade/upgrade plus real service tests for risky-intent routing, handoff idempotency, Gmail history reconciliation, duplicate ingestion, and the guarded USD 92 → USD 97 counteroffer flow.
3. **Runtime end to end:** starts an isolated database, API, and worker; checks `/health` and `/admin/status`; queues demo outreach; waits for a sent file outbox record; and validates the generated `.eml` recipient, Message-ID, and `USD 100.0000` price.

Each run uses a unique Compose project and temporary runtime/assets directories. It forces stub AI, file mail, log-only DingTalk, safe mode, disabled SMTP auto-send, empty external credentials, and separate database names regardless of the local `.env`. It does not start IMAP or call Anthropic, Gmail, SMTP, or DingTalk.

Failures print bounded container diagnostics before removing only the isolated resources. To retain the failed stack and temporary files for inspection:

```powershell
.\scripts\verify.ps1 -KeepOnFailure
```

A successful static check alone is not proof of a runnable MVP; the script must complete the runtime layer.

## API summary

All `/admin/*` endpoints use HTTP Basic authentication.

- `GET /health`
- `GET /admin/status`
- `GET /admin/history/status`
- `POST /admin/history/reconcile`
- `POST /admin/demo/seed`
- `POST /admin/demo/outreach`
- `POST /admin/demo/inbound`
- `POST /admin/imports/customers`
- `POST /admin/imports/prices`
- `GET /admin/cases`
- `GET /admin/cases/{id}`
- `POST /admin/cases/{id}/outreach`
- `GET /admin/handoffs`
- `POST /admin/handoffs/{id}`

This is an MVP control plane, not a public multi-tenant admin application. Put it behind a private network or authenticated reverse proxy before broader use.
