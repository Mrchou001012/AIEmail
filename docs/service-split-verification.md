# Service extraction verification — 2026-09-07

> Historical baseline report. The remaining directory relocation, nine failures,
> and fresh migration limitation below were addressed on 2026-09-08. See
> [the completion report](verification-20260908.md): 549 passed, zero failures or skips.

The extraction is complete: `app/services.py` is a 166-line compatibility facade. The API, worker, IMAP poller, recovery tool and job dispatcher call capability modules directly. The 42 modules covered by the new service-size guard are each at most 500 lines.

## Regression comparison

Both checkouts were tested sequentially against the same disposable PostgreSQL 16 test database, with database integration tests enabled. AI was stubbed, mail used the file transport, IMAP was disabled, and DingTalk used the log transport. Production services and NAS configuration were not involved.

| Checkout | Passed | Failed | Skipped |
| --- | ---: | ---: | ---: |
| Committed baseline `0f49268` | 521 | 9 | 0 |
| Completed extraction | 536 | 9 | 0 |

The failed test identifiers are identical in the two JUnit reports: **zero new failures**. The 15 additional passing tests cover facade compatibility, module size and eager import cycles. Ruff, Python compilation and `git diff --check` also pass.

This is not an all-green baseline. The following failures predate the extraction and are deliberately retained rather than weakening assertions or changing business behavior as part of a refactor:

In `tests/test_product_list_replies.py`:

- `test_excel_cas_request_attaches_verified_catalog_workbook`
- `test_human_category_answer_resumes_agent_to_product_list_draft`
- `test_manual_draft_regeneration_preserves_catalog_and_defaults_new_customer_to_prepayment`
- `test_product_list_payment_term_uses_latest_sent_customer_quote`
- `test_product_list_backfill_maps_specific_product_in_selected_category`
- `test_catalog_import_is_idempotent`
- `test_full_customer_workbook_stores_interest_category`
- `test_departed_reply_without_interest_researches_and_auto_sends`

In `tests/test_services_integration.py`:

- `test_departed_contact_is_suppressed_and_handed_off`

Observed failures include outdated catalog-category expectations, absent prepared-catalog facts, and departed-contact suppression assertions. Reproducing them on the committed baseline establishes that they are not introduced by this extraction; it does not resolve their underlying causes.

## Database migration limitation

Fresh `alembic upgrade head` failed at migration `0021` because `products.catalog_code` already exists. Migration `0001` calls the current `Base.metadata.create_all()`, which includes that newer field, while `0021` adds it again. Neither migration was changed by this extraction.

The disposable test database was therefore initialized from current ORM metadata for **both** regression runs. The results validate business execution and transaction behavior, not a fresh installation's migration chain. A migration fix requires separate verification of both fresh installation and upgrade paths.

## Maintenance

See [architecture.md](architecture.md) for module ownership, transaction boundaries and the dependency direction. Existing callable imports from `app.services` are retained; dependency-injection tests now patch the module that owns the implementation.
