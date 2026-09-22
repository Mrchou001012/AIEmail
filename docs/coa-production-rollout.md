# COA rollout template

This public document intentionally omits production hostnames, service paths,
share names, product directories, and operational URLs.

Before enabling the COA workflow in a private environment:

1. Mount the approved document source read-only on the application host.
2. Confirm the service account can read only the intended product-document
   subtree.
3. Configure `COA_CATALOG_ROOT`, `COA_CATALOG_PATH`, and
   `COA_PRODUCT_CATALOG_PATH` through the private environment.
4. Build the catalog with `scripts/sync_coa_catalog.py` and review every
   ambiguous or rejected candidate.
5. Keep `COA_AUTO_SEND_ENABLED=false` until lookup, fingerprint, attachment,
   and human-review checks pass in the target environment.
6. Verify that an unavailable mount, changed file, stale catalog, or ambiguous
   match fails closed without interrupting unrelated mail workflows.

Store the real commands, paths, unit names, backup steps, and rollback procedure
in a private runbook. Do not add them to this repository.
