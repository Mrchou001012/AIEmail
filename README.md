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

OpenAPI and interactive documentation routes are available only in `DEMO_MODE=true`. Production mode keeps `/docs`, `/redoc`, and `/openapi.json` disabled.

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
4. Every counteroffer creates a `PRICE_NEGOTIATION` handoff and no autonomous reply. Risky intents create specific handoffs: `SAMPLE_REQUEST`, `ORDER_COMMITMENT`, `SHIPPING_REQUEST`, `TECHNICAL_REQUEST`, or `COMPLAINT`. Pre-book requests, unresolved packaging/lead-time questions, nonstandard terms, attachment-dependent cases, suppressed contacts, and low-confidence cases also create a handoff and no autonomous commitment.

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

### Current India ready-stock policy

- Currency and unit: `INR` per `kg`.
- Spreadsheet column B is the ready-stock base price; column C is ignored.
- Below MOQ: human handoff.
- MOQ through less than 4×MOQ: base price plus the row's first-tier markup.
- 4×MOQ through 12×MOQ: base price plus the row's second-tier markup.
- Above 12×MOQ: human handoff.
- Trade term `EXW`; taxes and freight excluded; payment by prepayment.
- Quotes expire on Friday. On Saturday, the next expiry is the following Friday.
- An active B-column price policy means the product is handled as ready stock. Lead-time questions may be answered as `Ready stock`, but exact dispatch, shipping, and arrival dates still go to a human.
- All counteroffers, pre-book requests, and packaging questions without CRM data go to a human.
- `YAC-TBDMSC` is manual-only. An unspecified `YAC-N823` purity defaults to `YAC-N823(98%)`.

The generated import-ready workbook is `outputs/inr_price_policy_20260715/AIEmail_印度现货价格导入_20260715.xlsx`. Its `calculation_check` sheet exposes every quantity boundary and calculated unit price for review.

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

Adaptive thinking and effort controls are sent only to model families known to support them. Claude Haiku 4.5, Claude Opus 4.5, Claude Sonnet 4.5, and unknown compatible model identifiers run without optional thinking controls so a supported structured-output request is not rejected by an incompatible inference parameter.

The inbound-analysis schema requires every property while retaining nullable values and explicit false/empty defaults in the response. This avoids the exponential grammar cost of many optional properties without changing the downstream safety decisions.

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
IMAP_POLL_SECONDS=60
IMAP_BATCH_SIZE=50
IMAP_DAILY_DOWNLOAD_LIMIT_MB=1500
IMAP_MAX_BACKOFF_SECONDS=1800
MAIL_FROM=...
MAIL_TRANSPORT=file
MAX_SENDS_PER_HOUR=5
MAX_SENDS_PER_DAY=20
MIN_SEND_INTERVAL_SECONDS=120
SEND_INTERVAL_JITTER_SECONDS=180
GMAIL_TRANSIENT_COOLDOWN_SECONDS=600
GMAIL_DAILY_COOLDOWN_SECONDS=86400
EMAIL_PREFLIGHT_ENABLED=true
MX_CHECK_ENABLED=true
MX_CACHE_TTL_HOURS=168
MX_LOOKUP_TIMEOUT_SECONDS=5
MX_TEMPORARY_RETRY_MINUTES=30
DINGTALK_TRANSPORT=log
DINGTALK_WEBHOOK_URL=...
```

The mailbox guards are deliberately below Gmail's published hard limits. IMAP is single-connection, UID-incremental, byte-metered per UTC day, and exponentially backs off on failures. SMTP sends are spaced by a stable 2-5 minute interval and capped across the entire mailbox by combining synchronized Gmail Sent history with local SMTP delivery records. Gmail transient throttling pauses the whole mailbox for at least 10 minutes; a daily-limit response pauses it for 24 hours. The dashboard shows the active limits, today's IMAP usage, and any mailbox cooldown.

### Existing Gmail/OpenClaw history

Gmail synchronization is explicitly disabled until `IMAP_SYNC_ENABLED=true`. On the first enabled run, the poller snapshots the current highest UID independently for Sent and Inbox, imports at most `IMAP_BATCH_SIZE` messages per folder per cycle, and marks everything up to that snapshot as history. Historical Inbox messages are never queued for autonomous reply.

Sent is synchronized before Inbox. Every message is first linked to a customer/contact only when its participant address identifies exactly one contact globally. A case is then selected by Message-ID/In-Reply-To/References, an explicit unique product, an exact case subject, or a single open case for that contact. Ambiguous messages remain customer-linked without guessing a case, and a latest historical inbound reply creates a case-assignment handoff for human review. A historical thread whose latest message is inbound becomes `WAITING_HUMAN`; one whose latest message is outbound becomes `PAUSED`. Historical outbound mail linked either to the case or its contact blocks the initial-outreach workflow, preventing the application from treating an OpenClaw contact as new.

Monitor and rerun reconciliation through:

- `GET /admin/history/status`
- `POST /admin/history/reconcile`

The history status endpoint reports customer-unmatched messages separately from case-unmatched messages. Importing customer data automatically retries reconciliation; rows whose products are not in the active catalog still create customers and contacts but do not create a case. Keep file mail, safe mode, and disabled auto-send throughout history migration.

### Live new-thread safety

Live human-authored inbound mail primarily inherits an existing case when `In-Reply-To` or `References` resolves to a stored Message-ID and the sender matches that case's contact. Some clients, including observed Enterprise WeChat reply paths, omit both standard thread headers and prepend a localized reply marker such as `回复：`. In that narrow fallback, the application links only when the localized reply marker, normalized subject, sender/contact, product, currency, and one recent open case all match uniquely. Any ambiguity still creates a handoff instead of inheriting quotation or negotiation history.

An inbound message without thread headers that does not satisfy the narrow reply fallback is treated as a new inquiry. A fresh case is created only when the sender maps to one customer contact, exactly one active catalog product is identified, and the market currency is unambiguous. The fresh case starts at quotation round zero and does not inherit earlier prices or negotiation state. Possible same-contact/product cases are recorded in the audit event for visibility, but are not linked automatically.

The message is handed to a human without sending when it refers to prior commercial context (for example, a previous quotation or an earlier discussion), contains unmatched thread headers, maps the sender to multiple customer records, names zero or multiple products, or has ambiguous currency/catalog data. Standard policy checks still route counteroffers, samples, orders, packaging, shipping commitments, risky attachments, and low-confidence extraction to the existing handoff and DingTalk workflow.

Outbound replies use `In-Reply-To` and an ordered `References` chain for RFC threading. Their visible quoted section is built from the complete direct-parent MIME body in the immutable mail archive, so any conversation already quoted by the sender remains visible. AI analysis continues to use only the cleaned new-message text. Quoted HTML is allowlist-sanitized, tracking/inline images and active content are removed, and original attachments are never reattached.

### Human review workflow

A handoff is a durable database record, not merely a DingTalk message. Automatic sending stops for the affected case and the DingTalk notifier links to `/admin/handoffs/{id}/review`. The protected review page shows the source email, extracted facts, related cases, contact/product choices, latest quotation, and a conservative editable reply draft.

An authenticated reviewer can associate the inbound email with an existing case, create and associate a new case for a matching contact, edit and approve a reply, pause or resume the case, take over the case, or close the handoff. Approved replies record the handoff ID, reviewer account, approval timestamp, exact MIME message, and append-only audit event. They still pass recipient matching, do-not-contact and suppression checks, SAFE_MODE allowlisting, address/MX preflight, Gmail spacing, hourly/daily limits, and SMTP cooldowns. Explicitly human-approved replies may be delivered while autonomous sending is disabled; `MAIL_TRANSPORT=file` remains the global no-network-send control.

By default, sending an approved reply leaves the case in `HUMAN_TAKEOVER`. The reviewer must explicitly select “resume AI automation” when sending if subsequent standard replies should return to autonomous processing. In `DINGTALK_TRANSPORT=log` mode, notifications are recorded as `LOGGED` and no external request is made. Configure `DINGTALK_TRANSPORT=webhook`, `DINGTALK_WEBHOOK_URL`, and a reachable HTTPS `PUBLIC_BASE_URL` to receive clickable notifications outside local development. The review page remains protected by the configured admin credentials.

### Vacation and personnel-change automatic replies

Inbound messages are checked deterministically before Claude is called. The parser records standard automatic-response headers, the classified reply type, any return-date text, replacement email addresses, the handling timestamp, and an audit event.

- Out-of-office/vacation replies and generic automated acknowledgements are recorded and silently handled. The application does not reply to them and leaves the sales case active.
- Clear departure notices suppress the old contact and create a `PERSONNEL_CHANGE` handoff.
- New-contact or personnel-change notices create a `PERSONNEL_CHANGE` handoff without automatically switching the recipient.
- Unhandled automated replies create an `AUTOMATED_REPLY_REVIEW` handoff.

The dashboard marks automated replies in the recent-email list and exposes their extracted metadata in email details. Replacement recipients are never trusted automatically; a human must verify them before use.

### Recipient preflight and bounce suppression

Live SMTP sends run a deterministic recipient check before the message is claimed:

- The address must be syntactically valid.
- Its domain must publish a usable MX record. MX results are cached for seven days by default so many contacts at the same company do not cause repeated DNS traffic.
- DNS timeouts and nameserver failures defer the outbox item for 30 minutes; they never suppress the recipient.
- A missing domain, missing MX, or null MX blocks the current message and creates an `EMAIL_DELIVERABILITY` handoff. It is recorded but not permanently suppressed because DNS configuration can change.
- Invalid address syntax is permanently suppressed because it can never be delivered as written.

MX validation proves only that the domain accepts email; it cannot prove that a specific mailbox exists. Definitive mailbox validity comes from delivery feedback.

Inbound RFC delivery-status notifications are parsed before automatic-reply or AI processing. A hard invalid-recipient bounce is trusted for permanent suppression only when its original Message-ID/recipient can be correlated with an outbox message sent by this application. All matching contacts are suppressed and future queued mail is blocked. Mailbox-full/temporary failures, anti-spam or policy failures, unknown failures, and uncorrelated bounce-like messages are recorded and create a `BOUNCE_REVIEW` handoff instead of suppressing automatically. Equivalent synchronous SMTP `5.1.x` invalid-recipient rejections are handled immediately. The dashboard shows bounce type, handling metadata, preflight state, and the permanent-suppression count.

Safe activation sequence:

1. Enable Anthropic while leaving file mail and log DingTalk.
2. Set Gmail credentials while leaving `IMAP_SYNC_ENABLED=false`.
3. Set `IMAP_SYNC_ENABLED=true` and monitor `/admin/history/status` until both folders report `history_complete=true`.
4. Review `WAITING_HUMAN`, `PAUSED`, and unmatched history before enabling any live sending.
5. Set DingTalk webhook and `DINGTALK_TRANSPORT=webhook`.
6. Set an internal `RECIPIENT_ALLOWLIST` and keep `SAFE_MODE=true`.
7. Only then set `MAIL_TRANSPORT=smtp` and `AUTO_SEND_ENABLED=true` for internal tests.
8. Canary a small set of approved customers/products after verifying no duplicate messages, floor violations, or unapproved claims.

Autonomous-send stop: set `AUTO_SEND_ENABLED=false`. Inbound ingestion, drafting, handoff processing, and explicitly approved human replies continue. For a global network-send stop, set `MAIL_TRANSPORT=file` or stop the worker.

## Verification

Docker Desktop must be running with Linux containers. The verification script fails early with a clear message when the daemon is unavailable.

```powershell
.\scripts\verify.ps1
```

Verification has three layers:

1. **Static/unit:** Compose configuration, Ruff, Python compilation, and tests that do not require a database.
2. **PostgreSQL integration:** migration upgrade/downgrade/upgrade plus real service tests for risky-intent routing, counteroffer handoff, handoff idempotency, Gmail history reconciliation, and duplicate ingestion.
3. **Runtime end to end:** starts an isolated database, API, and worker; checks `/health` and `/admin/status`; queues demo outreach; waits for a sent file outbox record; and validates the generated `.eml` recipient, Message-ID, and `USD 100.0000` price.

Each run uses a unique Compose project and temporary runtime/assets directories. It forces stub AI, file mail, log-only DingTalk, safe mode, disabled SMTP auto-send, empty external credentials, and separate database names regardless of the local `.env`. It does not start IMAP or call Anthropic, Gmail, SMTP, or DingTalk.

Failures print bounded container diagnostics before removing only the isolated resources. To retain the failed stack and temporary files for inspection:

```powershell
.\scripts\verify.ps1 -KeepOnFailure
```

A successful static check alone is not proof of a runnable MVP; the script must complete the runtime layer.

## API summary

Product codes are normalized through `config/product_aliases.yaml`. The `code` value is the
customer-facing canonical form; legacy spreadsheet codes, shortened names, and punctuation variants
belong under `aliases`. The current business rule maps an unspecified `YAC-N823` to
`YAC-N823(98%)`; an explicitly stated 99% remains `YAC-N823(99%)`.

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
