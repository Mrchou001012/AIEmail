# Application architecture

The codebase is being migrated from a single service module to capability-based modules. The migration is deliberately incremental so production behavior and existing imports remain stable.

## Dependency direction

Entrypoints such as `api.py`, `worker.py`, and `imap_poller.py` call focused application modules. Focused modules may use database models and infrastructure clients, but they must not depend on the legacy `services.py` facade.

Current extracted boundaries:

- `jobs.py`: durable queue claiming, defer/retry bookkeeping, and job dispatch;
- `disposition_batches.py`: durable Anthropic batch lifecycle and per-email results;
- `disposition_service.py`: inbound disposition planning and reversible application;
- `quote_rendering.py`: deterministic quote rendering without database or delivery effects;
- `coa_delivery.py`: verified COA attachment preparation and reading;
- `email_identity.py`: conservative greeting and signature normalization.

`services.py` temporarily re-exports selected names so existing callers do not need a risky one-shot migration. It is frozen at 9,800 lines by an architecture test and must only shrink over time.

## Adding a feature

1. Choose the business capability that owns the behavior.
2. Add or extend a focused module; do not add the implementation to `services.py`.
3. Keep deterministic transformation separate from database, filesystem, email, and AI calls.
4. Expose the smallest required public function to the API or worker entrypoint.
5. Add unit tests in the same capability area and an integration test only where boundaries meet.
6. If an old caller still imports from `app.services`, add a compatibility import while migrating that caller. The compatibility facade must remain below its frozen line budget.

## Next extraction order

The safest remaining sequence is:

1. outbound delivery and reconciliation;
2. forwarding and human-reply workflows;
3. product-list and COA orchestration;
4. quotation orchestration;
5. inbound ingestion and case resolution.

Each extraction should preserve public behavior, pass the full regression suite, and avoid combining architectural movement with business-rule changes.
