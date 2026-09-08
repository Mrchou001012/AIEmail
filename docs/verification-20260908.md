# Local verification — 2026-09-08

## Result

Docker: PostgreSQL 15, Python 3.12, Node.js. **549 passed, 0 failed, 0 skipped**
in 52.02 seconds. All database integration tests were enabled. The test network
is internal, AI is stubbed, mail uses file transport, IMAP is disabled, and
DingTalk uses log transport. No production services were changed.

Ruff and whitespace checks passed. The XML report from this run is saved locally
at `runtime/verification-20260908.xml` (runtime artifacts are not committed).

## Completed work

- Finished relocation into nine capability packages documented in
  [architecture.md](architecture.md). Corrected the two repository-relative
  catalog/alias paths and the dynamic rollback-test imports left by relocation.
  The 166-line `app.services` facade retains callable compatibility; the 42
  extracted service modules remain within the 500-line guard.
- Dashboard API computes `cooldown_active` with the same strict future-time
  comparison used by SMTP delivery. Historical `cooldown_until` and reason remain
  available. The page trusts the boolean, or compares expiry against the server's
  `generated_at` for an older API (browser time is only the final fallback).
  Tests execute the actual dashboard JavaScript and check active, expired, exact
  expiry, absent and malformed timestamps, plus authoritative false values.
- A known contact's explicit company-wide product-list request can now create a
  product-pending case without enabling company research. It reaches the existing
  catalog preparation workflow, including its visibility and approval gates.
  Requests with unresolved scope continue to require category review.
- Added the missing `aiosqlite` development dependency and a Node-equipped test
  image so the Docker suite can execute all tests rather than error or skip.

## Original nine failures

| Failure group | Resolution |
| --- | --- |
| Workbook CAS/code assertions | Assert the audited customer-facing `AcAc` code and `123-54-6` CAS; absent content stays blank. |
| Category choices/import counts | Reflect all five approved categories. |
| AcAc backfill and imported customer interest | Use its dedicated `acetylacetone_salts` category. |
| Two full-catalog/payment draft scenarios | Fix early routing so drafts are prepared; assert 70 customer-visible products, excluding the internal-only row. |
| Two departed-contact assertions | Respect observation-only review and the enabled apply mode's earlier retirement of the uniquely linked old endpoint. The current human sender remains active. |

These changes preserve suppression safety. Tests for category-review/backfill now
use explicitly scoped requests rather than relying on generic requests being
incorrectly blocked. Inventory tests use a controlled business time, and injected
rollback failures target the owning quotation modules after relocation.

## Migration verification

Migration 0001 previously imported current `app.db` models, so fresh databases
already contained fields that migrations 0021–0023 then attempted to add. Its
schema is now frozen from the original `e9a5883` revision. Existing databases
already beyond 0001 do not rerun this revision.

The isolated runner verified:

1. Empty database through revision 0020, without future `catalog_code` columns.
2. Insertion of a legacy product, followed by upgrade to 0023.
3. Retention of that product and `catalog_visible=false` after upgrade.
4. A repeat `upgrade head` without changes or errors.
5. All integration tests against the migrated schema (no ORM `create_all` shortcut).

## Reproduce locally

From the repository root:

```bash
docker compose -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests
docker compose -f compose.test.yml cp tests:/app/runtime/test-results.xml runtime/test-results.xml
docker compose -f compose.test.yml down
```

This is a dedicated local verification project. It does not load `.env`, expose
database ports or mount production data. Its PostgreSQL data is temporary; use
`down` before a subsequent run because the runner requires an empty `_test` DB.
Production deployment remains the existing Python/systemd workflow.
