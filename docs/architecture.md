# Application architecture

Sales workflows live in capability-based modules. `services.py` is now a compatibility facade for existing callers; it contains no business workflow implementation.

## Package layout

| Directory | Responsibility |
| --- | --- |
| `app/inbound/` | Ingestion, automated replies, identity and inquiry matching |
| `app/quotations/` | Commercial context, quotation preparation and rendering |
| `app/catalogs/` | Product data, aliases, list delivery and historical backfill |
| `app/coa/` | COA requests, catalog verification, preview and delivery |
| `app/handoffs/` | Human review, draft generation, forwarding and agent continuation |
| `app/delivery/` | Sending, pacing, recipient guards and delivery recovery |
| `app/dispositions/` | Inbound classification, review, apply and rollback |
| `app/research/` | Company research and its business context |
| `app/common/` | Shared transaction, audit, identity and threading helpers |
| `scripts/testing/` | Isolated Docker migration and test runner |

API, worker and IMAP entrypoints remain in `app/`, keeping deployment commands stable.
Shared ORM models, settings, integrations and existing web assets also remain there.
Imports use the owning package; `app.services` retains legacy callable compatibility.
Catalog configuration paths are resolved from the repository root after relocation.

## Dependency direction

Entrypoints such as `api.py`, `worker.py`, and `imap_poller.py` call focused application modules. Focused modules may use database models and infrastructure clients, but they must not depend on the legacy `services.py` facade.

The API, IMAP poller, worker, recovery tool and durable job dispatcher import the owning modules directly. Job handlers retain local imports to avoid an eager cycle between job persistence and workflows that enqueue jobs.

| Capability | Owning modules |
| --- | --- |
| Receiving and matching email | `email_ingestion`, `inbound_followup`, `inquiry_matching`, `inquiry_resolution`, `reactivation_threading` |
| Inbound routing | `inbound_processing`, `automated_reply_service`, `bounce_service` |
| Automatic quotations | `quote_context`, `quote_clarification`, `single_product_quote`, `multi_product_quote`, `outreach_service`, `commercial_service` |
| Human quotations | `manual_quote_service`, `prepared_quote_service` |
| COA requests | `coa_service`; attachment preparation and verification remain in `coa_requests`, `coa_preview`, `coa_delivery` |
| Product catalog replies | `general_product_list`, `category_product_list`, `product_list_service` |
| Company research | `company_research_service`, `company_research_context` |
| Historical catalog requests | `product_list_backfill`, `product_list_backfill_prepare`, `product_list_backfill_apply`, `backfill_types` |
| Human review | `handoff_service` (case assignment), `handoff_preview`, `handoff_prepared_preview`, `human_reply_service`, `forwarding_service`, `agent_resume` |
| Outbound delivery | `outbound_delivery`, `outbound_guards`, `outbound_pacing`, `outbound_recovery` |
| Recipient safety | `delivery_safety` (preflight and address primitives), `contact_delivery_service` (human resolution) |
| Shared infrastructure | `service_core` (audit, outbox staging, transactional boundary and shared values), `email_threading`, `jobs`, `notifications` |
| Demo fixtures | `demo_service` |

`quote_rendering` and `email_identity` retain deterministic rendering and identity normalization. Disposition workflows remain in their existing `disposition_*` modules.

Architecture tests cap the compatibility facade at 250 lines and each module extracted in this service refactor at 500 lines. They also reject imports back to the facade and eager import cycles between the extracted modules. These are repository maintenance limits, not a universal rule for Python files.

## Transaction and compatibility contracts

Top-level business operations keep their rollback decorator. Extracted quotation phases run inside the inbound operation's transaction; they do not add a commit or swallow cancellation. Historical backfill still prepares and applies each selected handoff in order, retaining its existing per-item commits, idempotency checks and preview-only behavior.

COA verification, read-only NAS access, send-policy gates, human approval, and partial-COA handling are unchanged. The refactor does not change production configuration or send switches.

Existing callable imports from `app.services` remain available. Tests that replace dependencies should patch the owning module: for example `single_product_quote.stage_outbox`, `service_core.audit`, or `handoff_prepared_preview.prepare_detected_coa_preview`. Replacing an attribute on the compatibility facade does not replace a domain module's imported dependency. The legacy forwarding and send wrappers preserve their explicit transport/preflight injection points.

## Adding a feature

1. Choose the business capability that owns the behavior.
2. Add or extend a focused module; do not add the implementation to `services.py`.
3. Keep deterministic transformation separate from database, filesystem, email, and AI calls.
4. Expose the smallest required public function to the API or worker entrypoint.
5. Add unit tests in the same capability area and an integration test only where boundaries meet.
6. If an old caller still imports from `app.services`, add a compatibility import while migrating that caller. The compatibility facade must remain below its frozen line budget.

## Verification

Run Ruff, architecture/compatibility tests, and the business regression suite against a dedicated PostgreSQL database ending in `_test`. SQLite-only runs skip the database scenarios and cannot validate transactional rollback, concurrent delivery guards or manual approval. Keep baseline failures separate from refactor regressions when validating an existing checkout.
